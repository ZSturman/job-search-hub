"""Acceptance checks against isolated documents, databases, and Notion fixtures."""

import hashlib
import json
import sqlite3
import zipfile
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from job_search_hub import storage, workflow
from job_search_hub.cli import main, parser
from job_search_hub.config import NotionSettings, load_settings
from job_search_hub.db import open_store
from job_search_hub.library import (
    add_resume,
    approve_fact,
    catalog,
    stage_facts,
    suggest,
    sync_library,
)
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient, NotionError
from job_search_hub.notion.hub import bind, discover, page_id, provision, snapshot
from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles
from job_search_hub.transfer import backup, export_notion, import_documents, import_notion, restore
from tests.hub_fake import ROOT, HubNotion


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv("JOBHUB_NOTION_ROOT", ROOT)
    notion = HubNotion()
    provision(notion, ROOT)
    settings = bind(load_settings(), notion, ROOT)
    return settings, notion


def job(hub, decision="Apply", status="Review", **extra):
    settings, notion = hub
    return notion.create_page(
        parent={"data_source_id": settings.notion.data_sources["opportunities"]},
        properties={
            "Opportunity": P.title("Example • Platform Engineer"),
            "Role Title": P.rich_text("Platform Engineer"),
            "Description": P.rich_text(
                "Build reliable services and work with a collaborative engineering team."
            ),
            "URL": P.url("https://example.test/jobs/1"),
            "My Decision": P.select(decision),
            "Status": P.select(status),
            **extra,
        },
    )


def test_template_is_scoped_restartable_and_neutral(hub):
    settings, notion = hub
    workflow.validate_hub(settings, notion)
    assert len(settings.notion.data_sources) == 10
    profiles = notion.query_data_source(settings.notion.data_sources["search_profiles"])
    assert not P.read_multi_select(profiles[0]["properties"].get("Role Terms"))
    assert not notion.query_data_source(settings.notion.data_sources["resume_facts"])
    counts = (len(notion.schemas), len(notion.views), len(notion.pages), notion.counter)
    provision(notion, ROOT)
    assert (len(notion.schemas), len(notion.views), len(notion.pages)) == counts[:3]
    views = [
        kw["json"] for method, path, kw in notion.requests if method == "POST" and path == "/views"
    ]
    assert any(v.get("sorts") for v in views)
    assert any(v.get("filter") for v in views)
    assert any(any(not p["visible"] for p in v["configuration"]["properties"]) for v in views)
    assert all(
        kw["headers"]["Notion-Version"] == "2026-03-11"
        for method, path, kw in notion.requests
        if path == "/views"
    )
    assert settings.notion.api_version == "2025-09-03"


def test_template_refuses_existing_non_hub_page():
    notion = HubNotion()
    notion.blocks[ROOT] = [
        {"id": "old", "type": "paragraph", "paragraph": {"text": "Existing private page"}}
    ]
    with pytest.raises(ValueError, match="empty page"):
        provision(notion, ROOT)
    assert not notion.writes


def test_discovery_does_not_search_other_pages_and_rejects_duplicate_tables(hub):
    settings, notion = hub
    notion.blocks["unrelated"] = [
        {"id": "private", "type": "child_database", "child_database": {"title": "Profile Facts"}}
    ]
    assert discover(notion, ROOT)[0] == settings.notion.data_sources
    data_page = next(
        b["id"]
        for b in notion.blocks[ROOT]
        if b.get("child_page", {}).get("title") == "Data & diagnostics"
    )
    notion.blocks[data_page].append(
        {"id": "duplicate", "type": "child_database", "child_database": {"title": "Sources"}}
    )
    with pytest.raises(ValueError, match="Multiple Sources"):
        discover(notion, ROOT)


def test_page_url_and_schema_mismatch_guidance(hub):
    assert page_id("https://notion.so/Job-Search-Hub-" + ROOT.replace("-", "") + "?pvs=4") == ROOT
    settings, notion = hub
    del notion.schemas[settings.notion.data_sources["sources"]]["properties"]["Enabled"]
    with pytest.raises(ValueError, match="Enabled"):
        workflow.validate_hub(settings, notion)


def test_keywords_and_sources_use_the_same_notion_preferences(hub):
    from job_search_hub.commands.hub import _edit_keywords

    settings, notion = hub
    _edit_keywords(notion, settings, "My search", "role", ["platform engineer"])
    notion.create_page(
        parent={"data_source_id": settings.notion.data_sources["sources"]},
        properties={
            "Source": P.title("Employer"),
            "Type": P.rich_text("greenhouse"),
            "Target": P.rich_text("example-company"),
            "Enabled": P.checkbox(True),
            "Cadence": P.select("Daily"),
        },
    )
    configured, _ = snapshot(settings, notion)
    assert configured.search_terms.role_terms == ["platform engineer"]
    assert configured.adapters.greenhouse_boards == ["example-company"]
    assert configured.enabled_adapters == ("greenhouse",)
    profile = notion.query_data_source(settings.notion.data_sources["search_profiles"])[0]
    notion.update_page(profile["id"], {"Role Terms": P.multi_select(["teacher"])})
    assert snapshot(settings, notion)[0].search_terms.role_terms == ["teacher"]


