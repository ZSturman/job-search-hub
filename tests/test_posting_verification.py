from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx

from job_search_hub.ingest.base import RawPosting
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.ingest_stage import IngestStats, _ingest_posting
from job_search_hub.pipeline.lifecycle_sync import (
    create_manual_applications,
    reconcile_lifecycles,
)
from job_search_hub.pipeline.normalize import fingerprint
from job_search_hub.pipeline.posting_date_backfill import run_posting_date_backfill
from job_search_hub.pipeline.posting_dates import DATE_JOB_WAS_POSTED
from job_search_hub.pipeline.posting_verification import (
    POSTING_CHECKED_AT,
    PRE_SCORE_BLOCK_REASON,
    PostingVerificationResult,
    VerificationContext,
    close_stale_posted_opportunities,
    migrate_inconclusive_review_rows,
    verify_active_opportunities,
    verify_posting_context,
    verify_posting_url,
    verify_tick_preflight,
)
from job_search_hub.pipeline.score import Criterion
from job_search_hub.pipeline.score_stage import run_score


def _response(status_code: int, text: str = "Software Engineer at Acme") -> httpx.Response:
    return httpx.Response(
        status_code, text=text, request=httpx.Request("GET", "https://example.com/jobs/1")
    )


def _client(response: httpx.Response) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: response),
        follow_redirects=True,
    )


def test_verify_posting_url_closes_404_and_410() -> None:
    for status_code in (404, 410):
        with _client(_response(status_code)) as client:
            result = verify_posting_url("https://example.com/jobs/1", client=client)

        assert result.status == "closed"
        assert result.http_status == status_code


def test_verify_posting_url_closes_explicit_unavailable_text() -> None:
    with _client(_response(200, "This job is no longer available.")) as client:
        result = verify_posting_url("https://example.com/jobs/1", client=client)

    assert result.status == "closed"
    assert result.reason == "explicit closed-page text"


def test_verify_posting_url_closes_job_position_unavailable_text() -> None:
    body = "The job position is no longer available. You can check similar jobs."
    with _client(_response(200, body)) as client:
        result = verify_posting_url("https://example.com/jobs/1", client=client)

    assert result.status == "closed"
    assert result.reason == "explicit closed-page text"


def test_verify_posting_url_closes_jooble_closed_job_query() -> None:
    with _client(_response(200, "Similar jobs")) as client:
        result = verify_posting_url(
            "https://jooble.org/SearchResult?closedJob=True&loc=4",
            client=client,
        )

    assert result.status == "closed"
    assert "closedJob=true" in result.reason


def test_verify_posting_url_closes_jooble_redirect_to_closed_job_query() -> None:
    original_url = "https://jooble.org/jdp/8269795999383279474"
    closed_url = (
        "https://jooble.org/SearchResult?closedJob=True&loc=4"
        "&rgns=Sunnyvale%2C%20CA&ukw=sr.%20health%20ai%20software%20engineer"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == original_url:
            return httpx.Response(
                302,
                headers={"location": closed_url},
                request=request,
            )
        return httpx.Response(
            200,
            content="Similar jobs",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = verify_posting_url(original_url, client=client)

    assert result.status == "closed"
    assert "after redirect" in result.reason
    assert "closedJob=true" in result.reason
    assert result.final_url == closed_url


def test_verify_posting_url_marks_jooble_search_without_closed_query_unknown() -> None:
    with _client(_response(200, "Full-stack engineer jobs in United States")) as client:
        result = verify_posting_url(
            "https://jooble.org/SearchResult?loc=4&rgns=United%20States",
            client=client,
        )

    assert result.status == "unknown"
    assert "generic job search" in result.reason


def test_generic_redirect_is_unknown_even_when_search_page_mentions_closed_jobs() -> None:
    original_url = "https://www.adzuna.com/land/ad/5763572438"
    search_url = "https://www.adzuna.com/jobs/search?what=python"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == original_url:
            return httpx.Response(302, headers={"location": search_url}, request=request)
        return httpx.Response(
            200,
            text="This job is no longer available. Here are similar jobs.",
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = verify_posting_url(
            original_url,
            external_job_id="5763572438",
            source_adapter="adzuna",
            client=client,
        )

    assert result.status == "unknown"
    assert "generic job search" in result.reason


def test_verify_posting_url_closes_ashby_page_not_found_text() -> None:
    body = "Page not found\nThe page you requested was not found\nPowered by Ashby"
    with _client(_response(200, body)) as client:
        result = verify_posting_url(
            "https://jobs.ashbyhq.com/cursor/1d6dbbb2-a5af-41a6-a70a-ca97690d9313",
            client=client,
        )

    assert result.status == "closed"
    assert result.reason == "explicit closed-page text"


def test_verify_posting_url_keeps_normal_page_available() -> None:
    with _client(_response(200, "Senior Software Engineer - apply today")) as client:
        result = verify_posting_url("https://example.com/jobs/1", client=client)

    assert result.status == "available"


def test_verify_posting_url_keeps_ambiguous_failures_unknown() -> None:
    with _client(_response(403, "blocked")) as client:
        forbidden = verify_posting_url("https://example.com/jobs/1", client=client)

    timeout_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))
        )
    )
    with timeout_client:
        timeout = verify_posting_url("https://example.com/jobs/1", client=timeout_client)

    invalid = verify_posting_url("not a url")

    assert forbidden.status == "unknown"
    assert timeout.status == "unknown"
    assert invalid.status == "unknown"


