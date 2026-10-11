"""Safe direct-posting resolution and source-quality assessment.

Aggregator URLs are useful provenance, but are never silently promoted to a
verified opportunity.  This resolver is deliberately conservative: it only
follows public HTTP(S) hops, retains every hop, and requires evidence that the
destination is a specific active job before returning a canonical URL.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from job_search_hub.ingest.base import RawPosting
from job_search_hub.pipeline.normalize import normalize_company, normalize_role

_AGGREGATORS = {"adzuna", "jooble", "remotive", "arbeitnow", "rss", "imap"}
_OFFICIAL_ATS = {"greenhouse", "lever", "ashby", "recruitee", "smartrecruiters"}
_TRACKING_KEYS = re.compile(r"^(utm_.+|ref|source|campaign|trk|tracking|mc_.+|gh_.+|s_.+)$", re.I)
_GENERIC_PATHS = {"", "/", "/jobs", "/careers", "/career", "/search", "/job-search", "/openings"}
_CLOSED_RE = re.compile(
    r"\b(no longer (available|accepting)|position (has been )?filled|"
    r"job (is )?closed|expired|removed|not found)\b",
    re.I,
)
_PAYWALL_RE = re.compile(
    r"\b(sign in to continue|create an account|subscribe|payment required|purchase a)\b", re.I
)
_JOB_ID_RE = re.compile(r"(?:job|opening|requisition|req|position)[/_=-]?[a-z0-9-]{3,}", re.I)


@dataclass(frozen=True)
class CanonicalResolution:
    accepted: bool
    canonical_url: str
    discovery_url: str
    redirect_chain: list[str] = field(default_factory=list)
    resolution_method: str = ""
    source_quality: str = "supplemental"
    reason: str = ""
    http_status: int | None = None


def is_safe_destination(url: str) -> bool:
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
        # Reject a hostname resolving to a local service. A DNS failure is a
        # regular network error later, not proof that a public hostname is safe.
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo(host, None)}
        except socket.gaierror:
            return True
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return bool(addresses)


def clean_tracking_url(url: str) -> str:
    """Drop obvious wrappers/analytics while retaining job-identifying query keys."""
    parsed = urlsplit((url or "").strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not _TRACKING_KEYS.match(k)
    ]
    host = (parsed.hostname or "").lower()
    netloc = host
    if parsed.port and not (
        (parsed.scheme == "https" and parsed.port == 443)
        or (parsed.scheme == "http" and parsed.port == 80)
    ):
        netloc = f"{host}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), urlencode(sorted(kept)), "")
    )


def source_quality(adapter: str, url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if adapter in _OFFICIAL_ATS or any(
        marker in host
        for marker in (
            "greenhouse.io",
            "lever.co",
            "ashbyhq.com",
            "recruitee.com",
            "smartrecruiters.com",
            "workable.com",
            "personio.de",
        )
    ):
        return "official_ats"
    if adapter == "direct_discovery":
        return "direct_employer"
    if adapter in _AGGREGATORS:
        # A redirect out of an aggregator to an unrelated public employer/ATS
        # domain is materially better than the original discovery URL.
        aggregator_hosts = ("adzuna.", "jooble.", "remotive.", "arbeitnow.")
        return (
            "aggregator"
            if any(marker in host for marker in aggregator_hosts)
            else "direct_employer"
        )
    return "job_board"


def canonical_identity(posting: RawPosting, canonical_url: str) -> str:
    stable_id = str(
        (posting.extras or {}).get("canonical_job_id") or posting.external_job_id or ""
    ).strip()
    # Canonical URLs are the only identity shared by an official API listing
    # and an aggregator that successfully resolves to it.  Keep ATS ids in
    # provenance/extras, but do not let them split that cross-source group.
    normalized_url = clean_tracking_url(canonical_url)
    if normalized_url:
        return f"url:{normalized_url.casefold()}"
    if stable_id and posting.source_adapter in _OFFICIAL_ATS | {"direct_discovery"}:
        return f"jobid:{posting.source_adapter}:{stable_id.casefold()}"
    return ""


def _html_candidates(page: str, base_url: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for raw in re.findall(r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)", page, re.I):
        candidates.append((urljoin(base_url, html.unescape(raw)), "canonical_link"))
    for raw in re.findall(
        r"<meta[^>]+(?:property|name)=[\"'](?:og:url|twitter:url)[\"'][^>]+content=[\"']([^\"']+)",
        page,
        re.I,
    ):
        candidates.append((urljoin(base_url, html.unescape(raw)), "meta_url"))
    for raw in re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", page, re.I | re.S
    ):
        try:
            data = json.loads(html.unescape(raw.strip()))
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                type_value = item.get("@type", "")
                types = type_value if isinstance(type_value, list) else [type_value]
                if any(str(t).lower() == "jobposting" for t in types) and isinstance(
                    item.get("url"), str
                ):
                    candidates.append((urljoin(base_url, item["url"]), "jsonld_url"))
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return candidates


def _specific_active_job(page: str, url: str, posting: RawPosting) -> tuple[bool, str]:
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(page))).strip()
    if _CLOSED_RE.search(plain):
        return False, "closed_or_removed"
    if _PAYWALL_RE.search(plain):
        return False, "account_or_paywall"
    path = urlsplit(url).path.lower().rstrip("/")
    if path in _GENERIC_PATHS:
        return False, "generic_search_or_homepage"
    role = normalize_role(posting.role_title)
    company = normalize_company(posting.company)
    text = normalize_role(plain)
    role_words = [word for word in role.split() if len(word) > 2]
    role_match = bool(role_words) and sum(word in text for word in role_words) >= max(
        1, min(2, len(role_words))
    )
    company_match = bool(company and company in normalize_company(plain))
    structured = "jobposting" in page.lower()
    job_id = bool(_JOB_ID_RE.search(path))
    if role_match and (structured or job_id or company_match):
        return True, "identity_match"
    # Known ATS detail patterns normally contain a stable job identifier even
    # when company branding is rendered client-side.
    host = (urlsplit(url).hostname or "").lower()
    if role_match and any(
        marker in host
        for marker in ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "personio.de")
    ):
        return True, "official_ats_detail"
    return False, "job_identity_not_confirmed"


def resolve_posting(
    posting: RawPosting,
    *,
    timeout: float = 15.0,
    max_redirects: int = 8,
    max_response_bytes: int = 2_000_000,
    client: httpx.Client | None = None,
) -> CanonicalResolution:
    discovery_url = clean_tracking_url(posting.url)
    quality = source_quality(posting.source_adapter, discovery_url)
    if not is_safe_destination(discovery_url):
        return CanonicalResolution(
            False, "", discovery_url, source_quality=quality, reason="unsafe_destination"
        )
    owns_client = client is None
    client = client or httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "JobSearchHubJobVerifier/1.0"},
    )
    chain: list[str] = []
    current = discovery_url
    try:
        for _ in range(max_redirects + 1):
            if not is_safe_destination(current):
                return CanonicalResolution(
                    False,
                    "",
                    discovery_url,
                    chain,
                    source_quality=quality,
                    reason="unsafe_redirect",
                )
            response = client.get(current)
            chain.append(clean_tracking_url(str(response.url)))
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    return CanonicalResolution(
                        False,
                        "",
                        discovery_url,
                        chain,
                        source_quality=quality,
                        reason="redirect_without_location",
                        http_status=response.status_code,
                    )
                current = clean_tracking_url(urljoin(str(response.url), location))
                continue
            if response.status_code >= 400:
                return CanonicalResolution(
                    False,
                    "",
                    discovery_url,
                    chain,
                    source_quality=quality,
                    reason=f"http_{response.status_code}",
                    http_status=response.status_code,
                )
            if len(response.content) > max_response_bytes:
                return CanonicalResolution(
                    False,
                    "",
                    discovery_url,
                    chain,
                    source_quality=quality,
                    reason="response_too_large",
                    http_status=response.status_code,
                )
            page = response.text
            selected, method = current, "final_url"
            for candidate, candidate_method in _html_candidates(page, current):
                cleaned = clean_tracking_url(candidate)
                if is_safe_destination(cleaned):
                    selected, method = cleaned, candidate_method
                    break
            # A page may declare a direct canonical URL while the current
            # landing page is an aggregator.  Fetch it once and prove it is a
            # specific job rather than trusting metadata alone.
            candidate_page = page
            if selected != current:
                candidate_response = client.get(selected)
                if (
                    candidate_response.status_code < 400
                    and len(candidate_response.content) <= max_response_bytes
                ):
                    candidate_page = candidate_response.text
                    chain.append(clean_tracking_url(str(candidate_response.url)))
                else:
                    selected, method, candidate_page = current, "final_url", page
            active, reason = _specific_active_job(candidate_page, selected, posting)
            if not active:
                return CanonicalResolution(
                    False, "", discovery_url, chain, method, quality, reason, response.status_code
                )
            return CanonicalResolution(
                True,
                clean_tracking_url(selected),
                discovery_url,
                chain,
                method,
                source_quality(posting.source_adapter, selected),
                "verified",
                response.status_code,
            )
        return CanonicalResolution(
            False, "", discovery_url, chain, source_quality=quality, reason="redirect_limit"
        )
    except httpx.TimeoutException:
        return CanonicalResolution(
            False, "", discovery_url, chain, source_quality=quality, reason="timeout"
        )
    except httpx.HTTPError as exc:
        return CanonicalResolution(
            False,
            "",
            discovery_url,
            chain,
            source_quality=quality,
            reason=f"network_error:{type(exc).__name__}",
        )
    finally:
        if owns_client:
            client.close()
