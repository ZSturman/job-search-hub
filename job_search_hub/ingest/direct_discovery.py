"""Evidence-led discovery for employer career pages.

This module deliberately starts with the page the user configured rather than
guessing hundreds of API paths.  It records the concrete evidence used for a
strategy so a blocked or unsupported site is observable and retryable instead
of becoming a pipeline-wide failure.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from job_search_hub.browser_runtime import BrowserRuntime
from job_search_hub.config import DirectDiscoverySettings
from job_search_hub.ingest.base import RawPosting
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_JOB_WORD_RE = re.compile(r"\b(job|career|opening|position|role|vacanc)\w*\b", re.I)
_BLOCKED_STATUSES = {401, 403, 429}
_UNSUPPORTED_HOSTS = {"linkedin.com", "indeed.com", "wellfound.com"}
_BROWSER_SUPPORTED_PROVIDERS = {"workday", "teamtailor", "jobvite"}
_ATS_HOSTS = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "recruitee.com": "recruitee",
    "smartrecruiters.com": "smartrecruiters",
    "workable.com": "workable",
    "personio.de": "personio",
    "teamtailor.com": "teamtailor",
    "jobvite.com": "jobvite",
    "myworkdayjobs.com": "workday",
}


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", value or ""))).strip()


def _safe_public_http_url(url: str) -> bool:
    """Reject dangerous URLs before a background worker requests them."""
    try:
        parsed = urlsplit((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
    except ValueError:
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(host, None)}
        except socket.gaierror:
            return True
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return bool(addresses)


def ats_provider(url: str, page_text: str = "") -> str:
    """Identify public ATS evidence from a host, link, script, or page marker."""
    evidence = f"{url}\n{page_text[:250_000]}".lower()
    host = (urlsplit(url).hostname or "").lower()
    for domain, provider in _ATS_HOSTS.items():
        if host == domain or host.endswith(f".{domain}") or domain in evidence:
            return provider
    if "boards-api.greenhouse.io" in evidence:
        return "greenhouse"
    if "posting-api/job-board" in evidence or "api.ashbyhq.com" in evidence:
        return "ashby"
    if "api.lever.co/v0/postings" in evidence:
        return "lever"
    if "jobs.personio" in evidence:
        return "personio"
    return ""


def _target_token(url: str, provider: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if provider == "greenhouse" and parts:
        return parts[0]
    if provider in {"lever", "ashby", "recruitee", "smartrecruiters"} and parts:
        return parts[0]
    if provider == "personio":
        return host.split(".", 1)[0]
    if provider == "workable":
        if host.endswith(".workable.com"):
            return host.split(".", 1)[0]
        if parts:
            return parts[0]
    return ""


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, dict[str, str]]] = []
        self._href = ""
        self._attrs: dict[str, str] = {}
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "a" and attrs_dict.get("href"):
            self._href = attrs_dict["href"]
            self._attrs = attrs_dict
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip(), self._attrs))
            self._href, self._attrs, self._text = "", {}, []


def _json_values(value: object) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_values(child)


def _job_url(value: dict, base_url: str) -> str:
    for key in ("url", "absolute_url", "jobUrl", "job_url", "hostedUrl", "applyUrl", "apply_url"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return urljoin(base_url, candidate.strip())
    return ""


def _company(value: dict, fallback: str) -> str:
    org = value.get("hiringOrganization") or value.get("organization") or value.get("company")
    if isinstance(org, dict):
        return str(org.get("name") or fallback).strip()
    return str(org or fallback).strip()


def extract_jsonld_postings(page: str, *, base_url: str, company: str = "") -> list[RawPosting]:
    """Extract actual Schema.org JobPosting records, not generic page metadata."""
    records: list[RawPosting] = []
    for raw in re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        page,
        flags=re.I | re.S,
    ):
        try:
            data = json.loads(html.unescape(raw.strip()))
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _json_values(data):
            type_value = obj.get("@type", "")
            types = type_value if isinstance(type_value, list) else [type_value]
            if not any(str(item).lower() == "jobposting" for item in types):
                continue
            title = str(obj.get("title") or "").strip()
            url = _job_url(obj, base_url) or base_url
            if not title or not _safe_public_http_url(url):
                continue
            location = obj.get("jobLocation") or obj.get("applicantLocationRequirements") or ""
            if isinstance(location, list):
                location = ", ".join(
                    _strip_html(
                        str(
                            (x.get("address") or x).get("addressLocality", "")
                            if isinstance(x, dict)
                            else x
                        )
                    )
                    for x in location
                )
            elif isinstance(location, dict):
                address = location.get("address") or location
                location = (
                    ", ".join(
                        str(address.get(k) or "")
                        for k in ("addressLocality", "addressRegion", "addressCountry")
                    ).strip(", ")
                    if isinstance(address, dict)
                    else str(address)
                )
            records.append(
                RawPosting(
                    source="Employer structured data",
                    source_adapter="direct_discovery",
                    external_job_id=str(obj.get("identifier") or obj.get("@id") or url),
                    company=_company(obj, company or (urlsplit(base_url).hostname or "")),
                    role_title=title,
                    url=url,
                    description=_strip_html(str(obj.get("description") or "")),
                    location=_strip_html(str(location)),
                    extras={"strategy": "jsonld", "source_quality": "structured_employer"},
                )
            )
    return _dedupe_postings(records)


def extract_embedded_jobs(page: str, *, base_url: str, company: str = "") -> list[RawPosting]:
    """Extract conservative job objects from Next.js/serialized page state."""
    payloads = re.findall(
        r"<script[^>]+(?:id=[\"']__NEXT_DATA__[\"']|type=[\"']application/json[\"'])[^>]*>(.*?)</script>",
        page,
        flags=re.I | re.S,
    )
    records: list[RawPosting] = []
    for raw in payloads:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for obj in _json_values(data):
            title = str(obj.get("title") or obj.get("name") or obj.get("text") or "").strip()
            url = _job_url(obj, base_url)
            if not title or not url or not _safe_public_http_url(url):
                continue
            # Requiring a job-ish key prevents treating product cards as jobs.
            keys = " ".join(str(key).lower() for key in obj)
            if not _JOB_WORD_RE.search(keys) and "/job" not in urlsplit(url).path.lower():
                continue
            records.append(
                RawPosting(
                    source="Employer embedded data",
                    source_adapter="direct_discovery",
                    external_job_id=str(obj.get("id") or obj.get("jobId") or url),
                    company=_company(obj, company or (urlsplit(base_url).hostname or "")),
                    role_title=title,
                    url=url,
                    description=_strip_html(
                        str(obj.get("description") or obj.get("descriptionPlain") or "")
                    ),
                    location=_strip_html(str(obj.get("location") or "")),
                    extras={"strategy": "embedded_json", "source_quality": "structured_employer"},
                )
            )
    return _dedupe_postings(records)


def extract_html_job_links(page: str, *, base_url: str, company: str = "") -> list[RawPosting]:
    collector = _LinkCollector()
    try:
        collector.feed(page)
    except Exception:
        return []
    records: list[RawPosting] = []
    for href, text, attrs in collector.links:
        url = urljoin(base_url, href)
        haystack = f"{href} {text} {attrs.get('aria-label', '')} {attrs.get('data-testid', '')}"
        path_parts = [part for part in urlsplit(url).path.split("/") if part]
        ats_detail = bool(ats_provider(url) or ats_provider(base_url)) and len(path_parts) >= 2
        if (
            not _safe_public_http_url(url)
            or not text
            or not (_JOB_WORD_RE.search(haystack) or ats_detail)
        ):
            continue
        # Listing filters and broad careers links are discovery pages, not jobs.
        path = urlsplit(url).path.lower()
        if path.rstrip("/") in {"/jobs", "/careers", "/career", "/openings"}:
            continue
        records.append(
            RawPosting(
                source="Employer career page",
                source_adapter="direct_discovery",
                external_job_id=url,
                company=company or (urlsplit(base_url).hostname or ""),
                role_title=_strip_html(text),
                url=url,
                extras={"strategy": "html_links", "source_quality": "employer_html"},
            )
        )
    return _dedupe_postings(records)


def parse_feed(content: str, *, base_url: str, company: str = "") -> list[RawPosting]:
    """Parse RSS/Atom and Personio-like public XML feeds defensively."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    records: list[RawPosting] = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1].lower()
        if tag not in {"item", "entry", "position", "job"}:
            continue
        fields: dict[str, str] = {}
        for child in item.iter():
            child_tag = child.tag.rsplit("}", 1)[-1].lower()
            text = (child.text or "").strip()
            if text and child_tag not in fields:
                fields[child_tag] = text
            if child_tag == "link" and child.attrib.get("href"):
                fields["link"] = child.attrib["href"]
        title = fields.get("title") or fields.get("name") or fields.get("jobname") or ""
        url = fields.get("link") or fields.get("url") or fields.get("joburl") or ""
        url = urljoin(base_url, url)
        if not title or not _safe_public_http_url(url):
            continue
        records.append(
            RawPosting(
                source="Employer feed",
                source_adapter="direct_discovery",
                external_job_id=fields.get("id") or fields.get("reference") or url,
                company=company or (urlsplit(base_url).hostname or ""),
                role_title=_strip_html(title),
                url=url,
                description=_strip_html(fields.get("description") or fields.get("content") or ""),
                location=_strip_html(fields.get("location") or ""),
                extras={"strategy": "feed", "source_quality": "employer_feed"},
            )
        )
    return _dedupe_postings(records)