def test_adzuna_403_browser_closed_text_closes(monkeypatch) -> None:
    context = VerificationContext(
        url="https://www.adzuna.com/land/ad/5763572438",
        source_adapter="adzuna",
        external_job_id="5763572438",
        company="Encore Talent Solutions",
        role_title="Python, Typescript Developer",
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: _response(403, "blocked")),
        follow_redirects=True,
    )

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification._verify_with_browser",
        lambda ctx: PostingVerificationResult(
            status="closed",
            reason="browser-rendered closed-page text",
            checked_at=datetime.now(UTC),
            http_status=200,
            final_url=ctx.url,
        ),
    )

    with client:
        result = verify_posting_context(
            context,
            client=client,
            browser_fallback="auto",
            require_decisive=True,
        )

    assert result.status == "closed"
    assert result.reason == "browser-rendered closed-page text"


def test_adzuna_403_browser_unavailable_blocks_pre_score(monkeypatch, tmp_store) -> None:
    page = {
        "id": "adzuna-opportunity",
        "properties": {
            "Status": P.select("Fetched"),
            "Source Adapter": P.select("adzuna"),
            "External Job ID": P.rich_text("5763572438"),
            "URL": P.url("https://www.adzuna.com/land/ad/5763572438"),
            POSTING_CHECKED_AT: P.date_property(datetime.now(UTC).isoformat()),
        },
    }
    updates = []

    class Notion:
        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="unknown",
            reason="ambiguous HTTP 403",
            checked_at=datetime.now(UTC),
            http_status=403,
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification._verify_with_browser",
        lambda ctx: PostingVerificationResult(
            status="unknown",
            reason="browser fallback unavailable",
            checked_at=datetime.now(UTC),
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
        pages=[page],
        require_decisive=True,
    )

    assert stats.checked == 1
    assert stats.closed == 0
    assert stats.blocked_page_ids == {"adzuna-opportunity"}
    assert updates[0][0] == "adzuna-opportunity"
    assert updates[0][1]["Status"] == P.select("Verification Needed")
    assert updates[0][1]["Last Error"] == P.rich_text(
        f"{PRE_SCORE_BLOCK_REASON}: browser fallback unavailable"
    )


def test_unknown_posting_result_uses_local_exponential_backoff(tmp_store) -> None:
    checked_at = datetime.now(UTC)
    tmp_store.record_posting_verification(
        opportunity_page_id="blocked-opportunity",
        host="blocked.example",
        status="unknown",
        reason="ambiguous HTTP 403",
        checked_at=checked_at,
    )

    assert not tmp_store.posting_verification_due("blocked-opportunity", now=checked_at)
    assert not tmp_store.host_verification_due("blocked.example", now=checked_at)

    tmp_store.record_posting_verification(
        opportunity_page_id="blocked-opportunity",
        host="blocked.example",
        status="available",
        reason="reached posting",
        checked_at=checked_at,
    )

    assert tmp_store.posting_verification_due("blocked-opportunity", now=checked_at)
    assert tmp_store.host_verification_due("blocked.example", now=checked_at)


