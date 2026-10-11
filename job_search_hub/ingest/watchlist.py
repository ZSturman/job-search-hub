"""Watchlist adapter for priority career pages.

The watchlist is deliberately not a scraper. It checks configured URLs for
reachability and content changes, then records source health so the operator
can manually capture specific postings through the capture endpoint.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx

from job_search_hub.config import AdapterCredentials
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import error_result, skipped_result, timed_get
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

HEALTH_HEALTHY = "healthy"
HEALTH_BLOCKED = "blocked"
HEALTH_MISCONFIGURED = "misconfigured"
HEALTH_TRANSIENT = "transient_failure"


def read_watchlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _dedupe_urls(urls: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        clean = (url or "").strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
    return out


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class WatchlistSourceHealth:
    url: str
    status: str
    http_status: int | None = None
    error: str = ""
    content_hash: str = ""


def classify_watchlist_failure(exc: Exception) -> tuple[str, int | None]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status in {401, 403}:
        return HEALTH_BLOCKED, status
    if status in {404, 410}:
        return HEALTH_MISCONFIGURED, status
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return HEALTH_MISCONFIGURED, status
    return HEALTH_TRANSIENT, status


def _skippable_health(health: WatchlistSourceHealth | None) -> bool:
    return bool(health and health.status in {HEALTH_BLOCKED, HEALTH_MISCONFIGURED})


class WatchlistAdapter(Adapter):
    key = "watchlist"
    label = "Watchlist"

    def __init__(self, creds: AdapterCredentials, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.watchlist_file = creds.watchlist_file
        self.watchlist_urls = creds.watchlist_urls
        self.timeout = timeout
        self._source_health: dict[str, WatchlistSourceHealth] = {}

    @property
    def source_health(self) -> dict[str, WatchlistSourceHealth]:
        return dict(self._source_health)

    def refresh_source_health(self) -> None:
        self._source_health.clear()

    def _urls(self) -> list[str]:
        return _dedupe_urls([*read_watchlist(self.watchlist_file), *self.watchlist_urls])

    def plan_searches(self) -> Iterable[SearchRequest]:
        for url in self._urls():
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=url,
                target=url,
                target_kind="watchlist",
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason=f"no URLs configured ({self.watchlist_file} missing or empty)",
                )
            ]
        results: list[SearchResult] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in requests:
                try:
                    resp, elapsed_ms = timed_get(client, request)
                    if resp.is_error:
                        status = resp.status_code
                        health_status = (
                            HEALTH_BLOCKED
                            if status in {401, 403}
                            else HEALTH_MISCONFIGURED
                            if status in {404, 410}
                            else HEALTH_TRANSIENT
                        )
                        self._source_health[request.endpoint] = WatchlistSourceHealth(
                            url=request.endpoint,
                            status=health_status,
                            http_status=status,
                            error=resp.text[:500],
                        )
                        results.append(
                            error_result(
                                request,
                                error=resp.text[:500],
                                http_status=resp.status_code,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                        continue
                    digest = content_hash(resp.content)
                    self._source_health[request.endpoint] = WatchlistSourceHealth(
                        url=request.endpoint,
                        status=HEALTH_HEALTHY,
                        http_status=resp.status_code,
                        content_hash=digest,
                    )
                    results.append(
                        SearchResult(
                            request=request,
                            status="ok",
                            configured=True,
                            http_status=resp.status_code,
                            elapsed_ms=elapsed_ms,
                            raw_count=0,
                            deduped_count=0,
                            payload={
                                "content_hash": digest,
                                "manual_capture_required": True,
                                "note": (
                                    "watchlist checks reachability/change only; it does not scrape "
                                    "jobs"
                                ),
                            },
                        )
                    )
                except Exception as exc:
                    health_status, status = classify_watchlist_failure(exc)
                    self._source_health[request.endpoint] = WatchlistSourceHealth(
                        url=request.endpoint,
                        status=health_status,
                        http_status=status,
                        error=str(exc),
                    )
                    results.append(error_result(request, error=str(exc), http_status=status))
        return results

    def fetch(self) -> Iterable[RawPosting]:
        urls = self._urls()
        if not urls:
            log.info("watchlist: no URLs configured (%s missing or empty)", self.watchlist_file)
            return []
        failures: list[str] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for url in urls:
                existing_health = self._source_health.get(url)
                if _skippable_health(existing_health):
                    log.info(
                        (
                            "watchlist: skipping %s health=%s http_status=%s; refresh source "
                            "health to retry"
                        ),
                        url,
                        existing_health.status,
                        existing_health.http_status,
                    )
                    continue
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    digest = content_hash(resp.content)
                    self._source_health[url] = WatchlistSourceHealth(
                        url=url,
                        status=HEALTH_HEALTHY,
                        http_status=resp.status_code,
                        content_hash=digest,
                    )
                    log.debug(
                        "watchlist: %s reachable status=%s hash=%s manual_capture_required",
                        url,
                        resp.status_code,
                        digest[:12],
                    )
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    health_status, status = classify_watchlist_failure(exc)
                    detail = f"{status}: {exc}" if status else str(exc)
                    failures.append(f"{url} ({detail})")
                    self._source_health[url] = WatchlistSourceHealth(
                        url=url,
                        status=health_status,
                        http_status=status,
                        error=str(exc),
                    )
                    log.debug(
                        "watchlist: %s health=%s failed: %s", url, health_status, type(exc).__name__
                    )
        if failures:
            sample = "; ".join(failures[:3])
            more = f"; +{len(failures) - 3} more" if len(failures) > 3 else ""
            log.warning(
                "watchlist: %d/%d configured URLs failed reachability checks: %s%s",
                len(failures),
                len(urls),
                sample,
                more,
            )
        return []
