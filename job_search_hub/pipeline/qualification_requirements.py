"""Persist and deterministically resolve opportunity qualification requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from job_search_hub.config import Settings
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.answers import CachedAnswer
from job_search_hub.pipeline.qualification_keys import (
    canonical_key,
    question_family_for_intent,
    render_question,
)
from job_search_hub.pipeline.qualifications import (
    QualificationGate,
    QualificationRequirement,
    classify_answer,
)
from job_search_hub.topics import normalize_topics

_KIND_LABELS = {
    "skill": "Skill",
    "tool": "Tool",
    "credential": "Credential",
    "years_experience": "Years of Experience",
    "location": "Location",
    "relocation": "Relocation",
    "work_authorization": "Work Authorization",
    "other": "Other",
}

_IMPORTANCE_LABELS = {
    "hard_required": "Hard Required",
    "required": "Required",
    "preferred": "Preferred",
}

_RESOLUTION_LABELS = {
    "meets": "Meets",
    "partial": "Partial",
    "does_not_meet": "Does Not Meet",
    "unknown": "Unknown",
}

_KIND_KEYS_BY_LABEL = {label: key for key, label in _KIND_LABELS.items()}

_IMPORTANCE_KEYS_BY_LABEL = {label: key for key, label in _IMPORTANCE_LABELS.items()}


def _source_subject(value: str) -> str:
    """Recover a named tool from stored text when its bounded topic is Other."""
    subject = re.sub(
        r"^(?:experience|proficiency|familiarity|knowledge|skills?)\s+(?:with|in)\s+",
        "",
        value or "",
        flags=re.IGNORECASE,
    ).strip(" .")
    return subject if subject and len(subject) <= 120 else ""


def _canonicalize_requirement(
    requirement: QualificationRequirement,
) -> QualificationRequirement:
    """Return a requirement synchronized with the controlled topic vocabulary."""
    raw_keywords = tuple(requirement.keywords or requirement.topics)
    topics = tuple(
        normalize_topics(
            raw_keywords,
            fallback="Other",
            max_topics=4,
        )
    )

    intent = requirement.question_intent or "Other"
    answer_type = requirement.answer_type or "Yes/No"
    kind = requirement.kind or "other"

    return replace(
        requirement,
        question=render_question(
            intent=intent,
            topics=raw_keywords or topics,
            answer_type=answer_type,
        ),
        question_family=question_family_for_intent(intent),
        keywords=raw_keywords or topics,
        topics=topics,
        canonical_key=canonical_key(
            intent=intent,
            kind=kind,
            topics=topics,
            answer_type=answer_type,
        ),
    )


@dataclass
class PersistedQualification:
    page_id: str
    opportunity_id: str
    answer_id: str
    qualification: str
    canonical_key: str
    topics: tuple[str, ...]
    question_intent: str
    requirement_kind: str
    importance: str
    answer_type: str
    expected_operator: str
    expected_answer: str
    numeric_threshold: float | None
    meets_score: float
    partial_score: float
    does_not_meet_score: float
    score_weight: float
    failure_action: str
    resolution: str
    resolution_reason: str
    source_requirement: str
    fingerprint: str
    active: bool

    @property
    def outcome_score(self) -> float:
        if self.resolution == "Meets":
            return self.meets_score

        if self.resolution == "Partial":
            return self.partial_score

        return self.does_not_meet_score

    @property
    def is_awaiting(self) -> bool:
        return self.resolution in {
            "Awaiting Answer",
            "Unknown",
        }

    @property
    def hard_failure(self) -> bool:
        return self.failure_action == "Reject" and self.resolution == "Does Not Meet"

    def as_requirement(self) -> QualificationRequirement:
        kind = _KIND_KEYS_BY_LABEL.get(
            self.requirement_kind,
            "other",
        )
        importance = _IMPORTANCE_KEYS_BY_LABEL.get(
            self.importance,
            "preferred",
        )
        topics = tuple(self.topics)
        if topics == ("Other",):
            topics = (_source_subject(self.source_requirement or self.qualification) or "Other",)
        intent = self.question_intent or "Other"
        answer_type = self.answer_type or "Yes/No"

        return _canonicalize_requirement(
            QualificationRequirement(
                text=self.qualification,
                kind=kind,
                importance=importance,
                question=render_question(
                    intent=intent,
                    topics=topics,
                    answer_type=answer_type,
                ),
                question_family=question_family_for_intent(intent),
                answer_type=answer_type,
                minimum_value=self.numeric_threshold,
                keywords=topics,
                topics=topics,
                question_intent=intent,
                expected_operator=(self.expected_operator or "Human Review"),
                expected_answer=self.expected_answer,
                source_requirement=(self.source_requirement or self.qualification),
                canonical_key=canonical_key(
                    intent=intent,
                    kind=kind,
                    topics=topics,
                    answer_type=answer_type,
                ),
            )
        )


def requirement_fingerprint(
    opportunity_id: str,
    requirement: QualificationRequirement,
) -> str:
    requirement = _canonicalize_requirement(requirement)

    payload = "|".join(
        (
            opportunity_id,
            requirement.canonical_key,
            requirement.importance,
            requirement.expected_operator,
            requirement.expected_answer.strip().lower(),
            ("" if requirement.minimum_value is None else f"{requirement.minimum_value:g}"),
        )
    )

    return sha256(payload.encode("utf-8")).hexdigest()


def _read_requirement(
    page: dict,
) -> PersistedQualification:
    props = page.get("properties") or {}

    opportunity_ids = P.read_relation_ids(props.get("Opportunity"))
    answer_ids = P.read_relation_ids(props.get("Answer Bank Item"))

    def number(
        name: str,
        default: float,
    ) -> float:
        value = P.read_number(props.get(name))
        return default if value is None else float(value)

    topics = tuple(
        normalize_topics(
            P.read_multi_select(props.get("Topics")),
            fallback="Other",
            max_topics=4,
        )
    )

    requirement_kind = P.read_select(props.get("Requirement Kind"))
    kind = _KIND_KEYS_BY_LABEL.get(
        requirement_kind,
        "other",
    )
    question_intent = P.read_select(props.get("Question Intent")) or "Other"
    answer_type = P.read_select(props.get("Answer Type")) or "Yes/No"

    stored_canonical_key = P.read_text(props.get("Canonical Key"))
    normalized_canonical_key = canonical_key(
        intent=question_intent,
        kind=kind,
        topics=topics,
        answer_type=answer_type,
    )

    return PersistedQualification(
        page_id=page.get("id", ""),
        opportunity_id=(opportunity_ids[0] if opportunity_ids else ""),
        answer_id=(answer_ids[0] if answer_ids else ""),
        qualification=P.read_title(props.get("Qualification")),
        canonical_key=(normalized_canonical_key or stored_canonical_key),
        topics=topics,
        question_intent=question_intent,
        requirement_kind=requirement_kind,
        importance=P.read_select(props.get("Importance")),
        answer_type=answer_type,
        expected_operator=P.read_select(props.get("Expected Operator")),
        expected_answer=P.read_text(props.get("Expected Answer")),
        numeric_threshold=P.read_number(props.get("Numeric Threshold")),
        meets_score=number(
            "Meets Score",
            1.0,
        ),
        partial_score=number(
            "Partial Score",
            0.5,
        ),
        does_not_meet_score=number(
            "Does Not Meet Score",
            0.0,
        ),
        score_weight=number(
            "Score Weight",
            1.0,
        ),
        failure_action=P.read_select(props.get("Failure Action")),
        resolution=P.read_select(props.get("Resolution")),
        resolution_reason=P.read_text(props.get("Resolution Reason")),
        source_requirement=P.read_text(props.get("Source Requirement")),
        fingerprint=P.read_text(props.get("Requirement Fingerprint")),
        active=P.read_checkbox(props.get("Active")),
    )


def load_requirements(
    notion: NotionClient,
    settings: Settings,
    *,
    opportunity_id: str | None = None,
) -> list[PersistedQualification]:
    ds_id = settings.notion.data_sources["qualification_requirements"]

    filter_ = None

    if opportunity_id:
        filter_ = {
            "property": "Opportunity",
            "relation": {
                "contains": opportunity_id,
            },
        }

    return [
        _read_requirement(page)
        for page in notion.query_data_source(
            ds_id,
            filter_=filter_,
        )
    ]


def _resolution_for_gate_item(
    resolution,
) -> str:
    answer = resolution.answer

    if resolution.status in {
        "unknown",
        "partial",
    } and (answer is None or answer.status != "Approved" or not answer.answer.strip()):
        return "Awaiting Answer"

    return _RESOLUTION_LABELS.get(
        resolution.status,
        "Unknown",
    )


def upsert_gate_requirements(
    notion: NotionClient,
    settings: Settings,
    *,
    opportunity_id: str,
    gate: QualificationGate,
    dry_run: bool,
) -> list[PersistedQualification]:
    existing = {
        row.fingerprint: row
        for row in load_requirements(
            notion,
            settings,
            opportunity_id=opportunity_id,
        )
        if row.fingerprint
    }

    results: list[PersistedQualification] = []
    active_fingerprints: set[str] = set()

    for resolution in gate.resolutions:
        requirement = _canonicalize_requirement(resolution.requirement)

        fingerprint = requirement_fingerprint(
            opportunity_id,
            requirement,
        )
        active_fingerprints.add(fingerprint)

        importance = _IMPORTANCE_LABELS.get(
            requirement.importance,
            "Preferred",
        )
        resolution_label = _resolution_for_gate_item(resolution)

        score_weight = {
            "Hard Required": 3.0,
            "Required": 2.0,
            "Preferred": 1.0,
        }[importance]

        failure_action = "Reject" if importance == "Hard Required" else "Score Only"

        properties = {
            "Qualification": P.title(requirement.text),
            "Opportunity": P.relation([opportunity_id]),
            "Answer Bank Item": P.relation(
                [resolution.answer.page_id]
                if (resolution.answer and resolution.answer.page_id)
                else []
            ),
            "Canonical Key": P.rich_text(requirement.canonical_key),
            "Topics": P.multi_select(list(requirement.topics)),
            "Question Intent": P.select(requirement.question_intent or "Other"),
            "Requirement Kind": P.select(
                _KIND_LABELS.get(
                    requirement.kind,
                    "Other",
                )
            ),
            "Importance": P.select(importance),
            "Answer Type": P.select(requirement.answer_type or "Yes/No"),
            "Expected Operator": P.select(requirement.expected_operator or "Human Review"),
            "Expected Answer": P.rich_text(requirement.expected_answer),
            "Numeric Threshold": P.number(requirement.minimum_value),
            "Meets Score": P.number(1),
            "Partial Score": P.number(0.5),
            "Does Not Meet Score": P.number(0),
            "Score Weight": P.number(score_weight),
            "Failure Action": P.select(failure_action),
            "Resolution": P.select(resolution_label),
            "Resolution Reason": P.rich_text(resolution.reason),
            "Source Requirement": P.rich_text(requirement.source_requirement or requirement.text),
            "Requirement Fingerprint": P.rich_text(fingerprint),
            "Active": P.checkbox(True),
            "Last Evaluated": P.date_property(datetime.now(UTC)),
        }

        current = existing.get(fingerprint)
        page_id = current.page_id if current else ""

        if not dry_run:
            if current:
                notion.update_page(
                    page_id,
                    properties,
                )
            else:
                page = notion.create_page(
                    parent={
                        "data_source_id": (
                            settings.notion.data_sources["qualification_requirements"]
                        )
                    },
                    properties=properties,
                )
                page_id = page.get(
                    "id",
                    "",
                )

        results.append(
            PersistedQualification(
                page_id=(page_id or f"dry-run-{fingerprint[:20]}"),
                opportunity_id=opportunity_id,
                answer_id=(resolution.answer.page_id if resolution.answer is not None else ""),
                qualification=requirement.text,
                canonical_key=(requirement.canonical_key),
                topics=requirement.topics,
                question_intent=(requirement.question_intent or "Other"),
                requirement_kind=_KIND_LABELS.get(
                    requirement.kind,
                    "Other",
                ),
                importance=importance,
                answer_type=(requirement.answer_type or "Yes/No"),
                expected_operator=(requirement.expected_operator or "Human Review"),
                expected_answer=(requirement.expected_answer),
                numeric_threshold=(requirement.minimum_value),
                meets_score=1.0,
                partial_score=0.5,
                does_not_meet_score=0.0,
                score_weight=score_weight,
                failure_action=failure_action,
                resolution=resolution_label,
                resolution_reason=(resolution.reason),
                source_requirement=(requirement.source_requirement or requirement.text),
                fingerprint=fingerprint,
                active=True,
            )
        )

    for fingerprint, stale in existing.items():
        if fingerprint in active_fingerprints or not stale.active:
            continue

        if not dry_run:
            notion.update_page(
                stale.page_id,
                {"Active": P.checkbox(False)},
            )

    return results


def resolve_persisted_requirement(
    requirement: PersistedQualification,
    answer: CachedAnswer,
) -> tuple[str, str]:
    normalized_requirement = requirement.as_requirement()

    result = classify_answer(
        normalized_requirement,
        answer,
    )

    if result is None:
        return (
            "Partial",
            "approved answer requires human interpretation",
        )

    status, reason = result

    return (
        _RESOLUTION_LABELS.get(
            status,
            "Unknown",
        ),
        reason,
    )


def qualification_score(
    requirements: list[PersistedQualification],
) -> float:
    active = [requirement for requirement in requirements if requirement.active]

    weight = sum(requirement.score_weight for requirement in active)

    if not weight:
        return 1.0

    weighted_score = sum(
        requirement.score_weight * requirement.outcome_score for requirement in active
    )

    return weighted_score / weight
