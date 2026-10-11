"""Source catalog and configuration status helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from job_search_hub.config import Settings
from job_search_hub.storage import get_setting

_PUBLIC_BROWSER_ADAPTER_KEYS = {
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


@dataclass(frozen=True)
class SourceCatalogEntry:
    key: str
    name: str
    discovery_route: str = ""
    submission_assumption: str = ""
    implementation_note: str = ""
    adapter_key: str = ""
    implemented: bool = False
    manual_only: bool = False
    required_env: tuple[str, ...] = ()
    target_env: str = ""
    cadence: str = "manual"
    submission: str = "none"
    docs_url: str = ""
    status: str = "planned"
    targets: tuple[str, ...] = ()
    missing_env: tuple[str, ...] = ()
    last_check: str = ""
    last_error: str = ""
    next_allowed_at: str = ""


def source_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return key or "source"


def _bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(raw: str) -> tuple[str, ...]:
    raw = raw.strip()
    if not raw or raw in {"-", "—"}:
        return ()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return tuple(item.strip().strip("'\"") for item in raw.split(",") if item.strip())


def _split_env_list(raw: str) -> tuple[str, ...]:
    raw = raw.strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in re.split(r"[,|;]+", raw) if item.strip())


def _parse_catalog_file(path: Path) -> dict[str, dict[str, str]]:
    """Parse the tiny YAML subset used by ``data/source_catalog.yaml``."""
    if not path.exists():
        return {}
    rows: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() == "sources:":
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if current and current.get("key"):
                rows[current["key"]] = current
            current = {}
            stripped = stripped[2:].strip()
            if stripped:
                key, _, value = stripped.partition(":")
                current[key.strip()] = value.strip()
            continue
        if current is None or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        current[key.strip()] = value.strip()
    if current and current.get("key"):
        rows[current["key"]] = current
    return rows


def _env_present(name: str) -> bool:
    value = get_setting(name, "")
    return bool(value.strip())


def _read_lines(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return tuple(out)


def _targets_for(entry: SourceCatalogEntry, settings: Settings) -> tuple[str, ...]:
    fields = {
        "greenhouse": "greenhouse_boards",
        "lever": "lever_companies",
        "ashby": "ashby_boards",
        "recruitee": "recruitee_companies",
        "smartrecruiters": "smartrecruiters_companies",
    }
    if entry.adapter_key in fields:
        return tuple(getattr(settings.adapters, fields[entry.adapter_key]))
    env = entry.target_env
    if env == "JOBHUB_RSS_FEEDS_FILE":
        return _read_lines(settings.adapters.rss_feeds_file)
    if env == "JOBHUB_WATCHLIST_FILE":
        return tuple(
            dict.fromkeys(
                [
                    *_read_lines(settings.adapters.watchlist_file),
                    *settings.adapters.watchlist_urls,
                ]
            )
        )
    if env:
        values = _split_env_list(get_setting(env, ""))
        if values:
            return values
    if entry.adapter_key == "direct_discovery" and entry.docs_url:
        return (entry.docs_url,)
    if entry.adapter_key == "usajobs" and not entry.missing_env:
        keywords = _split_env_list(
            get_setting("JOBHUB_USAJOBS_KEYWORDS") or get_setting("JOBHUB_USAJOBS_KEYWORD", "")
        )
        passes = []
        if get_setting("JOBHUB_USAJOBS_LOCATION", "").strip():
            passes.append("local")
        if get_setting("JOBHUB_USAJOBS_REMOTE", "").strip().lower() in {"1", "true", "yes", "on"}:
            passes.append("remote")
        if not passes:
            passes.append("all")
        return tuple(f"{p}: {kw or 'all keywords'}" for p in passes for kw in (keywords or ("",)))
    if entry.adapter_key in {"remotive", "arbeitnow"}:
        return ("public-api",)
    if entry.adapter_key in _PUBLIC_BROWSER_ADAPTER_KEYS:
        return ("public-browser",)
    if entry.adapter_key in {"adzuna", "jooble"} and not entry.missing_env:
        return ("configured role-term searches",)
    if entry.adapter_key == "manual":
        return ("manual capture",)
    return ()


def _eligible_auto_discovery(entry: SourceCatalogEntry, settings: Settings) -> bool:
    """Promote concrete public source pages while preserving manual fallback."""
    if not entry.manual_only or not entry.docs_url or not settings.direct_discovery.enabled:
        return False
    host = (urlsplit(entry.docs_url).hostname or "").lower()
    # These sources are intentionally alert/manual-driven; do not treat an
    # account wall as a source discovery failure or attempt to automate it.
    protected = (
        "linkedin.com",
        "indeed.com",
        "wellfound.com",
        "read.cv",
        "producthunt.com",
        "elpha.com",
        "twitter.com",
        "x.com",
        "tbd.community",
        "productmarketingalliance.com",
        "getro.org",
    )
    return not any(host == domain or host.endswith(f".{domain}") for domain in protected)


def _apply_overrides(
    base: dict[str, SourceCatalogEntry],
    overrides: dict[str, dict[str, str]],
) -> dict[str, SourceCatalogEntry]:
    entries = dict(base)
    for key, row in overrides.items():
        current = entries.get(key) or SourceCatalogEntry(
            key=key,
            name=row.get("name", key.replace("_", " ").title()),
        )
        fields = {
            "name": row.get("name", current.name),
            "discovery_route": row.get("discovery_route", current.discovery_route),
            "submission_assumption": row.get(
                "submission_assumption",
                current.submission_assumption,
            ),
            "implementation_note": row.get("implementation_note", current.implementation_note),
            "adapter_key": row.get("adapter", row.get("adapter_key", current.adapter_key)),
            "implemented": _bool(row.get("implemented", str(current.implemented))),
            "manual_only": _bool(row.get("manual_only", str(current.manual_only))),
            "required_env": _list(row.get("required_env", ",".join(current.required_env))),
            "target_env": row.get("target_env", current.target_env),
            "cadence": row.get("cadence", current.cadence),
            "submission": row.get("submission", current.submission),
            "docs_url": row.get("docs_url", current.docs_url),
        }
        entries[key] = replace(current, **fields)
    return entries


def load_source_catalog(settings: Settings) -> list[SourceCatalogEntry]:
    entries = _apply_overrides({}, _parse_catalog_file(settings.source_catalog_file))

    out: list[SourceCatalogEntry] = []
    for entry in entries.values():
        auto_discover = _eligible_auto_discovery(entry, settings)
        if auto_discover:
            entry = replace(entry, adapter_key="direct_discovery", implemented=True)
        missing = tuple(env for env in entry.required_env if not _env_present(env))
        targets = _targets_for(entry, settings)
        status = "planned"
        if entry.manual_only and not entry.implemented:
            status = "manual_only"
        elif entry.implemented and (missing or not targets):
            status = "skipped"
            if not missing and not targets:
                missing = (f"{entry.target_env or entry.adapter_key or entry.key} target entries",)
        elif entry.implemented:
            status = "configured"
        if auto_discover:
            status = "auto_discover"
        out.append(replace(entry, status=status, targets=targets, missing_env=missing))
    return sorted(out, key=lambda e: (e.status != "configured", e.name.lower()))


def implemented_adapter_keys(entries: Iterable[SourceCatalogEntry]) -> set[str]:
    return {e.adapter_key for e in entries if e.implemented and e.adapter_key}


def format_source_table(entries: Iterable[SourceCatalogEntry]) -> str:
    rows = [
        "Source | Status | Adapter | Targets | Missing env | Cadence | Submission",
        "---|---|---|---:|---|---|---",
    ]
    for e in entries:
        rows.append(
            f"{e.name} | {e.status} | {e.adapter_key or '-'} | "
            f"{len(e.targets)} | {', '.join(e.missing_env) or '-'} | "
            f"{e.cadence} | {e.submission}"
        )
    return "\n".join(rows)
