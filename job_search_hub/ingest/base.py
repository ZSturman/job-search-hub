"""Adapter base class and the canonical `RawPosting` dataclass.

Each adapter handles one discovery route. It returns lightly-normalized
postings; deeper normalization (fingerprinting, dedupe, scoring) happens in
`pipeline/`. Applications are submitted manually by the user.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class RawPosting:
    """Normalized job posting envelope used by the rest of the pipeline."""

    source: str  # human label, e.g. "Greenhouse"
    source_adapter: str  # machine key, e.g. "greenhouse"
    external_job_id: str  # adapter-stable id (string)
    company: str
    role_title: str
    url: str
    description: str = ""  # plain-text description
    location: str = ""
    remote_status: str = "Unknown"  # Remote / Hybrid / On-site / Unknown
    schedule: str = "Unknown"
    employment_type: list[str] = field(default_factory=list)
    compensation_text: str = ""
    compensation_min: float | None = None
    compensation_max: float | None = None
    compensation_unit: str = "Unknown"  # Annual / Hourly / Monthly / Unknown
    posted_at: datetime | None = None
    discovered_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    extras: dict = field(default_factory=dict)


@dataclass
class SearchRequest:
    """One planned public-source query or target check."""

    source: str
    source_adapter: str
    endpoint: str
    target: str = ""
    target_kind: str = ""
    keyword: str = ""
    pass_name: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    """Visible dry-run/search-preview result for one request."""

    request: SearchRequest
    status: str
    configured: bool = True
    http_status: int | None = None
    elapsed_ms: float | None = None
    raw_count: int = 0
    deduped_count: int = 0
    accepted_count: int = 0
    examples: list[RawPosting] = field(default_factory=list)
    postings: list[RawPosting] = field(default_factory=list, repr=False)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    skip_reason: str = ""
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class Adapter:
    """Base discovery adapter.

    Subclasses set `key` (machine identifier) and `label` (human label),
    and implement `fetch()` returning an iterable of `RawPosting`.
    Subclasses must be importable without their credentials being set; only
    `fetch()` should fail when credentials are missing.
    """

    key: str = ""
    label: str = ""

    def __init__(self) -> None:
        if not self.key or not self.label:
            raise ValueError(f"{type(self).__name__} must set class attrs key and label")

    def fetch(self) -> Iterable[RawPosting]:  # pragma: no cover - abstract
        raise NotImplementedError

    def plan_searches(self) -> Iterable[SearchRequest]:
        """Return planned requests without network or mailbox side effects."""
        return []

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        """Run read-only preview requests for console/log visibility."""
        return []

    def record_failure(self, exc: Exception) -> None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        self.last_diagnostic = {
            "status": "rate_limited" if status == 429 else "http_error",
            "http_status": status,
            "error": f"{type(exc).__name__}" + (f" (HTTP {status})" if status else ""),
        }
