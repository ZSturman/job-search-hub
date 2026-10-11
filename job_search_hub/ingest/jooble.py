"""Jooble official REST API adapter."""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from job_search_hub.config import AdapterCredentials, SearchTermSettings
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import (
    dedupe_postings,
    error_result,
    result_from_postings,
    skipped_result,
)
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_DISPLAY_API = "https://jooble.org/api/[redacted]"
_API = "https://jooble.org/api/{key}"


class JoobleAdapter(Adapter):
    key = "jooble"
    label = "Jooble"

    def __init__(
        self,
        creds: AdapterCredentials,
        terms: SearchTermSettings,
        *,
        max_per_query: int = 10,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self.creds = creds
        self.role_terms = terms.role_terms
        self.max_per_query = max_per_query
        self.timeout = timeout

    def _skip_reason(self) -> str:
        if self.creds.jooble_api_key:
            return ""
        return "Jooble API key missing. Run jobhub setup --credential JOOBLE_API_KEY"

    def _payload_for(self, keyword: str) -> dict:
        payload = {
            "keywords": keyword,
            "page": "1",
            "ResultOnPage": str(self.max_per_query),
            "companysearch": "false",
        }
        if self.creds.jooble_location:
            payload["location"] = self.creds.jooble_location
        if self.creds.jooble_radius_km:
            payload["radius"] = self.creds.jooble_radius_km
        return payload

    def plan_searches(self) -> Iterable[SearchRequest]:
        if self._skip_reason():
            return []
        for keyword in self.role_terms:
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=_DISPLAY_API,
                target=self.creds.jooble_location or "all locations",
                target_kind="location",
                keyword=keyword,
                params=self._payload_for(keyword),
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        reason = self._skip_reason()
        if reason:
            return [skipped_result(source=self.label, source_adapter=self.key, reason=reason)]
        results: list[SearchResult] = []
        actual_endpoint = _API.format(key=self.creds.jooble_api_key)
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self.plan_searches():
                started = time.perf_counter()
                try:
                    resp = client.post(actual_endpoint, json=request.params)
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
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
                    jobs = (resp.json() or {}).get("jobs") or []
                    postings = [self._to_posting(job) for job in jobs]
                    for posting in postings:
                        posting.extras["search_bucket"] = request.keyword
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
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    results.append(
                        error_result(
                            request, error=str(exc), http_status=status, elapsed_ms=elapsed_ms
                        )
                    )
        return results

    def fetch(self) -> Iterable[RawPosting]:
        reason = self._skip_reason()
        if reason:
            self.last_diagnostic = {"status": "unconfigured", "error": reason}
            log.info("jooble: %s", reason)
            return
        actual_endpoint = _API.format(key=self.creds.jooble_api_key)
        seen: set[str] = set()
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self.plan_searches():
                try:
                    resp = client.post(actual_endpoint, json=request.params)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning(
                        "jooble: keyword=%r failed: %s", request.keyword, type(exc).__name__
                    )
                    continue
                jobs = (resp.json() or {}).get("jobs") or []
                log.debug("jooble: keyword=%r returned %d jobs", request.keyword, len(jobs))
                for posting in dedupe_postings(self._to_posting(job) for job in jobs):
                    posting.extras["search_bucket"] = request.keyword
                    key = posting.url or posting.external_job_id
                    if key in seen:
                        continue
                    seen.add(key)
                    yield posting

    @staticmethod
    def _to_posting(item: dict) -> RawPosting:
        updated = item.get("updated")
        posted_at = None
        try:
            if updated:
                posted_at = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
        job_type = str(item.get("type") or "").strip()
        salary = str(item.get("salary") or "").strip()
        return RawPosting(
            source="Jooble",
            source_adapter="jooble",
            external_job_id=str(item.get("id") or item.get("link") or ""),
            company=str(item.get("company") or item.get("source") or "").strip(),
            role_title=str(item.get("title") or "").strip(),
            url=str(item.get("link") or "").strip(),
            description=str(item.get("snippet") or ""),
            location=str(item.get("location") or "").strip(),
            employment_type=[job_type] if job_type else [],
            compensation_text=salary,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"source": item.get("source")},
        )