def test_configuration_ignores_old_env_and_cwd_dotenv(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTION_TOKEN", "old-secret")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "NOTION_TOKEN=old-private-token\nJOBHUB_NOTION_TOKEN=plaintext-token"
    )
    settings = load_settings()
    assert settings.notion.token == ""
    assert settings.hard_floor_annual_usd == 0
    assert settings.timezone == "UTC"
    assert settings.data_dir.is_relative_to(tmp_path)
    with pytest.raises(ValueError, match="Credentials"):
        storage.save_config({"settings": {"NOTION_TOKEN": "secret"}})


def test_no_plaintext_fallback_when_credential_store_fails(monkeypatch):
    def fail(*_):
        raise RuntimeError("locked credential store")

    monkeypatch.setattr("keyring.set_password", fail)
    with pytest.raises(ValueError, match="no plaintext"):
        storage.save_secret("NOTION_TOKEN", "secret")
    assert not storage.app_paths().config_file.exists()


def test_preview_client_rejects_every_write_and_allows_queries(httpx_mock):
    token = storage.READ_ONLY.set(True)
    try:
        with NotionClient(NotionSettings("fixture-token", "2025-09-03", {})) as notion:
            for method, path in [
                ("PATCH", "/pages/example"),
                ("POST", "/pages"),
                ("POST", "/databases"),
                ("POST", "/views"),
                ("DELETE", "/blocks/example"),
            ]:
                with pytest.raises(NotionError, match="cannot write"):
                    notion._request(method, path, json={})
            httpx_mock.add_response(
                method="GET",
                url="https://api.notion.com/v1/data_sources/example",
                json={"id": "example"},
            )
            httpx_mock.add_response(
                method="POST",
                url="https://api.notion.com/v1/data_sources/example/query",
                json={"results": [], "has_more": False},
            )
            assert list(notion.query_data_source("example")) == []
        with pytest.raises(ValueError, match="Preview"):
            storage.private_write(storage.app_paths().data / "resumes/example.pdf", b"no")
    finally:
        storage.READ_ONLY.reset(token)


def test_cli_help_status_and_conflicting_flags_are_safe(capsys):
    assert main([]) == 0
    assert "Start with jobhub setup" in capsys.readouterr().out
    assert main(["status"]) == 0
    assert not storage.app_paths().data.exists()
    for args in (
        ["run", "--preview", "--apply"],
        ["maintenance", "dedupe", "--preview", "--apply"],
    ):
        with pytest.raises(SystemExit):
            parser().parse_args(args)


def test_scoped_record_guard_preserves_foreign_hubs(hub):
    from job_search_hub.commands.hub import _require_owned

    settings, notion = hub
    outside = notion.create_page(
        parent={"data_source_id": "foreign"}, properties={"Status": P.select("Review")}
    )
    before = len(notion.writes)
    with pytest.raises(ValueError, match="does not belong"):
        _require_owned(notion, settings, outside["id"], "opportunities")
    assert len(notion.writes) == before


def test_apply_tracks_exactly_once_without_documents_and_survives_interruption(hub):
    settings, notion = hub
    page = job(hub, **{"Missing Answer Count": {"rollup": {"number": 2}}})
    notion.fail_relation_once = True
    first = reconcile_lifecycles(settings, notion=notion, dry_run=False)
    assert first.errors
    reconcile_lifecycles(settings, notion=notion, dry_run=False)
    apps = notion.query_data_source(settings.notion.data_sources["applications"])
    assert len(apps) == 1 and P.read_select(apps[0]["properties"]["Status"]) == "Planned"
    assert P.read_relation_ids(notion.get_page(page["id"])["properties"]["Application"]) == [
        apps[0]["id"]
    ]


def test_pdf_catalog_uses_content_ids_arbitrary_names_and_no_remote_upload(tmp_path, hub):
    settings, notion = hub
    source = tmp_path / "any résumé (final).pdf"
    source.write_bytes(b"%PDF-1.4\nfixture only\n%%EOF")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    row = add_resume(source, "Platform engineering", ["backend"])
    assert row["id"] == digest
    assert add_resume(source, "Platform engineering", ["backend"])["id"] == digest
    assert len(catalog()) == 1
    before = source.read_bytes()
    sync_library(notion, settings)
    payload = json.dumps(notion.query_data_source(settings.notion.data_sources["resume_library"]))
    assert digest in payload and str(source) not in payload and "%PDF" not in payload
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match="not a PDF"):
        invalid = tmp_path / "invalid.pdf"
        invalid.write_text("text")
        add_resume(invalid, "Invalid")