def parse_sitemap(content: str, *, base_url: str) -> list[str]:
    """Return job-looking URLs from a sitemap; callers still verify each page."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    urls: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() != "loc" or not element.text:
            continue
        candidate = urljoin(base_url, element.text.strip())
        if _safe_public_http_url(candidate) and _JOB_WORD_RE.search(candidate):
            urls.append(candidate)
    return list(dict.fromkeys(urls))


def parse_sitemap_index(content: str, *, base_url: str) -> list[str]:
    """Return child sitemap URLs from an explicitly supplied sitemap index."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    if root.tag.rsplit("}", 1)[-1].lower() != "sitemapindex":
        return []
    return list(
        dict.fromkeys(
            urljoin(base_url, element.text.strip())
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1].lower() == "loc"
            and element.text
            and _safe_public_http_url(urljoin(base_url, element.text.strip()))
        )
    )


def _evidenced_feed_urls(page: str, base_url: str) -> list[str]:
    """Use feeds only when the configured page advertises them."""
    urls: list[str] = []
    for match in re.finditer(r"<(?:link|a)\b[^>]+>", page, re.I):
        tag = match.group(0)
        href = re.search(r"\bhref=[\"']([^\"']+)", tag, re.I)
        if not href:
            continue
        candidate = urljoin(base_url, html.unescape(href.group(1)))
        if any(
            marker in tag.lower() for marker in ("rss", "atom", "sitemap", "xml", "feed")
        ) and _safe_public_http_url(candidate):
            urls.append(candidate)
    return list(dict.fromkeys(urls))[:3]


