"""Greenhouse public Job Board API adapter.

API: https://developers.greenhouse.io/job-board.html
Endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
No auth required for the public board.

Targets come from the runtime snapshot of Notion Sources.
Users add employer board identifiers through jobhub sources add or Notion.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlsplit

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

_LEGACY_API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
_NEW_BOARD = "https://job-boards.greenhouse.io/{board}"
_MAX_NEW_BOARD_PAGES = 20
_TAG_RE = re.compile(r"<[^>]+>")
_REMIX_CONTEXT_RE = re.compile(
    r"window\.__remixContext\s*=\s*(\{.*?\});</script>",
    re.DOTALL,
)


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return html.unescape(_TAG_RE.sub(" ", s)).strip()


def _board_token(value: str) -> str:
    """Return the Greenhouse board token from a token or public board URL."""
    raw = (value or "").strip().strip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if host.endswith("greenhouse.io") and path_parts:
        return path_parts[0]
    return raw


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _department_names(department: object) -> list[str]:
    if not isinstance(department, dict):
        return []
    names = [
        str(name).strip()
        for name in (*department.get("path", []), department.get("name"))
        if str(name or "").strip()
    ]
    return names


def _remix_context(html_text: str) -> dict:
    match = _REMIX_CONTEXT_RE.search(html_text or "")
    if not match:
        raise ValueError("Greenhouse Remix context not found")
    return json.loads(match.group(1))


def _new_board_jobs(html_text: str) -> tuple[list[dict], int]:
    context = _remix_context(html_text)
    route_data = (context.get("state") or {}).get("loaderData") or {}
    board_data = route_data.get("routes/$url_token") or {}
    job_posts = board_data.get("jobPosts") or {}
    jobs = job_posts.get("data") or []
    total_pages = int(job_posts.get("total_pages") or 1)
    return jobs, max(1, min(total_pages, _MAX_NEW_BOARD_PAGES))


class GreenhouseAdapter(Adapter):
    key = "greenhouse"
    label = "Greenhouse"

    def __init__(self, creds: AdapterCredentials, *, timeout: float = 30.0) -> None:
        super().__init__()
        self.boards = creds.greenhouse_boards
        self.timeout = timeout

    def plan_searches(self) -> Iterable[SearchRequest]:
        for board in self.boards:
            token = _board_token(board)
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=_LEGACY_API.format(board=token),
                target=token,
                target_kind="board",
                params={"content": "true"},
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="no boards configured (JOBHUB_GREENHOUSE_BOARDS empty)",
                )
            ]
        results: list[SearchResult] = []
        with httpx.Client(timeout=self.timeout) as client:
            for request in requests:
                try:
                    resp, elapsed_ms = timed_get(client, request)
                    if resp.status_code == 404:
                        postings = self._fetch_new_board_postings(client, request.target)
                        results.append(
                            result_from_postings(
                                request,
                                http_status=resp.status_code,
                                elapsed_ms=elapsed_ms,
                                postings=postings,
                                examples=examples,
                                payload={"fallback": "job-boards.greenhouse.io"},
                            )
                        )
                        continue
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
                    data = resp.json() if resp.content else {}
                    jobs = (data or {}).get("jobs") or []
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
            log.info("greenhouse: no boards configured (JOBHUB_GREENHOUSE_BOARDS empty)")
            return
        with httpx.Client(timeout=self.timeout) as client:
            for configured_board in self.boards:
                board = _board_token(configured_board)
                url = _LEGACY_API.format(board=board)
                try:
                    resp = client.get(url, params={"content": "true"})
                    if resp.status_code == 404:
                        postings = self._fetch_new_board_postings(client, board)
                        log.debug(
                            "greenhouse: board=%s fallback=job-boards returned %d jobs",
                            board,
                            len(postings),
                        )
                        yield from postings
                        continue
                    resp.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning("greenhouse: board=%s failed: %s", board, type(exc).__name__)
                    continue
                data = resp.json()
                jobs = data.get("jobs") or []
                log.debug("greenhouse: board=%s returned %d jobs", board, len(jobs))
                for job in jobs:
                    yield self._to_posting(board, job)

    def _fetch_new_board_postings(
        self,
        client: httpx.Client,
        board: str,
    ) -> list[RawPosting]:
        url = _NEW_BOARD.format(board=board)
        resp = client.get(url)
        resp.raise_for_status()
        jobs, total_pages = _new_board_jobs(resp.text)
        for page in range(2, total_pages + 1):
            page_resp = client.get(url, params={"page": str(page)})
            page_resp.raise_for_status()
            page_jobs, _ = _new_board_jobs(page_resp.text)
            jobs.extend(page_jobs)
        return [self._to_new_board_posting(board, job) for job in jobs]

    @staticmethod
    def _to_posting(board: str, job: dict) -> RawPosting:
        offices = ", ".join(o.get("name", "") for o in (job.get("offices") or []))
        location = ((job.get("location") or {}).get("name") or offices or "").strip()
        updated = job.get("updated_at") or job.get("created_at") or ""
        posted_at = _parse_datetime(updated)
        company = ((job.get("company") or {}).get("name")) or board
        return RawPosting(
            source="Greenhouse",
            source_adapter="greenhouse",
            external_job_id=str(job.get("id", "")),
            company=company,
            role_title=str(job.get("title") or "").strip(),
            url=str(job.get("absolute_url") or "").strip(),
            description=_strip_html(job.get("content") or ""),
            location=location,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={
                "board": board,
                "departments": [d.get("name") for d in (job.get("departments") or [])],
            },
        )

    @staticmethod
    def _to_new_board_posting(board: str, job: dict) -> RawPosting:
        departments = _department_names(job.get("department"))
        description = " ".join(
            part
            for part in (
                str(job.get("title") or "").strip(),
                str(job.get("location") or "").strip(),
                " / ".join(departments),
            )
            if part
        )
        return RawPosting(
            source="Greenhouse",
            source_adapter="greenhouse",
            external_job_id=str(job.get("id") or job.get("absolute_url") or ""),
            company=board,
            role_title=str(job.get("title") or "").strip(),
            url=str(job.get("absolute_url") or "").strip(),
            description=description,
            location=str(job.get("location") or "").strip(),
            posted_at=_parse_datetime(job.get("published_at") or job.get("updated_at")),
            discovered_at=datetime.now(UTC),
            extras={"board": board, "departments": departments, "api": "job-boards"},
        )