def test_inconclusive_review_migration_preserves_human_rows() -> None:
    pages = [
        {
            "id": "automation-row",
            "properties": {
                "Last Error": P.rich_text(f"{PRE_SCORE_BLOCK_REASON}: HTTP 403"),
                "My Decision": P.select("Undecided"),
                "Application": P.relation([]),
            },
        },
        {
            "id": "human-row",
            "properties": {
                "Last Error": P.rich_text(f"{PRE_SCORE_BLOCK_REASON}: HTTP 403"),
                "My Decision": P.select("Review"),
                "Application": P.relation([]),
            },
        },
    ]
    updates = []

    class Notion:
        def query_data_source(self, _data_source_id, **_kwargs):
            return pages

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    settings = SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opps"}))
    stats = migrate_inconclusive_review_rows(settings, Notion(), dry_run=False)

    assert stats.notion_updates == 1
    assert updates == [("automation-row", {"Status": P.select("Verification Needed")})]


def test_greenhouse_api_absence_closes_only_after_successful_authoritative_response() -> None:
    context = VerificationContext(
        url="https://boards.greenhouse.io/acme/jobs/123",
        source_adapter="greenhouse",
        external_job_id="123",
        company="Acme",
        role_title="Engineer",
    )

    def successful_api(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": 999, "absolute_url": "https://boards.greenhouse.io/acme/jobs/999"}
                    ]
                },
                request=request,
            )
        return _response(200, "Engineer at Acme")

    with httpx.Client(transport=httpx.MockTransport(successful_api)) as client:
        closed = verify_posting_context(context, client=client)

    assert closed.status == "closed"
    assert closed.reason == "Greenhouse board API membership absent"

    def failed_api_then_reachable_page(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(500, text="server error", request=request)
        return httpx.Response(200, text="Engineer at Acme", request=request)

    with httpx.Client(transport=httpx.MockTransport(failed_api_then_reachable_page)) as client:
        available = verify_posting_context(context, client=client)

    assert available.status == "available"


def test_authoritative_board_snapshot_is_shared_for_same_company() -> None:
    contexts = [
        VerificationContext(
            url="https://job-boards.greenhouse.io/acme/jobs/101",
            source_adapter="greenhouse",
            external_job_id="101",
        ),
        VerificationContext(
            url="https://job-boards.greenhouse.io/acme/jobs/202",
            source_adapter="greenhouse",
            external_job_id="202",
        ),
    ]
    api_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal api_calls
        if request.url.host == "boards-api.greenhouse.io":
            api_calls += 1
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 101,
                            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
                        },
                        {
                            "id": 202,
                            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/202",
                        },
                    ]
                },
                request=request,
            )
        return httpx.Response(200, text="Open role", request=request)

    snapshots = {}
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        results = [
            verify_posting_context(context, client=client, snapshot_cache=snapshots)
            for context in contexts
        ]

    assert [result.status for result in results] == ["available", "available"]
    assert api_calls == 1


