"""Thin httpx wrapper around the Notion API.

We pin the API version per `NOTION_API_VERSION` and target the data-sources
endpoints. Root-page discovery supplies data-source IDs, so we
read schemas via `GET /v1/data_sources/{id}` and query via
`POST /v1/data_sources/{id}/query`.

This module deliberately stays small: schema validation + page CRUD only.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from job_search_hub.config import NotionSettings
from job_search_hub.logging_setup import get_logger
from job_search_hub.storage import READ_ONLY

log = get_logger(__name__)
_NULL_CONTEXT = nullcontext()


class NotionError(RuntimeError):
    pass


class NotionRateLimitError(NotionError):
    pass


class NotionTransientError(NotionError):
    pass


@dataclass
class NotionRequestStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    overloaded_529: int = 0
    server_errors: int = 0
    transport_errors: int = 0
    failed_requests: int = 0


class NotionRequestCoordinator:
    """Coordinate retry pacing and write concurrency for Notion requests."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        max_delay: float = 30.0,
        max_write_concurrency: int = 1,
        sleep=time.sleep,
        jitter: bool = True,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.max_delay = max(0.0, max_delay)
        self._sleep = sleep
        self._jitter = jitter
        self._write_semaphore = threading.BoundedSemaphore(max(1, max_write_concurrency))
        self._lock = threading.Lock()
        self.stats = NotionRequestStats()

    def is_write(self, method: str, path: str) -> bool:
        method = method.upper()
        if method == "PATCH":
            return True
        return bool(method == "POST" and path == "/pages")

    def record_response(self, status_code: int) -> None:
        with self._lock:
            self.stats.requests += 1
            if status_code == 429:
                self.stats.rate_limited += 1
            elif status_code == 529:
                self.stats.overloaded_529 += 1
            elif status_code >= 500:
                self.stats.server_errors += 1

    def record_retry(self) -> None:
        with self._lock:
            self.stats.retries += 1

    def record_transport_error(self) -> None:
        with self._lock:
            self.stats.requests += 1
            self.stats.transport_errors += 1

    def record_failure(self) -> None:
        with self._lock:
            self.stats.failed_requests += 1

    def delay_seconds(self, attempt: int, retry_after: str | None) -> float:
        parsed = _retry_after_seconds(retry_after)
        if parsed is not None:
            base = parsed
        else:
            base = min(self.max_delay, max(1.0, 2 ** max(0, attempt - 1)))
        if self._jitter and base > 0:
            base += random.uniform(0.0, min(1.0, base * 0.25))
        return min(self.max_delay, max(0.0, base))

    def sleep(self, seconds: float) -> None:
        self._sleep(seconds)

    def write_guard(self):
        return self._write_semaphore


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds())


