"""USAJOBS Search API adapter.

Docs: https://developer.usajobs.gov/

Required credentials (set in `.env`):

  USAJOBS_USER_AGENT=your.email@example.com
  USAJOBS_AUTH_KEY=your_authorization_key

Optional query controls (env, all optional):

  USAJOBS_KEYWORDS         pipe-separated phrases; searched independently
  USAJOBS_LOCATION         local search location
  USAJOBS_REMOTE           "true" to add a separate remote pass

This adapter is read-only; no submission support.
"""

from __future__ import annotations

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

_API = "https://data.usajobs.gov/api/Search"


class USAJobsAdapter(Adapter):
    key = "usajobs"
    label = "USAJOBS"

    def __init__(
        self,
        creds: AdapterCredentials,
        *,
        max_per_query: int = 10,
        timeout: float = 30.0,
    ) -> None:
        super().__init__()
        self.creds = creds
        self.user_agent = creds.usajobs_user_agent
        self.auth_key = creds.usajobs_auth_key
        self.max_per_query = max_per_query
        self.timeout = timeout

    def _headers(self) -> dict:
        return {
            "Host": "data.usajobs.gov",
            "User-Agent": self.user_agent,
            "Authorization-Key": self.auth_key,
        }

    def _base_params(self) -> dict:
        params: dict = {
            "ResultsPerPage": str(self.max_per_query),
            "Page": "1",
            "Fields": "Full",
        }
        if self.creds.usajobs_job_category_codes:
            params["JobCategoryCode"] = ";".join(self.creds.usajobs_job_category_codes)
        if self.creds.usajobs_who_may_apply:
            params["WhoMayApply"] = self.creds.usajobs_who_may_apply
        if self.creds.usajobs_date_posted_days:
            params["DatePosted"] = str(self.creds.usajobs_date_posted_days)
        return params

    def _credential_skip(self) -> str:
        if self.user_agent and self.auth_key:
            return ""
        return (
            "USAJOBS credentials missing; set USAJOBS_USER_AGENT in TOML to the "
            "registration email, then jobhub setup --credential USAJOBS_AUTH_KEY"
        )

    def plan_searches(self) -> Iterable[SearchRequest]:
        if self._credential_skip():
            return []
        keywords = self.creds.usajobs_keywords or [""]
        for keyword in keywords:
            if self.creds.usajobs_location:
                params = self._base_params()
                params["LocationName"] = self.creds.usajobs_location
                if self.creds.usajobs_radius_miles:
                    params["Radius"] = str(self.creds.usajobs_radius_miles)
                if keyword:
                    params["Keyword"] = keyword
                yield SearchRequest(
                    source=self.label,
                    source_adapter=self.key,
                    endpoint=_API,
                    target=self.creds.usajobs_location,
                    target_kind="location",
                    keyword=keyword,
                    pass_name="local",
                    params=params,
                    headers=self._headers(),
                )
            if self.creds.usajobs_remote:
                params = self._base_params()
                params["RemoteIndicator"] = "true"
                if keyword:
                    params["Keyword"] = keyword
                yield SearchRequest(
                    source=self.label,
                    source_adapter=self.key,
                    endpoint=_API,
                    target="remote",
                    target_kind="remote",
                    keyword=keyword,
                    pass_name="remote",
                    params=params,
                    headers=self._headers(),
                )
            if not self.creds.usajobs_location and not self.creds.usajobs_remote:
                params = self._base_params()
                if keyword:
                    params["Keyword"] = keyword
                yield SearchRequest(
                    source=self.label,
                    source_adapter=self.key,
                    endpoint=_API,
                    target="all",
                    target_kind="scope",
                    keyword=keyword,
                    pass_name="all",
                    params=params,
                    headers=self._headers(),
                )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        reason = self._credential_skip()
        if reason:
            return [skipped_result(source=self.label, source_adapter=self.key, reason=reason)]
        requests = list(self.plan_searches())
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
                    items = self._items(resp.json() or {})
                    postings = [self._to_posting(item) for item in items]
                    for posting in postings:
                        posting.extras["search_bucket"] = f"{request.pass_name}:{request.keyword}"
                    results.append(
                        result_from_postings(
                            request,
                            http_status=resp.status_code,
                            elapsed_ms=elapsed_ms,
                            postings=postings,
                            examples=examples,
                            payload={"page": request.params.get("Page", "1")},
                        )
                    )
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    results.append(error_result(request, error=str(exc), http_status=status))
        return results

    def fetch(self) -> Iterable[RawPosting]:
        reason = self._credential_skip()
        if reason:
            self.last_diagnostic = {"status": "unconfigured", "error": reason}
            log.info("usajobs: %s", reason)
            return
        seen: set[str] = set()
        with httpx.Client(timeout=self.timeout) as client:
            for request in self.plan_searches():
                page = 1
                params = dict(request.params)
                while True:
                    params["Page"] = str(page)
                    page_request = SearchRequest(
                        source=request.source,
                        source_adapter=request.source_adapter,
                        endpoint=request.endpoint,
                        target=request.target,
                        target_kind=request.target_kind,
                        keyword=request.keyword,
                        pass_name=request.pass_name,
                        params=params,
                        headers=request.headers,
                    )
                    try:
                        resp = client.get(
                            page_request.endpoint, params=params, headers=request.headers
                        )
                        resp.raise_for_status()
                    except httpx.HTTPError as exc:
                        self.record_failure(exc)
                        log.warning(
                            "usajobs: pass=%s keyword=%r page=%d failed: %s",
                            request.pass_name,
                            request.keyword,
                            page,
                            exc,
                        )
                        break
                    data = resp.json() or {}
                    result = data.get("SearchResult") or {}
                    items = result.get("SearchResultItems") or []
                    if not items:
                        break
                    log.debug(
                        "usajobs: pass=%s keyword=%r page=%d items=%d",
                        request.pass_name,
                        request.keyword,
                        page,
                        len(items),
                    )
                    postings = dedupe_postings(self._to_posting(item) for item in items)
                    for posting in postings:
                        posting.extras["search_bucket"] = f"{request.pass_name}:{request.keyword}"
                        dedupe_key = posting.url or posting.external_job_id
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)
                        yield posting
                    break

    @staticmethod
    def _items(data: dict) -> list[dict]:
        result = data.get("SearchResult") or {}
        items = result.get("SearchResultItems") or []
        return items if isinstance(items, list) else []

    @staticmethod
    def _to_posting(item: dict) -> RawPosting:
        descriptor = item.get("MatchedObjectDescriptor") or {}
        positions = descriptor.get("PositionLocation") or []
        location = ", ".join(p.get("LocationName", "") for p in positions if p.get("LocationName"))
        remote_status = (
            "Remote"
            if descriptor.get("PositionLocationDisplay", "").lower().startswith("anywhere")
            else "Unknown"
        )
        title = descriptor.get("PositionTitle", "")
        organization = (
            descriptor.get("OrganizationName")
            or descriptor.get("DepartmentName")
            or "U.S. Government"
        )
        url = descriptor.get("PositionURI", "")
        external_id = descriptor.get("PositionID") or item.get("MatchedObjectId") or url
        # Salary
        comp_min = comp_max = None
        comp_unit = "Unknown"
        comp_text = ""
        ranges = descriptor.get("PositionRemuneration") or []
        if ranges:
            r0 = ranges[0]
            try:
                comp_min = float(r0.get("MinimumRange") or 0) or None
                comp_max = float(r0.get("MaximumRange") or 0) or None
            except (TypeError, ValueError):
                pass
            unit = (r0.get("RateIntervalCode") or "").lower()
            if "year" in unit or "annual" in unit:
                comp_unit = "Annual"
            elif "hour" in unit:
                comp_unit = "Hourly"
            comp_text = r0.get("Description", "")
        # Description text
        details = (
            descriptor.get("UserArea", {}).get("Details", {})
            if isinstance(descriptor.get("UserArea"), dict)
            else {}
        )
        description = (
            descriptor.get("QualificationSummary")
            or details.get("JobSummary")
            or descriptor.get("PositionFormattedDescription", "")
        )
        publication = descriptor.get("PublicationStartDate")
        posted_at = None
        try:
            if publication:
                posted_at = datetime.fromisoformat(publication.replace("Z", "+00:00"))
        except ValueError:
            posted_at = None

        return RawPosting(
            source="USAJOBS",
            source_adapter="usajobs",
            external_job_id=str(external_id),
            company=organization,
            role_title=title,
            url=url,
            description=description,
            location=location,
            remote_status=remote_status,
            compensation_text=comp_text,
            compensation_min=comp_min,
            compensation_max=comp_max,
            compensation_unit=comp_unit,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"agency": descriptor.get("OrganizationName")},
        )