def _dedupe_postings(postings: Iterable[RawPosting]) -> list[RawPosting]:
    output: list[RawPosting] = []
    seen: set[str] = set()
    for posting in postings:
        key = f"{posting.url.casefold()}\x00{posting.role_title.casefold()}"
        if key and key not in seen:
            seen.add(key)
            output.append(posting)
    return output


def _evidenced_ats_url(page: str, base_url: str) -> str:
    """Return an ATS URL already linked or referenced by the career page."""
    collector = _LinkCollector()
    try:
        collector.feed(page)
    except Exception:
        return ""
    for href, _text, _attrs in collector.links:
        candidate = urljoin(base_url, href)
        if ats_provider(candidate) and _safe_public_http_url(candidate):
            return candidate
    return ""


def _known_ats_postings(
    provider: str,
    target_url: str,
    *,
    client: httpx.Client,
    company: str,
    max_response_bytes: int,
) -> list[RawPosting]:
    """Use only public endpoints implied by the observed ATS board URL."""
    token = _target_token(target_url, provider)
    if not token:
        return []
    endpoint = ""
    try:
        if provider == "greenhouse":
            endpoint = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
            response = client.get(endpoint)
            if response.status_code >= 400 or len(response.content) > max_response_bytes:
                return []
            data = response.json() or {}
            out: list[RawPosting] = []
            for job in data.get("jobs") or []:
                location = (
                    (job.get("location") or {}).get("name", "")
                    if isinstance(job.get("location"), dict)
                    else ""
                )
                out.append(
                    RawPosting(
                        "Greenhouse",
                        "greenhouse",
                        str(job.get("id") or ""),
                        _company(job, company or token),
                        str(job.get("title") or ""),
                        str(job.get("absolute_url") or ""),
                        _strip_html(str(job.get("content") or "")),
                        location,
                        extras={
                            "strategy": "greenhouse_public_api",
                            "source_quality": "official_ats",
                        },
                    )
                )
            return _dedupe_postings(out)
        if provider == "lever":
            endpoint = f"https://api.lever.co/v0/postings/{token}?mode=json"
            response = client.get(endpoint)
            if response.status_code >= 400 or len(response.content) > max_response_bytes:
                return []
            out = []
            for job in response.json() or []:
                categories = job.get("categories") or {}
                out.append(
                    RawPosting(
                        "Lever",
                        "lever",
                        str(job.get("id") or ""),
                        company or token,
                        str(job.get("text") or ""),
                        str(job.get("hostedUrl") or job.get("applyUrl") or ""),
                        _strip_html(
                            str(job.get("descriptionPlain") or job.get("description") or "")
                        ),
                        str(categories.get("location") or ""),
                        extras={"strategy": "lever_public_api", "source_quality": "official_ats"},
                    )
                )
            return _dedupe_postings(out)
        if provider == "ashby":
            endpoint = (
                f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
            )
            response = client.get(endpoint)
            if response.status_code >= 400 or len(response.content) > max_response_bytes:
                return []
            out = []
            for job in (response.json() or {}).get("jobs") or []:
                out.append(
                    RawPosting(
                        "Ashby",
                        "ashby",
                        str(job.get("id") or job.get("jobId") or ""),
                        company or token,
                        str(job.get("title") or ""),
                        str(job.get("jobUrl") or job.get("applyUrl") or ""),
                        _strip_html(
                            str(job.get("descriptionHtml") or job.get("description") or "")
                        ),
                        str(job.get("location") or ""),
                        extras={"strategy": "ashby_public_api", "source_quality": "official_ats"},
                    )
                )
            return _dedupe_postings(out)
        if provider == "workable":
            # Workable's public careers widget is specifically designed for
            # unauthenticated account feeds.  Do not use its private API.
            endpoint = f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"
            response = client.get(endpoint)
            if response.status_code >= 400 or len(response.content) > max_response_bytes:
                return []
            jobs = (response.json() or {}).get("jobs") or []
            out = [
                RawPosting(
                    "Workable",
                    "workable",
                    str(job.get("id") or job.get("shortcode") or ""),
                    company or token,
                    str(job.get("title") or ""),
                    str(job.get("url") or job.get("application_url") or ""),
                    _strip_html(str(job.get("description") or "")),
                    str(job.get("location") or ""),
                    extras={"strategy": "workable_public_widget", "source_quality": "official_ats"},
                )
                for job in jobs
            ]
            return _dedupe_postings(out)
        if provider == "personio":
            endpoint = f"https://{token}.jobs.personio.de/xml"
            response = client.get(endpoint)
            if response.status_code >= 400 or len(response.content) > max_response_bytes:
                return []
            return parse_feed(response.text, base_url=endpoint, company=company or token)
    except (httpx.HTTPError, ValueError, TypeError):
        return []
    return []