def test_smartrecruiters_snapshot_paginates_before_membership_decision() -> None:
    context = VerificationContext(
        url="https://jobs.smartrecruiters.com/acme/999",
        source_adapter="smartrecruiters",
        external_job_id="999",
    )
    offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.smartrecruiters.com":
            offset = request.url.params.get("offset", "")
            offsets.append(offset)
            if offset == "0":
                return httpx.Response(
                    200,
                    json={"content": [{"id": str(i)} for i in range(100)], "totalFound": 101},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"content": [{"id": "999"}], "totalFound": 101},
                request=request,
            )
        return httpx.Response(200, text="Open role", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = verify_posting_context(context, client=client)

    assert result.status == "available"
    assert offsets == ["0", "100"]


def test_incomplete_authoritative_board_never_closes_a_posting() -> None:
    context = VerificationContext(
        url="https://jobs.smartrecruiters.com/acme/999",
        source_adapter="smartrecruiters",
        external_job_id="999",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.smartrecruiters.com":
            if request.url.params.get("offset") == "0":
                return httpx.Response(
                    200,
                    json={"content": [{"id": str(i)} for i in range(100)]},
                    request=request,
                )
            return httpx.Response(500, text="retry later", request=request)
        return httpx.Response(429, text="blocked", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        result = verify_posting_context(context, client=client)

    assert result.status == "unknown"
    assert result.status != "closed"


def test_tick_preflight_checks_actionable_and_stale_rows_without_repolling_review(
    monkeypatch, tmp_store
) -> None:
    recent = datetime.now(UTC).isoformat()
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    pages = [
        {
            "id": "review",
            "properties": {
                "Status": P.select("Review"),
                "URL": P.url("https://example.com/review"),
                POSTING_CHECKED_AT: P.date_property(recent),
            },
        },
        {
            "id": "recent-fetched",
            "properties": {
                "Status": P.select("Fetched"),
                "URL": P.url("https://example.com/recent"),
                POSTING_CHECKED_AT: P.date_property(recent),
            },
        },
        {
            "id": "stale-fetched",
            "properties": {
                "Status": P.select("Fetched"),
                "URL": P.url("https://example.com/stale"),
                POSTING_CHECKED_AT: P.date_property(stale),
            },
        },
    ]
    updates = []

    class Notion:
        def query_data_source(self, _data_source_id, **kwargs):
            filter_ = kwargs["filter_"]
            status = (
                filter_["and"][0]["select"]["equals"]
                if "and" in filter_
                else filter_["select"]["equals"]
            )
            rows = [page for page in pages if P.read_select(page["properties"]["Status"]) == status]
            if "and" in filter_:
                rows = [
                    page
                    for page in rows
                    if P.read_date(page["properties"].get(POSTING_CHECKED_AT)) == stale
                ]
            return rows

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_context",
        lambda *_args, **_kwargs: PostingVerificationResult(
            "available", "confirmed", datetime.now(UTC)
        ),
    )

    stats = verify_tick_preflight(_settings(), Notion(), tmp_store, dry_run=False)

    assert stats.checked == 1
    assert updates == []


def test_verify_active_opportunities_skips_recent_and_protected(
    monkeypatch,
    tmp_store,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    recent = datetime.now(UTC).isoformat()
    pages = [
        {
            "id": "old-active",
            "properties": {
                "Status": P.select("Fetched"),
                "URL": P.url("https://example.com/old"),
                POSTING_CHECKED_AT: P.date_property(old),
            },
        },
        {
            "id": "recent-active",
            "properties": {
                "Status": P.select("Review"),
                "URL": P.url("https://example.com/recent"),
                POSTING_CHECKED_AT: P.date_property(recent),
            },
        },
        {
            "id": "submitted",
            "properties": {
                "Status": P.select("Submitted"),
                "URL": P.url("https://example.com/submitted"),
            },
        },
    ]
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return pages

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="closed",
            reason="HTTP 404",
            checked_at=datetime.now(UTC),
            http_status=404,
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
    )

    assert stats.checked == 1
    assert stats.notion_updates == 1
    assert stats.dry_run_updates == 0
    assert stats.skipped_recent == 1
    assert stats.skipped_protected >= 1
    assert updates[0][0] == "old-active"
    assert updates[0][1]["Status"] == P.select("Closed")
    assert "posting verification: queue loaded" in caplog.text
    assert "POSTING  |" in caplog.text
    assert "checked=1" in caplog.text
    assert "notion_updates=1" in caplog.text


def test_verify_active_opportunities_dry_run_logs_unwritten_updates(
    monkeypatch,
    tmp_store,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    page = {
        "id": "active",
        "properties": {
            "Status": P.select("Fetched"),
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("Engineer"),
            "URL": P.url("https://example.com/active"),
        },
    }
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="available",
            reason="posting reachable",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=True,
    )

    assert stats.checked == 1
    assert stats.notion_updates == 0
    assert stats.dry_run_updates == 0
    assert updates == []
    assert "dry_run=true" in caplog.text
    assert "dry_run_updates=0" in caplog.text


def test_verify_active_opportunities_force_checks_recent_rows(
    monkeypatch,
    tmp_store,
) -> None:
    page = {
        "id": "recent-active",
        "properties": {
            "Status": P.select("Needs Input"),
            "URL": P.url("https://example.com/recent"),
            POSTING_CHECKED_AT: P.date_property(datetime.now(UTC).isoformat()),
        },
    }
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="available",
            reason="posting reachable",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
        force=True,
    )

    assert stats.checked == 1
    assert stats.skipped_recent == 0
    assert updates == []


def test_verify_active_opportunities_max_age_zero_checks_recent_rows(
    monkeypatch,
    tmp_store,
) -> None:
    page = {
        "id": "recent-active",
        "properties": {
            "Status": P.select("Review"),
            "URL": P.url("https://example.com/recent"),
            POSTING_CHECKED_AT: P.date_property(datetime.now(UTC).isoformat()),
        },
    }
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="available",
            reason="posting reachable",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
        max_age_hours=0,
    )

    assert stats.checked == 1
    assert stats.skipped_recent == 0
    assert updates == []


def test_verify_active_opportunities_limit_caps_checked_rows(
    monkeypatch,
    tmp_store,
) -> None:
    pages = [
        {
            "id": f"active-{index}",
            "properties": {
                "Status": P.select("Fetched"),
                "URL": P.url(f"https://example.com/{index}"),
            },
        }
        for index in range(3)
    ]

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return pages

        def update_page(self, page_id, properties):
            pass

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="available",
            reason="posting reachable",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
        limit=2,
    )

    assert stats.checked == 2


