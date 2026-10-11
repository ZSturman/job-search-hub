"""Local weekly metrics aggregated from the durable SQLite audit ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from job_search_hub.db import Store


@dataclass
class WeeklyAggregate:
    week_start: date
    sources_checked: int = 0
    postings_ingested: int = 0
    duplicates_removed: int = 0
    opportunities_scored: int = 0
    worth_a_look: int = 0
    fast_review: int = 0
    high_attention: int = 0
    skipped: int = 0
    missing_questions_created: int = 0
    answers_reused: int = 0
    manual_applications_created: int = 0
    manual_submissions: int = 0
    replies: int = 0
    screens: int = 0
    interviews: int = 0
    offers: int = 0
    errors: int = 0
    estimated_minutes_saved: int = 0
    notes: str = ""
    extra: dict = field(default_factory=dict)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def aggregate_week(store: Store, *, week_start: date | None = None) -> WeeklyAggregate:
    if week_start is None:
        week_start = _monday(datetime.now(UTC).date())
    week_end = week_start + timedelta(days=7)
    agg = WeeklyAggregate(week_start=week_start)

    with store.connect() as conn:
        rows = conn.execute(
            """
            SELECT actor, action, payload_json AS payload, occurred_at
            FROM audit_log
            WHERE occurred_at >= ? AND occurred_at < ?
            """,
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchall()

    for row in rows:
        action = row["action"]
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (ValueError, TypeError):
            payload = {}
        if action == "create_opportunity":
            agg.postings_ingested += 1
            if payload.get("excluded"):
                agg.skipped += 1
        elif action == "source_checked":
            agg.sources_checked += 1
        elif action == "remove_duplicate_opportunity":
            agg.duplicates_removed += 1
        elif action == "score_opportunity":
            agg.opportunities_scored += 1
            lane = (payload.get("lane") or "").strip()
            if lane == "Worth a Look":
                agg.worth_a_look += 1
            elif lane == "Fast Review":
                agg.fast_review += 1
            elif lane == "High Attention":
                agg.high_attention += 1
            elif lane == "Skip":
                agg.skipped += 1
        elif action == "manual_application":
            agg.manual_applications_created += 1
        elif action == "status" and payload.get("status") == "Submitted":
            agg.manual_submissions += 1
        elif action == "reply_received":
            agg.replies += 1
        elif action == "screen_received":
            agg.screens += 1
        elif action == "interview_scheduled":
            agg.interviews += 1
        elif action == "offer_received":
            agg.offers += 1
        elif action == "create_needs_answer":
            agg.missing_questions_created += 1
        elif action == "reuse_answer":
            agg.answers_reused += 1
        if "error" in action.lower():
            agg.errors += 1
    return agg