def _browser_postings(
    url: str,
    *,
    company: str,
    timeout_seconds: float,
    browser_runtime: BrowserRuntime | None = None,
) -> tuple[list[RawPosting], str]:
    """Controlled JS fallback; it never attempts login, CAPTCHA, or bypasses."""
    if browser_runtime is not None:
        rendered = browser_runtime.render(url, timeout_seconds=timeout_seconds, load_more=3)
        if rendered is None:
            return [], browser_runtime.error or "browser fallback unavailable"
        postings = extract_jsonld_postings(rendered.html, base_url=rendered.url, company=company)
        postings = postings or extract_embedded_jobs(
            rendered.html,
            base_url=rendered.url,
            company=company,
        )
        postings = postings or extract_html_job_links(
            rendered.html,
            base_url=rendered.url,
            company=company,
        )
        return postings, ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], "Playwright is not installed"
    playwright = browser = context = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(user_agent="JobSearchHubJobDiscovery/1.0")
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=max(1, int(timeout_seconds * 1000)))
        # Limited interactive loading covers common career boards without
        # turning the fallback into an uncontrolled scraper.
        for _ in range(3):
            button = page.get_by_role(
                "button", name=re.compile(r"load more|show more|more jobs", re.I)
            )
            if button.count() < 1:
                break
            button.first.click(timeout=2_000)
            page.wait_for_timeout(300)
        rendered = page.content()
        postings = extract_jsonld_postings(rendered, base_url=page.url, company=company)
        postings = postings or extract_embedded_jobs(rendered, base_url=page.url, company=company)
        postings = postings or extract_html_job_links(rendered, base_url=page.url, company=company)
        return postings, ""
    except Exception as exc:
        # Avoid exposing page content/cookies in logs or source health.
        return [], f"browser fallback failed: {type(exc).__name__}"
    finally:
        if context is not None:
            with suppress(Exception):
                context.close()
        if browser is not None:
            with suppress(Exception):
                browser.close()
        if playwright is not None:
            with suppress(Exception):
                playwright.stop()


