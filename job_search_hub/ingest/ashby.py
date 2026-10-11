"""Ashby public Job Postings API adapter.

API: https://developers.ashbyhq.com/docs/public-job-posting-api
Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
No auth is required for public job boards.
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

_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return html.unescape(_TAG_RE.sub(" ", s)).strip()


def _compensation(job: dict) -> tuple[str, float | None, float | None, str]:
    comp = job.get("compensation") or {}
    if not isinstance(comp, dict):
        return "", None, None, "Unknown"
    text = str(comp.get("compensationTierSummary") or comp.get("summary") or "").strip()
    minimum = maximum = None
    unit = "Unknown"
    for item in comp.get("scrapeableCompensationSalarySummary") or []:
        if not isinstance(item, dict):
            continue
        try:
            minimum = float(item.get("minValue") or item.get("min") or 0) or minimum
            maximum = float(item.get("maxValue") or item.get("max") or 0) or maximum
        except (TypeError, ValueError):
            pass
        interval = str(item.get("interval") or item.get("period") or "").lower()
        if "year" in interval:
            unit = "Annual"
        elif "hour" in interval:
            unit = "Hourly"
    return text, minimum, maximum, unit


class AshbyAdapter(Adapter):
    key = "ashby"
    label = "Ashby"

    def __init__(self, creds: AdapterCredentials, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.boards = creds.ashby_boards
        self.timeout = timeout

    def plan_searches(self) -> Iterable[SearchRequest]:
        for board in self.boards:
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=_API.format(board=board),
                target=board,
                target_kind="board",
                params={"includeCompensation": "true"},
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="no boards configured (JOBHUB_ASHBY_BOARDS empty)",
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
                    data = resp.json() or {}
                    jobs = data.get("jobs") or []
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
        if not self.boards:
            log.info("ashby: no boards configured (JOBHUB_ASHBY_BOARDS empty)")
            return
        with httpx.Client(timeout=self.timeout) as client:
            for board in self.boards:
                try:
                    resp = client.get(
                        _API.format(board=board),
                        params={"includeCompensation": "true"},
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning("ashby: board=%s failed: %s", board, type(exc).__name__)
                    continue
                data = resp.json() or {}
                jobs = data.get("jobs") or []
                log.debug("ashby: board=%s returned %d jobs", board, len(jobs))
                for job in jobs:
                    yield self._to_posting(board, job)

    @staticmethod
    def _to_posting(board: str, job: dict) -> RawPosting:
        location = str(job.get("location") or "").strip()
        secondary = [
            str(loc.get("location") or "").strip()
            for loc in (job.get("secondaryLocations") or [])
            if isinstance(loc, dict) and loc.get("location")
        ]
        if secondary:
            location = ", ".join([location] + secondary if location else secondary)
        comp_text, comp_min, comp_max, comp_unit = _compensation(job)
        listed_at = job.get("publishedAt") or job.get("createdAt") or ""
        posted_at = None
        try:
            if listed_at:
                posted_at = datetime.fromisoformat(str(listed_at).replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
        description = (
            job.get("descriptionHtml") or job.get("description") or job.get("jobDescription") or ""
        )
        url = str(job.get("jobUrl") or job.get("applyUrl") or "").strip()
        return RawPosting(
            source="Ashby",
            source_adapter="ashby",
            external_job_id=str(job.get("id") or job.get("jobId") or url),
            company=board,
            role_title=str(job.get("title") or "").strip(),
            url=url,
            description=_strip_html(str(description)),
            location=location,
            compensation_text=comp_text,
            compensation_min=comp_min,
            compensation_max=comp_max,
            compensation_unit=comp_unit,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={
                "board": board,
                "department": job.get("department"),
                "team": job.get("team"),
            },
        )
