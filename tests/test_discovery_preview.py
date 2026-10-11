from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from job_search_hub.config import load_settings
from job_search_hub.db import open_store
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import (
    dedupe_postings,
    format_preview_results,
    planned_searches,
    redacted_request,
)
from job_search_hub.ingest.usajobs import USAJobsAdapter
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.canonical_resolution import CanonicalResolution
from job_search_hub.pipeline.ingest_stage import (
    IngestStats,
    SupplementalResolutionStats,
    _ingest_posting,
    reconcile_notion_opportunities,
    run_ingest,
)
from job_search_hub.pipeline.normalize import description_hash, fingerprint
from job_search_hub.pipeline.posting_dates import DATE_JOB_WAS_POSTED


def _posting(
    *,
    company: str = "Acme",
    role: str = "Software Engineer",
    url: str = "https://example.com/jobs/1",
    adapter: str = "test",
    posted_at: datetime | None = None,
) -> RawPosting:
    return RawPosting(
        source=adapter.title(),
        source_adapter=adapter,
        external_job_id=url,
        company=company,
        role_title=role,
        url=url,
        description=(
            "Build and test reliable React and Python software, collaborate with "
            "engineers, review code, and improve automated delivery systems."
        ),
        posted_at=posted_at,
        discovered_at=datetime.now(UTC),
    )


