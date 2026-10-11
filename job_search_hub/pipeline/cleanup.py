"""Conservative cleanup for untouched opportunity backlog noise."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from job_search_hub.config import Settings
from job_search_hub.ingest.base import RawPosting
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.discovery import (
    PostingDecision,
    evaluate_posting,
    plausible_fit_title,
)
from job_search_hub.pipeline.lifecycle import (
    POST_APPLICATION_OPPORTUNITY_STATUSES,
    map_opportunity_status,
    read_status,
)


@dataclass
class CleanupStats:
    scanned: int = 0
    already_trashed: int = 0
    protected: int = 0
    eligible: int = 0
    would_archive: int = 0
    archived: int = 0
    retained_for_review: int = 0
    statuses_detected: int = 0
    statuses_migrated: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    retention_reasons: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    retained_examples: list[str] = field(default_factory=list)


def _posting_from_page(page: dict) -> RawPosting:
    props = page.get("properties") or {}
    return RawPosting(
        source=P.read_select(props.get("Source")),
        source_adapter=P.read_select(props.get("Source Adapter")),
        external_job_id=P.read_text(props.get("External Job ID")),
        company=P.read_text(props.get("Company")),
        role_title=P.read_text(props.get("Role Title")),
        url=P.read_url(props.get("URL")),
        description=P.read_text(props.get("Description")),
        location=P.read_text(props.get("Location")),
    )


def _protected(props: dict) -> bool:
    decision = P.read_select(props.get("My Decision"))
    status = map_opportunity_status(read_status(props.get("Status")))
    return bool(
        P.read_relation_ids(props.get("Application"))
        or P.read_checkbox(props.get("Priority"))
        or (decision and decision != "Undecided")
        or status in POST_APPLICATION_OPPORTUNITY_STATUSES
    )


def _migrate_status(
    notion: NotionClient,
    page: dict,
    stats: CleanupStats,
    *,
    commit: bool,
    migrate_statuses: bool,
) -> None:
    props = page.get("properties") or {}
    old_status = read_status(props.get("Status"))
    new_status = map_opportunity_status(old_status)
    if old_status == new_status:
        return
    stats.statuses_detected += 1
    if commit and migrate_statuses and page.get("id"):
        notion.update_page(page["id"], {"Status": P.select(new_status)})
        stats.statuses_migrated += 1
        time.sleep(0.35)


def _cleanup_retention_reason(
    posting: RawPosting,
    decision: PostingDecision,
) -> str:
    plausible_title = plausible_fit_title(posting.role_title)
    if decision.reason == "irrelevant" and plausible_title:
        return "plausible technical title"
    description_letters = sum(character.isalpha() for character in (posting.description or ""))
    if decision.reason == "non_english" and plausible_title and description_letters < 120:
        return "insufficient language evidence"
    return ""


def cleanup_opportunities(
    settings: Settings,
    *,
    notion: NotionClient,
    commit: bool = False,
    include_irrelevant: bool = False,
    migrate_statuses: bool = False,
    example_limit: int = 12,
) -> CleanupStats:
    stats = CleanupStats()
    ds_id = settings.notion.data_sources["opportunities"]
    pages_by_id: dict[str, dict] = {}
    anonymous_pages: list[dict] = []
    # Snapshot before any writes. Mutating a data source while traversing its
    # pagination can shift cursors and return the same page more than once.
    for page in notion.query_data_source(ds_id):
        page_id = page.get("id", "")
        if page_id:
            pages_by_id[page_id] = page
        else:
            anonymous_pages.append(page)

    for page in [*pages_by_id.values(), *anonymous_pages]:
        if page.get("archived") or page.get("in_trash"):
            stats.already_trashed += 1
            continue
        stats.scanned += 1
        props = page.get("properties") or {}
        if _protected(props):
            stats.protected += 1
            _migrate_status(
                notion,
                page,
                stats,
                commit=commit,
                migrate_statuses=migrate_statuses,
            )
            continue
        if P.read_number(props.get("Model Score")) is not None:
            stats.protected += 1
            _migrate_status(
                notion,
                page,
                stats,
                commit=commit,
                migrate_statuses=migrate_statuses,
            )
            continue

        stats.eligible += 1
        posting = _posting_from_page(page)
        decision = evaluate_posting(posting, settings)
        if not decision.accepted:
            retention_reason = _cleanup_retention_reason(posting, decision)
            if decision.reason == "irrelevant" and not include_irrelevant and not retention_reason:
                retention_reason = "relevance cleanup requires explicit opt-in"
            if retention_reason:
                stats.retained_for_review += 1
                stats.retention_reasons[retention_reason] = (
                    stats.retention_reasons.get(retention_reason, 0) + 1
                )
                if len(stats.retained_examples) < example_limit:
                    stats.retained_examples.append(
                        f"{retention_reason}: {posting.company} - {posting.role_title}"
                    )
                _migrate_status(
                    notion,
                    page,
                    stats,
                    commit=commit,
                    migrate_statuses=migrate_statuses,
                )
                continue
            stats.would_archive += 1
            stats.reasons[decision.reason] = stats.reasons.get(decision.reason, 0) + 1
            if len(stats.examples) < example_limit:
                company = P.read_text(props.get("Company")) or "Unknown company"
                title = P.read_text(props.get("Role Title")) or "Unknown role"
                detail = ""
                if decision.reason == "non_english":
                    detail = (
                        f" [language={decision.language or 'unknown'} "
                        f"confidence={decision.language_confidence:.2f} "
                        f"letters={decision.language_text_letters} "
                        f"mixed={decision.mixed_language}]"
                    )
                stats.examples.append(f"{decision.reason}: {company} - {title}{detail}")
            if commit and page.get("id"):
                notion.archive_page(page["id"])
                stats.archived += 1
                time.sleep(0.35)
            continue

        _migrate_status(
            notion,
            page,
            stats,
            commit=commit,
            migrate_statuses=migrate_statuses,
        )
    return stats


def format_cleanup(stats: CleanupStats, *, commit: bool) -> str:
    action = "Archived" if commit else "Would archive"
    reason_text = (
        ", ".join(f"{reason}={count}" for reason, count in sorted(stats.reasons.items())) or "none"
    )
    retained_text = (
        ", ".join(f"{reason}={count}" for reason, count in sorted(stats.retention_reasons.items()))
        or "none"
    )
    lines = [
        (
            f"Scanned {stats.scanned}; already trashed {stats.already_trashed}; "
            f"protected {stats.protected}; "
            f"eligible untouched rows {stats.eligible}."
        ),
        (f"{action} {stats.archived if commit else stats.would_archive}; reasons: {reason_text}."),
        (f"Retained for review {stats.retained_for_review}; safeguards: {retained_text}."),
        f"Legacy statuses detected: {stats.statuses_detected}; migrated: "
        f"{stats.statuses_migrated}.",
    ]
    if stats.examples:
        lines.append("Representative archive examples:")
        lines.extend(f"- {example}" for example in stats.examples)
    if stats.retained_examples:
        lines.append("Representative retained examples:")
        lines.extend(f"- {example}" for example in stats.retained_examples)
    if not commit:
        lines.append("Dry run only. Re-run with --apply to archive and migrate.")
    if stats.retention_reasons.get("relevance cleanup requires explicit opt-in"):
        lines.append(
            "Irrelevance candidates are retained by default. Add "
            "--include-irrelevant only after reviewing the retained examples."
        )
    if stats.statuses_detected and not stats.statuses_migrated:
        lines.append(
            "Legacy status migration was not requested. Add --migrate-statuses "
            "when you want to run that separate, slower write pass."
        )
    return "\n".join(lines)
