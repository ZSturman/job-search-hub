"""Shared helpers for persisted job posting dates and recency scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from job_search_hub.ingest.base import RawPosting
from job_search_hub.notion import properties as P

DATE_JOB_WAS_POSTED = "Date job was posted"
POSTING_RECENCY_KEY = "posting_recency"

RISKY_POSTED_DATE_ADAPTERS = {"adzuna", "jooble", "rss", "imap"}
PROTECTED_POSTED_DATE_STATUSES = {
    "Rejected",
    "Closed",
    "Submitted",
    "Interviewing",
    "Offer",
}


@dataclass(frozen=True)
class RecencyScore:
    score: float
    reason: str
    days_old: int | None = None


def normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_notion_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return normalize_datetime(parsed)


def read_posted_date(props: dict) -> datetime | None:
    return parse_notion_date(P.read_date(props.get(DATE_JOB_WAS_POSTED)))


def effective_posted_at(posting: RawPosting) -> datetime | None:
    """Return exact/estimated posted date that should be persisted.

    Manual capture has no adapter-side posting date unless the browser payload
    supplies one. In that case use discovered_at as a conservative estimate so
    manually captured rows still participate in recency scoring.
    """

    posted = normalize_datetime(posting.posted_at)
    if posted is not None:
        return posted
    if (posting.source_adapter or "").strip().lower() == "manual":
        return normalize_datetime(posting.discovered_at)
    return None


def same_posted_day(a: datetime | None, b: datetime | None) -> bool:
    a = normalize_datetime(a)
    b = normalize_datetime(b)
    if a is None or b is None:
        return a is b
    return a.date() == b.date()


def posting_date_property(value: datetime | None) -> dict:
    return P.date_property(normalize_datetime(value))


def posting_recency_score(
    posted_at: datetime | None,
    *,
    now: datetime | None = None,
) -> RecencyScore:
    """Strong freshness curve for Opportunity scoring.

    The score is intentionally firm: recent jobs get almost full credit, while
    60+ day listings become a meaningful drag and 120+ day listings are near
    the floor.
    """

    if posted_at is None:
        return RecencyScore(0.15, "missing posted date; treating freshness as low")
    posted_at = normalize_datetime(posted_at)
    now = normalize_datetime(now or datetime.now(UTC)) or datetime.now(UTC)
    days = max(0, int((now.date() - posted_at.date()).days))
    anchors = (
        (0, 1.00),
        (7, 0.95),
        (15, 0.78),
        (30, 0.55),
        (60, 0.30),
        (90, 0.18),
        (120, 0.08),
    )
    if days >= anchors[-1][0]:
        score = anchors[-1][1]
    else:
        score = anchors[-1][1]
        for (left_day, left_score), (right_day, right_score) in zip(
            anchors,
            anchors[1:],
            strict=True,
        ):
            if left_day <= days <= right_day:
                span = max(1, right_day - left_day)
                progress = (days - left_day) / span
                score = left_score + (right_score - left_score) * progress
                break
    return RecencyScore(
        max(0.0, min(1.0, score)),
        f"posted {days} day{'s' if days != 1 else ''} ago",
        days_old=days,
    )


def is_risky_posted_date_source(adapter: str) -> bool:
    return (adapter or "").strip().lower() in RISKY_POSTED_DATE_ADAPTERS