def test_fact_extraction_stays_local_until_review_then_deduplicates(monkeypatch, tmp_path, hub):
    settings, notion = hub
    pdf = tmp_path / "my.pdf"
    pdf.write_bytes(b"%PDF-fixture")
    document = add_resume(pdf, "Platform engineering")
    from job_search_hub.pipeline.resume_facts_sync import PdfResumeFact

    monkeypatch.setattr(
        "job_search_hub.pipeline.resume_facts_sync.extract_pdf_text", lambda path: "Skills: Python"
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.resume_facts_sync._deterministic_facts_from_text",
        lambda **kw: [
            PdfResumeFact(
                fact_id="fixture",
                fact_type="Skill",
                value="Python",
                evidence="Skills: Python",
                source="local",
                resume_title="Platform engineering",
                category="Language",
            )
        ],
    )
    before = len(notion.writes)
    facts = stage_facts(document["id"])
    assert len(notion.writes) == before
    assert not notion.query_data_source(settings.notion.data_sources["resume_facts"])
    approve_fact(notion, settings, facts[0]["id"])
    approve_fact(notion, settings, facts[0]["id"])
    rows = notion.query_data_source(settings.notion.data_sources["resume_facts"])
    assert len(rows) == 1
    assert P.read_checkbox(rows[0]["properties"]["Approved for Profile"])
    assert str(tmp_path) not in json.dumps(rows)


def test_suggestions_only_recommend_and_preserve_manual_selections(hub):
    settings, notion = hub
    page = job(hub, **{"Resume to use": P.rich_text("My chosen resume")})
    groups = suggest(notion, settings)
    assert groups[0]["document_id"] == ""
    assert groups[0]["job_ids"] == [page["id"]]
    assert (
        P.read_text(notion.get_page(page["id"])["properties"]["Resume to use"])
        == "My chosen resume"
    )
    assert groups[0].keys() == {"title", "document_id", "job_ids", "rationale"}
    before = len(notion.writes)
    suggest(notion, settings, preview=True)
    assert len(notion.writes) == before


def test_invalid_model_recommendations_do_not_change_selections(hub):
    settings, notion = hub
    job(hub)
    client = SimpleNamespace(
        chat=lambda **kw: json.dumps(
            {
                "groups": [
                    {"title": "Wrong", "document_id": "foreign", "job_ids": [], "rationale": "No"}
                ]
            }
        )
    )
    router = SimpleNamespace(get=lambda role: "fixture-chat")
    before = len(notion.writes)
    with pytest.raises(ValueError, match="coverage"):
        suggest(notion, settings, client=client, router=router)
    assert len(notion.writes) == before


def test_backup_restores_consistent_database_and_excludes_credentials(tmp_path, monkeypatch):
    storage.save_config({"settings": {"MODEL_PRIMARY": "fixture-model"}})
    store = open_store(storage.app_paths().data / "cache.sqlite")
    store.audit(actor="fixture", action="example", payload={"count": 1})
    monkeypatch.setenv("JOBHUB_NOTION_TOKEN", "secret-outside-backup")
    document = tmp_path / "source.pdf"
    document.write_bytes(b"%PDF-fixture")
    add_resume(document, "Example")
    archive = tmp_path / "safe.zip"
    backup(archive)
    with zipfile.ZipFile(archive) as z:
        assert not any(
            "secret-outside-backup" in z.read(name).decode(errors="ignore") for name in z.namelist()
        )
        assert not any(name.endswith(("-wal", "-shm")) for name in z.namelist())
    destination = tmp_path / "restored"
    restore(archive, destination)
    with sqlite3.connect(destination / "data/cache.sqlite") as conn:
        assert conn.execute("select count(*) from audit_log").fetchone()[0] == 1
    assert (destination / "data/resumes/index.json").exists()
    with pytest.raises(ValueError, match="empty"):
        restore(archive, destination)


def test_unsafe_archive_is_rejected_before_any_restore(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "jobhub-backup-v1"}))
        z.writestr("../escape", "bad")
    dest = tmp_path / "destination"
    with pytest.raises(ValueError, match="Unsafe"):
        restore(archive, dest)
    assert not dest.exists() and not (tmp_path / "escape").exists()


def test_one_way_document_import_is_previewable_restartable_and_preserves_source(tmp_path):
    source = tmp_path / "existing"
    source.mkdir()
    doc = source / "personal.pdf"
    doc.write_bytes(b"%PDF-synthetic")
    original = doc.read_bytes()
    assert import_documents(source)[0]["status"] == "would import"
    assert not catalog()
    assert import_documents(source, apply=True)[0]["status"] == "imported"
    assert import_documents(source, apply=True)[0]["status"] == "already imported"
    assert doc.read_bytes() == original


