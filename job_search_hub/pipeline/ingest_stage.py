"""Ingest stage: pull from adapters, normalize, dedupe, write to Notion.

For each qualified unique posting we either create a new `Opportunities` page
(status `Fetched`) or update last-seen on the existing one. The
model-scoring step is deliberately separate so a degraded LM Studio doesn't
block ingest.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from job_search_hub.browser_runtime import BrowserRuntime
from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.events import emit_event
from job_search_hub.ingest.base import RawPosting
from job_search_hub.ingest.organization_sources import (
    due_organization_source_targets,
    read_organization_source_targets,
)
from job_search_hub.ingest.registry import build_adapters
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.notion.topic_schema import repair_legacy_opportunity_topic_schema
from job_search_hub.notion.usage import validate_written_properties
from job_search_hub.pipeline.canonical_resolution import (
    CanonicalResolution,
    canonical_identity,
    clean_tracking_url,
    resolve_posting,
    source_quality,
)
from job_search_hub.pipeline.discovery import (
    GlobalIntakeState,
    admit_global_posting,
    global_admission_reason,
    select_postings,
)
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.normalize import (
    below_hard_floor,
    description_hash,
    excluded_by_keywords,
    fingerprint,
    short_description,
)
from job_search_hub.pipeline.organizations import ensure_organization
from job_search_hub.pipeline.posting_dates import (
    DATE_JOB_WAS_POSTED,
    effective_posted_at,
    posting_date_property,
    read_posted_date,
    same_posted_day,
)
from job_search_hub.pipeline.posting_verification import (
    ACTIVE_VERIFICATION_STATUSES,
    POSTING_CHECKED_AT,
    VerificationContext,
    posting_check_is_recent,
    verify_posting_context,
    verify_posting_url,
)
from job_search_hub.pipeline.progress import (
    StagePhase,
    report_item_failed,
    report_stage_complete,
    report_stage_detail,
    report_stage_heartbeat,
    report_stage_progress,
    report_stage_start,
)
from job_search_hub.pipeline.search_terms import annotate_posting

log = get_logger(__name__)


@dataclass
class IngestStats:
    sources_checked: int = 0
    postings_seen: int = 0
    duplicates: int = 0
    duplicates_removed: int = 0
    new_opportunities: int = 0
    refreshed: int = 0
    hard_excluded: int = 0
    non_english: int = 0
    excluded_title: int = 0
    irrelevant: int = 0
    filter_duplicates: int = 0
    cap_overflow: int = 0
    company_quota: int = 0
    profile_quota: int = 0
    cross_source_duplicates: int = 0
    locally_qualified: int = 0
    canonicalized: int = 0
    known_before_admission: int = 0
    unresolved_supplemental: int = 0
    supplemental_link_checks: int = 0
    supplemental_hosts_blocked: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class DedupeStats:
    pages_scanned: int = 0
    duplicate_pages: int = 0
    fingerprints_updated: int = 0


_HOST_BLOCKING_HTTP_STATUSES = {401, 403, 429}
_HOST_BLOCKING_REASONS = {"account_or_paywall"}


def _url_host(url: str) -> str:
    """Return a normalized host without raising for a malformed source URL."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


@dataclass
class SupplementalResolutionStats:
    """Bound outbound verification after a source host refuses automated access."""

    network_checks: int = 0
    verified: int = 0
    retained_as_supplemental: int = 0
    skipped_after_host_block: int = 0
    reason_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    blocked_hosts: dict[str, tuple[str, int | None]] = field(default_factory=dict)

    def resolve(self, posting: RawPosting, *, settings: Settings):
        discovery_url = clean_tracking_url(posting.url)
        source_host = _url_host(discovery_url)
        host_block = self.blocked_hosts.get(source_host)
        if host_block:
            reason, http_status = host_block
            result = CanonicalResolution(
                accepted=False,
                canonical_url="",
                discovery_url=discovery_url,
                resolution_method="host_circuit_breaker",
                source_quality=source_quality(posting.source_adapter, discovery_url),
                reason=f"blocked_{reason}",
                http_status=http_status,
            )
            self.skipped_after_host_block += 1
            self._record(result)
            return result, ""

        self.network_checks += 1
        result = resolve_posting(
            posting,
            timeout=settings.direct_discovery.timeout_seconds,
            max_redirects=settings.direct_discovery.max_redirects,
            max_response_bytes=settings.direct_discovery.max_response_bytes,
        )
        self._record(result)

        should_block = (
            result.http_status in _HOST_BLOCKING_HTTP_STATUSES
            or result.reason in _HOST_BLOCKING_REASONS
        )
        if not should_block:
            return result, ""
        # The final hop is the server that refused the request. This is more
        # precise than always treating the discovery/aggregator host as bad.
        refusing_host = _url_host((result.redirect_chain or [discovery_url])[-1])
        if refusing_host and refusing_host not in self.blocked_hosts:
            self.blocked_hosts[refusing_host] = (result.reason, result.http_status)
            return result, refusing_host
        return result, ""

    def _record(self, result: CanonicalResolution) -> None:
        if result.accepted:
            self.verified += 1
            return
        self.retained_as_supplemental += 1
        self.reason_counts[result.reason or "unresolved"] += 1

    def summary(self) -> str:
        reasons = (
            ", ".join(f"{reason}={count}" for reason, count in sorted(self.reason_counts.items()))
            or "none"
        )
        blocked = (
            ", ".join(
                f"{host} ({'HTTP ' + str(status) if status else reason})"
                for host, (reason, status) in sorted(self.blocked_hosts.items())
            )
            or "none"
        )
        return (
            f"requests={self.network_checks} verified={self.verified} "
            f"supplemental_only={self.retained_as_supplemental} "
            f"skipped_after_block={self.skipped_after_host_block} "
            f"reasons=[{reasons}] blocked_hosts=[{blocked}]"
        )

    def as_payload(self) -> dict:
        return {
            "network_checks": self.network_checks,
            "verified": self.verified,
            "supplemental_only": self.retained_as_supplemental,
            "skipped_after_host_block": self.skipped_after_host_block,
            "reasons": dict(self.reason_counts),
            "blocked_hosts": {
                host: {"reason": reason, "http_status": status}
                for host, (reason, status) in self.blocked_hosts.items()
            },
        }


