"""Adzuna official search API adapter."""

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
    timed_get,
)
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_API = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AdzunaAdapter(Adapter):
    key = "adzuna"
    label = "Adzuna"

    def __init__(
        self,
        creds: AdapterCredentials,
        terms: SearchTermSettings,
        *,
        max_per_query: int = 10,
        timeout: float = 30.0,
        requests_per_minute: int | None = None,
    ) -> None:
        super().__init__()
        self.creds = creds
        self.role_terms = terms.role_terms
        self.max_per_query = max_per_query
        self.timeout = timeout
        self.requests_per_minute = max(
            1,
            int(
                requests_per_minute
                if requests_per_minute is not None
                else creds.adzuna_requests_per_minute
            ),
        )

    def _skip_reason(self) -> str:
        if self.creds.adzuna_app_id and self.creds.adzuna_app_key:
            return ""
        return "Adzuna needs ADZUNA_APP_ID in TOML and jobhub setup --credential ADZUNA_APP_KEY"

    def plan_searches(self) -> Iterable[SearchRequest]:
        if self._skip_reason():
            return []
        for keyword in self.role_terms:
            params = {
                "app_id": self.creds.adzuna_app_id,
                "app_key": self.creds.adzuna_app_key,
                "what": keyword,
                "results_per_page": str(
                    min(self.creds.adzuna_results_per_page, self.max_per_query)
                ),
            }
            if self.creds.adzuna_location:
                params["where"] = self.creds.adzuna_location
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=_API.format(country=self.creds.adzuna_country),
                target=self.creds.adzuna_country,
                target_kind="country",
                keyword=keyword,
                params=params,
            )

    def _paced_searches(self, requests: Iterable[SearchRequest]) -> Iterable[SearchRequest]:
        """Yield API searches within the configured request-per-minute allowance."""
        planned = list(requests)
        if len(planned) > 1:
            log.info(
                "adzuna: pacing %d API searches at no more than %d request(s)/minute",
                len(planned),
                self.requests_per_minute,
            )
        interval = 60.0 / self.requests_per_minute
        next_allowed_at = time.monotonic()
        for request in planned:
            delay = next_allowed_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            next_allowed_at = time.monotonic() + interval
            yield request

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        reason = self._skip_reason()
        if reason:
            return [skipped_result(source=self.label, source_adapter=self.key, reason=reason)]
        results: list[SearchResult] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self._paced_searches(self.plan_searches()):
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
                    jobs = (resp.json() or {}).get("results") or []
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
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    results.append(error_result(request, error=str(exc), http_status=status))
        return results

    def fetch(self) -> Iterable[RawPosting]:
        reason = self._skip_reason()
        if reason:
            self.last_diagnostic = {"status": "unconfigured", "error": reason}
            log.info("adzuna: %s", reason)
            return
        seen: set[str] = set()
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self._paced_searches(self.plan_searches()):
                try:
                    resp = client.get(request.endpoint, params=request.params)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning(
                        "adzuna: keyword=%r failed: %s", request.keyword, type(exc).__name__
                    )
                    continue
                jobs = (resp.json() or {}).get("results") or []
                log.debug("adzuna: keyword=%r returned %d jobs", request.keyword, len(jobs))
                for posting in dedupe_postings(self._to_posting(job) for job in jobs):
                    posting.extras["search_bucket"] = request.keyword
                    key = posting.url or posting.external_job_id
                    if key in seen:
                        continue
                    seen.add(key)
                    yield posting

    @staticmethod
    def _to_posting(item: dict) -> RawPosting:
        company = item.get("company") or {}
        location = item.get("location") or {}
        created = item.get("created")
        posted_at = None
        try:
            if created:
                posted_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
        return RawPosting(
            source="Adzuna",
            source_adapter="adzuna",
            external_job_id=str(item.get("id") or item.get("redirect_url") or ""),
            company=str(company.get("display_name") or "").strip(),
            role_title=str(item.get("title") or "").strip(),
            url=str(item.get("redirect_url") or item.get("adref") or "").strip(),
            description=str(item.get("description") or ""),
            location=str(location.get("display_name") or "").strip(),
            compensation_min=_float(item.get("salary_min")),
            compensation_max=_float(item.get("salary_max")),
            compensation_unit="Annual"
            if item.get("salary_min") or item.get("salary_max")
            else "Unknown",
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"category": (item.get("category") or {}).get("label")},
        )