def test_verify_active_opportunities_cancels_unsubmitted_applications(
    monkeypatch,
    tmp_store,
) -> None:
    opportunity = {
        "id": "closed-opportunity",
        "properties": {
            "Status": P.select("Needs Input"),
            "URL": P.url("https://example.com/closed"),
            "Application": P.relation(["needs-review-app", "ready-app", "submitted-app"]),
        },
    }
    applications = {
        "needs-review-app": {"properties": {"Status": P.select("Needs Review")}},
        "ready-app": {"properties": {"Status": P.select("Shortlisted")}},
        "submitted-app": {"properties": {"Status": P.select("Submitted")}},
    }
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [opportunity]

        def get_page(self, page_id):
            return applications[page_id]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_verification.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="closed",
            reason="explicit closed-page text",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )

    stats = verify_active_opportunities(
        SimpleNamespace(notion=SimpleNamespace(data_sources={"opportunities": "opportunities"})),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
    )

    assert stats.closed == 1
    assert stats.applications_cancelled == 2
    assert ("needs-review-app", {"Status": P.select("Cancelled")}) in updates
    assert ("ready-app", {"Status": P.select("Cancelled")}) in updates
    assert not any(page_id == "submitted-app" for page_id, _ in updates)
    assert updates[-1][0] == "closed-opportunity"
    assert updates[-1][1]["Status"] == P.select("Closed")
    assert "Posting closed:" in P.read_text(updates[-1][1]["Last Error"])


def test_stale_posting_sweep_uses_calendar_month_cutoff_and_cancels_packets(tmp_store) -> None:
    pages = [
        {
            "id": "older",
            "properties": {
                "Status": P.select("Fetched"),
                DATE_JOB_WAS_POSTED: P.date_property("2026-03-30"),
                "Application": P.relation(["ready", "submitted"]),
            },
        },
        {
            "id": "boundary",
            "properties": {
                "Status": P.select("Fetched"),
                DATE_JOB_WAS_POSTED: P.date_property("2026-03-31"),
            },
        },
        {
            "id": "protected",
            "properties": {
                "Status": P.select("Submitted"),
                DATE_JOB_WAS_POSTED: P.date_property("2026-01-01"),
            },
        },
    ]
    updates = []

    class Notion:
        def get_page(self, page_id):
            return {
                "properties": {
                    "Status": P.select("Submitted" if page_id == "submitted" else "Shortlisted")
                }
            }

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    settings = SimpleNamespace(
        timezone="America/Los_Angeles",
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
    )
    stats = close_stale_posted_opportunities(
        settings,
        Notion(),
        tmp_store,
        dry_run=False,
        pages=pages,
        now=datetime(2026, 5, 31, 12, tzinfo=UTC),
    )

    assert stats.stale_posted_closed == 1
    assert stats.closed_page_ids == {"older"}
    assert stats.applications_cancelled == 1
    assert ("ready", {"Status": P.select("Cancelled")}) in updates
    older_update = next(properties for page_id, properties in updates if page_id == "older")
    assert older_update["Status"] == P.select("Closed")
    assert "posted_date_exceeded_two_months" in P.read_text(older_update["Last Error"])
    assert not any(page_id in {"boundary", "protected", "submitted"} for page_id, _ in updates)


def _settings():
    return SimpleNamespace(
        notion=SimpleNamespace(
            data_sources={"opportunities": "opportunities", "applications": "applications"}
        ),
        hard_floor_annual_usd=60000,
        hard_floor_hourly_usd=30,
        discovery=SimpleNamespace(max_new_per_run=10),
    )


def test_posting_verification_queries_only_stale_active_rows(tmp_store) -> None:
    filters = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            filters.append(kwargs.get("filter_"))
            return []

    stats = verify_active_opportunities(
        _settings(),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
        max_age_hours=48,
    )

    assert stats.candidates == 0
    assert filters
    # The whole-active-set stale-posted sweep runs first; URL checking itself
    # remains limited to rows with stale/missing Posting Checked At values.
    assert any(filter_.get("and") for filter_ in filters)
    assert any(not filter_.get("and") for filter_ in filters)
    assert all(
        any(branch.get("or") for branch in filter_["and"])
        for filter_ in filters
        if filter_.get("and")
    )