def test_notion_import_remaps_relations_and_resumes_after_interruption(hub):
    settings, notion = hub
    job(hub)
    reconcile_lifecycles(settings, notion=notion, dry_run=False)
    export = export_notion(notion, settings, ROOT)
    other = HubNotion()
    provision(other, ROOT)
    dest = bind(load_settings(), other, ROOT)
    # Export root is a source identity; destination fixture gets its own private root.
    source_root = "00000000-0000-0000-0000-000000000002"
    export["root"] = source_root
    import_notion(export, other, dest, ROOT, apply=True)
    import_notion(export, other, dest, ROOT, apply=True)
    apps = other.query_data_source(dest.notion.data_sources["applications"])
    jobs = other.query_data_source(dest.notion.data_sources["opportunities"])
    assert len(apps) == len(jobs) == 1
    assert P.read_relation_ids(apps[0]["properties"]["Opportunity"]) == [jobs[0]["id"]]
    assert P.read_text(jobs[0]["properties"]["Import ID"])
    with pytest.raises(ValueError, match="must differ"):
        import_notion(dict(export, root=ROOT), other, dest, ROOT, apply=True)


def test_overlapping_refresh_lock_fails_without_modifying_data(tmp_path):
    path = tmp_path / "lock"
    with (
        workflow.run_lock(path),
        pytest.raises(ValueError, match="Another refresh"),
        workflow.run_lock(path),
    ):
        pass


@pytest.mark.parametrize("platform_name", ["Darwin", "Linux"])
def test_schedule_preview_has_independent_labels_and_does_not_register(monkeypatch, platform_name):
    from job_search_hub.scheduling import enable

    monkeypatch.setattr("platform.system", lambda: platform_name)
    monkeypatch.setattr(
        "subprocess.run", lambda *_a, **_kw: pytest.fail("preview invoked service manager")
    )
    result = enable(6, preview=True)
    assert "job_search_hub" in json.dumps(result)
    assert "casino" not in json.dumps(result).lower()
    assert not storage.app_paths().config.exists()


def test_verification_failure_blocks_ai_but_tracks_application(hub, monkeypatch, tmp_path):
    settings, notion = hub
    job(hub)

    @contextmanager
    def session(**kw):
        yield settings, notion

    @contextmanager
    def models(*_a, **_kw):
        yield SimpleNamespace(), SimpleNamespace()

    monkeypatch.setattr(workflow, "session", session)
    monkeypatch.setattr(workflow, "models", models)
    monkeypatch.setattr("job_search_hub.logging_setup.setup_logging", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_date_backfill.run_posting_date_backfill",
        lambda *_a, **_kw: SimpleNamespace(errors=[]),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_tick_preflight",
        lambda *_a, **_kw: SimpleNamespace(errors=["required verification failed"]),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.run_score",
        lambda *_a, **_kw: pytest.fail("AI ran after required verification failed"),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.organizations.sync_opportunity_organizations",
        lambda *_a, **_kw: None,
    )
    result = workflow.refresh()
    assert result["errors"]
    assert len(notion.query_data_source(settings.notion.data_sources["applications"])) == 1


def test_preview_session_copies_cache_to_scratch_and_preserves_original(hub, monkeypatch):
    settings, notion = hub
    store = open_store(settings.sqlite_path)
    store.audit(actor="fixture", action="existing")
    before = settings.sqlite_path.read_bytes()
    monkeypatch.setenv("JOBHUB_NOTION_ROOT", ROOT)
    monkeypatch.setattr(workflow, "NotionClient", lambda *_a, **_kw: notion)
    with workflow.session(preview=True) as (temporary, _):
        assert temporary.sqlite_path != settings.sqlite_path
        with sqlite3.connect(temporary.sqlite_path) as connection:
            assert connection.execute("select action from audit_log").fetchone()[0] == "existing"
    assert settings.sqlite_path.read_bytes() == before


def test_resume_suggestion_rejects_long_or_multiline_model_prose(hub):
    settings, notion = hub
    page = job(hub)
    client = SimpleNamespace(
        chat=lambda **kw: json.dumps(
            {
                "groups": [
                    {
                        "title": "Reusable resume",
                        "document_id": "",
                        "job_ids": [page["id"]],
                        "rationale": "Summary\nDrafted resume section",
                    }
                ]
            }
        )
    )
    router = SimpleNamespace(get=lambda role: "fixture-chat")
    before = len(notion.writes)
    with pytest.raises(ValueError, match="metadata"):
        suggest(notion, settings, client=client, router=router)
    assert len(notion.writes) == before
