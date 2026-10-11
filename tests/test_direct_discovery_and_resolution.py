from __future__ import annotations

import httpx

import job_search_hub.ingest.direct_discovery as direct
from job_search_hub.config import DirectDiscoverySettings
from job_search_hub.ingest.base import RawPosting
from job_search_hub.ingest.direct_discovery import (
    ats_provider,
    extract_embedded_jobs,
    extract_jsonld_postings,
    parse_feed,
    parse_sitemap,
    parse_sitemap_index,
)
from job_search_hub.pipeline.canonical_resolution import clean_tracking_url, resolve_posting


def _direct_settings(*, browser_enabled: bool = True) -> DirectDiscoverySettings:
    return DirectDiscoverySettings(
        enabled=True,
        timeout_seconds=5,
        max_redirects=2,
        max_response_bytes=100_000,
        max_targets_per_run=5,
        retry_hours=12,
        rediscovery_days=14,
        browser_enabled=browser_enabled,
    )


def test_jsonld_and_next_state_extract_specific_jobs() -> None:
    page = """
    <script type="application/ld+json">{"@type":"JobPosting","title":"Platform Engineer",
      "url":"/jobs/42","description":"Build things",
      "hiringOrganization":{"name":"Acme"}}</script>
    <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"jobs":[
      {"id":"43","title":"Data Engineer","jobUrl":"/jobs/43","description":"Data work"}
    ]}}}</script>
    """
    jsonld = extract_jsonld_postings(page, base_url="https://careers.acme.example/careers")
    embedded = extract_embedded_jobs(
        page, base_url="https://careers.acme.example/careers", company="Acme"
    )
    assert [(p.role_title, p.url) for p in jsonld] == [
        ("Platform Engineer", "https://careers.acme.example/jobs/42")
    ]
    assert [(p.role_title, p.url) for p in embedded] == [
        ("Data Engineer", "https://careers.acme.example/jobs/43")
    ]


def test_ats_detection_recognizes_public_hosts_and_page_evidence() -> None:
    assert ats_provider("https://jobs.lever.co/acme") == "lever"
    assert (
        ats_provider(
            "https://careers.acme.example",
            "<script src='https://api.ashbyhq.com/posting-api/job-board/acme'></script>",
        )
        == "ashby"
    )
    assert ats_provider("https://acme.jobs.personio.de/xml") == "personio"


def test_feed_and_sitemap_parsing_ignore_non_job_urls() -> None:
    feed = (
        "<rss><channel><item><title>Engineer</title><link>/jobs/99</link>"
        "<description>Build</description></item></channel></rss>"
    )
    sitemap = """<urlset><url><loc>https://acme.example/jobs/99</loc></url><url><loc>https://acme.example/about</loc></url></urlset>"""
    jobs = parse_feed(feed, base_url="https://acme.example/feed", company="Acme")
    assert jobs[0].url == "https://acme.example/jobs/99"
    assert parse_sitemap(sitemap, base_url="https://acme.example/sitemap.xml") == [
        "https://acme.example/jobs/99"
    ]
    index = "<sitemapindex><sitemap><loc>/jobs-sitemap.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap_index(index, base_url="https://acme.example/sitemap.xml") == [
        "https://acme.example/jobs-sitemap.xml"
    ]


def test_canonical_resolution_follows_safe_redirect_and_rejects_generic_page() -> None:
    source = "https://jooble.example/click?utm_source=x"
    direct = "https://jobs.acme.example/jobs/123?ref=board"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://jooble.example"):
            return httpx.Response(302, headers={"location": direct}, request=request)
        return httpx.Response(
            200,
            text=(
                '<script type="application/ld+json">'
                '{"@type":"JobPosting","url":"https://jobs.acme.example/jobs/123",'
                '"title":"Senior Engineer"}</script> Senior Engineer Acme'
            ),
            request=request,
        )

    posting = RawPosting("Jooble", "jooble", "x", "Acme", "Senior Engineer", source)
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = resolve_posting(posting, client=client)
    assert result.accepted is True
    assert result.canonical_url == "https://jobs.acme.example/jobs/123"
    assert result.source_quality == "direct_employer"
    assert len(result.redirect_chain) == 2

    generic = RawPosting(
        "Jooble", "jooble", "x", "Acme", "Senior Engineer", "https://jooble.example/jobs"
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="Senior Engineer jobs", request=req)
        )
    ) as client:
        rejected = resolve_posting(generic, client=client)
    assert rejected.accepted is False
    assert rejected.reason == "generic_search_or_homepage"


def test_tracking_cleanup_and_private_redirect_rejection() -> None:
    assert (
        clean_tracking_url("https://jobs.acme.example/jobs/1?utm_source=x&job=1#top")
        == "https://jobs.acme.example/jobs/1?job=1"
    )
    posting = RawPosting(
        "Jooble", "jooble", "x", "Acme", "Engineer", "https://jooble.example/click"
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                302, headers={"location": "http://127.0.0.1/private"}, request=req
            )
        )
    ) as client:
        result = resolve_posting(posting, client=client)
    assert result.accepted is False
    assert result.reason == "unsafe_redirect"


def test_playwright_fallback_is_limited_to_known_ats_targets(monkeypatch) -> None:
    calls: list[str] = []

    def browser(url: str, **_kwargs):
        calls.append(url)
        return [], "no rendered jobs"

    monkeypatch.setattr(direct, "_browser_postings", browser)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>empty</html>", request=request)
    )
    with httpx.Client(transport=transport) as client:
        generic = direct.discover_target(
            "https://careers.acme.example", settings=_direct_settings(), client=client
        )
        known_ats = direct.discover_target(
            "https://jobs.jobvite.com/acme", settings=_direct_settings(), client=client
        )

    assert generic.status == "no_jobs_found"
    assert known_ats.status == "browser_required"
    assert calls == ["https://jobs.jobvite.com/acme"]
