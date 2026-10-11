"""Backfill and retry handling for missing Opportunity posted dates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.ingest.base import RawPosting
from job_search_hub.ingest.registry import build_adapters
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.normalize import fingerprint, normalize_url
from job_search_hub.pipeline.posting_dates import (
    DATE_JOB_WAS_POSTED,
    PROTECTED_POSTED_DATE_STATUSES,
    effective_posted_at,
    posting_date_property,
    read_posted_date,
    same_posted_day,
)
from job_search_hub.pipeline.posting_verification import (
    ACTIVE_VERIFICATION_STATUSES,
    VerificationContext,
    verify_posting_context,
)

log = get_logger(__name__)

_SOURCE_ESTIMATE_ADAPTERS = {"jooble", "rss", "imap", "manual"}
_NON_FETCH_ADAPTERS = {"manual", "watchlist"}


@dataclass
class PostingDateBackfillStats:
    candidates: int = 0
    skipped_existing: int = 0
    skipped_protected: int = 0
    skipped_attempt_limit: int = 0
    fetched_postings: int = 0
    updated: int = 0
    dry_run_updates: int = 0
    exact_dates: int = 0
    estimated_dates: int = 0
    closed: int = 0
    unknown: int = 0
    applications_cancelled: int = 0
    dry_run_applications_cancelled: int = 0
    updated_page_ids: list[str] = field(default_factory=list)
    closed_page_ids: set[str] = field(default_factory=set, repr=False)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _MatchedPostingDate:
    posted_at: datetime
    estimated: bool
    reason: str


def _page_posting(page: dict) -> RawPosting:
    props = page.get("properties") or {}
    return RawPosting(
        source=P.read_select(props.get("Source")),
        source_adapter=P.read_select(props.get("Source Adapter")),
        external_job_id=P.read_text(props.get("External Job ID")),
        company=P.read_text(props.get("Company")),
        role_title=P.read_text(props.get("Role Title")),
        url=P.read_url(props.get("URL")),
        description=P.read_text(props.get("Description")),
        location=P.read_text(props.get("Location")),
    )


def _posting_keys(posting: RawPosting) -> set[str]:
    keys = {f"fp:{fingerprint(posting)}"}
    if posting.url:
        keys.add(f"url:{normalize_url(posting.url)}")
    if posting.external_job_id:
        keys.add(
            f"external:{(posting.source_adapter or '').lower()}:"
            + f"{posting.external_job_id.strip().lower()}"
        )
    return {key for key in keys if not key.endswith(":")}


def _build_posting_date_index(
    settings: Settings,
    adapters: set[str],
    stats: PostingDateBackfillStats,
) -> dict[str, _MatchedPostingDate]:
    index: dict[str, _MatchedPostingDate] = {}
    fetch_adapters = sorted(adapter for adapter in adapters if adapter not in _NON_FETCH_ADAPTERS)
    if not fetch_adapters:
        return index
    for adapter in build_adapters(settings, only=fetch_adapters):
        try:
            postings = list(adapter.fetch())
        except Exception as exc:
            message = f"{adapter.key}: {exc}"
            stats.errors.append(message)
            log.warning("posting-date backfill: source fetch failed: %s", message)
            continue
        stats.fetched_postings += len(postings)
        for posting in postings:
            posted_at = effective_posted_at(posting)
            if posted_at is None:
                continue
            estimated = (posting.source_adapter or "").lower() in _SOURCE_ESTIMATE_ADAPTERS
            reason = (
                f"{posting.source_adapter} {'estimated' if estimated else 'provided'} posted date"
            )
            matched = _MatchedPostingDate(
                posted_at=posted_at,
                estimated=estimated,
                reason=reason,
            )
            for key in _posting_keys(posting):
                index.setdefault(key, matched)
    return index


def _match_date(page: dict, index: dict[str, _MatchedPostingDate]) -> _MatchedPostingDate | None:
    posting = _page_posting(page)
    adapter = (posting.source_adapter or "").lower()
    if adapter == "manual":
        props = page.get("properties") or {}
        discovered = P.read_date(props.get("Discovered At"))
        if discovered:
            try:
                parsed = datetime.fromisoformat(discovered.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return _MatchedPostingDate(
                posted_at=parsed,
                estimated=True,
                reason="manual capture discovered date estimate",
            )
    for key in _posting_keys(posting):
        if key in index:
            return index[key]
    return None


def _active_pages(notion: NotionClient, data_source_id: str) -> list[dict]:
    pages: list[dict] = []
    seen: set[str] = set()
    for status in sorted(ACTIVE_VERIFICATION_STATUSES):
        for page in notion.query_data_source(
            data_source_id,
            filter_={"property": "Status", "select": {"equals": status}},
        ):
            page_id = page.get("id", "")
            if page_id and page_id in seen:
                continue
            if page_id:
                seen.add(page_id)
            pages.append(page)
    return pages


def _cancel_unsubmitted_applications(
    notion: NotionClient,
    application_ids: list[str],
    *,
    dry_run: bool,
    stats: PostingDateBackfillStats,
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
            log.warning("posting-date backfill: failed to cancel %s", message)
    return cancelled


def _attempt_count(store: Store, page_id: str) -> int:
    if not page_id or not hasattr(store, "posting_date_attempt_count"):
        return 0
    try:
        return store.posting_date_attempt_count(page_id)
    except Exception as exc:
        log.warning("posting-date backfill: could not read attempt count for %s: %s", page_id, exc)
        return 0


def _record_attempt(
    store: Store,
    *,
    page_id: str,
    source_adapter: str,
    url: str,
    status: str,
    reason: str,
    resolved: bool,
    dry_run: bool,
) -> int:
    prior = _attempt_count(store, page_id)
    if dry_run or not page_id or not hasattr(store, "record_posting_date_attempt"):
        return prior + 1
    return store.record_posting_date_attempt(
        opportunity_page_id=page_id,
        source_adapter=source_adapter,
        url=url,
        status=status,
        reason=reason,
        resolved=resolved,
    )


def _update_page(
    notion: NotionClient,
    page_id: str,
    properties: dict,
    *,
    dry_run: bool,
    stats: PostingDateBackfillStats,
) -> None:
    if dry_run:
        stats.dry_run_updates += 1
        return
    notion.update_page(page_id, properties)
    stats.updated += 1


def _close_page(
    notion: NotionClient,
    page: dict,
    *,
    reason: str,
    dry_run: bool,
    stats: PostingDateBackfillStats,
) -> None:
    page_id = page.get("id", "")
    props = page.get("properties") or {}
    _cancel_unsubmitted_applications(
        notion,
        P.read_relation_ids(props.get("Application")),
        dry_run=dry_run,
        stats=stats,
    )
    update = {
        "Status": P.select("Closed"),
        "Last Error": P.rich_text(reason[:1900]),
    }
    _update_page(notion, page_id, update, dry_run=dry_run, stats=stats)
    stats.closed += 1
    if page_id:
        stats.closed_page_ids.add(page_id)


def run_posting_date_backfill(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    dry_run: bool,
    limit: int | None = None,
    force: bool = False,
    max_attempts: int = 3,
    pages: list[dict] | None = None,
) -> PostingDateBackfillStats:
    """Fill missing posted dates and retire unconfirmable risky rows."""

    stats = PostingDateBackfillStats()
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats

    pages = list(pages) if pages is not None else _active_pages(notion, ds_id)
    queue: list[dict] = []
    needed_adapters: set[str] = set()
    for page in pages:
        if limit is not None and len(queue) >= max(0, limit):
            break
        props = page.get("properties") or {}
        page_id = page.get("id", "")
        status = read_status(props.get("Status"))
        adapter = P.read_select(props.get("Source Adapter")).lower()
        if status in PROTECTED_POSTED_DATE_STATUSES:
            stats.skipped_protected += 1
            continue
        if read_posted_date(props) is not None and not force:
            stats.skipped_existing += 1
            continue
        if not force and _attempt_count(store, page_id) >= max(1, max_attempts):
            stats.skipped_attempt_limit += 1
            continue
        queue.append(page)
        needed_adapters.add(adapter)

    stats.candidates = len(queue)
    index = _build_posting_date_index(settings, needed_adapters, stats)

    for page in queue:
        props = page.get("properties") or {}
        page_id = page.get("id", "")
        adapter = P.read_select(props.get("Source Adapter")).lower()
        url = P.read_url(props.get("URL"))
        existing = read_posted_date(props)
        matched = _match_date(page, index)
        if matched is not None and not same_posted_day(existing, matched.posted_at):
            update = {
                DATE_JOB_WAS_POSTED: posting_date_property(matched.posted_at),
                "Updated At": P.date_property(datetime.now(UTC)),
                "Last Error": P.rich_text(""),
            }
            _update_page(notion, page_id, update, dry_run=dry_run, stats=stats)
            _record_attempt(
                store,
                page_id=page_id,
                source_adapter=adapter,
                url=url,
                status="date_found",
                reason=matched.reason,
                resolved=True,
                dry_run=dry_run,
            )
            if matched.estimated:
                stats.estimated_dates += 1
            else:
                stats.exact_dates += 1
            if page_id:
                stats.updated_page_ids.append(page_id)
            continue
        if matched is not None:
            _record_attempt(
                store,
                page_id=page_id,
                source_adapter=adapter,
                url=url,
                status="date_already_current",
                reason=matched.reason,
                resolved=True,
                dry_run=dry_run,
            )
            continue

        context = VerificationContext.from_page(page)
        result = verify_posting_context(context, settings=settings)
        if result.closed:
            _record_attempt(
                store,
                page_id=page_id,
                source_adapter=adapter,
                url=url,
                status="closed",
                reason=result.reason,
                resolved=True,
                dry_run=dry_run,
            )
            _close_page(
                notion,
                page,
                reason=f"posting unavailable while backfilling posted date: {result.reason}",
                dry_run=dry_run,
                stats=stats,
            )
            continue

        _record_attempt(
            store,
            page_id=page_id,
            source_adapter=adapter,
            url=url,
            status="unconfirmed",
            reason=result.reason,
            resolved=False,
            dry_run=dry_run,
        )
        stats.unknown += 1
        if page_id:
            _update_page(
                notion,
                page_id,
                {"Next Action": P.rich_text("Review posting date and availability")},
                dry_run=dry_run,
                stats=stats,
            )
    log.info("posting-date backfill: %s", stats)
    return stats