def test_recent_posting_checks_are_summarized_without_progress_spam(caplog, tmp_store) -> None:
    page = {
        "id": "recent",
        "properties": {
            "Status": P.select("Fetched"),
            "URL": P.url("https://example.com/jobs/1"),
            POSTING_CHECKED_AT: P.date_property(datetime.now(UTC).isoformat()),
        },
    }

    class Notion:
        pass

    caplog.set_level(logging.INFO, logger="job_search_hub.pipeline.posting_verification")
    stats = verify_active_opportunities(
        _settings(),
        notion=Notion(),
        store=tmp_store,
        dry_run=False,
        pages=[page],
        max_age_hours=48,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert stats.skipped_recent == 1
    assert sum("skipped recently checked" in message for message in messages) == 0
    assert sum("skipped 1 recently checked row(s)" in message for message in messages) == 1


def _posting() -> RawPosting:
    return RawPosting(
        source="Test",
        source_adapter="test",
        external_job_id="job-1",
        company="Acme",
        role_title="Software Engineer",
        url="https://example.com/jobs/1",
        description="Build useful software.",
    )


class _Notion:
    def __init__(self, existing=None) -> None:
        self.created = []
        self.updated = []
        self.pages = {existing["id"]: existing} if existing else {}

    def query_data_source(self, data_source_id, **kwargs):
        return list(self.pages.values())

    def get_page(self, page_id):
        return self.pages[page_id]

    def create_page(self, *, parent, properties):
        page = {"id": "new-page", "properties": properties}
        self.created.append(page)
        self.pages[page["id"]] = page
        return page

    def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        self.pages[page_id]["properties"].update(properties)


def test_ingest_new_closed_posting_creates_closed_row(monkeypatch, tmp_store) -> None:
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="closed",
            reason="HTTP 404",
            checked_at=datetime.now(UTC),
            http_status=404,
        ),
    )
    notion = _Notion()
    stats = IngestStats()

    _ingest_posting(
        _posting(),
        settings=_settings(),
        notion=notion,
        store=tmp_store,
        stats=stats,
        dry_run=False,
    )

    props = notion.created[0]["properties"]
    assert props["Status"] == P.select("Closed")
    assert props[POSTING_CHECKED_AT]["date"]["start"]


def test_ingest_new_available_posting_remains_fetched(monkeypatch, tmp_store) -> None:
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="available",
            reason="posting reachable",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )
    notion = _Notion()
    stats = IngestStats()

    _ingest_posting(
        _posting(),
        settings=_settings(),
        notion=notion,
        store=tmp_store,
        stats=stats,
        dry_run=False,
    )

    assert notion.created[0]["properties"]["Status"] == P.select("Fetched")


def test_ingest_duplicate_active_posting_can_be_closed(monkeypatch, tmp_store) -> None:
    posting = _posting()
    tmp_store.upsert_opportunity_cache(
        fingerprint=fingerprint(posting),
        notion_page_id="existing",
        source=posting.source,
        source_adapter=posting.source_adapter,
        external_job_id=posting.external_job_id,
        company=posting.company,
        role_title=posting.role_title,
        url=posting.url,
        description_hash="old",
    )
    existing = {
        "id": "existing",
        "properties": {
            "Status": P.select("Fetched"),
            "URL": P.url(posting.url),
            POSTING_CHECKED_AT: P.date_property(datetime.now(UTC) - timedelta(days=3)),
        },
    }
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.verify_posting_url",
        lambda *args, **kwargs: PostingVerificationResult(
            status="closed",
            reason="HTTP 404",
            checked_at=datetime.now(UTC),
            http_status=404,
        ),
    )
    notion = _Notion(existing)
    stats = IngestStats()

    _ingest_posting(
        posting,
        settings=_settings(),
        notion=notion,
        store=tmp_store,
        stats=stats,
        dry_run=False,
    )

    assert notion.updated[-1][0] == "existing"
    assert notion.updated[-1][1]["Status"] == P.select("Closed")


