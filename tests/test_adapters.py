"""Tests for the IMAP and RSS adapters' parsing helpers (no network)."""

import json
from dataclasses import replace
from datetime import UTC
from email.message import EmailMessage

import httpx

from job_search_hub.config import load_settings
from job_search_hub.ingest.adzuna import AdzunaAdapter
from job_search_hub.ingest.arbeitnow import ArbeitnowAdapter
from job_search_hub.ingest.ashby import AshbyAdapter
from job_search_hub.ingest.base import SearchRequest
from job_search_hub.ingest.greenhouse import (
    GreenhouseAdapter,
    _board_token,
    _new_board_jobs,
)
from job_search_hub.ingest.imap_alerts import IMAPAdapter, _company_from_url, _strip
from job_search_hub.ingest.jooble import JoobleAdapter
from job_search_hub.ingest.lever import LeverAdapter
from job_search_hub.ingest.recruitee import RecruiteeAdapter
from job_search_hub.ingest.remotive import RemotiveAdapter
from job_search_hub.ingest.smartrecruiters import SmartRecruitersAdapter
from job_search_hub.ingest.usajobs import USAJobsAdapter
from job_search_hub.ingest.watchlist import (
    HEALTH_BLOCKED,
    HEALTH_MISCONFIGURED,
    HEALTH_TRANSIENT,
    WatchlistAdapter,
    WatchlistSourceHealth,
    classify_watchlist_failure,
    content_hash,
    read_watchlist,
)
from job_search_hub.pipeline.ingest_stage import _record_watchlist_health


def test_company_from_url():
    assert _company_from_url("https://acme.com/jobs/1") == "acme"
    assert _company_from_url("https://jobs.greenhouse.io/foo") == "greenhouse"
    assert _company_from_url("not a url") == ""


def test_strip_html():
    assert _strip("<p>Hi <b>there</b></p>") == "Hi there"


def test_extract_postings_filters_unsubscribe():
    msg = EmailMessage()
    msg["Subject"] = "Daily Job Alert"
    msg["From"] = "alerts@example.com"
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 -0000"
    body = """
    <html><body>
      <a href="https://acme.com/jobs/123">Senior Engineer at Acme</a>
      <a href="https://example.com/unsubscribe?x=1">Unsubscribe</a>
      <a href="https://example.com/preferences">Manage preferences</a>
      <a href="https://other.com/jobs/456">Backend Engineer</a>
    </body></html>
    """
    msg.set_content(body, subtype="html")
    postings = list(IMAPAdapter._extract_postings("alerts@example.com", msg))
    urls = [p.url for p in postings]
    assert "https://acme.com/jobs/123" in urls
    assert "https://other.com/jobs/456" in urls
    assert not any("unsubscribe" in u for u in urls)
    assert not any("preferences" in u for u in urls)


def test_extract_postings_skips_short_anchor_text():
    msg = EmailMessage()
    msg["Subject"] = "Alert"
    msg["From"] = "x@y"
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 -0000"
    msg.set_content('<a href="https://acme.com/jobs/1">go</a>', subtype="html")
    postings = list(IMAPAdapter._extract_postings("x@y", msg))
    assert postings == []