@dataclass
class DiscoveryResult:
    target_url: str
    strategy: str
    status: str
    postings: list[RawPosting] = field(default_factory=list)
    http_status: int | None = None
    evidence: dict = field(default_factory=dict)
    error: str = ""


def discover_target(
    url: str,
    *,
    settings: DirectDiscoverySettings,
    client: httpx.Client | None = None,
    company: str = "",
    browser_runtime: BrowserRuntime | None = None,
) -> DiscoveryResult:
    """Discover a configured career source using observed, bounded evidence."""
    if not _safe_public_http_url(url):
        return DiscoveryResult(url, "none", "unsupported", error="unsafe or non-HTTP target")
    host = (urlsplit(url).hostname or "").lower()
    if any(host == domain or host.endswith(f".{domain}") for domain in _UNSUPPORTED_HOSTS):
        return DiscoveryResult(
            url,
            "manual",
            "manual_required",
            error="site requires alert/manual capture; automated access is not attempted",
        )
    owns_client = client is None
    client = client or httpx.Client(
        timeout=settings.timeout_seconds,
        follow_redirects=True,
        max_redirects=settings.max_redirects,
        headers={"User-Agent": "JobSearchHubJobDiscovery/1.0 (+configured-career-source)"},
    )
    try:
        response = client.get(url)
        if response.status_code in _BLOCKED_STATUSES:
            status = "rate_limited" if response.status_code == 429 else "blocked"
            return DiscoveryResult(
                url, "none", status, http_status=response.status_code, error="access restricted"
            )
        if response.status_code in {404, 410}:
            return DiscoveryResult(
                url,
                "none",
                "misconfigured",
                http_status=response.status_code,
                error="target not found",
            )
        response.raise_for_status()
        if len(response.content) > settings.max_response_bytes:
            return DiscoveryResult(
                url,
                "none",
                "unsupported",
                http_status=response.status_code,
                error="response exceeds configured size",
            )
        page = response.text
        observed_ats = _evidenced_ats_url(page, str(response.url))
        host_provider = ats_provider(str(response.url))
        provider = ats_provider(observed_ats) if observed_ats else host_provider
        if provider:
            postings = _known_ats_postings(
                provider,
                observed_ats or str(response.url),
                client=client,
                company=company,
                max_response_bytes=settings.max_response_bytes,
            )
            if postings:
                return DiscoveryResult(
                    url,
                    f"{provider}_public_endpoint",
                    "healthy",
                    postings,
                    response.status_code,
                    {"provider": provider, "ats_url": observed_ats or str(response.url)},
                )
        # Existing provider adapters are configured independently.  Here we
        # discover public structured data on the selected target itself rather
        # than speculative endpoints, which is safe for employer career pages.
        postings = extract_jsonld_postings(page, base_url=str(response.url), company=company)
        if postings:
            return DiscoveryResult(
                url, "jsonld", "healthy", postings, response.status_code, {"provider": provider}
            )
        postings = extract_embedded_jobs(page, base_url=str(response.url), company=company)
        if postings:
            return DiscoveryResult(
                url,
                "embedded_json",
                "healthy",
                postings,
                response.status_code,
                {"provider": provider},
            )
        content_type = response.headers.get("content-type", "").lower()
        if any(
            marker in content_type for marker in ("xml", "rss", "atom")
        ) or page.lstrip().startswith("<?xml"):
            postings = parse_feed(page, base_url=str(response.url), company=company)
            if postings:
                return DiscoveryResult(
                    url, "feed", "healthy", postings, response.status_code, {"provider": provider}
                )
            if "sitemap" in str(response.url).lower():
                return DiscoveryResult(
                    url,
                    "sitemap",
                    "healthy",
                    [],
                    response.status_code,
                    {
                        "urls": parse_sitemap(page, base_url=str(response.url)),
                        "sitemap_children": parse_sitemap_index(page, base_url=str(response.url)),
                    },
                )
        for feed_url in _evidenced_feed_urls(page, str(response.url)):
            try:
                feed_response = client.get(feed_url)
                if (
                    feed_response.status_code >= 400
                    or len(feed_response.content) > settings.max_response_bytes
                ):
                    continue
                postings = parse_feed(
                    feed_response.text,
                    base_url=str(feed_response.url),
                    company=company,
                )
                if postings:
                    return DiscoveryResult(
                        url,
                        "advertised_feed",
                        "healthy",
                        postings,
                        response.status_code,
                        {"feed_url": str(feed_response.url), "provider": provider},
                    )
            except httpx.HTTPError:
                continue
        postings = extract_html_job_links(page, base_url=str(response.url), company=company)
        if postings:
            return DiscoveryResult(
                url, "html_links", "healthy", postings, response.status_code, {"provider": provider}
            )
        # Rendering is intentionally confined to the ATS implementations
        # explicitly covered by this adapter. A generic board that happens to
        # use JavaScript stays a manual source instead of becoming an
        # unbounded Playwright scrape target.
        if settings.browser_enabled and provider in _BROWSER_SUPPORTED_PROVIDERS:
            postings, browser_error = _browser_postings(
                str(response.url),
                company=company,
                timeout_seconds=settings.timeout_seconds,
                browser_runtime=browser_runtime,
            )
            if postings:
                return DiscoveryResult(
                    url,
                    "playwright",
                    "healthy",
                    postings,
                    response.status_code,
                    {"provider": provider, "rendered": True},
                )
            return DiscoveryResult(
                url,
                "playwright",
                "browser_required",
                http_status=response.status_code,
                evidence={"provider": provider},
                error=browser_error or "rendered page did not expose job records",
            )
        if provider in _BROWSER_SUPPORTED_PROVIDERS:
            return DiscoveryResult(
                url,
                "browser_fallback" if settings.browser_enabled else "manual",
                "browser_required" if settings.browser_enabled else "unsupported",
                http_status=response.status_code,
                evidence={"provider": provider},
                error="no stable public structured evidence on initial page",
            )
        return DiscoveryResult(
            url,
            "none",
            "no_jobs_found",
            http_status=response.status_code,
            evidence={"provider": provider},
        )
    except httpx.TimeoutException:
        return DiscoveryResult(url, "none", "transient_failure", error="request timeout")
    except httpx.HTTPError as exc:
        return DiscoveryResult(url, "none", "transient_failure", error=str(exc)[:300])
    finally:
        if owns_client:
            client.close()