def test_posting_date_backfill_updates_exact_and_estimated_dates(monkeypatch, tmp_store) -> None:
    exact_page = {
        "id": "exact",
        "properties": {
            "Status": P.select("Fetched"),
            "Source": P.select("Greenhouse"),
            "Source Adapter": P.select("greenhouse"),
            "External Job ID": P.rich_text("gh-1"),
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("Software Engineer"),
            "URL": P.url("https://example.com/jobs/gh-1"),
            "Description": P.rich_text("Build software."),
            DATE_JOB_WAS_POSTED: P.date_property(None),
        },
    }
    estimated_page = {
        "id": "estimated",
        "properties": {
            "Status": P.select("Fetched"),
            "Source": P.select("Jooble"),
            "Source Adapter": P.select("jooble"),
            "External Job ID": P.rich_text("jb-1"),
            "Company": P.rich_text("Beta"),
            "Role Title": P.rich_text("QA Automation Engineer"),
            "URL": P.url("https://example.com/jobs/jb-1"),
            "Description": P.rich_text("Test software."),
            DATE_JOB_WAS_POSTED: P.date_property(None),
        },
    }

    class Adapter:
        def __init__(self, key, posting):
            self.key = key
            self.posting = posting

        def fetch(self):
            return [self.posting]

    postings = {
        "greenhouse": RawPosting(
            source="Greenhouse",
            source_adapter="greenhouse",
            external_job_id="gh-1",
            company="Acme",
            role_title="Software Engineer",
            url="https://example.com/jobs/gh-1",
            description="Build software.",
            posted_at=datetime(2026, 6, 1, tzinfo=UTC),
        ),
        "jooble": RawPosting(
            source="Jooble",
            source_adapter="jooble",
            external_job_id="jb-1",
            company="Beta",
            role_title="QA Automation Engineer",
            url="https://example.com/jobs/jb-1",
            description="Test software.",
            posted_at=datetime(2026, 6, 2, tzinfo=UTC),
        ),
    }
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_date_backfill.build_adapters",
        lambda settings, only=None: [Adapter(key, postings[key]) for key in only],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_date_backfill.verify_posting_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("matched dates should not require URL verification")
        ),
    )
    notion = _Notion()
    notion.pages = {"exact": exact_page, "estimated": estimated_page}

    stats = run_posting_date_backfill(
        _settings(),
        notion=notion,
        store=tmp_store,
        dry_run=False,
        pages=[exact_page, estimated_page],
        limit=5,
    )

    assert stats.candidates == 2
    assert stats.exact_dates == 1
    assert stats.estimated_dates == 1
    assert stats.updated_page_ids == ["exact", "estimated"]
    assert P.read_date(notion.pages["exact"]["properties"][DATE_JOB_WAS_POSTED]).startswith(
        "2026-06-01"
    )
    assert P.read_date(notion.pages["estimated"]["properties"][DATE_JOB_WAS_POSTED]).startswith(
        "2026-06-02"
    )


def test_posting_date_backfill_keeps_unknown_date_for_review(monkeypatch, tmp_store) -> None:
    page = {
        "id": "risky",
        "properties": {
            "Status": P.select("Fetched"),
            "Source": P.select("Adzuna"),
            "Source Adapter": P.select("adzuna"),
            "External Job ID": P.rich_text("ad-1"),
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("Software Engineer"),
            "URL": P.url("https://adzuna.example/jobs/ad-1"),
            "Description": P.rich_text("Build software."),
            "Application": P.relation(["app-1"]),
            DATE_JOB_WAS_POSTED: P.date_property(None),
        },
    }
    application = {"id": "app-1", "properties": {"Status": P.select("Shortlisted")}}
    tmp_store.record_posting_date_attempt(
        opportunity_page_id="risky",
        source_adapter="adzuna",
        url="https://adzuna.example/jobs/ad-1",
        status="unconfirmed",
        reason="first",
    )
    tmp_store.record_posting_date_attempt(
        opportunity_page_id="risky",
        source_adapter="adzuna",
        url="https://adzuna.example/jobs/ad-1",
        status="unconfirmed",
        reason="second",
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_date_backfill.build_adapters",
        lambda settings, only=None: [],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.posting_date_backfill.verify_posting_context",
        lambda *args, **kwargs: PostingVerificationResult(
            status="available",
            reason="reachable but no source date",
            checked_at=datetime.now(UTC),
            http_status=200,
        ),
    )
    notion = _Notion()
    notion.pages = {"risky": page, "app-1": application}

    stats = run_posting_date_backfill(
        _settings(),
        notion=notion,
        store=tmp_store,
        dry_run=False,
        pages=[page],
        max_attempts=3,
    )

    assert stats.closed == 0 and stats.unknown == 1
    assert notion.pages["risky"]["properties"]["Status"] == P.select("Fetched")
    assert notion.pages["app-1"]["properties"]["Status"] == P.select("Shortlisted")
    assert tmp_store.posting_date_attempt("risky")["last_status"] == "unconfirmed"


