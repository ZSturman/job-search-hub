"""SmartRecruiters public Posting API adapter."""

from __future__ import annotations

import html
import re
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

_API = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


class SmartRecruitersAdapter(Adapter):
    key = "smartrecruiters"
    label = "SmartRecruiters"

    def __init__(
        self,
        creds: AdapterCredentials,
        terms: SearchTermSettings,
        *,
        max_per_query: int = 10,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self.companies = creds.smartrecruiters_companies
        self.role_terms = terms.role_terms
        self.max_per_query = max_per_query
        self.timeout = timeout

    def plan_searches(self) -> Iterable[SearchRequest]:
        for company in self.companies:
            for keyword in self.role_terms:
                yield SearchRequest(
                    source=self.label,
                    source_adapter=self.key,
                    endpoint=_API.format(company=company),
                    target=company,
                    target_kind="company",
                    keyword=keyword,
                    params={"q": keyword, "limit": str(self.max_per_query), "offset": "0"},
                )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        if not self.companies:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="no companies configured (JOBHUB_SMARTRECRUITERS_COMPANIES empty)",
                )
            ]
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
                    jobs = (resp.json() or {}).get("content") or []
                    postings = [self._to_posting(request.target, job) for job in jobs]
                    for posting in postings:
                        posting.extras["search_bucket"] = f"{request.target}:{request.keyword}"
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
            log.info(
                "smartrecruiters: no companies configured (JOBHUB_SMARTRECRUITERS_COMPANIES empty)"
            )
            return
        seen: set[str] = set()
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for request in self.plan_searches():
                try:
                    resp = client.get(request.endpoint, params=request.params)
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning(
                        "smartrecruiters: company=%s keyword=%r failed: %s",
                        request.target,
                        request.keyword,
                        exc,
                    )
                    continue
                jobs = (resp.json() or {}).get("content") or []
                log.debug(
                    "smartrecruiters: company=%s keyword=%r returned %d jobs",
                    request.target,
                    request.keyword,
                    len(jobs),
                )
                for posting in dedupe_postings(
                    self._to_posting(request.target, job) for job in jobs
                ):
                    posting.extras["search_bucket"] = f"{request.target}:{request.keyword}"
                    key = posting.url or posting.external_job_id
                    if key in seen:
                        continue
                    seen.add(key)
                    yield posting

    @staticmethod
    def _to_posting(company_slug: str, item: dict) -> RawPosting:
        company = item.get("company") or {}
        location_data = item.get("location") or {}
        location_parts = [
            str(location_data.get("city") or "").strip(),
            str(location_data.get("region") or "").strip(),
            str(location_data.get("country") or "").strip(),
        ]
        location = ", ".join(part for part in location_parts if part)
        remote_status = "Remote" if location_data.get("remote") else "Unknown"
        employment = item.get("typeOfEmployment") or {}
        url = str(
            item.get("webPostingUrl") or item.get("postingUrl") or item.get("ref") or ""
        ).strip()
        description = _strip_html(
            str(item.get("jobAd") or item.get("description") or item.get("summary") or "")
        )
        released = (
            item.get("releasedDate")
            or item.get("createdOn")
            or item.get("createdAt")
            or item.get("updatedOn")
            or item.get("updatedAt")
        )
        posted_at = None
        try:
            if released:
                posted_at = datetime.fromisoformat(str(released).replace("Z", "+00:00"))
        except ValueError:
            posted_at = None
        return RawPosting(
            source="SmartRecruiters",
            source_adapter="smartrecruiters",
            external_job_id=str(item.get("id") or item.get("uuid") or url),
            company=str(company.get("name") or company_slug),
            role_title=str(item.get("name") or "").strip(),
            url=url,
            description=description,
            location=location,
            remote_status=remote_status,
            employment_type=[employment.get("label")] if employment.get("label") else [],
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"company_identifier": company_slug},
        )