def test_blank_source_list_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_SOURCE_CATALOG_FILE", str(tmp_path / "catalog.yaml"))
    (tmp_path / "catalog.yaml").write_text(
        """
sources:
  - key: lever
    name: Lever
    adapter: lever
    implemented: true
    required_env: LEVER_COMPANIES
    target_env: LEVER_COMPANIES
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBHUB_LEVER_COMPANIES", "")
    settings = load_settings(env_file=tmp_path / "missing.env")

    items = planned_searches(settings, sources=["lever"])

    assert len(items) == 1
    assert isinstance(items[0], SearchResult)
    assert items[0].status == "skipped"
    assert "JOBHUB_LEVER_COMPANIES" in items[0].skip_reason


def test_usajobs_local_remote_keyword_expansion_and_redaction(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_USAJOBS_USER_AGENT", "me@example.com")
    monkeypatch.setenv("JOBHUB_USAJOBS_AUTH_KEY", "private-key")
    monkeypatch.setenv("JOBHUB_USAJOBS_KEYWORDS", "software engineer|data engineer")
    monkeypatch.setenv("JOBHUB_USAJOBS_LOCATION", "Seattle, Washington")
    monkeypatch.setenv("JOBHUB_USAJOBS_RADIUS_MILES", "50")
    monkeypatch.setenv("JOBHUB_USAJOBS_REMOTE", "true")
    monkeypatch.setenv("JOBHUB_USAJOBS_WHO_MAY_APPLY", "public")
    monkeypatch.setenv("JOBHUB_USAJOBS_DATE_POSTED_DAYS", "14")
    monkeypatch.setenv("JOBHUB_USAJOBS_JOB_CATEGORY_CODES", "2210;1550")
    settings = load_settings(env_file=tmp_path / "missing.env")

    requests = list(USAJobsAdapter(settings.adapters).plan_searches())

    assert len(requests) == 4
    assert {r.keyword for r in requests} == {"software engineer", "data engineer"}
    assert {r.pass_name for r in requests} == {"local", "remote"}
    assert all(r.params["WhoMayApply"] == "public" for r in requests)
    assert all(r.params["DatePosted"] == "14" for r in requests)
    assert all(r.params["JobCategoryCode"] == "2210;1550" for r in requests)

    redacted = redacted_request(requests[0])
    assert redacted.headers["Authorization-Key"] == "[redacted]"
    assert redacted.headers["User-Agent"] == "[redacted]"


def test_dedupe_and_console_output_include_examples():
    posting = _posting()
    duplicate = _posting()
    other = _posting(company="Other", role="Data Engineer", url="https://example.com/jobs/2")

    assert len(dedupe_postings([posting, duplicate, other])) == 2

    text = format_preview_results(
        [
            SearchResult(
                request=SearchRequest(
                    source="Ashby",
                    source_adapter="ashby",
                    endpoint="https://api.ashbyhq.com/posting-api/job-board/acme",
                    target="acme",
                    target_kind="board",
                ),
                status="ok",
                http_status=200,
                elapsed_ms=42,
                raw_count=2,
                deduped_count=2,
                examples=[posting, other],
            )
        ]
    )

    assert "Software Engineer | Acme" in text
    assert "Data Engineer | Other" in text
    assert "Per-source summary" in text
    assert "Aggregate totals" in text


def test_dry_run_ingest_does_not_write_opportunity_cache(monkeypatch, tmp_path):
    class FakeAdapter(Adapter):
        key = "fake"
        label = "Fake"

        def fetch(self) -> Iterable[RawPosting]:
            yield _posting(adapter="fake")

    settings = load_settings(env_file=tmp_path / "missing.env")
    store = open_store(tmp_path / "store.sqlite")
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.build_adapters",
        lambda settings, only=None, organization_targets=None: [FakeAdapter()],
    )

    class FakeNotion:
        def query_data_source(self, data_source_id):
            return iter(())

    stats = run_ingest(
        settings,
        notion=FakeNotion(),
        store=store,
        dry_run=True,
        only_adapters=["fake"],
    )

    assert stats.new_opportunities == 1
    from job_search_hub.pipeline.normalize import fingerprint

    fp = fingerprint(_posting(adapter="fake"))
    assert store.find_opportunity_by_fingerprint(fp) is None


def test_supplemental_link_block_stops_repeating_refused_host_requests(monkeypatch, tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    resolution_stats = SupplementalResolutionStats()
    calls = []

    def blocked(posting, **kwargs):
        calls.append(posting.url)
        return CanonicalResolution(
            accepted=False,
            canonical_url="",
            discovery_url=posting.url,
            redirect_chain=[posting.url],
            source_quality="aggregator",
            reason="http_403",
            http_status=403,
        )

    monkeypatch.setattr("job_search_hub.pipeline.ingest_stage.resolve_posting", blocked)
    first, first_block = resolution_stats.resolve(
        _posting(adapter="adzuna", url="https://www.adzuna.example/land/ad/1"),
        settings=settings,
    )
    second, second_block = resolution_stats.resolve(
        _posting(adapter="adzuna", url="https://www.adzuna.example/land/ad/2"),
        settings=settings,
    )

    assert first.reason == "http_403"
    assert first_block == "www.adzuna.example"
    assert second.reason == "blocked_http_403"
    assert second.resolution_method == "host_circuit_breaker"
    assert second_block == ""
    assert calls == ["https://www.adzuna.example/land/ad/1"]
    assert resolution_stats.network_checks == 1
    assert resolution_stats.skipped_after_host_block == 1
    assert "http_403=1" in resolution_stats.summary()
    assert "blocked_http_403=1" in resolution_stats.summary()


def test_ingest_logs_one_clear_summary_for_a_blocked_supplemental_source(
    monkeypatch, tmp_path, caplog
):
    class FakeAdapter(Adapter):
        key = "adzuna"
        label = "Adzuna"

        def fetch(self) -> Iterable[RawPosting]:
            yield _posting(adapter="adzuna", url="https://www.adzuna.example/land/ad/1")
            yield _posting(adapter="adzuna", url="https://www.adzuna.example/land/ad/2")

    settings = load_settings(env_file=tmp_path / "missing.env")
    store = open_store(tmp_path / "store.sqlite")
    calls = []

    def blocked(posting, **kwargs):
        calls.append(posting.url)
        return CanonicalResolution(
            accepted=False,
            canonical_url="",
            discovery_url=posting.url,
            redirect_chain=[posting.url],
            source_quality="aggregator",
            reason="http_429",
            http_status=429,
        )

    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.build_adapters",
        lambda settings, only=None, organization_targets=None: [FakeAdapter()],
    )
    monkeypatch.setattr("job_search_hub.pipeline.ingest_stage.resolve_posting", blocked)

    class FakeNotion:
        def query_data_source(self, data_source_id, **kwargs):
            return iter(())

    with caplog.at_level("INFO"):
        stats = run_ingest(
            settings,
            notion=FakeNotion(),
            store=store,
            dry_run=True,
            only_adapters=["adzuna"],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert len(calls) == 1
    assert calls[0] in {
        "https://www.adzuna.example/land/ad/1",
        "https://www.adzuna.example/land/ad/2",
    }
    assert stats.unresolved_supplemental == 2
    assert stats.supplemental_link_checks == 1
    assert stats.supplemental_hosts_blocked == 1
    assert sum("link verification blocked" in message for message in messages) == 1
    assert any(
        "adapter=adzuna link-resolution summary" in message
        and "requests=1" in message
        and "skipped_after_block=1" in message
        for message in messages
    )


def test_ingest_filters_before_resolving_supplemental_links(monkeypatch, tmp_path):
    class FakeAdapter(Adapter):
        key = "adzuna"
        label = "Adzuna"

        def fetch(self) -> Iterable[RawPosting]:
            for job_id in range(3):
                yield _posting(
                    adapter="adzuna",
                    url=f"https://www.adzuna.example/land/ad/{job_id}",
                )

    settings = load_settings(env_file=tmp_path / "missing.env")
    settings = replace(
        settings,
        discovery=replace(settings.discovery, max_per_query=1, max_per_source=1),
    )
    store = open_store(tmp_path / "store.sqlite")
    calls = []

    def resolved(posting, **kwargs):
        calls.append(posting.url)
        return CanonicalResolution(
            accepted=True,
            canonical_url=posting.url,
            discovery_url=posting.url,
            redirect_chain=[posting.url],
            resolution_method="final_url",
            source_quality="aggregator",
            reason="verified",
            http_status=200,
        )

    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.build_adapters",
        lambda settings, only=None, organization_targets=None: [FakeAdapter()],
    )
    monkeypatch.setattr("job_search_hub.pipeline.ingest_stage.resolve_posting", resolved)

    class FakeNotion:
        def query_data_source(self, data_source_id, **kwargs):
            return iter(())

    stats = run_ingest(
        settings,
        notion=FakeNotion(),
        store=store,
        dry_run=True,
        only_adapters=["adzuna"],
    )

    assert len(calls) == 1
    assert stats.cap_overflow == 2
    assert stats.supplemental_link_checks == 1


def test_ingest_writes_posted_date_on_create(monkeypatch, tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["opportunities"] = "opportunities"
    store = open_store(tmp_path / "store.sqlite")
    posted_at = datetime(2026, 6, 2, 12, tzinfo=UTC)
    posting = _posting(posted_at=posted_at)
    created = {}

    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.verify_posting_url",
        lambda *args, **kwargs: SimpleNamespace(
            closed=False,
            status="available",
            reason="ok",
            checked_at=datetime.now(UTC),
            http_status=200,
            final_url=posting.url,
        ),
    )

    class FakeNotion:
        def query_data_source(self, data_source_id, **kwargs):
            return iter(())

        def create_page(self, **kwargs):
            created.update(kwargs["properties"])
            return {"id": "created"}

    stats = IngestStats()
    _ingest_posting(
        posting,
        settings=settings,
        notion=FakeNotion(),
        store=store,
        stats=stats,
        dry_run=False,
    )

    assert stats.new_opportunities == 1
    assert P.read_date(created[DATE_JOB_WAS_POSTED]).startswith("2026-06-02")


def test_ingest_refreshes_posted_date_on_duplicate(monkeypatch, tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["opportunities"] = "opportunities"
    store = open_store(tmp_path / "store.sqlite")
    posted_at = datetime(2026, 6, 3, 8, tzinfo=UTC)
    posting = _posting(posted_at=posted_at)
    fp = fingerprint(posting)
    store.upsert_opportunity_cache(
        fingerprint=fp,
        notion_page_id="existing",
        source=posting.source,
        source_adapter=posting.source_adapter,
        external_job_id=posting.external_job_id,
        company=posting.company,
        role_title=posting.role_title,
        url=posting.url,
        description_hash=description_hash(posting.description),
    )
    updates = []

    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.verify_posting_url",
        lambda *args, **kwargs: SimpleNamespace(
            closed=False,
            status="available",
            reason="ok",
            checked_at=datetime.now(UTC),
            http_status=200,
            final_url=posting.url,
        ),
    )

    class FakeNotion:
        def get_page(self, page_id):
            return {
                "id": page_id,
                "properties": {
                    "Status": P.select("Fetched"),
                    DATE_JOB_WAS_POSTED: P.date_property(None),
                },
            }

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    stats = IngestStats()
    _ingest_posting(
        posting,
        settings=settings,
        notion=FakeNotion(),
        store=store,
        stats=stats,
        dry_run=False,
    )

    assert stats.duplicates == 1
    assert updates
    assert P.read_date(updates[0][1][DATE_JOB_WAS_POSTED]).startswith("2026-06-03")


def test_reconcile_trashes_duplicate_notion_pages_and_seeds_cache(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["opportunities"] = "opportunities"
    store = open_store(tmp_path / "store.sqlite")

    def text(value: str) -> dict:
        return {"rich_text": [{"plain_text": value}]}

    def page(page_id: str, url: str, status: str, created: str) -> dict:
        return {
            "id": page_id,
            "created_time": created,
            "properties": {
                "Company": text("Acme"),
                "Role Title": text("Software Engineer"),
                "URL": P.url(url),
                "Source": P.select("Adzuna"),
                "Source Adapter": P.select("adzuna"),
                "External Job ID": text("123"),
                "Fingerprint": text("old"),
                "Description": text("Build software"),
                "Location": text("Seattle"),
                "Status": P.select(status),
            },
        }

    class FakeNotion:
        def __init__(self):
            self.pages = [
                page(
                    "older",
                    "https://www.adzuna.com/land/ad/123?se=one",
                    "Fetched",
                    "2026-06-01T00:00:00Z",
                ),
                page(
                    "progressed",
                    "https://www.adzuna.com/land/ad/123?se=two",
                    "Submitted",
                    "2026-06-02T00:00:00Z",
                ),
            ]
            self.updated = []
            self.archived = []

        def query_data_source(self, data_source_id, **kwargs):
            return iter(self.pages)

        def update_page(self, page_id, properties):
            self.updated.append((page_id, properties))

        def archive_page(self, page_id):
            self.archived.append(page_id)

    notion = FakeNotion()
    stats = reconcile_notion_opportunities(
        settings,
        notion=notion,
        store=store,
        dry_run=False,
    )

    assert stats.pages_scanned == 2
    assert stats.duplicate_pages == 1
    assert notion.archived == ["older"]
    assert notion.updated[0][0] == "progressed"
    fp = fingerprint(_posting(url="https://www.adzuna.com/land/ad/123?se=three"))
    assert store.find_opportunity_by_fingerprint(fp)["notion_page_id"] == "progressed"


def test_ingest_checks_notion_when_local_cache_is_empty(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["opportunities"] = "opportunities"
    store = open_store(tmp_path / "store.sqlite")
    posting = _posting(url="https://www.adzuna.com/land/ad/123?se=new")
    fp = fingerprint(posting)

    class FakeNotion:
        def query_data_source(self, data_source_id, **kwargs):
            assert kwargs["filter_"] == {
                "property": "Fingerprint",
                "rich_text": {"equals": fp},
            }
            return iter([{"id": "existing", "properties": {}}])

        def create_page(self, **kwargs):
            raise AssertionError("existing Notion opportunity must not be recreated")

    stats = IngestStats()
    _ingest_posting(
        posting,
        settings=settings,
        notion=FakeNotion(),
        store=store,
        stats=stats,
        dry_run=False,
    )

    assert stats.new_opportunities == 0
    assert stats.duplicates == 1
    assert store.find_opportunity_by_fingerprint(fp)["notion_page_id"] == "existing"