def _criterion() -> Criterion:
    return Criterion(
        page_id="criterion-1",
        name="Fit",
        key="fit",
        active=True,
        category="Skills",
        weight=1,
        direction="Positive",
        evaluation_method="LLM",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="Score fit.",
        minimum_value=None,
        maximum_value=None,
        sort_order=1,
    )


def test_score_skips_rows_closed_by_preflight(monkeypatch, tmp_store) -> None:
    page = {
        "id": "opportunity-1",
        "properties": {
            "Status": P.select("Fetched"),
            "URL": P.url("https://example.com/jobs/1"),
        },
    }

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            desired = (kwargs.get("filter_") or {}).get("property")
            if desired == "Status":
                status = kwargs["filter_"]["select"]["equals"]
                return [page] if P.read_select(page["properties"]["Status"]) == status else []
            return [page] if P.read_select(page["properties"]["Status"]) == "Fetched" else []

        def update_page(self, page_id, properties):
            page["properties"].update(properties)

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda *args, **kwargs: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.verify_active_opportunities",
        lambda settings, *, notion, store, dry_run, **kwargs: (
            notion.update_page("opportunity-1", {"Status": P.select("Closed")})
            or SimpleNamespace(closed_page_ids={"opportunity-1"})
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._score_opportunity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("closed row must not be scored")
        ),
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=tmp_store,
        router=SimpleNamespace(),
        client=SimpleNamespace(),
        dry_run=False,
    )

    assert stats.scored == 0


def test_score_preflight_ignores_recent_posting_check(monkeypatch, tmp_store) -> None:
    page = {
        "id": "opportunity-1",
        "properties": {
            "Status": P.select("Fetched"),
            "URL": P.url("https://www.adzuna.com/land/ad/5763572438"),
            "Source Adapter": P.select("adzuna"),
            POSTING_CHECKED_AT: P.date_property(datetime.now(UTC).isoformat()),
        },
    }
    captured = {}

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            page["properties"].update(properties)

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda *args, **kwargs: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.verify_active_opportunities",
        lambda settings, *, notion, store, dry_run, **kwargs: (
            captured.update(kwargs)
            or SimpleNamespace(closed_page_ids={"opportunity-1"}, blocked_page_ids=set())
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._score_opportunity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("closed row must not be scored")
        ),
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=tmp_store,
        router=SimpleNamespace(),
        client=SimpleNamespace(),
        dry_run=False,
    )

    assert stats.scored == 0
    assert captured["force"] is True
    assert captured["max_age_hours"] == 0
    assert captured["require_decisive"] is True
    assert captured["pages"] == [page]


def test_manual_application_sync_skips_closed_rows() -> None:
    page = {
        "id": "opportunity-1",
        "properties": {
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("Engineer"),
            "Status": P.select("Closed"),
            "Model Score": P.number(0.9),
            "URL": P.url("https://example.com/jobs/1"),
            "Application": P.relation([]),
        },
    }
    created = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page] if P.read_select(page["properties"]["Status"]) == "Review" else []

        def create_page(self, **kwargs):
            created.append(kwargs)
            return {"id": "app-1"}

        def update_page(self, page_id, properties):
            raise AssertionError("closed row must not be advanced")

    stats = create_manual_applications(
        _settings(),
        notion=Notion(),
        dry_run=False,
    )

    assert stats.manual_applications_created == 0
    assert created == []


def test_lifecycle_sync_does_not_revive_rejected_or_closed_rows() -> None:
    rows = [
        {
            "id": "rejected",
            "properties": {
                "Status": P.select("Rejected"),
                "My Decision": P.select("Apply"),
            },
        },
        {
            "id": "closed",
            "properties": {
                "Status": P.select("Closed"),
                "My Decision": P.select("Apply"),
            },
        },
    ]

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            filter_ = kwargs.get("filter_") or {}
            if filter_.get("property") == "My Decision":
                decision = filter_["select"]["equals"]
                return [
                    row
                    for row in rows
                    if P.read_select(row["properties"].get("My Decision")) == decision
                ]
            return []

        def update_page(self, page_id, properties):
            raise AssertionError("terminal opportunity must not be revived")

    stats = reconcile_lifecycles(
        SimpleNamespace(
            notion=SimpleNamespace(
                data_sources={
                    "opportunities": "opportunities",
                    "applications": "applications",
                }
            )
        ),
        notion=Notion(),
        dry_run=False,
    )

    assert stats.queued == 0
    assert stats.rejected == 0
