"""Source catalog reporting and live source health checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from job_search_hub.browser_runtime import BrowserRuntime
from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.events import emit_event
from job_search_hub.ingest.browser_boards import BOARDS, BrowserBoardAdapter, WorkWithLinearAdapter
from job_search_hub.ingest.direct_discovery import discover_target
from job_search_hub.ingest.greenhouse import _board_token, _new_board_jobs
from job_search_hub.ingest.watchlist import content_hash
from job_search_hub.logging_setup import get_logger
from job_search_hub.source_catalog import SourceCatalogEntry, load_source_catalog
from job_search_hub.storage import get_setting

log = get_logger(__name__)


@dataclass
class SourceCheckResult:
    source_key: str
    name: str
    adapter_key: str
    target: str
    status: str
    configured: bool
    postings_seen: int = 0
    http_status: int | None = None
    error: str = ""
    next_allowed_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _next_allowed(retry_after: float | None) -> str:
    if retry_after is None:
        return ""
    return (datetime.now(UTC) + timedelta(seconds=retry_after)).isoformat()


def _record(
    *,
    settings: Settings,
    store: Store,
    entry: SourceCatalogEntry,
    result: SourceCheckResult,
) -> None:
    store.record_source_run(
        source_key=result.source_key,
        adapter_key=result.adapter_key,
        target=result.target,
        status=result.status,
        configured=result.configured,
        postings_seen=result.postings_seen,
        http_status=result.http_status,
        rate_limit_remaining=str(result.payload.get("rate_limit_remaining") or ""),
        retry_after_seconds=result.payload.get("retry_after_seconds"),
        next_allowed_at=result.next_allowed_at,
        error=result.error,
        payload=result.payload,
    )
    emit_event(
        settings.log_dir,
        event="source_check",
        stage="source-check",
        message=f"{entry.name}: {result.target or '-'} -> {result.status}",
        payload=result.__dict__,
    )


def _count_rss(content: bytes) -> int:
    try:
        import feedparser
    except ImportError:
        return 0
    parsed = feedparser.parse(content)
    return len(parsed.entries or [])


def _check_http_json(
    client: httpx.Client,
    *,
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    count_path: tuple[str, ...],
) -> tuple[int, int, dict]:
    resp = client.get(url, params=params, headers=headers)
    retry = _retry_after(resp)
    payload = {
        "retry_after_seconds": retry,
        "rate_limit_remaining": (
            resp.headers.get("X-RateLimit-Remaining")
            or resp.headers.get("x-ratelimit-remaining")
            or ""
        ),
    }
    resp.raise_for_status()
    data = resp.json() or {}
    cur: Any = data
    for part in count_path:
        cur = cur.get(part) if isinstance(cur, dict) else None
    count = len(cur or []) if isinstance(cur, list) else int(cur or 0)
    return resp.status_code, count, payload


def _check_entry(
    entry: SourceCatalogEntry,
    *,
    settings: Settings,
    store: Store,
    client: httpx.Client,
    browser_runtime: BrowserRuntime | None = None,
) -> list[SourceCheckResult]:
    adapter = entry.adapter_key
    if not entry.implemented:
        return []
    if entry.status == "skipped":
        return [
            SourceCheckResult(
                source_key=entry.key,
                name=entry.name,
                adapter_key=adapter,
                target=", ".join(entry.targets),
                status="skipped",
                configured=False,
                error=", ".join(entry.missing_env),
            )
        ]
    if adapter in {"manual", "imap"}:
        target = ", ".join(entry.targets) or entry.target_env or adapter
        return [
            SourceCheckResult(
                source_key=entry.key,
                name=entry.name,
                adapter_key=adapter,
                target=target,
                status="configured",
                configured=True,
                payload={"manual_only": entry.manual_only, "live_fetch": False},
            )
        ]
    if not entry.targets:
        return [
            SourceCheckResult(
                source_key=entry.key,
                name=entry.name,
                adapter_key=adapter,
                target="",
                status="missing_targets",
                configured=False,
            )
        ]

    out: list[SourceCheckResult] = []
    for target in entry.targets:
        try:
            if adapter == "rss":
                resp = client.get(target)
                retry = _retry_after(resp)
                resp.raise_for_status()
                count = _count_rss(resp.content)
                payload = {"retry_after_seconds": retry}
                status = "ok"
                http_status = resp.status_code
            elif adapter == "greenhouse":
                board = _board_token(target)
                resp = client.get(
                    f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                    params={"content": "true"},
                )
                retry = _retry_after(resp)
                if resp.status_code == 404:
                    fallback = client.get(f"https://job-boards.greenhouse.io/{board}")
                    fallback.raise_for_status()
                    jobs, total_pages = _new_board_jobs(fallback.text)
                    count = len(jobs)
                    http_status = resp.status_code
                    payload = {
                        "fallback": "job-boards.greenhouse.io",
                        "fallback_status": fallback.status_code,
                        "retry_after_seconds": retry,
                        "total_pages": total_pages,
                    }
                else:
                    resp.raise_for_status()
                    data = resp.json() or {}
                    jobs = data.get("jobs") or []
                    count = len(jobs) if isinstance(jobs, list) else 0
                    http_status = resp.status_code
                    payload = {"retry_after_seconds": retry}
                status = "ok"
            elif adapter == "lever":
                resp = client.get(
                    f"https://api.lever.co/v0/postings/{target}",
                    params={"mode": "json"},
                )
                retry = _retry_after(resp)
                resp.raise_for_status()
                data = resp.json() or []
                count = len(data) if isinstance(data, list) else 0
                payload = {"retry_after_seconds": retry}
                status = "ok"
                http_status = resp.status_code
            elif adapter == "ashby":
                http_status, count, payload = _check_http_json(
                    client,
                    url=f"https://api.ashbyhq.com/posting-api/job-board/{target}",
                    params={"includeCompensation": "true"},
                    count_path=("jobs",),
                )
                status = "ok"
            elif adapter == "usajobs":
                headers = {
                    "Host": "data.usajobs.gov",
                    "User-Agent": get_setting("JOBHUB_USAJOBS_USER_AGENT", ""),
                    "Authorization-Key": get_setting("JOBHUB_USAJOBS_AUTH_KEY", ""),
                }
                params = {"ResultsPerPage": "25", "Page": "1"}
                if get_setting("JOBHUB_USAJOBS_KEYWORD", "").strip():
                    params["Keyword"] = get_setting("JOBHUB_USAJOBS_KEYWORD", "").strip()
                if get_setting("JOBHUB_USAJOBS_LOCATION", "").strip():
                    params["LocationName"] = get_setting("JOBHUB_USAJOBS_LOCATION", "").strip()
                http_status, count, payload = _check_http_json(
                    client,
                    url="https://data.usajobs.gov/api/Search",
                    params=params,
                    headers=headers,
                    count_path=("SearchResult", "SearchResultItems"),
                )
                status = "ok"
            elif adapter == "watchlist":
                resp = client.get(target)
                retry = _retry_after(resp)
                resp.raise_for_status()
                digest = content_hash(resp.content)
                previous = store.latest_source_run(source_key=entry.key, target=target)
                previous_hash = ""
                if previous and previous["payload_json"]:
                    try:
                        previous_hash = json.loads(previous["payload_json"]).get("content_hash", "")
                    except ValueError:
                        previous_hash = ""
                status = "changed" if previous_hash and previous_hash != digest else "ok"
                count = 0
                payload = {"content_hash": digest, "retry_after_seconds": retry}
                http_status = resp.status_code
            elif adapter == "direct_discovery":
                result = discover_target(
                    target,
                    settings=settings.direct_discovery,
                    client=client,
                    browser_runtime=browser_runtime,
                )
                status = result.status
                count = len(result.postings)
                http_status = result.http_status
                payload = {"strategy": result.strategy, "evidence": result.evidence}
                if result.error:
                    payload["reason"] = result.error
            elif adapter in {board.key for board in BOARDS} | {"work_with_linear"}:
                if adapter == "work_with_linear":
                    browser_adapter = WorkWithLinearAdapter(settings)
                else:
                    board = next(board for board in BOARDS if board.key == adapter)
                    browser_adapter = BrowserBoardAdapter(settings, board)
                browser_adapter.browser_runtime = browser_runtime
                postings = list(browser_adapter.fetch())
                diagnostic = browser_adapter.last_diagnostic
                status = str(diagnostic.get("status") or "ok")
                count = len(postings)
                http_status = diagnostic.get("http_status")
                payload = {"browser": diagnostic}
                if diagnostic.get("error"):
                    payload["reason"] = diagnostic["error"]
            else:
                status = "planned"
                count = 0
                payload = {}
                http_status = None
            retry_after = payload.get("retry_after_seconds")
            out.append(
                SourceCheckResult(
                    source_key=entry.key,
                    name=entry.name,
                    adapter_key=adapter,
                    target=target,
                    status=status,
                    configured=True,
                    postings_seen=count,
                    http_status=http_status,
                    next_allowed_at=_next_allowed(retry_after),
                    payload=payload,
                )
            )
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            out.append(
                SourceCheckResult(
                    source_key=entry.key,
                    name=entry.name,
                    adapter_key=adapter,
                    target=target,
                    status="error",
                    configured=True,
                    http_status=status_code,
                    error=str(exc),
                )
            )
    return out


def run_source_check(
    settings: Settings,
    *,
    store: Store,
    live: bool = False,
    only: list[str] | None = None,
) -> list[SourceCheckResult]:
    entries = load_source_catalog(settings)
    if only:
        selected = set(only)
        entries = [e for e in entries if e.key in selected or e.adapter_key in selected]
    if not live:
        return [
            SourceCheckResult(
                source_key=e.key,
                name=e.name,
                adapter_key=e.adapter_key,
                target=", ".join(e.targets),
                status=e.status,
                configured=e.status == "configured",
                error=", ".join(e.missing_env),
            )
            for e in entries
        ]

    results: list[SourceCheckResult] = []
    with (
        BrowserRuntime() as browser_runtime,
        httpx.Client(timeout=30.0, follow_redirects=True) as client,
    ):
        for entry in entries:
            for result in _check_entry(
                entry,
                settings=settings,
                store=store,
                client=client,
                browser_runtime=browser_runtime,
            ):
                results.append(result)
                _record(settings=settings, store=store, entry=entry, result=result)
                log.info(
                    "source-check: %s target=%s status=%s count=%d",
                    entry.key,
                    result.target,
                    result.status,
                    result.postings_seen,
                )
    return results


def format_source_check(results: list[SourceCheckResult]) -> str:
    rows = [
        "Source | Adapter | Target | Status | Count | HTTP | Error",
        "---|---|---|---|---:|---:|---",
    ]
    for r in results:
        target = (r.target or "-").replace("|", "/")
        error = (r.error or "-").replace("\n", " ")[:120]
        rows.append(
            f"{r.name} | {r.adapter_key or '-'} | {target} | {r.status} | "
            f"{r.postings_seen} | {r.http_status or '-'} | {error}"
        )
    return "\n".join(rows)
