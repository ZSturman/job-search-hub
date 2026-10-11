"""In-memory tick metrics emitted to the local log and SQLite audit trail."""

from __future__ import annotations

from dataclasses import dataclass, field

from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class TickMetrics:
    sources_checked: int = 0
    postings_ingested: int = 0
    duplicates: int = 0
    new_opportunities: int = 0
    scored: int = 0
    by_lane: dict[str, int] = field(default_factory=dict)
    excluded: int = 0
    awaiting_answer: int = 0
    awaiting_resolved: int = 0
    questions_created: int = 0
    questions_reused: int = 0
    applications_created: int = 0
    submissions: int = 0
    errors: list[str] = field(default_factory=list)


def emit(metrics: TickMetrics) -> None:
    log.info(
        "tick metrics: sources=%d postings=%d new=%d dupes=%d scored=%d "
        "lanes=%s excluded=%d awaiting=%d resolved=%d questions=%d reused=%d "
        "applications=%d submissions=%d errors=%d",
        metrics.sources_checked,
        metrics.postings_ingested,
        metrics.new_opportunities,
        metrics.duplicates,
        metrics.scored,
        dict(metrics.by_lane),
        metrics.excluded,
        metrics.awaiting_answer,
        metrics.awaiting_resolved,
        metrics.questions_created,
        metrics.questions_reused,
        metrics.applications_created,
        metrics.submissions,
        len(metrics.errors),
    )
