"""Lever public Postings API adapter.

API: https://github.com/lever/postings-api
Endpoint: GET https://api.lever.co/v0/postings/{company}?mode=json
No auth required for public boards.

Targets come from the runtime snapshot of Notion Sources.
Users add employer board identifiers through jobhub sources add or Notion.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from job_search_hub.config import AdapterCredentials
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import (
    error_result,
    result_from_postings,
    skipped_result,
    timed_get,
)
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_API = "https://api.lever.co/v0/postings/{company}"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return html.unescape(_TAG_RE.sub(" ", s)).strip()


class LeverAdapter(Adapter):
    key = "lever"
    label = "Lever"

    def __init__(self, creds: AdapterCredentials, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.companies = creds.lever_companies
        self.timeout = timeout

    def plan_searches(self) -> Iterable[SearchRequest]:
        for company in self.companies:
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=_API.format(company=company),
                target=company,
                target_kind="company",
                params={"mode": "json"},
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="no companies configured (JOBHUB_LEVER_COMPANIES empty)",
                )
            ]
        results: list[SearchResult] = []
        with httpx.Client(timeout=self.timeout) as client:
            for request in requests:
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
                    jobs = resp.json() or []
                    postings = [self._to_posting(request.target, job) for job in jobs]
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
        if not self.companies:
            log.info("lever: no companies configured (JOBHUB_LEVER_COMPANIES empty)")
            return
        with httpx.Client(timeout=self.timeout) as client:
            for company in self.companies:
                try:
                    resp = client.get(_API.format(company=company), params={"mode": "json"})
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning("lever: company=%s failed: %s", company, type(exc).__name__)
                    continue
                jobs = resp.json() or []
                log.debug("lever: company=%s returned %d jobs", company, len(jobs))
                for job in jobs:
                    yield self._to_posting(company, job)

    @staticmethod
    def _to_posting(company: str, job: dict) -> RawPosting:
        categories = job.get("categories") or {}
        location = str(categories.get("location") or "").strip()
        commitment = str(categories.get("commitment") or "").strip()
        posted_ms = job.get("createdAt")
        posted_at: datetime | None = None
        if isinstance(posted_ms, (int, float)):
            posted_at = datetime.fromtimestamp(posted_ms / 1000.0, tz=UTC)
        text = _strip_html(job.get("descriptionPlain") or job.get("description") or "")
        return RawPosting(
            source="Lever",
            source_adapter="lever",
            external_job_id=str(job.get("id") or ""),
            company=company,
            role_title=str(job.get("text") or "").strip(),
            url=str(job.get("hostedUrl") or job.get("applyUrl") or "").strip(),
            description=text,
            location=location,
            employment_type=[commitment] if commitment else [],
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"company": company, "team": categories.get("team")},
        )