def _assert_valid_opportunity_write(properties: dict) -> None:
    """Fail before a page write could create an unbounded Notion topic option."""
    issues = validate_written_properties("opportunities", properties)
    if issues:
        details = "; ".join(f"{issue.property_name}: {issue.message}" for issue in issues)
        raise ValueError(f"Invalid Opportunities write payload: {details}")


def _record_watchlist_health(store: Store, adapter, *, dry_run: bool) -> None:
    """Persist individual manual-page observations in the source-run ledger."""
    for url, health in getattr(adapter, "source_health", {}).items():
        previous = store.latest_source_run(source_key="watchlist", target=url)
        previous_hash = ""
        if previous and previous["payload_json"]:
            try:
                previous_hash = str(json.loads(previous["payload_json"]).get("content_hash") or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_hash = ""
        if health.status == "healthy":
            if not previous:
                observation = "new"
            elif previous_hash == health.content_hash:
                observation = "unchanged"
            else:
                observation = "changed"
        else:
            observation = health.status
        payload = {
            "content_hash": health.content_hash,
            "observation": observation,
            "manual_capture_required": True,
            "dry_run": dry_run,
        }
        store.record_source_run(
            source_key="watchlist",
            adapter_key="watchlist",
            target=url,
            status=observation,
            configured=True,
            http_status=health.http_status,
            error=health.error,
            payload=payload,
        )
        log.info(
            "watchlist: %s %s http_status=%s manual_capture_required",
            observation,
            url,
            health.http_status,
        )


def _record_direct_discovery_health(store: Store, adapter, *, dry_run: bool) -> None:
    """Make every target's result visible without making it a pipeline error."""
    for target, result in getattr(adapter, "source_health", {}).items():
        store.record_source_run(
            source_key="direct_discovery",
            adapter_key="direct_discovery",
            target=target,
            status=result.status,
            configured=True,
            postings_seen=len(result.postings),
            http_status=result.http_status,
            error=result.error,
            payload={"strategy": result.strategy, "evidence": result.evidence, "dry_run": dry_run},
        )


def _prepare_canonical_posting(
    posting: RawPosting,
    *,
    settings: Settings,
    store: Store,
    resolution_stats: SupplementalResolutionStats | None = None,
) -> RawPosting | None:
    """Resolve supplemental links before selection and preserve all provenance."""
    original_url = posting.url
    supplemental = posting.source_adapter in {
        "adzuna",
        "jooble",
        "remotive",
        "arbeitnow",
        "rss",
        "imap",
    }
    # Direct APIs already publish detail URLs; still attach a canonical identity
    # without needlessly re-fetching every source record before verification.
    if not supplemental and posting.source_adapter != "direct_discovery":
        canonical = clean_tracking_url(posting.url)
        posting.url = canonical
        posting.extras = {
            **(posting.extras or {}),
            "original_discovery_url": original_url,
            "canonical_url": canonical,
            "canonical_identity": canonical_identity(posting, canonical),
            "source_quality": source_quality(posting.source_adapter, canonical),
            "resolution_method": "direct_source_url",
        }
        store.record_posting_provenance(
            identity_key=posting.extras["canonical_identity"],
            discovery_url=original_url,
            canonical_url=canonical,
            source_adapter=posting.source_adapter,
            source_quality=posting.extras["source_quality"],
            resolution_method="direct_source_url",
            verification_status="pending",
        )
        return posting
    if resolution_stats is None:
        result = resolve_posting(
            posting,
            timeout=settings.direct_discovery.timeout_seconds,
            max_redirects=settings.direct_discovery.max_redirects,
            max_response_bytes=settings.direct_discovery.max_response_bytes,
        )
        newly_blocked_host = ""
    else:
        result, newly_blocked_host = resolution_stats.resolve(posting, settings=settings)
    if newly_blocked_host:
        log.warning(
            "ingest: adapter=%s link verification blocked by host=%s reason=%s; "
            "no further requests to that host will be made this run",
            posting.source_adapter,
            newly_blocked_host,
            result.reason,
        )
    if not result.accepted:
        identity = f"unverified:{posting.source_adapter}:{clean_tracking_url(original_url)}"
        store.record_posting_provenance(
            identity_key=identity,
            discovery_url=original_url,
            canonical_url="",
            source_adapter=posting.source_adapter,
            source_quality=result.source_quality,
            resolution_method=result.resolution_method or "unresolved",
            verification_status=result.reason,
            redirect_chain=result.redirect_chain,
        )
        return None
    posting.url = result.canonical_url
    posting.extras = {
        **(posting.extras or {}),
        "original_discovery_url": result.discovery_url,
        "canonical_url": result.canonical_url,
        "canonical_identity": canonical_identity(posting, result.canonical_url),
        "source_quality": result.source_quality,
        "resolution_method": result.resolution_method,
        "redirect_chain": result.redirect_chain,
    }
    store.record_posting_provenance(
        identity_key=posting.extras["canonical_identity"],
        discovery_url=result.discovery_url,
        canonical_url=result.canonical_url,
        source_adapter=posting.source_adapter,
        source_quality=result.source_quality,
        resolution_method=result.resolution_method,
        verification_status="verified",
        redirect_chain=result.redirect_chain,
    )
    return posting


def _build_properties(
    posting: RawPosting,
    *,
    status: str = "Fetched",
    posting_checked_at: datetime | None = None,
    organization_id: str = "",
) -> dict:
    excluded, reason = excluded_by_keywords(posting)
    posted_at = effective_posted_at(posting)
    props: dict = {
        "Opportunity": P.title(f"{posting.company} - {posting.role_title}".strip(" -")),
        "Company": P.rich_text(posting.company),
        "Role Title": P.rich_text(posting.role_title),
        "URL": P.url(posting.url),
        "Source": P.select(posting.source),
        "Source Adapter": P.select(posting.source_adapter),
        "External Job ID": P.rich_text(posting.external_job_id),
        "Fingerprint": P.rich_text(fingerprint(posting)),
        "Description": P.rich_text(short_description(posting.description)),
        "Discovered At": P.date_property(posting.discovered_at),
        DATE_JOB_WAS_POSTED: posting_date_property(posted_at),
        "Updated At": P.date_property(datetime.now(UTC)),
        POSTING_CHECKED_AT: P.date_property(posting_checked_at),
        "Location": P.rich_text(posting.location),
        "Remote Status": P.select(posting.remote_status or "Unknown"),
        "Schedule": P.select(posting.schedule or "Unknown"),
        "Employment Type": P.multi_select(posting.employment_type or []),
        "Compensation Text": P.rich_text(posting.compensation_text),
        "Compensation Min": P.number(posting.compensation_min),
        "Compensation Max": P.number(posting.compensation_max),
        "Compensation Unit": P.select(posting.compensation_unit or "Unknown"),
        "Hard Exclusion": P.checkbox(excluded),
        "Exclusion Reason": P.rich_text(reason),
        "Top Matches": P.rich_text(str((posting.extras or {}).get("top_matches") or "")),
        "Status": P.select(status),
        "My Decision": P.select("Undecided"),
    }
    if organization_id:
        props["Organization"] = P.relation([organization_id])
    return props


def _posting_verification_payload(posting: RawPosting, result) -> dict:
    return {
        "result": result.status,
        "reason": result.reason,
        "url": posting.url,
        "http_status": result.http_status,
        "final_url": result.final_url,
    }


_STATUS_PRIORITY = {
    "Offer": 110,
    "Interviewing": 100,
    "Submitted": 90,
    "Shortlisted": 80,
    "Review": 70,
    "Needs Input": 65,
    "Scoring": 30,
    "Fetched": 25,
    "Rejected": 20,
    "Closed": 15,
    "Error": 5,
}


def _notion_page_posting(page: dict) -> RawPosting:
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


def _survivor_score(page: dict) -> tuple[int, int, float]:
    props = page.get("properties") or {}
    status_score = _STATUS_PRIORITY.get(read_status(props.get("Status")), 0)
    populated = sum(
        bool(
            P.read_text(prop)
            or P.read_title(prop)
            or P.read_url(prop)
            or P.read_number(prop) is not None
            or P.read_select(prop)
            or read_status(prop)
            or P.read_relation_ids(prop)
        )
        for prop in props.values()
    )
    created = page.get("created_time") or "9999-12-31T23:59:59+00:00"
    try:
        created_timestamp = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        created_timestamp = float("inf")
    return status_score, populated, -created_timestamp


def reconcile_notion_opportunities(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    dry_run: bool,
) -> DedupeStats:
    """Make Notion canonical before ingest and trash duplicate pages."""
    stats = DedupeStats()
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats

    groups: dict[str, list[tuple[dict, RawPosting]]] = defaultdict(list)
    seen_page_ids: set[str] = set()
    for page in notion.query_data_source(
        ds_id,
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
    ):
        page_id = page.get("id", "")
        if page_id and page_id in seen_page_ids:
            continue
        if page_id:
            seen_page_ids.add(page_id)
        if page.get("in_trash") or page.get("archived"):
            continue
        posting = _notion_page_posting(page)
        if not posting.company and not posting.role_title and not posting.url:
            log.warning(
                "notion dedupe: skipping incomplete opportunity page id=%s",
                page.get("id", ""),
            )
            continue
        fp = fingerprint(posting)
        groups[fp].append((page, posting))
        stats.pages_scanned += 1

    for fp, rows in groups.items():
        survivor_page, survivor = max(rows, key=lambda row: _survivor_score(row[0]))
        survivor_id = survivor_page.get("id", "")
        duplicate_ids = [
            page.get("id", "") for page, _posting in rows if page.get("id", "") != survivor_id
        ]
        stats.duplicate_pages += len(duplicate_ids)

        stored_fp = P.read_text((survivor_page.get("properties") or {}).get("Fingerprint"))
        if stored_fp != fp:
            stats.fingerprints_updated += 1
            if not dry_run and survivor_id:
                notion.update_page(survivor_id, {"Fingerprint": P.rich_text(fp)})

        if dry_run:
            continue

        for duplicate_id in duplicate_ids:
            notion.archive_page(duplicate_id)
            store.audit(
                actor="ingest",
                action="remove_duplicate_opportunity",
                target_kind="opportunity",
                target_id=duplicate_id,
                payload={"fingerprint": fp, "survivor_page_id": survivor_id},
            )
        store.remove_opportunity_cache_pages(duplicate_ids)
        store.remove_stale_opportunity_fingerprints(
            notion_page_id=survivor_id,
            fingerprint=fp,
        )
        store.upsert_opportunity_cache(
            fingerprint=fp,
            notion_page_id=survivor_id,
            source=survivor.source or "Unknown",
            source_adapter=survivor.source_adapter or "notion",
            external_job_id=survivor.external_job_id,
            company=survivor.company,
            role_title=survivor.role_title,
            url=survivor.url,
            description_hash=description_hash(survivor.description),
        )

    if not dry_run:
        store.prune_opportunity_cache(set(groups))

    log.info(
        "notion dedupe: scanned=%d duplicates=%d fingerprints_updated=%d dry_run=%s",
        stats.pages_scanned,
        stats.duplicate_pages,
        stats.fingerprints_updated,
        dry_run,
    )
    return stats


def _find_notion_opportunity(
    notion: NotionClient,
    *,
    data_source_id: str,
    fp: str,
) -> dict | None:
    filter_ = {"property": "Fingerprint", "rich_text": {"equals": fp}}
    for page in notion.query_data_source(data_source_id, filter_=filter_):
        if not page.get("in_trash") and not page.get("archived"):
            return page
    return None


def _posting_is_known(
    posting: RawPosting,
    *,
    settings: Settings,
    notion: NotionClient,
    store: Store,
    dry_run: bool,
) -> bool:
    """Populate the local cache when necessary and report durable duplicates."""
    fp = fingerprint(posting)
    if store.find_opportunity_by_fingerprint(fp) is not None:
        return True
    if dry_run:
        return False
    page = _find_notion_opportunity(
        notion,
        data_source_id=settings.notion.data_sources["opportunities"],
        fp=fp,
    )
    if page is None:
        return False
    store.upsert_opportunity_cache(
        fingerprint=fp,
        notion_page_id=page.get("id", ""),
        source=posting.source,
        source_adapter=posting.source_adapter,
        external_job_id=posting.external_job_id,
        company=posting.company,
        role_title=posting.role_title,
        url=posting.url,
        description_hash=description_hash(posting.description),
    )
    return True


def _ingest_posting(
    posting: RawPosting,
    *,
    settings: Settings,
    notion: NotionClient,
    store: Store,
    stats: IngestStats,
    dry_run: bool,
    organization_cache: dict[str, str] | None = None,
    browser_runtime: BrowserRuntime | None = None,
) -> None:
    fp = fingerprint(posting)
    cached = store.find_opportunity_by_fingerprint(fp)
    cached_page: dict | None = None
    if cached is None and not dry_run:
        ds_id = settings.notion.data_sources["opportunities"]
        existing_page = _find_notion_opportunity(
            notion,
            data_source_id=ds_id,
            fp=fp,
        )
        if existing_page:
            page_id = existing_page.get("id", "")
            store.upsert_opportunity_cache(
                fingerprint=fp,
                notion_page_id=page_id,
                source=posting.source,
                source_adapter=posting.source_adapter,
                external_job_id=posting.external_job_id,
                company=posting.company,
                role_title=posting.role_title,
                url=posting.url,
                description_hash=description_hash(posting.description),
            )
            cached = store.find_opportunity_by_fingerprint(fp)
            cached_page = existing_page

    excluded, _ = excluded_by_keywords(posting, getattr(settings, "excluded_terms", ()))
    if not excluded:
        floor_excl, _ = below_hard_floor(
            posting,
            annual_floor_usd=settings.hard_floor_annual_usd,
            hourly_floor_usd=settings.hard_floor_hourly_usd,
        )
        if floor_excl:
            excluded = True

    verification = None
    if cached is None:
        should_check = True
    elif cached_page is not None:
        props = cached_page.get("properties") or {}
        should_check = read_status(
            props.get("Status")
        ) in ACTIVE_VERIFICATION_STATUSES and not posting_check_is_recent(cached_page)
    else:
        page_id = cached["notion_page_id"]
        should_check = False
        if page_id and not dry_run:
            try:
                cached_page = notion.get_page(page_id)
                props = cached_page.get("properties") or {}
                should_check = read_status(
                    props.get("Status")
                ) in ACTIVE_VERIFICATION_STATUSES and not posting_check_is_recent(cached_page)
            except Exception as exc:
                log.warning("ingest: could not load cached opportunity %s: %s", page_id, exc)

    if should_check:
        if posting.extras.get("rendered"):
            verification = verify_posting_context(
                VerificationContext(
                    url=posting.url,
                    role_title=posting.role_title,
                    company=posting.company,
                    external_job_id=posting.external_job_id,
                    source_adapter=posting.source_adapter,
                ),
                settings=settings,
                browser_fallback="always",
                browser_runtime=browser_runtime,
            )
        else:
            verification = verify_posting_url(
                posting.url,
                role_title=posting.role_title,
                company=posting.company,
                external_job_id=posting.external_job_id,
                source_adapter=posting.source_adapter,
            )
        store.audit(
            actor="ingest",
            action="verify_posting",
            target_kind="opportunity",
            target_id=(cached["notion_page_id"] if cached is not None else fp),
            dry_run=dry_run,
            payload=_posting_verification_payload(posting, verification),
        )

    closed_by_verification = bool(verification and verification.closed)
    initial_status = "Closed" if closed_by_verification else "Rejected" if excluded else "Fetched"
    if excluded:
        stats.hard_excluded += 1

    organization_id = ensure_organization(
        notion,
        settings,
        company=posting.company,
        source_type=posting.source_adapter,
        dry_run=dry_run,
        cache=organization_cache,
    )

    if cached is None:
        if stats.new_opportunities >= settings.discovery.max_new_per_run:
            stats.cap_overflow += 1
            return
        stats.new_opportunities += 1
        if dry_run:
            log.info(
                "[dry-run] would create opportunity company=%s role=%s",
                posting.company,
                posting.role_title,
            )
            return
        if not store.claim_idempotency("create_opportunity", fp, ttl_seconds=300):
            stats.new_opportunities -= 1
            stats.duplicates += 1
            log.info(
                "ingest: duplicate create already in progress company=%s role=%s",
                posting.company,
                posting.role_title,
            )
            return
        properties = _build_properties(
            posting,
            status=initial_status,
            posting_checked_at=(verification.checked_at if verification is not None else None),
            organization_id=organization_id,
        )
        _assert_valid_opportunity_write(properties)
        page = notion.create_page(
            parent={"data_source_id": settings.notion.data_sources["opportunities"]},
            properties=properties,
        )
        page_id = page.get("id", "")
        store.upsert_opportunity_cache(
            fingerprint=fp,
            notion_page_id=page_id,
            source=posting.source,
            source_adapter=posting.source_adapter,
            external_job_id=posting.external_job_id,
            company=posting.company,
            role_title=posting.role_title,
            url=posting.url,
            description_hash=description_hash(posting.description),
        )
        store.audit(
            actor="ingest",
            action="create_opportunity",
            target_kind="opportunity",
            target_id=page_id,
            payload={
                "fingerprint": fp,
                "company": posting.company,
                "role": posting.role_title,
                "excluded": excluded,
            },
        )
    else:
        stats.duplicates += 1
        new_hash = description_hash(posting.description)
        page_id = cached["notion_page_id"]
        posted_at = effective_posted_at(posting)
        update: dict = {}
        if cached["description_hash"] != new_hash:
            update["Description"] = P.rich_text(short_description(posting.description))
            update["Updated At"] = P.date_property(datetime.now(UTC))
        if posted_at is not None:
            existing_posted = None
            if cached_page is not None:
                existing_posted = read_posted_date(cached_page.get("properties") or {})
            if not same_posted_day(existing_posted, posted_at):
                update[DATE_JOB_WAS_POSTED] = posting_date_property(posted_at)
                update["Updated At"] = P.date_property(datetime.now(UTC))
        if verification is not None:
            update[POSTING_CHECKED_AT] = P.date_property(verification.checked_at)
            if verification.closed:
                update["Status"] = P.select("Closed")
        if organization_id and cached_page is not None:
            existing_org = P.read_relation_ids(
                (cached_page.get("properties") or {}).get("Organization")
            )
            if not existing_org:
                update["Organization"] = P.relation([organization_id])
        if update and page_id:
            _assert_valid_opportunity_write(update)
            if not dry_run:
                notion.update_page(page_id, update)
            stats.refreshed += 1
        store.upsert_opportunity_cache(
            fingerprint=fp,
            source=posting.source,
            source_adapter=posting.source_adapter,
            external_job_id=posting.external_job_id,
            company=posting.company,
            role_title=posting.role_title,
            url=posting.url,
            description_hash=new_hash,
        )


def run_ingest(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    dry_run: bool,
    only_adapters: list[str] | None = None,
    live: bool = False,
    browser_runtime: BrowserRuntime | None = None,
) -> IngestStats:
    stats = IngestStats()
    topic_schema_repair = repair_legacy_opportunity_topic_schema(
        settings,
        notion=notion,
        dry_run=dry_run,
    )
    if topic_schema_repair.changed:
        action = "would repair" if dry_run else "repaired"
        log.warning(
            "ingest: %s deprecated Opportunity topic schema options_removed=%d "
            "options_added=%d row_values_cleared=%d",
            action,
            topic_schema_repair.options_removed,
            topic_schema_repair.options_added,
            topic_schema_repair.rows_repaired,
        )
    organization_source_targets = due_organization_source_targets(
        read_organization_source_targets(notion, settings),
        store,
    )
    adapters = build_adapters(
        settings,
        only=only_adapters,
        organization_targets=organization_source_targets,
    )
    for adapter in adapters:
        # The reusable direct adapter keeps its strategy/health cache local to
        # SQLite; normal source adapters remain completely independent.
        if hasattr(adapter, "browser_runtime"):
            adapter.browser_runtime = browser_runtime
        if adapter.key == "direct_discovery":
            adapter.store = store
    report_stage_start(
        StagePhase.INGEST,
        len(adapters),
        current_step="fetching configured job sources",
        next_step="score the eligible opportunities",
    )
    log.info("ingest: running %d adapter(s): %s", len(adapters), [a.key for a in adapters])
    organization_cache: dict[str, str] = {}
    intake_state = GlobalIntakeState()
    for adapter_index, adapter in enumerate(adapters, start=1):
        stats.sources_checked += 1
        adapter_seen = 0
        adapter_fetched = 0
        adapter_new_before = stats.new_opportunities
        adapter_known_before_admission = 0
        adapter_global_company_quota = 0
        adapter_global_cross_source_duplicates = 0
        adapter_error = ""
        selection = None
        resolution_stats = SupplementalResolutionStats()
        if live:
            emit_event(
                settings.log_dir,
                event="adapter_start",
                stage="ingest",
                message=f"starting adapter {adapter.key}",
                payload={"adapter": adapter.key, "dry_run": dry_run},
            )
        try:
            report_stage_detail(
                StagePhase.INGEST,
                current_item=f"{adapter_index}/{len(adapters)} {adapter.key}",
                current_step="fetching postings from source",
                next_step="normalize, deduplicate, and write new rows to Notion",
            )
            with report_stage_heartbeat(
                StagePhase.INGEST,
                current_item=f"{adapter_index}/{len(adapters)} {adapter.key}",
                current_step="fetching postings from source",
                next_step="normalize, deduplicate, and write new rows to Notion",
            ):
                postings = list(adapter.fetch())
            diagnostic = getattr(adapter, "last_diagnostic", {})
            if diagnostic.get("error"):
                adapter_error = str(diagnostic["error"])
                stats.errors.append(f"{adapter.key}: {adapter_error}")
            adapter_fetched = len(postings)
            if adapter_fetched:
                report_stage_detail(
                    StagePhase.INGEST,
                    current_item=f"{adapter_index}/{len(adapters)} {adapter.key}",
                    current_step=(
                        f"fetched {adapter_fetched} posting(s); filtering and capping before "
                        "supplemental-link verification"
                    ),
                    next_step="filter verified postings and write eligible rows to Notion",
                )
            # Qualification is local and deterministic. Do it before touching
            # aggregator links so a broad search cannot turn into hundreds of
            # unnecessary requests to a third-party redirect service.
            selection = select_postings(postings, settings)
            stats.non_english += selection.stats.non_english
            stats.excluded_title += selection.stats.excluded_title
            stats.irrelevant += selection.stats.irrelevant
            stats.filter_duplicates += selection.stats.duplicates
            stats.cap_overflow += selection.stats.cap_overflow
            stats.company_quota += selection.stats.company_quota
            stats.profile_quota += selection.stats.profile_quota
            stats.cross_source_duplicates += selection.stats.cross_source_duplicates
            postings = selection.accepted
            stats.locally_qualified += len(postings)
            if adapter_fetched:
                report_stage_detail(
                    StagePhase.INGEST,
                    current_item=f"{adapter_index}/{len(adapters)} {adapter.key}",
                    current_step=(
                        f"{len(postings)} eligible posting(s) after local filtering; "
                        "resolving their supplemental links"
                    ),
                    next_step="write verified eligible rows to Notion",
                )
            canonical_postings: list[RawPosting] = []
            for posting in postings:
                prepared = _prepare_canonical_posting(
                    posting,
                    settings=settings,
                    store=store,
                    resolution_stats=resolution_stats,
                )
                if prepared is None:
                    stats.unresolved_supplemental += 1
                    continue
                canonical_postings.append(prepared)
            postings = canonical_postings
            stats.canonicalized += len(postings)
            adapter_seen = len(postings)
            stats.postings_seen += len(postings)
            stats.supplemental_link_checks += resolution_stats.network_checks
            stats.supplemental_hosts_blocked += len(resolution_stats.blocked_hosts)
            if resolution_stats.network_checks or resolution_stats.retained_as_supplemental:
                level = log.warning if resolution_stats.blocked_hosts else log.info
                level(
                    "ingest: adapter=%s link-resolution summary | %s",
                    adapter.key,
                    resolution_stats.summary(),
                )
            report_stage_detail(
                StagePhase.INGEST,
                current_item=f"{adapter_index}/{len(adapters)} {adapter.key}",
                current_step=(
                    f"processing {len(postings)} accepted postings; rejected "
                    f"{sum(selection.stats.reasons.values())}"
                ),
                next_step="continue to the next source",
            )
            for posting_index, posting in enumerate(postings, start=1):
                posting = annotate_posting(posting, settings)
                # Global diversity is committed only after this canonical row
                # is known to be genuinely new.  Existing cache rows still
                # pass through `_ingest_posting` to refresh source metadata.
                is_known = _posting_is_known(
                    posting,
                    settings=settings,
                    notion=notion,
                    store=store,
                    dry_run=dry_run,
                )
                if is_known:
                    stats.known_before_admission += 1
                    adapter_known_before_admission += 1
                else:
                    admission_reason = global_admission_reason(posting, settings, intake_state)
                    if admission_reason:
                        if admission_reason == "company_quota":
                            stats.company_quota += 1
                            adapter_global_company_quota += 1
                        else:
                            stats.cross_source_duplicates += 1
                            adapter_global_cross_source_duplicates += 1
                        continue
                if live:
                    emit_event(
                        settings.log_dir,
                        event="posting_seen",
                        stage="ingest",
                        message=f"{adapter.key}: {posting.company} - {posting.role_title}",
                        payload={
                            "adapter": adapter.key,
                            "company": posting.company,
                            "role_title": posting.role_title,
                            "url": posting.url,
                        },
                    )
                try:
                    new_before = stats.new_opportunities
                    with report_stage_heartbeat(
                        StagePhase.INGEST,
                        current_item=(
                            f"{adapter.key} posting {posting_index}/{len(postings)}: "
                            f"{posting.company} - {posting.role_title}"
                        ),
                        current_step=(
                            "checking duplicates, verifying URL, and writing Notion "
                            "updates if needed"
                        ),
                        next_step="finish this source, then continue ingest",
                    ):
                        _ingest_posting(
                            posting,
                            settings=settings,
                            notion=notion,
                            store=store,
                            stats=stats,
                            dry_run=dry_run,
                            organization_cache=organization_cache,
                            browser_runtime=browser_runtime,
                        )
                    if not is_known and stats.new_opportunities > new_before:
                        # This is deliberately after the successful creation
                        # path.  A failed resolver/write or idempotency race
                        # cannot starve a later official board.
                        admit_global_posting(posting, settings, intake_state)
                    if posting_index == 1 or posting_index % 100 == 0:
                        report_stage_detail(
                            StagePhase.INGEST,
                            current_item=f"{adapter.key} posting {posting_index}/{len(postings)}",
                            current_step=(
                                f"Notion writes: {stats.new_opportunities} new, "
                                f"{stats.refreshed} refreshed; "
                                f"{stats.duplicates} duplicates skipped, "
                                f"{stats.duplicates_removed} existing duplicates removed"
                            ),
                            next_step="finish this source, then continue ingest",
                        )
                except Exception as exc:
                    log.exception("ingest: failed to process posting from %s: %s", adapter.key, exc)
                    stats.errors.append(f"{adapter.key}: {exc}")
                    adapter_error = str(exc)
        except Exception as exc:
            log.exception("ingest: adapter %s failed: %s", adapter.key, exc)
            stats.errors.append(f"{adapter.key}: {exc}")
            adapter_error = str(exc)
            report_item_failed(StagePhase.INGEST)
        finally:
            if adapter.key == "watchlist":
                _record_watchlist_health(store, adapter, dry_run=dry_run)
            if adapter.key == "direct_discovery":
                _record_direct_discovery_health(store, adapter, dry_run=dry_run)
            browser_diagnostic = getattr(adapter, "last_diagnostic", {})
            browser_status = str(browser_diagnostic.get("status") or "")
            source_status = (
                browser_status
                if adapter_error and browser_status
                else ("error" if adapter_error else "ok")
            )
            if not adapter_error and browser_status in {
                "access_restricted",
                "browser_unavailable",
                "http_error",
                "public_listing_not_exposed",
            }:
                source_status = browser_status
            store.record_source_run(
                source_key=adapter.key,
                adapter_key=adapter.key,
                target="ingest",
                status=source_status,
                configured=True,
                postings_seen=adapter_seen,
                error=adapter_error,
                payload={
                    "dry_run": dry_run,
                    "non_english": selection.stats.non_english if selection else 0,
                    "excluded_title": selection.stats.excluded_title if selection else 0,
                    "irrelevant": selection.stats.irrelevant if selection else 0,
                    "filter_duplicates": selection.stats.duplicates if selection else 0,
                    "cap_overflow": selection.stats.cap_overflow if selection else 0,
                    "company_quota": (
                        (selection.stats.company_quota if selection else 0)
                        + adapter_global_company_quota
                    ),
                    "profile_quota": selection.stats.profile_quota if selection else 0,
                    "cross_source_duplicates": (
                        (selection.stats.cross_source_duplicates if selection else 0)
                        + adapter_global_cross_source_duplicates
                    ),
                    "postings_fetched": adapter_fetched,
                    "locally_qualified": len(selection.accepted) if selection else 0,
                    "canonicalized": adapter_seen,
                    "canonicalization_failed": (
                        max(0, len(selection.accepted) - adapter_seen) if selection else 0
                    ),
                    "known_before_admission": adapter_known_before_admission,
                    "created": stats.new_opportunities - adapter_new_before,
                    "browser": browser_diagnostic,
                    "link_resolution": resolution_stats.as_payload(),
                },
            )
            store.audit(
                actor="ingest",
                action="source_checked",
                target_kind="source",
                target_id=adapter.key,
                dry_run=dry_run,
                payload={
                    "adapter": adapter.key,
                    "postings_fetched": adapter_fetched,
                    "postings_seen": adapter_seen,
                    "link_resolution": resolution_stats.as_payload(),
                    "status": source_status,
                    "error": adapter_error,
                },
            )
            if live:
                emit_event(
                    settings.log_dir,
                    event="adapter_finish",
                    stage="ingest",
                    message=f"finished adapter {adapter.key}: {adapter_seen} postings",
                    payload={
                        "adapter": adapter.key,
                        "postings_seen": adapter_seen,
                        "error": adapter_error,
                    },
                )
            report_stage_progress(
                StagePhase.INGEST,
                current_item=adapter.key,
                items_completed=adapter_index,
                current_step=(
                    f"source finished with {adapter_fetched} fetched, {adapter_seen} verified; "
                    f"{resolution_stats.retained_as_supplemental} supplemental-only; "
                    f"{stats.new_opportunities} total new Notion rows"
                ),
                force=True,
            )
    report_stage_complete(
        StagePhase.INGEST,
        items_completed=len(adapters),
        items_failed=sum(1 for error in stats.errors if error),
        items_total=len(adapters),
    )
    log.info("ingest: %s", stats)
    return stats
