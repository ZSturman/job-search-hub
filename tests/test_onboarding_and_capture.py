"""Onboarding, optional capture, and privacy failures without live services."""

import importlib
import json
from contextlib import contextmanager

import pytest

from job_search_hub import storage
from job_search_hub.cli import main
from job_search_hub.config import load_settings
from job_search_hub.models import LMStudioClient, StructuredOutputError
from job_search_hub.notion.hub import bind, snapshot
from tests.hub_fake import ROOT, HubNotion


def test_setup_discovers_root_and_persists_only_nonsecret_configuration(monkeypatch):
    notion = HubNotion()
    monkeypatch.setenv("JOBHUB_NOTION_TOKEN", "synthetic-test-token")
    monkeypatch.setattr("job_search_hub.notion.client.NotionClient", lambda *a, **kw: notion)
    assert (
        main(
            [
                "setup",
                "--root-url",
                f"https://www.notion.so/{ROOT.replace('-', '')}",
                "--create-template",
                "--non-interactive",
                "--role",
                "librarian",
            ]
        )
        == 0
    )
    config = storage.read_config()["settings"]
    assert config["NOTION_ROOT"] == ROOT
    assert len([key for key in config if key.startswith("NOTION_DS_")]) == 10
    assert "synthetic-test-token" not in storage.app_paths().config_file.read_text()
    runtime, _ = snapshot(bind(load_settings(), notion, ROOT), notion, preview=False)
    assert runtime.search_terms.role_terms == ["librarian"]
    assert runtime.adapters.usajobs_keywords == ["librarian"]


def test_interactive_setup_does_not_request_optional_credentials(monkeypatch):
    notion = HubNotion()
    monkeypatch.setenv("JOBHUB_NOTION_TOKEN", "synthetic-test-token")
    monkeypatch.setattr("job_search_hub.notion.client.NotionClient", lambda *a, **kw: notion)
    responses = iter(["librarian, archivist", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr("getpass.getpass", lambda _: pytest.fail("unexpected credential prompt"))
    assert main(["setup", "--root-url", ROOT, "--create-template"]) == 0
    assert storage.read_config()["settings"]["ONBOARDING_COMPLETE"] is True


def test_capture_import_is_side_effect_free_and_requests_are_authenticated(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from job_search_hub.capture import server

    monkeypatch.setenv("JOBHUB_CAPTURE_AUTH_TOKEN", "synthetic-capture-token")
    settings = load_settings()
    importlib.reload(server)
    assert not settings.data_dir.exists()
    calls = []

    @contextmanager
    def session(**kwargs):
        calls.append(kwargs)
        yield settings, object()

    def ingest(posting, **kwargs):
        assert kwargs["dry_run"] is False
        assert posting.source_adapter == "manual"
        calls.append(posting.url)
        kwargs["stats"].new_opportunities += 1

    monkeypatch.setattr("job_search_hub.pipeline.ingest_stage._ingest_posting", ingest)
    app = server.create_app(settings, session_factory=session)
    assert isinstance(app, fastapi.FastAPI)
    with TestClient(app) as client:
        payload = {"url": "https://example.test/jobs/1", "role_title": "Librarian"}
        assert client.post("/capture", json=payload).status_code == 401
        assert (
            client.post(
                "/capture", json=payload, headers={"Authorization": "Bearer bad"}
            ).status_code
            == 403
        )
        assert not settings.data_dir.exists()
        result = client.post(
            "/capture", json=payload, headers={"Authorization": "Bearer synthetic-capture-token"}
        )
        assert result.json()["new"] == 1
        assert result.json()["processed"] is True
        assert calls == [{"preferences": True}, "https://example.test/jobs/1"]
        assert (settings.data_dir / "capture.jsonl").stat().st_mode & 0o777 == 0o600
        assert client.get("/docs").status_code == 404


def test_malformed_model_response_never_appears_in_default_logs(monkeypatch, caplog):
    with LMStudioClient(load_settings().lmstudio) as client:
        private = "PRIVATE CANDIDATE DETAIL"
        monkeypatch.setattr(
            client, "_post", lambda *_: {"choices": [{"message": {"content": private}}]}
        )
        with pytest.raises(StructuredOutputError) as error:
            client.chat_json(
                model="test",
                messages=[{"role": "user", "content": "test"}],
                json_schema={"type": "object", "properties": {}, "additionalProperties": False},
                task="test",
            )
        assert private not in str(error.value)
        assert private not in caplog.text


def test_source_failure_is_distinct_from_successful_empty_feed(httpx_mock):
    from job_search_hub.ingest.remotive import RemotiveAdapter

    httpx_mock.add_response(
        url="https://remotive.com/api/remote-jobs", status_code=429, json={"error": "rate limited"}
    )
    adapter = RemotiveAdapter(load_settings().search_terms)
    assert list(adapter.fetch()) == []
    assert adapter.last_diagnostic["status"] == "rate_limited"
    assert adapter.last_diagnostic["http_status"] == 429
    assert "rate limited" not in json.dumps(adapter.last_diagnostic["error"]).lower()


def test_normal_listing_is_focused_and_json_remains_available(capsys):
    from job_search_hub.commands.common import records
    from job_search_hub.notion import properties as P

    rows = [
        {
            "id": ROOT,
            "properties": {
                "Opportunity": P.title("Example librarian"),
                "Status": P.select("Review"),
                "My Decision": P.select("Shortlist"),
                "Model Score": P.number(0.8),
                "Description": P.rich_text("DETAILED INTERNAL FIELD"),
            },
        }
    ]
    records(rows)
    display = capsys.readouterr().out
    assert "DETAILED INTERNAL FIELD" not in display
    assert "Example" in display and "Review" in display
    records(rows, json_output=True)
    assert json.loads(capsys.readouterr().out) == rows


def test_authenticated_source_failure_does_not_log_credential(httpx_mock, monkeypatch, caplog):
    from job_search_hub.ingest.adzuna import AdzunaAdapter
    from job_search_hub.ingest.base import SearchRequest
    from job_search_hub.ingest.preview import error_result

    monkeypatch.setenv("JOBHUB_ADZUNA_APP_ID", "test-app")
    monkeypatch.setenv("JOBHUB_ADZUNA_APP_KEY", "PRIVATE_SOURCE_CREDENTIAL")
    monkeypatch.setenv("JOBHUB_JOB_SEARCH_ROLE_TERMS", "librarian")
    settings = load_settings()
    httpx_mock.add_response(status_code=403, json={"error": "PRIVATE_SOURCE_CREDENTIAL"})
    adapter = AdzunaAdapter(settings.adapters, settings.search_terms)
    monkeypatch.setattr(adapter, "_paced_searches", lambda rows: iter(list(rows)[:1]))
    assert list(adapter.fetch()) == []
    assert "PRIVATE_SOURCE_CREDENTIAL" not in caplog.text
    result = error_result(
        SearchRequest("Test", "adzuna", "https://example.test"),
        error="PRIVATE_SOURCE_CREDENTIAL",
        http_status=403,
    )
    assert "PRIVATE_SOURCE_CREDENTIAL" not in result.error


def test_benchmark_inspection_does_not_create_persistent_logs(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "job_search_hub.models.benchmark.ModelRouter",
        lambda *a, **kw: SimpleNamespace(discover=lambda: {}),
    )
    monkeypatch.setattr(
        "job_search_hub.models.benchmark.setup_logging",
        lambda *a, **kw: pytest.fail("inspection configured persistent file logging"),
    )
    assert main(["maintenance", "benchmark", "--tasks", "chat"]) == 0
    assert not storage.app_paths().data.exists()
