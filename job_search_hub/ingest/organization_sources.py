"""Organization-derived source targets.

Active Notion Organizations can contribute structured ATS targets and direct
career-page discovery targets.  Unknown pages are no longer synthetic/manual
watchlist-only sources; the evidence-led adapter decides whether they expose
public jobs and records a clear blocked/unsupported reason when they do not.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient

ATS_ADAPTERS = {"greenhouse", "lever", "ashby", "recruitee", "smartrecruiters"}
log = get_logger(__name__)


@dataclass(frozen=True)
class OrganizationSourceTarget:
    organization_id: str
    organization_name: str
    careers_url: str
    adapter_key: str
    target: str
    check_cadence: str = ""


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = (value or "").strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def parse_organization_careers_url(
    url: str,
    *,
    organization_name: str = "",
) -> tuple[str, str]:
    """Return ``(adapter_key, target)`` for a Careers URL.

    Unknown or plain career pages return ``("direct_discovery", url)``.
    """
    clean = (url or "").strip()
    if not clean:
        return "", ""
    parsed = urlsplit(clean if "://" in clean else f"https://{clean}")
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and path_parts:
        return "greenhouse", path_parts[0]
    if host.endswith(".greenhouse.io") and path_parts:
        return "greenhouse", path_parts[0]

    if host == "jobs.lever.co" and path_parts:
        return "lever", path_parts[0]

    if host == "jobs.ashbyhq.com" and path_parts:
        return "ashby", path_parts[0]

    if host.endswith(".recruitee.com"):
        return "recruitee", host.split(".", 1)[0]

    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"} and path_parts:
        return "smartrecruiters", path_parts[0]

    if "smartrecruiters.com" in host and path_parts:
        return "smartrecruiters", path_parts[0]

    return "direct_discovery", clean


def read_organization_source_targets(
    notion: NotionClient,
    settings: Settings,
) -> list[OrganizationSourceTarget]:
    ds_id = settings.notion.data_sources.get("organizations", "")
    if not ds_id:
        return []

    targets: list[OrganizationSourceTarget] = []
    filt = {"property": "Active", "checkbox": {"equals": True}}
    for page in notion.query_data_source(ds_id, filter_=filt):
        props = page.get("properties") or {}
        url = P.read_url(props.get("Careers URL"))
        name = P.read_title(props.get("Organization"))
        if not url:
            continue
        adapter_key, target = parse_organization_careers_url(
            url,
            organization_name=name,
        )
        if not adapter_key or not target:
            continue
        targets.append(
            OrganizationSourceTarget(
                organization_id=page.get("id", ""),
                organization_name=name,
                careers_url=url,
                adapter_key=adapter_key,
                target=target,
                check_cadence=P.read_select(props.get("Check Cadence")),
            )
        )
    return targets


def _source_run_at(row) -> datetime | None:
    if not row:
        return None
    try:
        value = row["occurred_at"]
    except (KeyError, TypeError):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def due_organization_source_targets(
    targets: list[OrganizationSourceTarget],
    store: Store,
    *,
    now: datetime | None = None,
) -> list[OrganizationSourceTarget]:
    """Apply Notion's Daily/Weekly cadence to page-based discovery targets."""
    current = now or datetime.now(UTC)
    due: list[OrganizationSourceTarget] = []
    for target in targets:
        if target.adapter_key not in {"watchlist", "direct_discovery"}:
            due.append(target)
            continue
        cadence = target.check_cadence.casefold()
        interval = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}.get(cadence)
        if interval is None and target.adapter_key == "direct_discovery":
            # Direct discovery is read-only and has its own backoff/cache, so
            # active employer careers URLs become useful automatically even if
            # they predate the old manual Check Cadence convention.
            interval = timedelta(days=1)
        if interval is None:
            log.info(
                "organization source %s remains manual; Check Cadence=%s",
                target.organization_name,
                target.check_cadence or "Manual",
            )
            continue
        last = _source_run_at(
            store.latest_source_run(source_key=target.adapter_key, target=target.careers_url)
        )
        if last is None or current - last >= interval:
            due.append(target)
    return due


def settings_with_organization_targets(
    settings: Settings,
    targets: list[OrganizationSourceTarget],
) -> Settings:
    creds = settings.adapters
    by_adapter: dict[str, list[str]] = {
        "greenhouse": list(creds.greenhouse_boards),
        "lever": list(creds.lever_companies),
        "ashby": list(creds.ashby_boards),
        "recruitee": list(creds.recruitee_companies),
        "smartrecruiters": list(creds.smartrecruiters_companies),
        "watchlist": list(creds.watchlist_urls),
    }
    for target in targets:
        if target.adapter_key in by_adapter:
            by_adapter[target.adapter_key].append(target.target)

    adapters = replace(
        creds,
        greenhouse_boards=_dedupe(by_adapter["greenhouse"]),
        lever_companies=_dedupe(by_adapter["lever"]),
        ashby_boards=_dedupe(by_adapter["ashby"]),
        recruitee_companies=_dedupe(by_adapter["recruitee"]),
        smartrecruiters_companies=_dedupe(by_adapter["smartrecruiters"]),
        watchlist_urls=_dedupe(by_adapter["watchlist"]),
    )
    return replace(settings, adapters=adapters)
