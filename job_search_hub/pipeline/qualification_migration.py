"""One-time migration into canonical answers and persisted qualifications."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.models import LMStudioClient, ModelRouter
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.answers import CachedAnswer, load_answer_bank
from job_search_hub.pipeline.qualification_keys import (
    canonical_key,
    infer_question_intent,
    normalized_answer,
    topic_from_question,
)
from job_search_hub.pipeline.qualification_requirements import load_requirements
from job_search_hub.pipeline.score_stage import run_score
from job_search_hub.topics import normalize_topics


@dataclass
class QualificationMigrationStats:
    answers_scanned: int = 0
    answers_canonicalized: int = 0
    duplicate_groups: int = 0
    model_duplicate_groups: int = 0
    duplicates_deprecated: int = 0
    conflicts: int = 0
    opportunities_relinked: int = 0
    applications_relinked: int = 0
    requirements_relinked: int = 0
    awaiting_candidates: int = 0
    awaiting_backfilled: int = 0
    awaiting_deferred: int = 0
    deferred: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    canonical_answers: list[CachedAnswer] = field(
        default_factory=list,
        repr=False,
    )


_MODEL_CONSOLIDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["duplicate_groups"],
    "properties": {
        "duplicate_groups": {
            "type": "array",
            "maxItems": 80,
            "items": {
                "type": "array",
                "minItems": 2,
                "maxItems": 12,
                "items": {
                    "type": "integer",
                    "minimum": 0,
                },
            },
        }
    },
}


def _kind_for_answer(
    answer: CachedAnswer,
    intent: str,
) -> str:
    if intent == "Years of Experience":
        return "years_experience"

    if intent == "Credential":
        return "credential"

    if intent == "Location":
        return "location"

    if intent == "Relocation":
        return "relocation"

    if intent == "Work Authorization":
        return "work_authorization"

    if answer.question_family == "Skills":
        return "tool" if intent == "Experience" else "skill"

    return "other"


def canonical_metadata(
    answer: CachedAnswer,
) -> tuple[str, tuple[str, ...], str, str]:
    """Return fully normalized canonical metadata for an Answer Bank row."""
    intent = answer.question_intent or infer_question_intent(
        answer.question,
        answer.question_family,
        answer.answer_type,
    )

    source_topics: tuple[str, ...] | list[str]

    if answer.topics:
        source_topics = answer.topics
    else:
        inferred_topic = topic_from_question(
            answer.question,
            answer.question_family,
        )
        source_topics = (inferred_topic,) if inferred_topic else ()

    topics = tuple(
        normalize_topics(
            source_topics,
            fallback="Other",
            max_topics=4,
        )
    )

    key = canonical_key(
        intent=intent,
        kind=_kind_for_answer(
            answer,
            intent,
        ),
        topics=topics,
        answer_type=answer.answer_type,
    )

    normalized = normalized_answer(
        answer.answer,
        answer.answer_type,
    )

    return (
        key,
        topics,
        intent,
        normalized,
    )


def _canonical_rank(
    answer: CachedAnswer,
) -> tuple:
    return (
        -int(answer.status == "Approved" and bool(answer.answer.strip())),
        -int(answer.approved_for_reuse),
        -answer.use_count,
        answer.created_time or "9999-12-31T23:59:59Z",
        answer.page_id,
    )


def _model_duplicate_groups(
    answers: list[CachedAnswer],
    *,
    router: ModelRouter | None,
    client: LMStudioClient | None,
) -> list[list[int]]:
    if router is None or client is None:
        return []

    candidates = [
        (index, answer)
        for index, answer in enumerate(answers)
        if answer.status != "Deprecated"
        and not answer.sensitive
        and not answer.context_specific
        and answer.answer_type
        in {
            "Yes/No",
            "Number",
            "Short Text",
        }
    ]

    if len(candidates) < 2:
        return []

    payload = [
        {
            "index": index,
            "question": answer.question,
            "answer_type": answer.answer_type,
            "status": answer.status,
            "normalized_answer": answer.normalized_answer,
            "topics": list(answer.topics),
            "question_intent": answer.question_intent,
        }
        for index, answer in candidates[:200]
    ]

    system = (
        "Find Answer Bank rows that ask for the same reusable candidate fact. "
        "Group only true duplicates with the same answer type and semantic intent. "
        "Do not group years-of-experience with yes/no experience. Do not group "
        "location or relocation rows unless they ask about the same geographic level. "
        "Do not group rows where the answer value could differ by opportunity. "
        "Treat topic labels as values from a controlled vocabulary."
    )

    try:
        data = client.chat_json(
            model=router.get("primary"),
            messages=[
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": json_dumps(
                        {
                            "answers": payload,
                        }
                    ),
                },
            ],
            json_schema=_MODEL_CONSOLIDATION_SCHEMA,
            max_tokens=2048,
            stage="answer-bank",
            role="primary",
            task="model_answer_bank_duplicate_groups",
        )
    except Exception:
        return []

    groups: list[list[int]] = []
    max_index = len(answers) - 1

    for group in data.get("duplicate_groups") or []:
        if not isinstance(group, list):
            continue

        parsed: set[int] = set()

        for value in group:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue

            if 0 <= index <= max_index:
                parsed.add(index)

        indexes = sorted(parsed)

        if len(indexes) >= 2:
            groups.append(indexes)

    return groups


def _compatible_model_group(
    group: list[CachedAnswer],
    metadata: dict[
        str,
        tuple[str, tuple[str, ...], str, str],
    ],
) -> bool:
    answer_types = {answer.answer_type for answer in group}
    intents = {metadata[answer.page_id][2] for answer in group}

    if len(answer_types) != 1 or len(intents) != 1:
        return False

    approved_values = {
        metadata[answer.page_id][3]
        for answer in group
        if (answer.status == "Approved" and answer.answer.strip())
    }

    return len(approved_values) <= 1


def _apply_model_duplicate_overrides(
    answers: list[CachedAnswer],
    metadata: dict[
        str,
        tuple[str, tuple[str, ...], str, str],
    ],
    *,
    router: ModelRouter | None,
    client: LMStudioClient | None,
    stats: QualificationMigrationStats,
) -> None:
    duplicate_groups = _model_duplicate_groups(
        answers,
        router=router,
        client=client,
    )

    for indexes in duplicate_groups:
        group = [answers[index] for index in indexes if answers[index].page_id in metadata]

        if len(group) < 2 or not _compatible_model_group(
            group,
            metadata,
        ):
            continue

        canonical = sorted(
            group,
            key=_canonical_rank,
        )[0]

        key, canonical_topics, intent, normalized = metadata[canonical.page_id]

        topics = tuple(
            normalize_topics(
                canonical_topics,
                fallback="Other",
                max_topics=4,
            )
        )

        canonical_kind = _kind_for_answer(
            canonical,
            intent,
        )
        key = canonical_key(
            intent=intent,
            kind=canonical_kind,
            topics=topics,
            answer_type=canonical.answer_type,
        )

        for answer in group:
            current_normalized = metadata[answer.page_id][3]

            metadata[answer.page_id] = (
                key,
                topics,
                intent,
                current_normalized,
            )

            answer.canonical_key = key
            answer.topics = topics
            answer.question_intent = intent

            if not answer.normalized_answer:
                answer.normalized_answer = normalized

        stats.model_duplicate_groups += 1


def json_dumps(
    value: dict,
) -> str:
    import json

    return json.dumps(
        value,
        ensure_ascii=True,
    )


def _replace_relation(
    notion: NotionClient,
    *,
    page_id: str,
    property_name: str,
    duplicate_ids: set[str],
    canonical_id: str,
    dry_run: bool,
) -> bool:
    page = notion.get_page(page_id)
    props = page.get("properties") or {}

    current = P.read_relation_ids(props.get(property_name))

    if not any(answer_id in duplicate_ids for answer_id in current):
        return False

    updated = list(
        dict.fromkeys(
            canonical_id if answer_id in duplicate_ids else answer_id for answer_id in current
        )
    )

    if not dry_run:
        notion.update_page(
            page_id,
            {property_name: P.relation(updated)},
        )

    return True


def migrate_answer_bank(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    dry_run: bool,
    router: ModelRouter | None = None,
    client: LMStudioClient | None = None,
) -> QualificationMigrationStats:
    stats = QualificationMigrationStats()

    answers = load_answer_bank(
        notion,
        settings,
    )
    stats.answers_scanned = len(answers)

    metadata: dict[
        str,
        tuple[str, tuple[str, ...], str, str],
    ] = {}

    for answer in answers:
        if answer.status == "Deprecated":
            continue

        values = canonical_metadata(answer)
        metadata[answer.page_id] = values

        key, topics, intent, normalized = values

        answer.canonical_key = key
        answer.topics = topics
        answer.question_intent = intent
        answer.normalized_answer = normalized

    _apply_model_duplicate_overrides(
        answers,
        metadata,
        router=router,
        client=client,
        stats=stats,
    )

    groups: dict[
        str,
        list[CachedAnswer],
    ] = {}

    for answer in answers:
        if answer.status == "Deprecated" or answer.page_id not in metadata:
            continue

        key, topics, intent, normalized = metadata[answer.page_id]

        topics = tuple(
            normalize_topics(
                topics,
                fallback="Other",
                max_topics=4,
            )
        )

        key = canonical_key(
            intent=intent,
            kind=_kind_for_answer(
                answer,
                intent,
            ),
            topics=topics,
            answer_type=answer.answer_type,
        )

        metadata[answer.page_id] = (
            key,
            topics,
            intent,
            normalized,
        )

        answer.canonical_key = key
        answer.topics = topics
        answer.question_intent = intent
        answer.normalized_answer = normalized

        groups.setdefault(
            key,
            [],
        ).append(answer)

        if not dry_run:
            notion.update_page(
                answer.page_id,
                {
                    "Canonical Key": P.rich_text(key),
                    "Topics": P.multi_select(list(topics)),
                    "Question Intent": P.select(intent),
                    "Normalized Answer": P.rich_text(normalized),
                },
            )

        stats.answers_canonicalized += 1

    all_requirements = load_requirements(
        notion,
        settings,
    )

    for key, candidates in groups.items():
        if len(candidates) < 2:
            continue

        stats.duplicate_groups += 1

        approved_values = {
            metadata[candidate.page_id][3]
            for candidate in candidates
            if (candidate.status == "Approved" and candidate.answer.strip())
        }

        if len(approved_values) > 1:
            stats.conflicts += 1

            for candidate in candidates:
                candidate.status = "Needs Review"

            if not dry_run:
                for candidate in candidates:
                    notion.update_page(
                        candidate.page_id,
                        {
                            "Status": P.select("Needs Review"),
                            "Notes": P.rich_text(
                                "Conflicting approved answers share "
                                f"canonical key {key}; review before reuse."
                            ),
                        },
                    )

            continue

        canonical = sorted(
            candidates,
            key=_canonical_rank,
        )[0]

        duplicates = [
            candidate for candidate in candidates if candidate.page_id != canonical.page_id
        ]

        duplicate_ids = {candidate.page_id for candidate in duplicates}

        related_opportunities = list(
            dict.fromkeys(
                opportunity_id
                for candidate in candidates
                for opportunity_id in candidate.related_opportunity_ids
            )
        )

        related_applications = list(
            dict.fromkeys(
                application_id
                for candidate in candidates
                for application_id in candidate.related_application_ids
            )
        )

        fact_sources = list(
            dict.fromkeys(
                fact_id for candidate in candidates for fact_id in candidate.fact_source_ids
            )
        )

        if not dry_run:
            (
                key_value,
                canonical_topics,
                intent,
                normalized,
            ) = metadata[canonical.page_id]

            canonical_topics = tuple(
                normalize_topics(
                    canonical_topics,
                    fallback="Other",
                    max_topics=4,
                )
            )

            key_value = canonical_key(
                intent=intent,
                kind=_kind_for_answer(
                    canonical,
                    intent,
                ),
                topics=canonical_topics,
                answer_type=canonical.answer_type,
            )

            notion.update_page(
                canonical.page_id,
                {
                    "Canonical Key": P.rich_text(key_value),
                    "Topics": P.multi_select(list(canonical_topics)),
                    "Question Intent": P.select(intent),
                    "Normalized Answer": P.rich_text(normalized),
                    "Related Opportunities": P.relation(related_opportunities),
                    "Related Applications": P.relation(related_applications),
                    "Fact Sources": P.relation(fact_sources),
                    "Use Count": P.number(sum(candidate.use_count for candidate in candidates)),
                },
            )

        for opportunity_id in related_opportunities:
            if _replace_relation(
                notion,
                page_id=opportunity_id,
                property_name="Answer Bank Items",
                duplicate_ids=duplicate_ids,
                canonical_id=canonical.page_id,
                dry_run=dry_run,
            ):
                stats.opportunities_relinked += 1

        for application_id in related_applications:
            if _replace_relation(
                notion,
                page_id=application_id,
                property_name="Answer Bank Items",
                duplicate_ids=duplicate_ids,
                canonical_id=canonical.page_id,
                dry_run=dry_run,
            ):
                stats.applications_relinked += 1

        for requirement in all_requirements:
            if requirement.answer_id not in duplicate_ids:
                continue

            if not dry_run:
                notion.update_page(
                    requirement.page_id,
                    {
                        "Answer Bank Item": P.relation([canonical.page_id]),
                    },
                )

            stats.requirements_relinked += 1

        canonical.related_opportunity_ids = related_opportunities
        canonical.related_application_ids = related_applications
        canonical.fact_source_ids = fact_sources
        canonical.use_count = sum(candidate.use_count for candidate in candidates)

        for duplicate in duplicates:
            duplicate.status = "Deprecated"
            duplicate.superseded_by_ids = [canonical.page_id]
            duplicate.approved_for_reuse = False

            if not dry_run:
                notion.update_page(
                    duplicate.page_id,
                    {
                        "Status": P.select("Deprecated"),
                        "Superseded By": P.relation([canonical.page_id]),
                        "Approved for Reuse": P.checkbox(False),
                    },
                )

            stats.duplicates_deprecated += 1

        store.audit(
            actor="migrate-qualifications",
            action="merge_answer_duplicates",
            target_kind="answer",
            target_id=canonical.page_id,
            dry_run=dry_run,
            payload={
                "canonical_key": key,
                "duplicates": list(duplicate_ids),
            },
        )

    stats.canonical_answers = answers

    return stats


def _defer_backfill(
    *,
    page_id: str,
    reason: str,
    store: Store,
    stats: QualificationMigrationStats,
    dry_run: bool,
) -> None:
    stats.awaiting_deferred += 1
    stats.deferred.append(f"{page_id}: {reason}")

    store.audit(
        actor="migrate-qualifications",
        action="awaiting_backfill_deferred",
        target_kind="opportunity",
        target_id=page_id,
        dry_run=dry_run,
        payload={
            "reason": reason,
        },
    )


def _description_is_truncated(
    description: str,
) -> bool:
    cleaned = re.sub(
        r"(?:\s|&nbsp;|&#160;|<br\s*/?>)+$",
        "",
        description,
        flags=re.IGNORECASE,
    )

    return cleaned.endswith(
        (
            "\u2026",
            "...",
        )
    )


def find_awaiting_backfill_candidates(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    stats: QualificationMigrationStats,
    dry_run: bool,
) -> list[dict]:
    pages = list(
        notion.query_data_source(
            settings.notion.data_sources["opportunities"],
            filter_={
                "property": "Status",
                "select": {
                    "equals": "Awaiting Answer",
                },
            },
        )
    )

    candidates: list[dict] = []

    for page in pages:
        page_id = page.get(
            "id",
            "",
        )
        props = page.get("properties") or {}

        requirements = load_requirements(
            notion,
            settings,
            opportunity_id=page_id,
        )

        if requirements and P.read_number(props.get("Base Model Score")) is not None:
            continue

        stats.awaiting_candidates += 1

        description = P.read_text(props.get("Description")).strip()

        if not description:
            _defer_backfill(
                page_id=page_id,
                reason=("stored job description is empty"),
                store=store,
                stats=stats,
                dry_run=dry_run,
            )
            continue

        if _description_is_truncated(description):
            _defer_backfill(
                page_id=page_id,
                reason=("stored job description is truncated"),
                store=store,
                stats=stats,
                dry_run=dry_run,
            )
            continue

        candidates.append(page)

    return candidates


def defer_backfill_candidates(
    pages: list[dict],
    *,
    reason: str,
    store: Store,
    stats: QualificationMigrationStats,
    dry_run: bool,
) -> None:
    for page in pages:
        _defer_backfill(
            page_id=page.get(
                "id",
                "",
            ),
            reason=reason,
            store=store,
            stats=stats,
            dry_run=dry_run,
        )


def backfill_awaiting_opportunities(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    router: ModelRouter,
    client: LMStudioClient,
    stats: QualificationMigrationStats,
    dry_run: bool,
    pages: list[dict] | None = None,
) -> None:
    pages = (
        pages
        if pages is not None
        else find_awaiting_backfill_candidates(
            settings,
            notion=notion,
            store=store,
            stats=stats,
            dry_run=dry_run,
        )
    )

    for page in pages:
        result = run_score(
            settings,
            notion=notion,
            store=store,
            router=router,
            client=client,
            pages_override=[page],
            answers_override=(stats.canonical_answers),
            dry_run=dry_run,
        )

        stats.awaiting_backfilled += result.awaiting_answer + result.scored
        stats.errors.extend(result.errors)
