"""Arbeitnow public job-board API adapter."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import (
    dedupe_postings,
    error_result,
    result_from_postings,
    timed_get,
)
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_API = "https://www.arbeitnow.com/api/job-board-api"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


class ArbeitnowAdapter(Adapter):
    key = "arbeitnow"
    label = "Arbeitnow"

    def __init__(self, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.timeout = timeout

    def plan_searches(self) -> Iterable[SearchRequest]:
        yield SearchRequest(
            source=self.label,
            source_adapter=self.key,
            endpoint=_API,
            target="public-api",
            target_kind="feed",
            pass_name="broad",
        )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        results: list[SearchResult] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self.plan_searches():
                try:
                    resp, elapsed_ms = timed_get(client, request)
                    if resp.is_error:
                        results.append(
                            error_result(
                                request,
                                error=resp.text[:500],
                                http_status=resp.status_code,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                        continue
                    jobs = (resp.json() or {}).get("data") or []
                    postings = [self._to_posting(job) for job in jobs]
                    results.append(
                        result_from_postings(
                            request,
                            http_status=resp.status_code,
                            elapsed_ms=elapsed_ms,
                            postings=postings,
                            examples=examples,
                        )
                    )
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    results.append(error_result(request, error=str(exc), http_status=status))
        return results

    def fetch(self) -> Iterable[RawPosting]:
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self.plan_searches():
                try:
                    resp = client.get(request.endpoint, params=request.params)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning("arbeitnow: failed: %s", type(exc).__name__)
                    continue
                jobs = (resp.json() or {}).get("data") or []
                log.debug("arbeitnow: returned %d jobs", len(jobs))
                yield from dedupe_postings(self._to_posting(job) for job in jobs)

    @staticmethod
    def _to_posting(item: dict) -> RawPosting:
        created = item.get("created_at")
        posted_at = None
        try:
            if isinstance(created, (int, float)):
                posted_at = datetime.fromtimestamp(float(created), tz=UTC)
            elif created:
                posted_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except (ValueError, OSError, OverflowError):
            posted_at = None
        job_types = [
            str(value).strip() for value in (item.get("job_types") or []) if str(value).strip()
        ]
        tags = [str(value).strip() for value in (item.get("tags") or []) if str(value).strip()]
        return RawPosting(
            source="Arbeitnow",
            source_adapter="arbeitnow",
            external_job_id=str(item.get("slug") or item.get("url") or ""),
            company=str(item.get("company_name") or "").strip(),
            role_title=str(item.get("title") or "").strip(),
            url=str(item.get("url") or "").strip(),
            description=_strip_html(str(item.get("description") or "")),
            location=str(item.get("location") or "").strip(),
            remote_status="Remote" if item.get("remote") else "Unknown",
            employment_type=job_types,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"tags": tags},
        )