class NotionClient:
    BASE_URL = "https://api.notion.com/v1"

    def __init__(
        self,
        settings: NotionSettings,
        *,
        timeout: float = 30.0,
        coordinator: NotionRequestCoordinator | None = None,
        read_only: bool = False,
    ) -> None:
        if not settings.token:
            raise NotionError(
                "Notion is not connected. Run jobhub setup or set JOBHUB_NOTION_TOKEN."
            )
        self.read_only = READ_ONLY.get() or read_only
        self.settings = settings
        self._resolved_ds_ids: dict[str, str] = {}
        self._coordinator = coordinator or NotionRequestCoordinator()
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Notion-Version": settings.api_version,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def stats(self) -> NotionRequestStats:
        return self._coordinator.stats

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if (
            (self.read_only or READ_ONLY.get())
            and method.upper() != "GET"
            and not (method.upper() == "POST" and path.endswith("/query"))
        ):
            raise NotionError("Preview/inspection cannot write to Notion.")
        guard = (
            self._coordinator.write_guard()
            if self._coordinator.is_write(method, path)
            else _NULL_CONTEXT
        )
        with guard:
            for attempt in range(1, self._coordinator.max_attempts + 1):
                try:
                    resp = self._client.request(method, path, **kwargs)
                except httpx.TransportError as exc:
                    self._coordinator.record_transport_error()
                    if attempt >= self._coordinator.max_attempts:
                        self._coordinator.record_failure()
                        raise
                    self._sleep_before_retry(method, path, attempt, None, str(exc))
                    continue

                self._coordinator.record_response(resp.status_code)
                if resp.status_code < 400:
                    return resp.json()
                if resp.status_code == 429:
                    if attempt >= self._coordinator.max_attempts:
                        self._coordinator.record_failure()
                        raise NotionRateLimitError(
                            f"Notion {method} {path} -> 429: {resp.text[:500]}"
                        )
                    self._sleep_before_retry(
                        method,
                        path,
                        attempt,
                        resp.headers.get("Retry-After"),
                        resp.text[:500],
                    )
                    continue
                if resp.status_code == 529 or resp.status_code >= 500:
                    if attempt >= self._coordinator.max_attempts:
                        self._coordinator.record_failure()
                        raise NotionTransientError(
                            f"Notion {method} {path} -> {resp.status_code}: {resp.text[:500]}"
                        )
                    self._sleep_before_retry(
                        method,
                        path,
                        attempt,
                        resp.headers.get("Retry-After"),
                        resp.text[:500],
                    )
                    continue
                self._coordinator.record_failure()
                raise NotionError(
                    f"Notion {method} {path} -> {resp.status_code}: {resp.text[:500]}"
                )

        raise AssertionError("unreachable Notion request loop exit")

    def _sleep_before_retry(
        self,
        method: str,
        path: str,
        attempt: int,
        retry_after: str | None,
        reason: str,
    ) -> None:
        self._coordinator.record_retry()
        delay = self._coordinator.delay_seconds(attempt, retry_after)
        log.info(
            "Notion %s %s retrying attempt=%d/%d delay=%.2fs reason=%s",
            method,
            path,
            attempt + 1,
            self._coordinator.max_attempts,
            delay,
            reason,
        )
        self._coordinator.sleep(delay)

    def _resolve_data_source_id(self, data_source_or_database_id: str) -> str:
        """Resolve either a data source id or a database id to a data source id.

        Notion's newer APIs use data source ids, while many users copy database ids
        from workspace URLs. We accept both for robustness.
        """
        if data_source_or_database_id in self._resolved_ds_ids:
            return self._resolved_ds_ids[data_source_or_database_id]

        raw_id = data_source_or_database_id
        try:
            data = self._request("GET", f"/data_sources/{raw_id}")
            resolved = data.get("id") or raw_id
            self._resolved_ds_ids[raw_id] = resolved
            return resolved
        except NotionError as ds_exc:
            # Fall through to database lookup only for not-found errors.
            msg = str(ds_exc)
            if " -> 404:" not in msg:
                raise

        db = self._request("GET", f"/databases/{raw_id}")
        data_sources = db.get("data_sources") or []
        if not data_sources:
            raise NotionError(
                f"Database {raw_id} has no data_sources; cannot resolve data source id."
            )
        if len(data_sources) > 1:
            names = [d.get("name", "") for d in data_sources]
            raise NotionError(
                f"Database {raw_id} contains multiple data sources {names}; "
                "set NOTION_DS_* to a specific data source id."
            )
        resolved = data_sources[0].get("id", "")
        if not resolved:
            raise NotionError(f"Database {raw_id} data source entry is missing id; cannot resolve.")
        self._resolved_ds_ids[raw_id] = resolved
        return resolved

    # ---- schema ----
    def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        resolved = self._resolve_data_source_id(data_source_id)
        return self._request("GET", f"/data_sources/{resolved}")

    def update_data_source(
        self,
        data_source_id: str,
        *,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        resolved = self._resolve_data_source_id(data_source_id)
        return self._request(
            "PATCH",
            f"/data_sources/{resolved}",
            json={"properties": properties},
        )

    # ---- query ----
    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        resolved = self._resolve_data_source_id(data_source_id)
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if filter_:
                body["filter"] = filter_
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/data_sources/{resolved}/query", json=body)
            yield from data.get("results", [])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")

    def query_data_source_page(
        self,
        data_source_id: str,
        *,
        filter_: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 100,
        start_cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one Notion query page without following pagination."""
        resolved = self._resolve_data_source_id(data_source_id)
        body: dict[str, Any] = {"page_size": page_size}
        if filter_:
            body["filter"] = filter_
        if sorts:
            body["sorts"] = sorts
        if start_cursor:
            body["start_cursor"] = start_cursor
        return self._request("POST", f"/data_sources/{resolved}/query", json=body)

    # ---- pages ----
    def create_page(self, *, parent: dict, properties: dict) -> dict[str, Any]:
        ds_id = parent.get("data_source_id")
        if ds_id:
            parent = dict(parent)
            parent["data_source_id"] = self._resolve_data_source_id(str(ds_id))
        return self._request("POST", "/pages", json={"parent": parent, "properties": properties})

    def update_page(self, page_id: str, properties: dict) -> dict[str, Any]:
        return self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def archive_page(self, page_id: str) -> dict[str, Any]:
        """Move a page to Notion trash.

        Notion does not expose permanent deletion through the public API.
        """
        try:
            return self._request(
                "PATCH",
                f"/pages/{page_id}",
                json={"in_trash": True},
            )
        except NotionError as exc:
            if "Can't edit block that is archived" in str(exc):
                return {"id": page_id, "archived": True, "in_trash": True}
            raise

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")
