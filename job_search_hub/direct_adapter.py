"""Adapter wrapper for reusable employer career-page discovery."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from job_search_hub.browser_runtime import BrowserRuntime
from job_search_hub.config import Settings
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.direct_discovery import DiscoveryResult, discover_target
from job_search_hub.ingest.preview import skipped_result
from job_search_hub.ingest.watchlist import read_watchlist
from job_search_hub.source_catalog import load_source_catalog

_MANUAL_ONLY_HOSTS = {
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
}


def _unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = (value or "").strip().casefold()
        if key and key not in seen:
            seen.add(key)
            output.append((value or "").strip())
    return output


class DirectDiscoveryAdapter(Adapter):
    key = "direct_discovery"
    label = "Direct employer discovery"

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.store = None
        self.organization_targets = []
        self.browser_runtime: BrowserRuntime | None = None
        self._results: dict[str, DiscoveryResult] = {}

    def _targets(self) -> list[str]:
        watchlist = [
            *read_watchlist(self.settings.adapters.watchlist_file),
            *self.settings.adapters.watchlist_urls,
        ]
        organization_urls = [target.careers_url for target in self.organization_targets]
        # A catalog docs_url is only promoted when it is a concrete source
        # page.  The known access-controlled boards intentionally remain
        # manual/alert-driven and report that reason in source health.
        catalog_urls = [
            entry.docs_url
            for entry in load_source_catalog(self.settings)
            if entry.manual_only
            and entry.docs_url
            and not any(host in entry.docs_url.lower() for host in _MANUAL_ONLY_HOSTS)
        ]
        return _unique([*watchlist, *organization_urls, *catalog_urls])[
            : self.settings.direct_discovery.max_targets_per_run
        ]

    def plan_searches(self) -> Iterable[SearchRequest]:
        for target in self._targets():
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=target,
                target=target,
                target_kind="career_source",
            )

    def _due(self, target: str) -> bool:
        if self.store is None:
            return True
        row = self.store.source_target_state(target)
        if not row:
            return True
        now = datetime.now(UTC)
        try:
            retry = row["next_retry_at"]
            if retry and datetime.fromisoformat(retry.replace("Z", "+00:00")) > now:
                return False
            success = row["last_success_at"]
            if success:
                discovered = datetime.fromisoformat(success.replace("Z", "+00:00"))
                return (
                    discovered + timedelta(days=self.settings.direct_discovery.rediscovery_days)
                    <= now
                )
        except (TypeError, ValueError):
            return True
        return True

    def _record(self, result: DiscoveryResult) -> None:
        self._results[result.target_url] = result
        if self.store is None:
            return
        success = datetime.now(UTC).isoformat() if result.status == "healthy" else None
        retry = None
        if result.status not in {"healthy", "manual_required", "unsupported", "misconfigured"}:
            retry = (
                datetime.now(UTC) + timedelta(hours=self.settings.direct_discovery.retry_hours)
            ).isoformat()
        self.store.upsert_source_target_state(
            target_url=result.target_url,
            source_key=self.key,
            strategy=result.strategy,
            status=result.status,
            evidence={
                **result.evidence,
                "postings": len(result.postings),
                "http_status": result.http_status,
            },
            last_success_at=success,
            next_retry_at=retry,
            last_error=result.error,
        )

    @property
    def source_health(self) -> dict[str, DiscoveryResult]:
        return dict(self._results)

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="no watchlist or catalog targets",
                )
            ]
        results: list[SearchResult] = []
        for request in requests:
            result = discover_target(
                request.target,
                settings=self.settings.direct_discovery,
                browser_runtime=self.browser_runtime,
            )
            self._results[request.target] = result
            results.append(
                SearchResult(
                    request=request,
                    status="ok"
                    if result.status == "healthy"
                    else "skipped"
                    if result.status in {"manual_required", "unsupported"}
                    else "error",
                    configured=True,
                    http_status=result.http_status,
                    raw_count=len(result.postings),
                    deduped_count=len(result.postings),
                    accepted_count=len(result.postings),
                    examples=result.postings[:examples],
                    postings=result.postings,
                    error=result.error,
                    payload={
                        "strategy": result.strategy,
                        "status": result.status,
                        **result.evidence,
                    },
                )
            )
        return results

    def fetch(self) -> Iterable[RawPosting]:
        if not self.settings.direct_discovery.enabled:
            return []
        output: list[RawPosting] = []
        for target in self._targets():
            if not self._due(target):
                continue
            result = discover_target(
                target,
                settings=self.settings.direct_discovery,
                browser_runtime=self.browser_runtime,
            )
            self._record(result)
            output.extend(result.postings)
        return output
