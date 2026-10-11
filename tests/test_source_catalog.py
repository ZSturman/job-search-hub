from job_search_hub.config import load_settings
from job_search_hub.ingest.preview import planned_searches
from job_search_hub.ingest.registry import REGISTRY
from job_search_hub.source_catalog import format_source_table, load_source_catalog


def test_source_catalog_reports_missing_env_and_targets(monkeypatch, tmp_path):
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
sources:
  - key: greenhouse
    name: Greenhouse
    adapter: greenhouse
    implemented: true
    required_env: JOBHUB_GREENHOUSE_BOARDS
    target_env: JOBHUB_GREENHOUSE_BOARDS
    cadence: daily
    submission: review-gated
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBHUB_SOURCE_CATALOG_FILE", str(catalog))
    settings = load_settings(env_file=tmp_path / "missing.env")

    rows = {r.key: r for r in load_source_catalog(settings)}
    assert rows["greenhouse"].status == "skipped"
    assert rows["greenhouse"].missing_env == ("JOBHUB_GREENHOUSE_BOARDS",)

    monkeypatch.setenv("JOBHUB_GREENHOUSE_BOARDS", "acme,otherco")
    settings = load_settings(env_file=tmp_path / "missing.env")
    rows = {r.key: r for r in load_source_catalog(settings)}
    assert rows["greenhouse"].status == "configured"
    assert rows["greenhouse"].targets == ("acme", "otherco")
    assert "Greenhouse" in format_source_table(rows.values())


def test_source_catalog_includes_former_matrix_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUB_SOURCE_CATALOG_FILE", raising=False)
    settings = load_settings(env_file=tmp_path / "missing.env")
    keys = {r.key for r in load_source_catalog(settings)}
    assert {"ashby", "greenhouse", "rss", "remotive", "built_in"} <= keys
    assert not {"paws", "yc_seattle_hiring", "seattle_humane"} & keys


def test_implemented_source_catalog_adapters_exist(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUB_SOURCE_CATALOG_FILE", raising=False)
    settings = load_settings(env_file=tmp_path / "missing.env")
    rows = load_source_catalog(settings)
    implemented = {row.adapter_key for row in rows if row.implemented and row.adapter_key}
    assert implemented <= (set(REGISTRY) | {"manual"})


def test_configured_source_adapters_appear_in_preview(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBHUB_SOURCE_CATALOG_FILE", raising=False)
    monkeypatch.setenv("JOBHUB_GREENHOUSE_BOARDS", "acme")
    monkeypatch.setenv("JOBHUB_ASHBY_BOARDS", "acme")
    monkeypatch.setenv("JOBHUB_USAJOBS_USER_AGENT", "me@example.com")
    monkeypatch.setenv("JOBHUB_USAJOBS_AUTH_KEY", "key")
    monkeypatch.setenv("JOBHUB_ADZUNA_APP_ID", "id")
    monkeypatch.setenv("JOBHUB_ADZUNA_APP_KEY", "key")
    monkeypatch.setenv("JOBHUB_JOOBLE_API_KEY", "key")
    settings = load_settings(env_file=tmp_path / "missing.env")
    configured = {
        row.adapter_key
        for row in load_source_catalog(settings)
        if row.status == "configured" and row.adapter_key and row.adapter_key != "manual"
    }
    previewed = {
        getattr(item, "request", item).source_adapter for item in planned_searches(settings)
    }

    assert configured <= previewed


def test_no_source_docs_runtime_or_readme_references():
    checked = [
        "README.md",
        "job_search_hub/source_catalog.py",
        "job_search_hub/ingest/preview.py",
        "job_search_hub/pipeline/source_check.py",
    ]
    for path in checked:
        text = __import__("pathlib").Path(path).read_text(encoding="utf-8")
        assert "source_docs/JOB_SOURCE_MATRIX.md" not in text
