"""Conservative posting availability checks.

The verifier is intentionally cautious: only definite closure signals mark a
posting closed. Ambiguous HTTP/network failures are recorded as unknown so the
pipeline does not false-close viable opportunities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx

from job_search_hub.browser_runtime import BrowserRuntime
from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.posting_dates import DATE_JOB_WAS_POSTED, read_posted_date
from job_search_hub.storage import get_setting

log = get_logger(__name__)

POSTING_CHECKED_AT = "Posting Checked At"
PRE_SCORE_BLOCK_REASON = "Posting availability check inconclusive before scoring"

ACTIVE_VERIFICATION_STATUSES = {
    "Fetched",
    "Scoring",
    "Review",
    "Verification Needed",
    "Awaiting Answer",
    "Needs Input",
    "Shortlisted",
    "Error",
}
PROTECTED_VERIFICATION_STATUSES = {
    "Rejected",
    "Closed",
    "Submitted",
    "Interviewing",
    "Offer",
}

_CLOSED_PATTERNS = (
    re.compile(r"\bpage\s+not\s+found\b", re.I),
    re.compile(r"\bpage\s+you\s+requested\s+was\s+not\s+found\b", re.I),
    re.compile(r"\bjob\s+no\s+longer\s+available\b", re.I),
    re.compile(r"\bjob\s+is\s+no\s+longer\s+available\b", re.I),
    re.compile(r"\bjob\s+position\s+is\s+no\s+longer\s+available\b", re.I),
    re.compile(r"\bjob\s+posting\s+(?:does\s+not\s+exist|not\s+found)\b", re.I),
    re.compile(r"\bposting\s+no\s+longer\s+available\b", re.I),
    re.compile(r"\bposition\s+(?:has\s+been\s+)?filled\b", re.I),
    re.compile(r"\bno\s+longer\s+accepting\s+applications\b", re.I),
    re.compile(r"\bthis\s+job\s+(?:has\s+)?closed\b", re.I),
    re.compile(r"\bthis\s+position\s+(?:has\s+)?closed\b", re.I),
)

_API_AUTHORITATIVE_ADAPTERS = {
    "arbeitnow",
    "ashby",
    "greenhouse",
    "lever",
    "recruitee",
    "remotive",
    "smartrecruiters",
    "usajobs",
}
_AGGREGATOR_ADAPTERS = {"adzuna", "jooble"}
_BROWSER_FALLBACK_ADAPTERS = _AGGREGATOR_ADAPTERS | {"imap", "rss"}
_PUBLIC_BROWSER_BOARD_ADAPTERS = {
    "wellfound",
    "weloveproduct",
    "mind_the_product",
    "dribbble",
    "levels_fyi",
    "pocit",
    "y_combinator",
    "resources_for_humans",
    "built_in",
    "the_muse",
    "remote_com",
    "welcome_to_the_jungle",
    "people_ops_job_board",
    "blind",
    "index_ventures",
    "earlybird",
    "general_catalyst",
    "speedinvest",
    "planet_a",
    "antler",
    "creandum",
    "kleiner_perkins",
    "work_with_indies",
    "career_vault",
    "escape_the_city",
    "open_to_work_remote",
    "experimentation_jobs",
    "people_first_jobs",
    "people_ops_jobs",
    "fractional_jobs",
    "product_jobs_anywhere",
    "elixir_jobs",
    "design_systems_jobs",
    "work_with_linear",
}
_BROWSER_FALLBACK_HOSTS = (
    "adzuna.",
    "jooble.",
    "indeed.",
    "linkedin.",
    "glassdoor.",
)
_SNAPSHOT_ADAPTERS = {"ashby", "greenhouse", "lever", "recruitee", "smartrecruiters"}
_TICK_ALWAYS_VERIFY_STATUSES = {"Shortlisted"}
_TICK_STALE_VERIFICATION_HOURS = 24
_TICK_ACTIONABLE_LIMIT = 50
_TICK_BACKLOG_LIMIT = 50


@dataclass(frozen=True)
class VerificationContext:
    url: str
    source_adapter: str = ""
    external_job_id: str = ""
    company: str = ""
    role_title: str = ""
    status: str = ""
    page_id: str = ""

    @classmethod
    def from_page(cls, page: dict) -> VerificationContext:
        props = page.get("properties") or {}
        return cls(
            url=P.read_url(props.get("URL")),
            source_adapter=P.read_select(props.get("Source Adapter")).lower(),
            external_job_id=P.read_text(props.get("External Job ID")),
            company=P.read_text(props.get("Company")),
            role_title=P.read_text(props.get("Role Title")),
            status=read_status(props.get("Status")),
            page_id=page.get("id", ""),
        )


@dataclass(frozen=True)
class PostingVerificationResult:
    status: str
    reason: str
    checked_at: datetime
    http_status: int | None = None
    final_url: str = ""

    @property
    def closed(self) -> bool:
        return self.status == "closed"


@dataclass(frozen=True)
class AuthoritativeBoardSnapshot:
    """A single board response shared by all postings from that board.

    ``complete`` is deliberately separate from a successful HTTP response.
    A partial SmartRecruiters pagination run, an invalid payload, or an API
    failure can still show that a specific job exists, but must never prove
    that a different job is closed.
    """

    adapter: str
    target: str
    jobs: tuple[dict, ...]
    complete: bool
    reason: str
    http_status: int | None = None
    final_url: str = ""


@dataclass
class PostingVerificationStats:
    candidates: int = 0
    checked: int = 0
    available: int = 0
    closed: int = 0
    unknown: int = 0
    notion_updates: int = 0
    dry_run_updates: int = 0
    applications_cancelled: int = 0
    dry_run_applications_cancelled: int = 0
    skipped_recent: int = 0
    skipped_protected: int = 0
    skipped_missing_url: int = 0
    stale_posted_closed: int = 0
    closed_page_ids: set[str] = field(default_factory=set, repr=False)
    unknown_page_ids: set[str] = field(default_factory=set, repr=False)
    blocked_page_ids: set[str] = field(default_factory=set, repr=False)
    errors: list[str] = field(default_factory=list)

    def absorb(self, other: PostingVerificationStats) -> PostingVerificationStats:
        for name in (
            "candidates",
            "checked",
            "available",
            "closed",
            "unknown",
            "notion_updates",
            "dry_run_updates",
            "applications_cancelled",
            "dry_run_applications_cancelled",
            "skipped_recent",
            "skipped_protected",
            "skipped_missing_url",
            "stale_posted_closed",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.closed_page_ids.update(other.closed_page_ids)
        self.unknown_page_ids.update(other.unknown_page_ids)
        self.blocked_page_ids.update(other.blocked_page_ids)
        self.errors.extend(other.errors)
        return self


def _unknown(reason: str, *, checked_at: datetime | None = None) -> PostingVerificationResult:
    return PostingVerificationResult(
        status="unknown",
        reason=reason,
        checked_at=checked_at or datetime.now(UTC),
    )


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _closed_url_signal(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    closed_job = query.get("closedJob") or query.get("closedjob") or []
    if parsed.netloc.lower().endswith("jooble.org") and any(
        value.strip().lower() == "true" for value in closed_job
    ):
        return "jooble closedJob=true"
    return ""


def _closed_signal(text: str) -> str:
    body = text[:200_000]
    for pattern in _CLOSED_PATTERNS:
        if pattern.search(body):
            return pattern.pattern
    return ""


def _json_items(data, *keys: str) -> list[dict]:
    value = data
    for key in keys:
        if not isinstance(value, dict):
            return []
        value = value.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _match_value(value: object, needles: set[str]) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text in needles:
        return True
    return any(needle and needle in text for needle in needles)


def _context_needles(context: VerificationContext) -> set[str]:
    values = {
        context.external_job_id,
        context.url,
        urlparse(context.url).path.strip("/").split("/")[-1],
    }
    return {value.strip().lower() for value in values if value and value.strip()}


def _match_job(
    jobs: list[dict],
    context: VerificationContext,
    *,
    id_keys: tuple[str, ...] = ("id",),
    url_keys: tuple[str, ...] = ("url",),
) -> bool:
    needles = _context_needles(context)
    if not needles:
        return False
    for job in jobs:
        for key in id_keys:
            if _match_value(job.get(key), needles):
                return True
        for key in url_keys:
            if _match_value(job.get(key), needles):
                return True
    return False


def _source_target(context: VerificationContext) -> str:
    parsed = urlparse(context.url)
    parts = [part for part in parsed.path.split("/") if part]
    adapter = context.source_adapter
    host = parsed.netloc.lower()
    if adapter == "greenhouse" and host.endswith("greenhouse.io") and parts:
        return parts[0]
    if adapter == "lever" and "lever.co" in host and parts:
        return parts[0]
    if adapter == "ashby" and "ashbyhq.com" in host and parts:
        return parts[0]
    if adapter == "recruitee" and host.endswith(".recruitee.com"):
        return host.split(".")[0]
    if adapter == "smartrecruiters" and "smartrecruiters.com" in host and parts:
        return parts[0]
    return re.sub(r"[^A-Za-z0-9_-]+", "", context.company or "")


def _api_result(
    status: str,
    reason: str,
    checked_at: datetime,
    *,
    http_status: int | None = None,
    final_url: str = "",
) -> PostingVerificationResult:
    return PostingVerificationResult(
        status=status,
        reason=reason,
        checked_at=checked_at,
        http_status=http_status,
        final_url=final_url,
    )


def _board_snapshot_key(context: VerificationContext) -> tuple[str, str] | None:
    target = _source_target(context)
    if context.source_adapter not in _SNAPSHOT_ADAPTERS or not target:
        return None
    return context.source_adapter, target.casefold()


def _incomplete_snapshot(
    adapter: str,
    target: str,
    reason: str,
    *,
    jobs: list[dict] | None = None,
    http_status: int | None = None,
    final_url: str = "",
) -> AuthoritativeBoardSnapshot:
    return AuthoritativeBoardSnapshot(
        adapter=adapter,
        target=target,
        jobs=tuple(jobs or []),
        complete=False,
        reason=reason,
        http_status=http_status,
        final_url=final_url,
    )


def _complete_snapshot(
    adapter: str,
    target: str,
    jobs: list[dict],
    reason: str,
    response: httpx.Response,
) -> AuthoritativeBoardSnapshot:
    return AuthoritativeBoardSnapshot(
        adapter=adapter,
        target=target,
        jobs=tuple(job for job in jobs if isinstance(job, dict)),
        complete=True,
        reason=reason,
        http_status=response.status_code,
        final_url=str(response.url),
    )


def _load_authoritative_board_snapshot(
    context: VerificationContext,
    *,
    client: httpx.Client,
) -> AuthoritativeBoardSnapshot | None:
    """Load a board once; only a complete listing can prove an absence."""
    adapter = context.source_adapter
    target = _source_target(context)
    if adapter not in _SNAPSHOT_ADAPTERS or not target:
        return None
    try:
        if adapter == "greenhouse":
            resp = client.get(
                f"https://boards-api.greenhouse.io/v1/boards/{target}/jobs",
                params={"content": "true"},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json() or {}
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                return _incomplete_snapshot(
                    adapter,
                    target,
                    "Greenhouse board API returned an invalid jobs payload",
                    http_status=resp.status_code,
                    final_url=str(resp.url),
                )
            return _complete_snapshot(adapter, target, jobs, "Greenhouse board API complete", resp)

        if adapter == "lever":
            resp = client.get(
                f"https://api.lever.co/v0/postings/{target}",
                params={"mode": "json"},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            jobs = resp.json() or []
            if not isinstance(jobs, list):
                return _incomplete_snapshot(
                    adapter,
                    target,
                    "Lever postings API returned an invalid jobs payload",
                    http_status=resp.status_code,
                    final_url=str(resp.url),
                )
            return _complete_snapshot(adapter, target, jobs, "Lever postings API complete", resp)

        if adapter == "ashby":
            resp = client.get(
                f"https://api.ashbyhq.com/posting-api/job-board/{target}",
                params={"includeCompensation": "true"},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json() or {}
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, list):
                return _incomplete_snapshot(
                    adapter,
                    target,
                    "Ashby posting API returned an invalid jobs payload",
                    http_status=resp.status_code,
                    final_url=str(resp.url),
                )
            return _complete_snapshot(adapter, target, jobs, "Ashby posting API complete", resp)

        if adapter == "recruitee":
            resp = client.get(
                f"https://{target}.recruitee.com/api/offers/",
                headers={"accept": "application/json"},
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json() or {}
            if isinstance(data, list):
                jobs = data
            elif isinstance(data, dict):
                jobs = next(
                    (
                        data[key]
                        for key in ("offers", "jobs", "data")
                        if isinstance(data.get(key), list)
                    ),
                    None,
                )
            else:
                jobs = None
            if not isinstance(jobs, list):
                return _incomplete_snapshot(
                    adapter,
                    target,
                    "Recruitee offers API returned an invalid jobs payload",
                    http_status=resp.status_code,
                    final_url=str(resp.url),
                )
            return _complete_snapshot(adapter, target, jobs, "Recruitee offers API complete", resp)

        if adapter == "smartrecruiters":
            endpoint = f"https://api.smartrecruiters.com/v1/companies/{target}/postings"
            jobs: list[dict] = []
            offset = 0
            page_size = 100
            last_response: httpx.Response | None = None
            for _page in range(1, 1001):
                resp = client.get(endpoint, params={"limit": page_size, "offset": offset})
                last_response = resp
                if resp.status_code == 404 and offset == 0:
                    return None
                resp.raise_for_status()
                data = resp.json() or {}
                batch = data.get("content") if isinstance(data, dict) else None
                if not isinstance(batch, list):
                    return _incomplete_snapshot(
                        adapter,
                        target,
                        "SmartRecruiters postings API returned an invalid content payload",
                        jobs=jobs,
                        http_status=resp.status_code,
                        final_url=str(resp.url),
                    )
                jobs.extend(job for job in batch if isinstance(job, dict))
                raw_total = (
                    data.get("totalFound", data.get("total")) if isinstance(data, dict) else None
                )
                if raw_total is not None:
                    try:
                        if len(jobs) >= int(raw_total):
                            return _complete_snapshot(
                                adapter, target, jobs, "SmartRecruiters postings API complete", resp
                            )
                    except (TypeError, ValueError):
                        return _incomplete_snapshot(
                            adapter,
                            target,
                            "SmartRecruiters postings API returned an invalid total",
                            jobs=jobs,
                            http_status=resp.status_code,
                            final_url=str(resp.url),
                        )
                if len(batch) < page_size:
                    return _complete_snapshot(
                        adapter, target, jobs, "SmartRecruiters postings API complete", resp
                    )
                offset += len(batch)
            return _incomplete_snapshot(
                adapter,
                target,
                "SmartRecruiters postings API pagination limit reached",
                jobs=jobs,
                http_status=last_response.status_code if last_response else None,
                final_url=str(last_response.url) if last_response else endpoint,
            )
    except (json.JSONDecodeError, ValueError, httpx.HTTPError) as exc:
        return _incomplete_snapshot(adapter, target, f"{adapter} source API check failed: {exc}")
    return None


def _snapshot_api_result(
    snapshot: AuthoritativeBoardSnapshot,
    context: VerificationContext,
    *,
    checked_at: datetime,
) -> PostingVerificationResult:
    keys = {
        "greenhouse": (("id",), ("absolute_url",), "Greenhouse board API"),
        "lever": (("id",), ("hostedUrl", "applyUrl"), "Lever postings API"),
        "ashby": (("id", "jobId"), ("jobUrl", "applyUrl"), "Ashby posting API"),
        "recruitee": (
            ("id", "slug"),
            ("careers_url", "url", "job_url", "slug_url"),
            "Recruitee offers API",
        ),
        "smartrecruiters": (
            ("id", "uuid"),
            ("webPostingUrl", "postingUrl", "ref"),
            "SmartRecruiters postings API",
        ),
    }
    id_keys, url_keys, label = keys[snapshot.adapter]
    if _match_job(list(snapshot.jobs), context, id_keys=id_keys, url_keys=url_keys):
        return _api_result(
            "available",
            f"{label} membership found",
            checked_at,
            http_status=snapshot.http_status,
            final_url=snapshot.final_url,
        )
    if snapshot.complete:
        return _api_result(
            "closed",
            f"{label} membership absent",
            checked_at,
            http_status=snapshot.http_status,
            final_url=snapshot.final_url,
        )
    return _api_result(
        "unknown",
        snapshot.reason,
        checked_at,
        http_status=snapshot.http_status,
        final_url=snapshot.final_url,
    )


def _source_api_verification(
    context: VerificationContext,
    *,
    settings: Settings | None,
    client: httpx.Client,
    checked_at: datetime,
    snapshot_cache: dict[tuple[str, str], AuthoritativeBoardSnapshot | None] | None = None,
) -> PostingVerificationResult | None:
    adapter = context.source_adapter
    if adapter not in _API_AUTHORITATIVE_ADAPTERS and adapter not in _AGGREGATOR_ADAPTERS:
        return None

    target = _source_target(context)
    if adapter in {"greenhouse", "lever", "ashby", "recruitee", "smartrecruiters"} and not target:
        return None

    snapshot_key = _board_snapshot_key(context)
    if snapshot_key is not None:
        cache = snapshot_cache if snapshot_cache is not None else {}
        if snapshot_key not in cache:
            cache[snapshot_key] = _load_authoritative_board_snapshot(context, client=client)
        snapshot = cache[snapshot_key]
        return _snapshot_api_result(snapshot, context, checked_at=checked_at) if snapshot else None

    try:
        if adapter == "remotive":
            resp = client.get("https://remotive.com/api/remote-jobs")
            resp.raise_for_status()
            jobs = (resp.json() or {}).get("jobs") or []
            found = _match_job(
                [job for job in jobs if isinstance(job, dict)],
                context,
                id_keys=("id",),
                url_keys=("url",),
            )
            return _api_result(
                "available" if found else "closed",
                "Remotive API membership found" if found else "Remotive API membership absent",
                checked_at,
                http_status=resp.status_code,
                final_url=str(resp.url),
            )

        if adapter == "arbeitnow":
            resp = client.get("https://www.arbeitnow.com/api/job-board-api")
            resp.raise_for_status()
            jobs = (resp.json() or {}).get("data") or []
            found = _match_job(
                [job for job in jobs if isinstance(job, dict)],
                context,
                id_keys=("slug",),
                url_keys=("url",),
            )
            return _api_result(
                "available" if found else "closed",
                "Arbeitnow API membership found" if found else "Arbeitnow API membership absent",
                checked_at,
                http_status=resp.status_code,
                final_url=str(resp.url),
            )

        if adapter == "usajobs":
            creds = getattr(settings, "adapters", None)
            user_agent = getattr(creds, "usajobs_user_agent", "") if creds else ""
            auth_key = getattr(creds, "usajobs_auth_key", "") if creds else ""
            if not user_agent or not auth_key or not context.external_job_id:
                return None
            resp = client.get(
                "https://data.usajobs.gov/api/Search",
                params={
                    "ResultsPerPage": "10",
                    "Page": "1",
                    "Fields": "Full",
                    "PositionID": context.external_job_id,
                },
                headers={
                    "Host": "data.usajobs.gov",
                    "User-Agent": user_agent,
                    "Authorization-Key": auth_key,
                },
            )
            resp.raise_for_status()
            items = _json_items(resp.json() or {}, "SearchResult", "SearchResultItems")
            jobs = [
                item.get("MatchedObjectDescriptor") or item
                for item in items
                if isinstance(item, dict)
            ]
            found = _match_job(
                [job for job in jobs if isinstance(job, dict)],
                context,
                id_keys=("PositionID", "MatchedObjectId"),
                url_keys=("PositionURI",),
            )
            return _api_result(
                "available" if found else "closed",
                "USAJOBS Search API membership found"
                if found
                else "USAJOBS Search API membership absent",
                checked_at,
                http_status=resp.status_code,
                final_url=str(resp.url),
            )

        if adapter == "adzuna":
            creds = getattr(settings, "adapters", None)
            app_id = getattr(creds, "adzuna_app_id", "") if creds else ""
            app_key = getattr(creds, "adzuna_app_key", "") if creds else ""
            country = getattr(creds, "adzuna_country", "us") if creds else "us"
            if not app_id or not app_key or not context.external_job_id:
                return None
            resp = client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={
                    "app_id": app_id,
                    "app_key": app_key,
                    "what": context.external_job_id,
                    "results_per_page": "10",
                },
            )
            resp.raise_for_status()
            jobs = (resp.json() or {}).get("results") or []
            found = _match_job(
                [job for job in jobs if isinstance(job, dict)],
                context,
                id_keys=("id",),
                url_keys=("redirect_url", "adref"),
            )
            if found:
                return _api_result(
                    "available",
                    "Adzuna API still returns matching job",
                    checked_at,
                    http_status=resp.status_code,
                    final_url=str(resp.url),
                )
            return None

    except (json.JSONDecodeError, ValueError, httpx.HTTPError) as exc:
        return _api_result(
            "unknown",
            f"{adapter} source API check failed: {exc}",
            checked_at,
        )

    return None


def _browser_mode(value: str | None = None) -> str:
    mode = (value or get_setting("JOBHUB_POSTING_CHECK_BROWSER", "auto")).strip().lower()
    if mode in {"0", "false", "no", "off", "never"}:
        return "off"
    if mode in {"1", "true", "yes", "on", "always"}:
        return "always"
    return "auto"


def _is_high_risk_source(context: VerificationContext) -> bool:
    host = urlparse(context.url).netloc.lower()
    return context.source_adapter in _AGGREGATOR_ADAPTERS or any(
        marker in host for marker in _BROWSER_FALLBACK_HOSTS
    )


def _canonical_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return parsed.netloc.casefold(), parsed.path.rstrip("/").casefold()


def _is_generic_job_destination(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.strip("/").casefold()
    segments = [part for part in path.split("/") if part]
    if any(part in {"search", "searchresult", "search-results", "job-search"} for part in segments):
        return True
    if path in {"jobs", "job", "careers", "career"}:
        return True
    return bool(parsed.query and any(part in {"jobs", "job"} for part in segments))


def _normalized_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _page_confirms_identity(
    context: VerificationContext,
    *,
    text: str,
    final_url: str,
) -> bool:
    if _canonical_url(context.url) == _canonical_url(final_url):
        return True
    haystack = _normalized_identity(f"{text[:200_000]} {final_url}")
    external_id = _normalized_identity(context.external_job_id)
    if len(external_id) >= 5 and external_id in haystack:
        return True
    expected_path = _normalized_identity(urlparse(context.url).path.split("/")[-1])
    if len(expected_path) >= 5 and expected_path in haystack:
        return True
    title = _normalized_identity(context.role_title)
    company = _normalized_identity(context.company)
    return bool(len(title) >= 5 and len(company) >= 3 and title in haystack and company in haystack)


def _identity_failure(
    context: VerificationContext,
    *,
    text: str,
    final_url: str,
) -> str:
    redirected = _canonical_url(context.url) != _canonical_url(final_url)
    high_risk = _is_high_risk_source(context)
    if _is_generic_job_destination(final_url):
        return "redirected to a generic job search or category page"
    if (high_risk or redirected) and not _page_confirms_identity(
        context,
        text=text,
        final_url=final_url,
    ):
        return "page did not confirm the expected posting identity"
    return ""


def _should_try_browser(
    context: VerificationContext,
    result: PostingVerificationResult,
    *,
    browser_fallback: str | None,
    require_decisive: bool,
) -> bool:
    mode = _browser_mode(browser_fallback)
    if mode == "off" or result.closed:
        return False
    if mode == "always":
        return True
    if context.source_adapter in _PUBLIC_BROWSER_BOARD_ADAPTERS:
        return True
    if context.source_adapter in _BROWSER_FALLBACK_ADAPTERS and result.status != "available":
        return True
    if _is_high_risk_source(context) and result.status == "unknown":
        return True
    return require_decisive and _is_high_risk_source(context) and result.status != "closed"


def _unknown_blocks_pre_score(
    context: VerificationContext,
    result: PostingVerificationResult,
    *,
    require_decisive: bool,
) -> bool:
    return require_decisive and result.status == "unknown"


def _verify_with_browser(
    context: VerificationContext,
    *,
    browser_runtime: BrowserRuntime | None = None,
) -> PostingVerificationResult:
    checked_at = datetime.now(UTC)
    runtime = browser_runtime or BrowserRuntime()
    owns_runtime = browser_runtime is None
    try:
        rendered = runtime.render(context.url, timeout_seconds=15)
        if rendered is None:
            return _unknown(
                runtime.error or "browser posting check failed",
                checked_at=checked_at,
            )
        status_code = rendered.http_status
        final_url = rendered.url
        text = re.sub(r"<[^>]+>", " ", rendered.html)[:200_000]
        url_signal = _closed_url_signal(final_url)
        if url_signal:
            return PostingVerificationResult(
                status="closed",
                reason=f"explicit closed URL signal after redirect: {url_signal}",
                checked_at=checked_at,
                http_status=status_code,
                final_url=final_url,
            )
        if status_code in {404, 410}:
            return PostingVerificationResult(
                status="closed",
                reason=f"browser HTTP {status_code}",
                checked_at=checked_at,
                http_status=status_code,
                final_url=final_url,
            )
        if status_code in {403, 429} or (status_code is not None and status_code >= 500):
            return PostingVerificationResult(
                status="unknown",
                reason=f"browser ambiguous HTTP {status_code}",
                checked_at=checked_at,
                http_status=status_code,
                final_url=final_url,
            )
        identity_failure = _identity_failure(context, text=text, final_url=final_url)
        if identity_failure:
            return PostingVerificationResult(
                status="unknown",
                reason=identity_failure,
                checked_at=checked_at,
                http_status=status_code,
                final_url=final_url,
            )
        if _closed_signal(text):
            return PostingVerificationResult(
                status="closed",
                reason="browser-rendered closed-page text",
                checked_at=checked_at,
                http_status=status_code,
                final_url=final_url,
            )
        return PostingVerificationResult(
            status="available",
            reason="browser-rendered page reachable",
            checked_at=checked_at,
            http_status=status_code,
            final_url=final_url,
        )
    finally:
        if owns_runtime:
            runtime.close()


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[" + "-" * width + "]"
    filled = min(width, max(0, int(done / total * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _should_log_progress(done: int, total: int) -> bool:
    return total <= 25 or done in {1, total} or done % 5 == 0


def _posting_label(props: dict) -> str:
    company = P.read_text(props.get("Company")) or "Unknown company"
    role_title = P.read_text(props.get("Role Title")) or "Unknown role"
    return f"{company} - {role_title}"


def _log_verification_progress(
    *,
    done: int,
    total: int,
    stats: PostingVerificationStats,
    current: str,
    result: str,
) -> None:
    percent = (done / total * 100.0) if total else 0.0
    log.info(
        "POSTING  | %s %d/%d pages, %d left (%5.1f%%) | "
        "checked=%d available=%d closed=%d unknown=%d "
        "notion_updates=%d dry_run_updates=%d "
        "skipped_recent=%d skipped_missing_url=%d skipped_protected=%d | "
        "current: %s | result: %s",
        _progress_bar(done, total),
        done,
        total,
        max(0, total - done),
        percent,
        stats.checked,
        stats.available,
        stats.closed,
        stats.unknown,
        stats.notion_updates,
        stats.dry_run_updates,
        stats.skipped_recent,
        stats.skipped_missing_url,
        stats.skipped_protected,
        current,
        result,
    )


def verify_posting_url(
    url: str,
    *,
    role_title: str = "",
    company: str = "",
    external_job_id: str = "",
    source_adapter: str = "",
    client: httpx.Client | None = None,
) -> PostingVerificationResult:
    checked_at = datetime.now(UTC)
    url = (url or "").strip()
    if not _valid_http_url(url):
        return _unknown("missing or unsupported posting URL", checked_at=checked_at)
    url_signal = _closed_url_signal(url)
    if url_signal:
        return PostingVerificationResult(
            status="closed",
            reason=f"explicit closed URL signal: {url_signal}",
            checked_at=checked_at,
            final_url=url,
        )

    headers = {
        "User-Agent": ("Mozilla/5.0 (compatible; JobSearchHubBot/1.0; +https://localhost/jobhub)"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    owns_client = client is None
    http = client or httpx.Client(timeout=10.0, follow_redirects=True, headers=headers)
    try:
        resp = http.get(url, headers=headers)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        return _unknown(f"invalid posting URL: {exc}", checked_at=checked_at)
    except httpx.TimeoutException as exc:
        return _unknown(f"posting check timed out: {exc}", checked_at=checked_at)
    except httpx.HTTPError as exc:
        return _unknown(f"posting check failed: {exc}", checked_at=checked_at)
    finally:
        if owns_client:
            http.close()

    final_url = str(resp.url)
    final_url_signal = _closed_url_signal(final_url)
    if final_url_signal:
        return PostingVerificationResult(
            status="closed",
            reason=f"explicit closed URL signal after redirect: {final_url_signal}",
            checked_at=checked_at,
            http_status=resp.status_code,
            final_url=final_url,
        )

    if resp.status_code in {404, 410}:
        return PostingVerificationResult(
            status="closed",
            reason=f"HTTP {resp.status_code}",
            checked_at=checked_at,
            http_status=resp.status_code,
            final_url=final_url,
        )
    if resp.status_code in {403, 429} or resp.status_code >= 500:
        return PostingVerificationResult(
            status="unknown",
            reason=f"ambiguous HTTP {resp.status_code}",
            checked_at=checked_at,
            http_status=resp.status_code,
            final_url=final_url,
        )
    if resp.status_code >= 400:
        return PostingVerificationResult(
            status="unknown",
            reason=f"unhandled HTTP {resp.status_code}",
            checked_at=checked_at,
            http_status=resp.status_code,
            final_url=final_url,
        )

    identity_failure = _identity_failure(
        VerificationContext(
            url=url,
            source_adapter=source_adapter,
            external_job_id=external_job_id,
            company=company,
            role_title=role_title,
        ),
        text=resp.text or "",
        final_url=final_url,
    )
    if identity_failure:
        return PostingVerificationResult(
            status="unknown",
            reason=identity_failure,
            checked_at=checked_at,
            http_status=resp.status_code,
            final_url=final_url,
        )

    signal = _closed_signal(resp.text or "")
    if signal:
        return PostingVerificationResult(
            status="closed",
            reason="explicit closed-page text",
            checked_at=checked_at,
            http_status=resp.status_code,
            final_url=final_url,
        )

    context = " / ".join(part for part in (company, role_title) if part)
    reason = f"posting reachable{f': {context}' if context else ''}"
    return PostingVerificationResult(
        status="available",
        reason=reason,
        checked_at=checked_at,
        http_status=resp.status_code,
        final_url=final_url,
    )


def verify_posting_context(
    context: VerificationContext,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    browser_fallback: str | None = None,
    require_decisive: bool = False,
    snapshot_cache: dict[tuple[str, str], AuthoritativeBoardSnapshot | None] | None = None,
    browser_runtime: BrowserRuntime | None = None,
) -> PostingVerificationResult:
    checked_at = datetime.now(UTC)
    url = (context.url or "").strip()
    if not _valid_http_url(url):
        return _unknown("missing or unsupported posting URL", checked_at=checked_at)

    headers = {
        "User-Agent": ("Mozilla/5.0 (compatible; JobSearchHubBot/1.0; +https://localhost/jobhub)"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    owns_client = client is None
    http = client or httpx.Client(timeout=10.0, follow_redirects=True, headers=headers)
    try:
        api_result = _source_api_verification(
            context,
            settings=settings,
            client=http,
            checked_at=checked_at,
            snapshot_cache=snapshot_cache,
        )
        if api_result is not None and api_result.closed:
            return api_result

        html_result = verify_posting_url(
            url,
            role_title=context.role_title,
            company=context.company,
            external_job_id=context.external_job_id,
            source_adapter=context.source_adapter,
            client=http,
        )
        if html_result.closed:
            return html_result

        identity_unverified = html_result.status == "unknown" and html_result.reason in {
            "redirected to a generic job search or category page",
            "page did not confirm the expected posting identity",
        }
        browser_result: PostingVerificationResult | None = None

        if _should_try_browser(
            context,
            html_result,
            browser_fallback=browser_fallback,
            require_decisive=require_decisive,
        ):
            browser_result = (
                _verify_with_browser(context, browser_runtime=browser_runtime)
                if browser_runtime is not None
                else _verify_with_browser(context)
            )
            if browser_result.closed:
                return browser_result
            if browser_result.status == "available":
                return browser_result
            if require_decisive and _is_high_risk_source(context):
                return browser_result

        if identity_unverified:
            return browser_result or html_result

        if api_result is not None and api_result.status == "available":
            return api_result
        if html_result.status == "available":
            return html_result
        if api_result is not None and api_result.status == "unknown":
            return api_result
        return html_result
    finally:
        if owns_client:
            http.close()


def _parse_checked_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _recently_checked(page: dict, *, cutoff: datetime) -> bool:
    props = page.get("properties") or {}
    checked = _parse_checked_at(P.read_date(props.get(POSTING_CHECKED_AT)))
    return bool(checked and checked > cutoff)


def posting_check_is_recent(page: dict, *, max_age_hours: int = 48) -> bool:
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    return _recently_checked(page, cutoff=cutoff)


def _stale_posting_checked_filter(cutoff: datetime) -> dict:
    return {
        "or": [
            {"property": POSTING_CHECKED_AT, "date": {"before": cutoff.isoformat()}},
            {"property": POSTING_CHECKED_AT, "date": {"is_empty": True}},
        ]
    }


def _active_status_pages(
    notion: NotionClient,
    data_source_id: str,
    *,
    checked_before: datetime | None = None,
    statuses: set[str] | None = None,
) -> list[dict]:
    pages: list[dict] = []
    seen: set[str] = set()
    stale_filter = _stale_posting_checked_filter(checked_before) if checked_before else None
    for status in sorted(statuses or ACTIVE_VERIFICATION_STATUSES):
        status_filter = {"property": "Status", "select": {"equals": status}}
        filter_ = (
            {"and": [status_filter, stale_filter]} if stale_filter is not None else status_filter
        )
        for page in notion.query_data_source(
            data_source_id,
            filter_=filter_,
        ):
            page_id = page.get("id", "")
            if page_id and page_id in seen:
                continue
            if page_id:
                seen.add(page_id)
            pages.append(page)
    return pages


def _two_calendar_month_cutoff(now: datetime, timezone: str) -> date:
    """Return the local calendar date exactly two months before ``now``.

    Month-end dates clamp to the last available day, so this has the ordinary
    human reading of "two months" rather than treating it as sixty days.
    """
    try:
        local = now.astimezone(ZoneInfo(timezone))
    except Exception:
        local = now.astimezone(UTC)
    month = local.month - 2
    year = local.year
    if month <= 0:
        year -= 1
        month += 12
    from calendar import monthrange

    return date(year, month, min(local.day, monthrange(year, month)[1]))


def close_stale_posted_opportunities(
    settings: Settings,
    notion: NotionClient,
    store: Store,
    *,
    dry_run: bool,
    pages: list[dict] | None = None,
    now: datetime | None = None,
) -> PostingVerificationStats:
    """Close active rows posted strictly more than two calendar months ago."""
    stats = PostingVerificationStats()
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats
    cutoff = _two_calendar_month_cutoff(
        now or datetime.now(UTC), getattr(settings, "timezone", "UTC")
    )
    try:
        local_timezone = ZoneInfo(getattr(settings, "timezone", "UTC"))
    except Exception:
        local_timezone = UTC
    candidates = list(pages) if pages is not None else _active_status_pages(notion, ds_id)
    for page in candidates:
        props = page.get("properties") or {}
        context = VerificationContext.from_page(page)
        if context.status not in ACTIVE_VERIFICATION_STATUSES:
            continue
        posted_at = read_posted_date(props)
        if posted_at is None:
            continue
        raw_posted_date = P.read_date(props.get(DATE_JOB_WAS_POSTED)) or ""
        # Notion date-only values represent a calendar day, not midnight UTC.
        posted_date = (
            posted_at.date()
            if "T" not in raw_posted_date
            else posted_at.astimezone(local_timezone).date()
        )
        if posted_date >= cutoff:
            continue
        stats.candidates += 1
        stats.closed += 1
        stats.stale_posted_closed += 1
        if context.page_id:
            stats.closed_page_ids.add(context.page_id)
        cancelled = _cancel_unsubmitted_applications(
            notion,
            P.read_relation_ids(props.get("Application")),
            dry_run=dry_run,
            stats=stats,
        )
        update = {
            "Status": P.select("Closed"),
            "Last Error": P.rich_text(
                "Posting closed: posted_date_exceeded_two_months "
                f"(posted {posted_date.isoformat()}, cutoff {cutoff.isoformat()})"
            ),
        }
        if dry_run:
            stats.dry_run_updates += 1
        elif context.page_id:
            notion.update_page(context.page_id, update)
            stats.notion_updates += 1
        if hasattr(store, "audit"):
            store.audit(
                actor="posting_verification",
                action="close_stale_posting",
                target_kind="opportunity",
                target_id=context.page_id,
                dry_run=dry_run,
                payload={
                    "reason": "posted_date_exceeded_two_months",
                    "posted_date": posted_date.isoformat(),
                    "cutoff": cutoff.isoformat(),
                    "cancelled_applications": cancelled,
                },
            )
    if stats.stale_posted_closed:
        log.info(
            "posting verification: closed %d posting(s) older than calendar cutoff=%s",
            stats.stale_posted_closed,
            cutoff.isoformat(),
        )
    return stats


def _cancel_unsubmitted_applications(
    notion: NotionClient,
    application_ids: list[str],
    *,
    dry_run: bool,
    stats: PostingVerificationStats,
) -> list[str]:
    cancelled: list[str] = []
    for application_id in application_ids:
        if not application_id:
            continue
        try:
            application = notion.get_page(application_id)
            status = read_status((application.get("properties") or {}).get("Status"))
            if status == "Submitted":
                continue
            cancelled.append(application_id)
            if dry_run:
                stats.dry_run_applications_cancelled += 1
            else:
                notion.update_page(application_id, {"Status": P.select("Cancelled")})
                stats.applications_cancelled += 1
        except Exception as exc:
            message = f"application {application_id}: {exc}"
            stats.errors.append(message)
            log.warning("posting verification: failed to cancel %s", message)
    return cancelled


def _verification_update(
    result: PostingVerificationResult,
    context: VerificationContext,
    *,
    require_decisive: bool,
) -> dict:
    if result.closed:
        return {
            POSTING_CHECKED_AT: P.date_property(result.checked_at),
            "Status": P.select("Closed"),
            "Last Error": P.rich_text(f"Posting closed: {result.reason}"),
        }
    if _unknown_blocks_pre_score(context, result, require_decisive=require_decisive):
        if context.status == "Verification Needed":
            return {}
        return {
            POSTING_CHECKED_AT: P.date_property(result.checked_at),
            "Status": P.select("Verification Needed"),
            "Last Error": P.rich_text(f"{PRE_SCORE_BLOCK_REASON}: {result.reason}"),
        }
    if result.status == "available" and context.status == "Verification Needed":
        # A previously ambiguous aggreggator/blocked result is safe to return
        # to normal intake only once a later check positively reaches it.
        return {
            POSTING_CHECKED_AT: P.date_property(result.checked_at),
            "Status": P.select("Fetched"),
            "Last Error": P.rich_text(""),
        }
    # A timestamp alone is not a meaningful state transition.  It used to
    # cause one Notion write per retry, which is precisely the rate-limit
    # pressure this local cadence cache is designed to remove.
    return {}


def _record_result(
    *,
    stats: PostingVerificationStats,
    result: PostingVerificationResult,
    context: VerificationContext,
    require_decisive: bool,
) -> None:
    stats.checked += 1
    if result.status == "available":
        stats.available += 1
    elif result.status == "closed":
        stats.closed += 1
        if context.page_id:
            stats.closed_page_ids.add(context.page_id)
    else:
        stats.unknown += 1
        if context.page_id:
            stats.unknown_page_ids.add(context.page_id)
            if _unknown_blocks_pre_score(
                context,
                result,
                require_decisive=require_decisive,
            ):
                stats.blocked_page_ids.add(context.page_id)


def verify_opportunity_pages(
    settings: Settings,
    notion: NotionClient,
    store: Store,
    pages: list[dict],
    *,
    dry_run: bool,
    max_age_hours: int = 48,
    force: bool = False,
    limit: int | None = None,
    require_decisive: bool = False,
    browser_fallback: str | None = None,
    snapshot_cache: dict[tuple[str, str], AuthoritativeBoardSnapshot | None] | None = None,
    browser_runtime: BrowserRuntime | None = None,
) -> PostingVerificationStats:
    stats = PostingVerificationStats()
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    total = len(pages)
    board_snapshots = snapshot_cache if snapshot_cache is not None else {}
    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        for index, page in enumerate(pages, start=1):
            props = page.get("properties") or {}
            context = VerificationContext.from_page(page)
            current = _posting_label(props)
            if context.status in PROTECTED_VERIFICATION_STATUSES:
                stats.skipped_protected += 1
                if _should_log_progress(index, total):
                    _log_verification_progress(
                        done=index,
                        total=total,
                        stats=stats,
                        current=current,
                        result=f"skipped protected status={context.status or '-'}",
                    )
                continue
            if context.status not in ACTIVE_VERIFICATION_STATUSES:
                if _should_log_progress(index, total):
                    _log_verification_progress(
                        done=index,
                        total=total,
                        stats=stats,
                        current=current,
                        result=f"skipped inactive status={context.status or '-'}",
                    )
                continue
            stats.candidates += 1
            if not context.url:
                stats.skipped_missing_url += 1
                if _should_log_progress(index, total):
                    _log_verification_progress(
                        done=index,
                        total=total,
                        stats=stats,
                        current=current,
                        result="skipped missing URL",
                    )
                continue
            if (
                not force
                and not require_decisive
                and max_age_hours > 0
                and _recently_checked(page, cutoff=cutoff)
            ):
                stats.skipped_recent += 1
                continue
            host = urlparse(context.url).netloc.casefold()
            if (
                not force
                and hasattr(store, "posting_verification_due")
                and not store.posting_verification_due(context.page_id)
            ):
                stats.skipped_recent += 1
                continue
            if (
                not force
                and hasattr(store, "host_verification_due")
                and not store.host_verification_due(host)
            ):
                stats.skipped_recent += 1
                continue
            if limit is not None and stats.checked >= max(0, limit):
                break

            result = verify_posting_context(
                context,
                settings=settings,
                client=client,
                browser_fallback=browser_fallback,
                require_decisive=require_decisive,
                snapshot_cache=board_snapshots,
                browser_runtime=browser_runtime,
            )
            _record_result(
                stats=stats,
                result=result,
                context=context,
                require_decisive=require_decisive,
            )
            if context.page_id and hasattr(store, "record_posting_verification"):
                store.record_posting_verification(
                    opportunity_page_id=context.page_id,
                    host=host,
                    status=result.status,
                    reason=result.reason,
                    checked_at=result.checked_at,
                )

            update = _verification_update(
                result,
                context,
                require_decisive=require_decisive,
            )
            cancelled_applications: list[str] = []
            if result.closed:
                cancelled_applications = _cancel_unsubmitted_applications(
                    notion,
                    P.read_relation_ids(props.get("Application")),
                    dry_run=dry_run,
                    stats=stats,
                )
            if dry_run and update:
                stats.dry_run_updates += 1
            elif context.page_id and update:
                notion.update_page(context.page_id, update)
                stats.notion_updates += 1

            if hasattr(store, "audit"):
                store.audit(
                    actor="posting_verification",
                    action="verify_posting",
                    target_kind="opportunity",
                    target_id=context.page_id,
                    dry_run=dry_run,
                    payload={
                        "status": context.status,
                        "source_adapter": context.source_adapter,
                        "result": result.status,
                        "reason": result.reason,
                        "url": context.url,
                        "http_status": result.http_status,
                        "final_url": result.final_url,
                        "require_decisive": require_decisive,
                        "pre_score_blocked": context.page_id in stats.blocked_page_ids,
                        "cancelled_applications": cancelled_applications,
                    },
                )
            if _should_log_progress(index, total):
                _log_verification_progress(
                    done=index,
                    total=total,
                    stats=stats,
                    current=current,
                    result=f"{result.status}: {result.reason}",
                )
    if stats.skipped_recent:
        log.info(
            "posting verification: skipped %d recently checked row(s) "
            "max_age_hours=%d force=%s require_decisive=%s",
            stats.skipped_recent,
            max_age_hours,
            force,
            require_decisive,
        )
    return stats


def verify_tick_preflight(
    settings: Settings,
    notion: NotionClient,
    store: Store,
    *,
    dry_run: bool,
    browser_fallback: str | None = None,
    browser_runtime: BrowserRuntime | None = None,
) -> PostingVerificationStats:
    """Strictly check score-adjacent rows, and stale-check other active rows.

    The two queues share board snapshots, so a company board is fetched at
    most once per tick session even when it contains rows in both queues.
    """
    stats = PostingVerificationStats()
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats
    # This deliberately happens before bounded URL queues: posted-date
    # retention is a deterministic whole-active-set policy, not a sample.
    stats.absorb(
        close_stale_posted_opportunities(
            settings,
            notion,
            store,
            dry_run=dry_run,
        )
    )
    now = datetime.now(UTC)
    always_pages = _active_status_pages(
        notion,
        ds_id,
        statuses=_TICK_ALWAYS_VERIFY_STATUSES,
    )[:_TICK_ACTIONABLE_LIMIT]
    stale_pages = _active_status_pages(
        notion,
        ds_id,
        checked_before=now - timedelta(hours=_TICK_STALE_VERIFICATION_HOURS),
        statuses=ACTIVE_VERIFICATION_STATUSES - _TICK_ALWAYS_VERIFY_STATUSES,
    )[:_TICK_BACKLOG_LIMIT]
    seen: set[str] = set()
    queues: list[list[dict]] = []
    for pages in (always_pages, stale_pages):
        deduped: list[dict] = []
        for page in pages:
            page_id = page.get("id", "")
            if page_id and page_id in stats.closed_page_ids:
                continue
            if page_id and page_id in seen:
                continue
            if page_id:
                seen.add(page_id)
            deduped.append(page)
        queues.append(deduped)

    snapshots: dict[tuple[str, str], AuthoritativeBoardSnapshot | None] = {}
    for queue_index, pages in enumerate(queues):
        if not pages:
            continue
        stats.absorb(
            verify_opportunity_pages(
                settings,
                notion,
                store,
                pages,
                dry_run=dry_run,
                force=queue_index == 0,
                max_age_hours=0 if queue_index == 0 else _TICK_STALE_VERIFICATION_HOURS,
                require_decisive=True,
                browser_fallback=browser_fallback,
                snapshot_cache=snapshots,
                browser_runtime=browser_runtime,
            )
        )
    log.info(
        "posting verification tick preflight: always=%d stale=%d checked=%d "
        "available=%d closed=%d unknown=%d review_blocked=%d",
        len(queues[0]),
        len(queues[1]),
        stats.checked,
        stats.available,
        stats.closed,
        stats.unknown,
        len(stats.blocked_page_ids),
    )
    return stats


def migrate_inconclusive_review_rows(
    settings: Settings,
    notion: NotionClient,
    *,
    dry_run: bool,
    limit: int | None = None,
) -> PostingVerificationStats:
    """Move only automation-created inconclusive Review rows to the new queue.

    Human-reviewed rows are deliberately untouched: an explicit decision or a
    linked Application is stronger evidence of operator intent than the old
    automation error text.
    """
    stats = PostingVerificationStats()
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats
    for page in notion.query_data_source(
        ds_id,
        filter_={"property": "Status", "select": {"equals": "Review"}},
    ):
        if limit is not None and stats.candidates >= max(0, limit):
            break
        props = page.get("properties") or {}
        error = P.read_text(props.get("Last Error")).casefold()
        automation_error = (
            PRE_SCORE_BLOCK_REASON.casefold() in error or "availability check inconclusive" in error
        )
        if not automation_error:
            continue
        if P.read_select(props.get("My Decision")) not in {"", "Undecided"}:
            continue
        if P.read_relation_ids(props.get("Application")):
            continue
        stats.candidates += 1
        if dry_run:
            stats.dry_run_updates += 1
        elif page.get("id"):
            notion.update_page(page["id"], {"Status": P.select("Verification Needed")})
            stats.notion_updates += 1
    return stats


def verify_active_opportunities(
    settings: Settings,
    notion: NotionClient,
    store: Store,
    dry_run: bool,
    max_age_hours: int = 48,
    force: bool = False,
    limit: int | None = None,
    pages: list[dict] | None = None,
    require_decisive: bool = False,
    browser_fallback: str | None = None,
    browser_runtime: BrowserRuntime | None = None,
) -> PostingVerificationStats:
    stats = PostingVerificationStats()
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats

    log.info(
        "posting verification: loading active opportunities statuses=%s",
        sorted(ACTIVE_VERIFICATION_STATUSES),
    )
    checked_before = None
    if not force and not require_decisive and max_age_hours > 0:
        checked_before = datetime.now(UTC) - timedelta(hours=max_age_hours)
    supplied_pages = list(pages) if pages is not None else None
    stats.absorb(
        close_stale_posted_opportunities(
            settings,
            notion,
            store,
            dry_run=dry_run,
            pages=supplied_pages,
        )
    )
    pages = (
        supplied_pages
        if supplied_pages is not None
        else _active_status_pages(notion, ds_id, checked_before=checked_before)
    )
    pages = [page for page in pages if page.get("id", "") not in stats.closed_page_ids]
    log.info(
        "posting verification: queue loaded pages=%d max_age_hours=%d force=%s "
        "limit=%s dry_run=%s require_decisive=%s browser_fallback=%s stale_filter=%s",
        len(pages),
        max_age_hours,
        force,
        limit,
        dry_run,
        require_decisive,
        _browser_mode(browser_fallback),
        bool(checked_before),
    )
    if dry_run:
        log.warning(
            "posting verification: dry_run=true; checked URLs will be logged "
            "but Posting Checked At will not be written to Notion. Re-run with --apply "
            "to persist timestamps and Closed status updates."
        )

    stats.absorb(
        verify_opportunity_pages(
            settings,
            notion,
            store,
            pages,
            dry_run=dry_run,
            max_age_hours=max_age_hours,
            force=force,
            limit=limit,
            require_decisive=require_decisive,
            browser_fallback=browser_fallback,
            browser_runtime=browser_runtime,
        )
    )
    log.info("posting verification: %s", stats)
    return stats