def test_adzuna_paces_searches_at_the_configured_api_limit(monkeypatch, tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    adapter = AdzunaAdapter(settings.adapters, settings.search_terms, requests_per_minute=25)
    requests = [
        SearchRequest(source="Adzuna", source_adapter="adzuna", endpoint="https://example.com/1"),
        SearchRequest(source="Adzuna", source_adapter="adzuna", endpoint="https://example.com/2"),
    ]
    delays = []
    monkeypatch.setattr("job_search_hub.ingest.adzuna.time.sleep", delays.append)

    assert list(adapter._paced_searches(requests)) == requests
    assert len(delays) == 1
    assert 0 < delays[0] <= 60 / 25


def test_ashby_to_posting_maps_core_fields():
    posting = AshbyAdapter._to_posting(
        "acme",
        {
            "id": "job1",
            "title": "Research Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/acme/job1",
            "location": "Seattle",
            "descriptionHtml": "<p>Build tools.</p>",
            "publishedAt": "2026-06-02T12:00:00Z",
            "department": "Engineering",
            "compensation": {
                "compensationTierSummary": "$100k - $120k",
                "scrapeableCompensationSalarySummary": [
                    {
                        "minValue": 100000,
                        "maxValue": 120000,
                        "interval": "YEAR",
                    }
                ],
            },
        },
    )
    assert posting.source_adapter == "ashby"
    assert posting.company == "acme"
    assert posting.role_title == "Research Engineer"
    assert posting.description == "Build tools."
    assert posting.compensation_min == 100000
    assert posting.compensation_unit == "Annual"
    assert posting.posted_at and posting.posted_at.date().isoformat() == "2026-06-02"


def test_greenhouse_new_board_helpers_parse_remix_jobs():
    payload = {
        "state": {
            "loaderData": {
                "routes/$url_token": {
                    "jobPosts": {
                        "total_pages": 2,
                        "data": [
                            {
                                "id": 123,
                                "title": "Software Engineer",
                                "location": "Seattle, WA",
                                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/123",
                                "published_at": "2026-06-01T10:00:00-04:00",
                                "department": {"name": "Engineering", "path": ["Product"]},
                            }
                        ],
                    }
                }
            }
        }
    }
    html = (
        f"<html><body><script>window.__remixContext = {json.dumps(payload)};</script></body></html>"
    )

    jobs, total_pages = _new_board_jobs(html)
    posting = GreenhouseAdapter._to_new_board_posting("acme", jobs[0])

    assert _board_token("https://job-boards.greenhouse.io/acme/jobs/123") == "acme"
    assert _board_token("boards.greenhouse.io/acme") == "acme"
    assert _board_token("acme") == "acme"
    assert total_pages == 2
    assert posting.source_adapter == "greenhouse"
    assert posting.role_title == "Software Engineer"
    assert posting.location == "Seattle, WA"
    assert posting.posted_at and posting.posted_at.date().isoformat() == "2026-06-01"
    assert posting.extras["departments"] == ["Product", "Engineering"]
    assert "Product / Engineering" in posting.description


def test_watchlist_helpers(tmp_path):
    path = tmp_path / "watchlist.txt"
    path.write_text("# comment\nhttps://example.com/jobs\n\n", encoding="utf-8")
    assert read_watchlist(path) == ["https://example.com/jobs"]
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abcd")


def test_watchlist_classifies_source_health_failures() -> None:
    request = httpx.Request("GET", "https://example.com/jobs")
    response = httpx.Response(403, request=request)
    status, http_status = classify_watchlist_failure(
        httpx.HTTPStatusError("blocked", request=request, response=response)
    )

    assert status == HEALTH_BLOCKED
    assert http_status == 403

    response = httpx.Response(404, request=request)
    status, http_status = classify_watchlist_failure(
        httpx.HTTPStatusError("missing", request=request, response=response)
    )

    assert status == HEALTH_MISCONFIGURED
    assert http_status == 404

    status, http_status = classify_watchlist_failure(httpx.ConnectError("dns", request=request))

    assert status == HEALTH_TRANSIENT
    assert http_status is None


def test_watchlist_skips_known_blocked_sources_until_refreshed(monkeypatch, tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    creds = replace(
        settings.adapters,
        watchlist_file=tmp_path / "watchlist.txt",
        watchlist_urls=["https://blocked.example/jobs"],
    )
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def get(self, url):
            calls.append(url)
            request = httpx.Request("GET", url)
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("blocked", request=request, response=response)

    monkeypatch.setattr("job_search_hub.ingest.watchlist.httpx.Client", FakeClient)
    adapter = WatchlistAdapter(creds)

    assert list(adapter.fetch()) == []
    assert adapter.source_health["https://blocked.example/jobs"].status == HEALTH_BLOCKED
    assert list(adapter.fetch()) == []
    assert calls == ["https://blocked.example/jobs"]

    adapter.refresh_source_health()
    assert list(adapter.fetch()) == []
    assert calls == ["https://blocked.example/jobs", "https://blocked.example/jobs"]


def test_watchlist_health_ledger_reports_new_unchanged_changed_and_failure(tmp_store) -> None:
    url = "https://watch.example/careers"

    class Adapter:
        source_health = {
            url: WatchlistSourceHealth(
                url=url,
                status="healthy",
                http_status=200,
                content_hash="first",
            )
        }

    adapter = Adapter()
    _record_watchlist_health(tmp_store, adapter, dry_run=False)
    assert tmp_store.latest_source_run(source_key="watchlist", target=url)["status"] == "new"

    _record_watchlist_health(tmp_store, adapter, dry_run=False)
    assert tmp_store.latest_source_run(source_key="watchlist", target=url)["status"] == "unchanged"

    adapter.source_health[url] = WatchlistSourceHealth(
        url=url, status="healthy", http_status=200, content_hash="second"
    )
    _record_watchlist_health(tmp_store, adapter, dry_run=False)
    assert tmp_store.latest_source_run(source_key="watchlist", target=url)["status"] == "changed"

    adapter.source_health[url] = WatchlistSourceHealth(
        url=url, status=HEALTH_BLOCKED, http_status=403, error="blocked"
    )
    _record_watchlist_health(tmp_store, adapter, dry_run=False)
    latest = tmp_store.latest_source_run(source_key="watchlist", target=url)
    assert latest["status"] == HEALTH_BLOCKED
    assert latest["error"] == "blocked"


def test_new_adapter_mapping_helpers():
    recruitee = RecruiteeAdapter._to_posting(
        "acme",
        {
            "id": 1,
            "title": "Backend Engineer",
            "careers_url": "https://acme.recruitee.com/o/backend-engineer",
            "location": {"city": "Seattle"},
            "description": "<p>Python APIs</p>",
            "published_at": "2026-06-03T09:00:00Z",
        },
    )
    assert recruitee.source_adapter == "recruitee"
    assert recruitee.posted_at and recruitee.posted_at.date().isoformat() == "2026-06-03"

    lever = LeverAdapter._to_posting(
        "acme",
        {
            "id": "lever-1",
            "text": "Platform Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/lever-1",
            "createdAt": 1780483200000,
            "categories": {"location": "Remote", "commitment": "Full-time"},
        },
    )
    assert lever.source_adapter == "lever"
    assert lever.posted_at and lever.posted_at.astimezone(UTC).date().isoformat() == "2026-06-03"

    sr = SmartRecruitersAdapter._to_posting(
        "acme",
        {
            "id": "job1",
            "name": "Frontend Engineer",
            "company": {"name": "Acme"},
            "location": {"city": "Seattle", "region": "WA", "remote": True},
            "webPostingUrl": "https://jobs.smartrecruiters.com/acme/job1",
            "typeOfEmployment": {"label": "Full-time"},
            "createdAt": "2026-06-04T12:00:00Z",
        },
    )
    assert sr.source_adapter == "smartrecruiters"
    assert sr.remote_status == "Remote"
    assert sr.employment_type == ["Full-time"]
    assert sr.posted_at and sr.posted_at.date().isoformat() == "2026-06-04"

    remotive = RemotiveAdapter._to_posting(
        {
            "id": 2,
            "title": "ML Engineer",
            "company_name": "Remote Co",
            "url": "https://remotive.com/jobs/2",
            "description": "<p>PyTorch</p>",
            "publication_date": "2026-06-05T08:00:00Z",
        }
    )
    assert remotive.remote_status == "Remote"
    assert remotive.posted_at and remotive.posted_at.date().isoformat() == "2026-06-05"

    arbeitnow = ArbeitnowAdapter._to_posting(
        {
            "slug": "job-3",
            "title": "Data Engineer",
            "company_name": "Work Co",
            "url": "https://arbeitnow.com/jobs/job-3",
            "remote": True,
            "job_types": ["Full-time"],
            "created_at": 1780704000,
        }
    )
    assert arbeitnow.source_adapter == "arbeitnow"
    assert (
        arbeitnow.posted_at
        and arbeitnow.posted_at.astimezone(UTC).date().isoformat() == "2026-06-06"
    )

    adzuna = AdzunaAdapter._to_posting(
        {
            "id": "4",
            "title": "Analytics Engineer",
            "company": {"display_name": "Data Co"},
            "location": {"display_name": "Seattle, WA"},
            "redirect_url": "https://adzuna.example/4",
            "salary_min": "90000",
            "created": "2026-06-06T07:00:00Z",
        }
    )
    assert adzuna.compensation_min == 90000
    assert adzuna.posted_at and adzuna.posted_at.date().isoformat() == "2026-06-06"

    jooble = JoobleAdapter._to_posting(
        {
            "id": "5",
            "title": "QA Automation Engineer",
            "company": "Quality Co",
            "location": "Seattle",
            "link": "https://jooble.example/5",
            "type": "Full-time",
            "updated": "2026-06-07T10:00:00Z",
        }
    )
    assert jooble.employment_type == ["Full-time"]
    assert jooble.posted_at and jooble.posted_at.date().isoformat() == "2026-06-07"

    usajobs = USAJobsAdapter._to_posting(
        {
            "MatchedObjectId": "6",
            "MatchedObjectDescriptor": {
                "PositionID": "6",
                "PositionTitle": "Software Engineer",
                "OrganizationName": "Agency",
                "PositionURI": "https://www.usajobs.gov/job/6",
                "PublicationStartDate": "2026-06-08T00:00:00Z",
            },
        }
    )
    assert usajobs.source_adapter == "usajobs"
    assert usajobs.posted_at and usajobs.posted_at.date().isoformat() == "2026-06-08"


def test_imap_fetch_uses_readonly_and_body_peek(monkeypatch, tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "Daily Job Alert"
    msg["From"] = "alerts@example.com"
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 -0000"
    msg.set_content('<a href="https://acme.com/jobs/1">Software Engineer</a>', subtype="html")
    raw = msg.as_bytes()
    calls = []

    class FakeIMAP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def login(self, username, password):
            calls.append(("login", username, password))

        def select(self, mailbox, readonly=False):
            calls.append(("select", mailbox, readonly))
            return "OK", []

        def search(self, *args):
            calls.append(("search", args))
            return "OK", [b"1"]

        def fetch(self, mid, query):
            calls.append(("fetch", mid, query))
            return "OK", [(b"1", raw)]

        def logout(self):
            calls.append(("logout",))

    monkeypatch.setenv("JOBHUB_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("JOBHUB_IMAP_USERNAME", "me")
    monkeypatch.setenv("JOBHUB_IMAP_PASSWORD", "secret")
    monkeypatch.setenv("JOBHUB_IMAP_ALERT_SENDERS", "alerts@example.com")
    monkeypatch.setattr("job_search_hub.ingest.imap_alerts.imaplib.IMAP4_SSL", FakeIMAP)

    settings = load_settings(env_file=tmp_path / "missing.env")
    postings = list(IMAPAdapter(settings.adapters, settings.imap).fetch())

    assert len(postings) == 1
    assert ("select", "INBOX", True) in calls
    assert any(call == ("fetch", b"1", "(BODY.PEEK[])") for call in calls)
