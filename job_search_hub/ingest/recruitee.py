"""Recruitee Careers Site API adapter."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from job_search_hub.config import AdapterCredentials
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

_API = "https://{company}.recruitee.com/api/offers/"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def _items(data) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("offers", "jobs", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


class RecruiteeAdapter(Adapter):
    key = "recruitee"
    label = "Recruitee"

    def __init__(self, creds: AdapterCredentials, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.companies = creds.recruitee_companies
        self.timeout = timeout

    def plan_searches(self) -> Iterable[SearchRequest]:
        for company in self.companies:
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=_API.format(company=company),
                target=company,
                target_kind="company",
                headers={"accept": "application/json"},
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="no companies configured (JOBHUB_RECRUITEE_COMPANIES empty)",
                )
            ]
        results: list[SearchResult] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
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
                    offers = _items(resp.json() or {})
                    postings = [self._to_posting(request.target, offer) for offer in offers]
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
            log.info("recruitee: no companies configured (JOBHUB_RECRUITEE_COMPANIES empty)")
            return
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self.plan_searches():
                try:
                    resp = client.get(
                        request.endpoint,
                        params=request.params,
                        headers=request.headers,
                    )
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning(
                        "recruitee: company=%s failed: %s", request.target, type(exc).__name__
                    )
                    continue
                offers = _items(resp.json() or {})
                log.debug("recruitee: company=%s returned %d jobs", request.target, len(offers))
                yield from dedupe_postings(
                    self._to_posting(request.target, offer) for offer in offers
                )

    @staticmethod
    def _to_posting(company: str, item: dict) -> RawPosting:
        title = str(item.get("title") or item.get("name") or "").strip()
        locations = item.get("locations") or item.get("location") or []
        if isinstance(locations, list):
            location = ", ".join(
                str(loc.get("name") or loc.get("city") or loc.get("country") or "").strip()
                for loc in locations
                if isinstance(loc, dict)
            ).strip(", ")
        elif isinstance(locations, dict):
            location = str(locations.get("name") or locations.get("city") or "").strip()
        else:
            location = str(locations or "").strip()
        url = str(
            item.get("careers_url")
            or item.get("url")
            or item.get("job_url")
            or item.get("slug_url")
            or ""
        ).strip()
        description = _strip_html(
            str(
                item.get("description") or item.get("description_html") or item.get("summary") or ""
            )
        )
        published = item.get("published_at") or item.get("created_at") or item.get("updated_at")
        posted_at = None
        try:
            if published:
                posted_at = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
        return RawPosting(
            source="Recruitee",
            source_adapter="recruitee",
            external_job_id=str(item.get("id") or item.get("slug") or url),
            company=str(item.get("company_name") or company),
            role_title=title,
            url=url,
            description=description,
            location=location,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"company": company},
        )
