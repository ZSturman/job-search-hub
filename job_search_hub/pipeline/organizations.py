"""Organization Notion helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from job_search_hub.config import Settings
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient

log = get_logger(__name__)

CREATE_MISSING_MODES = {"trusted", "never", "all"}
TRUSTED_CREATE_ADAPTERS = {
    "adzuna",
    "arbeitnow",
    "ashby",
    "greenhouse",
    "jooble",
    "lever",
    "recruitee",
    "remotive",
    "smartrecruiters",
    "usajobs",
}
LINK_ONLY_ADAPTERS = {"imap", "manual", "rss", "watchlist"}
_NOISY_COMPANY_PATTERNS = (
    re.compile(r"^https?://", re.I),
    re.compile(r"^www\.", re.I),
    re.compile(r"^\[[^\]]+\]"),
    re.compile(r"\bremote jobs?\b", re.I),
    re.compile(r"\bjob search\b", re.I),
    re.compile(r"\bjob board\b", re.I),
    re.compile(r"\bprogramming jobs?\b", re.I),
    re.compile(r"\bdevops sysadmin jobs?\b", re.I),
    re.compile(r"\bdesign jobs?\b", re.I),
    re.compile(r"\bmarketing jobs?\b", re.I),
)


def normalize_organization_name(name: str) -> str:
    return " ".join((name or "").strip().split()).casefold()


def _organization_title(name: str) -> str:
    return " ".join((name or "").strip().split())


def organization_is_noisy(company: str) -> tuple[bool, str]:
    title = _organization_title(company)
    if not title:
        return True, "missing company"
    if len(title) > 70:
        return True, "company label is too long"
    if "/" in title:
        return True, "company label looks like a path or URL"
    if ":" in title and title.split(":", 1)[0].casefold() in {
        "we work remotely",
        "remoteok",
        "remote ok",
        "remotive",
        "rss",
    }:
        return True, "company label is a source/category title"
    for pattern in _NOISY_COMPANY_PATTERNS:
        if pattern.search(title):
            return True, "company label is a source/category title"
    return False, ""


def create_missing_allowed(source_type: str, create_missing: str) -> bool:
    mode = create_missing if create_missing in CREATE_MISSING_MODES else "trusted"
    adapter = (source_type or "").strip().casefold()
    if mode == "all":
        return True
    if mode == "never":
        return False
    if adapter in LINK_ONLY_ADAPTERS:
        return False
    return adapter in TRUSTED_CREATE_ADAPTERS


@dataclass
class OrganizationLinkResult:
    action: str
    reason: str = ""
    company: str = ""
    organization_id: str = ""


def resolve_organization_link(
    notion: NotionClient,
    settings: Settings,
    *,
    company: str,
    source_type: str = "",
    dry_run: bool,
    create_missing: str = "trusted",
    cache: dict[str, str] | None = None,
) -> OrganizationLinkResult:
    ds_id = settings.notion.data_sources.get("organizations", "")
    title = _organization_title(company)
    key = normalize_organization_name(title)
    if not ds_id or not title:
        return OrganizationLinkResult(
            "skip_missing_company",
            "missing Organization data source or company",
            title,
        )

    noisy, reason = organization_is_noisy(title)
    if noisy:
        return OrganizationLinkResult("skip_noisy", reason, title)

    if cache is not None and key in cache:
        return OrganizationLinkResult(
            "link_existing",
            "cached existing Organization",
            title,
            cache[key],
        )

    page = find_organization(notion, settings, title)
    if page is not None:
        page_id = page.get("id", "")
        props = page.get("properties") or {}
        existing = set(P.read_multi_select(props.get("Source Types")))
        updates = {}
        if source_type and source_type not in existing:
            updates["Source Types"] = P.multi_select(sorted(existing | {source_type}))
        if page_id and updates and not dry_run:
            notion.update_page(page_id, updates)
        if cache is not None and page_id:
            cache[key] = page_id
        return OrganizationLinkResult(
            "link_existing",
            "matched existing Organization",
            title,
            page_id,
        )

    if not create_missing_allowed(source_type, create_missing):
        return OrganizationLinkResult(
            "skip_link_only_missing",
            f"source adapter {source_type or '(unknown)'} is link-existing-only",
            title,
        )

    if dry_run:
        return OrganizationLinkResult("create", "would create Organization", title)

    properties = {
        "Organization": P.title(title),
        "Source Types": P.multi_select([source_type] if source_type else []),
        "Last Checked": P.date_property(datetime.now(UTC)),
        "Active": P.checkbox(True),
    }
    page = notion.create_page(
        parent={"data_source_id": ds_id},
        properties=properties,
    )
    page_id = page.get("id", "")
    if cache is not None and page_id:
        cache[key] = page_id
    return OrganizationLinkResult("create", "created Organization", title, page_id)


def find_organization(
    notion: NotionClient,
    settings: Settings,
    name: str,
) -> dict | None:
    ds_id = settings.notion.data_sources.get("organizations", "")
    title = _organization_title(name)
    if not ds_id or not title:
        return None
    filt = {"property": "Organization", "title": {"equals": title}}
    for page in notion.query_data_source(ds_id, filter_=filt):
        if not page.get("in_trash") and not page.get("archived"):
            return page
    return None


def ensure_organization(
    notion: NotionClient,
    settings: Settings,
    *,
    company: str,
    source_type: str = "",
    dry_run: bool,
    create_missing: str = "trusted",
    cache: dict[str, str] | None = None,
) -> str:
    result = resolve_organization_link(
        notion,
        settings,
        company=company,
        source_type=source_type,
        dry_run=dry_run,
        create_missing=create_missing,
        cache=cache,
    )
    if dry_run and result.action == "create":
        log.info("[dry-run] would create Organization row company=%s", result.company)
    elif result.action.startswith("skip"):
        log.info(
            "organization skipped company=%s source=%s reason=%s",
            result.company,
            source_type,
            result.reason,
        )
    return result.organization_id


@dataclass
class OrganizationSyncStats:
    scanned: int = 0
    linked: int = 0
    created: int = 0
    would_create: int = 0
    link_existing: int = 0
    would_link_existing: int = 0
    skipped_existing: int = 0
    skipped_missing_company: int = 0
    skipped_noisy: int = 0
    skipped_link_only_missing: int = 0
    errors: list[str] = field(default_factory=list)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def add_example(self, key: str, value: str) -> None:
        bucket = self.examples.setdefault(key, [])
        if value and len(bucket) < 10:
            bucket.append(value)


def sync_opportunity_organizations(
    settings: Settings,
    *,
    notion: NotionClient,
    dry_run: bool,
    limit: int | None = None,
    create_missing: str = "trusted",
) -> OrganizationSyncStats:
    stats = OrganizationSyncStats()
    opportunity_ds = settings.notion.data_sources.get("opportunities", "")
    if not opportunity_ds or not settings.notion.data_sources.get("organizations"):
        return stats
    cache: dict[str, str] = {}
    for page in notion.query_data_source(opportunity_ds):
        if limit is not None and stats.scanned >= limit:
            break
        stats.scanned += 1
        props = page.get("properties") or {}
        page_id = page.get("id", "")
        if P.read_relation_ids(props.get("Organization")):
            stats.skipped_existing += 1
            continue
        company = P.read_text(props.get("Company"))
        if not company:
            stats.skipped_missing_company += 1
            continue
        try:
            result = resolve_organization_link(
                notion,
                settings,
                company=company,
                source_type=P.read_select(props.get("Source Adapter")),
                dry_run=dry_run,
                create_missing=create_missing,
                cache=cache,
            )
            if result.action == "create":
                if dry_run:
                    stats.would_create += 1
                    stats.add_example("would_create", result.company)
                else:
                    stats.created += 1
                    stats.add_example("created", result.company)
            elif result.action == "link_existing":
                if dry_run:
                    stats.would_link_existing += 1
                    stats.add_example("would_link_existing", result.company)
                else:
                    stats.link_existing += 1
                    stats.add_example("link_existing", result.company)
            elif result.action == "skip_noisy":
                stats.skipped_noisy += 1
                stats.add_example("skipped_noisy", f"{result.company} ({result.reason})")
                continue
            elif result.action == "skip_link_only_missing":
                stats.skipped_link_only_missing += 1
                stats.add_example(
                    "skipped_link_only_missing",
                    f"{result.company} ({result.reason})",
                )
                continue
            elif result.action == "skip_missing_company":
                stats.skipped_missing_company += 1
                continue

            if dry_run and result.action in {"create", "link_existing"}:
                stats.linked += 1
            elif result.organization_id and page_id:
                notion.update_page(page_id, {"Organization": P.relation([result.organization_id])})
                stats.linked += 1
        except Exception as exc:
            log.exception("org-sync failed for opportunity %s", page_id)
            stats.errors.append(f"{page_id or '(missing id)'}: {exc}")
    return stats
