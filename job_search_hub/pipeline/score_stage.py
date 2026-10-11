"""Score stage: load Scoring Criteria, score opportunities awaiting scoring."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.ingest.base import RawPosting
from job_search_hub.logging_setup import get_logger
from job_search_hub.models import LMStudioClient, ModelRouter, StructuredOutputError
from job_search_hub.notify import Notifier, stable_key
from job_search_hub.notion import properties as Pread
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.answers import (
    CachedAnswer,
    cosine_similarity,
    create_needs_answer_page,
    link_answer_to_opportunity,
    load_answer_bank,
    normalize_question,
)
from job_search_hub.pipeline.answers import (
    create_needs_answer as _legacy_create_needs_answer,
)
from job_search_hub.pipeline.discovery import score_preflight
from job_search_hub.pipeline.evaluators import (
    FastFitAssessment,
    evaluate_criterion,
    evaluate_fast_fit_batch,
    evaluate_llm_batch,
)
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.posting_dates import (
    DATE_JOB_WAS_POSTED,
    POSTING_RECENCY_KEY,
    posting_recency_score,
    read_posted_date,
)
from job_search_hub.pipeline.posting_verification import verify_active_opportunities
from job_search_hub.pipeline.profile import Fact, load_facts
from job_search_hub.pipeline.progress import (
    StagePhase,
    report_item_failed,
    report_stage_complete,
    report_stage_detail,
    report_stage_heartbeat,
    report_stage_progress,
    report_stage_queue,
    report_stage_start,
)
from job_search_hub.pipeline.qualification_requirements import (
    PersistedQualification,
    load_requirements,
    qualification_score,
    resolve_persisted_requirement,
    upsert_gate_requirements,
)
from job_search_hub.pipeline.qualifications import (
    QualificationGate,
    evaluate_qualification_gate,
    resolve_requirement_from_facts,
)
from job_search_hub.pipeline.score import (
    ComponentResult,
    Criterion,
    ScoringResult,
    assign_lane,
    load_active_criteria,
    write_scores,
)

log = get_logger(__name__)

# Kept as a module attribute for tests and older callers that monkeypatch this
# name while the production path uses create_needs_answer_page for Notion URLs.
create_needs_answer = _legacy_create_needs_answer

_RESCORE_PROTECTED_STATUSES = {"Rejected", "Submitted", "Interviewing", "Offer", "Closed"}
_NORMAL_SCORE_STATUSES = {"Fetched", "Scoring"}
_SCORELESS_ACTIVE_STATUSES = {
    "Review",
    "Needs Input",
    "Shortlisted",
}
_REMOTEOK_PENALTY = 0.08
_FOREIGN_MARKET_PENALTY = 0.20
_MIXED_MARKET_PENALTY = 0.10
_FOREIGN_MARKET_CAP = 0.49
_CORE_MISMATCH_CAP = 0.24
_DEFAULT_POSTING_RECENCY_WEIGHT = 3.0
_FAST_FIT_BATCH_SIZE = 16
_FAST_FIT_KEYS = {
    "transferable_skill_fit",
    "required_skill_overlap",
    "interview_plausibility",
}


@dataclass
class ScoreStats:
    scored: int = 0
    excluded: int = 0
    criteria_active: int = 0
    errors: list[str] = field(default_factory=list)
    degraded_evaluations: int = 0
    awaiting_answer: int = 0
    awaiting_resolved: int = 0
    awaiting_requeued: int = 0
    posting_review: int = 0
    questions_created: int = 0
    questions_reused: int = 0
    answer_notifications: list[AnswerNotificationItem] = field(default_factory=list)
    by_lane: dict[str, int] = field(
        default_factory=lambda: {
            "Worth a Look": 0,
            "Fast Review": 0,
            "High Attention": 0,
            "Skip": 0,
        }
    )

    def absorb(self, other: ScoreStats) -> ScoreStats:
        self.scored += other.scored
        self.excluded += other.excluded
        self.criteria_active = max(self.criteria_active, other.criteria_active)
        self.errors.extend(other.errors)
        self.degraded_evaluations += other.degraded_evaluations
        self.awaiting_answer += other.awaiting_answer
        self.awaiting_resolved += other.awaiting_resolved
        self.awaiting_requeued += other.awaiting_requeued
        self.posting_review += other.posting_review
        self.questions_created += other.questions_created
        self.questions_reused += other.questions_reused
        self.answer_notifications.extend(other.answer_notifications)
        for lane, count in other.by_lane.items():
            self.by_lane[lane] = self.by_lane.get(lane, 0) + count
        return self

    def __str__(self) -> str:
        error_sample = self.errors[:3]
        more_errors = max(0, len(self.errors) - len(error_sample))
        error_text = f", error_sample={error_sample}" if error_sample else ""
        if more_errors:
            error_text += f", errors_more={more_errors}"
        return (
            "ScoreStats("
            f"scored={self.scored}, excluded={self.excluded}, "
            f"criteria_active={self.criteria_active}, errors={len(self.errors)}"
            f"{error_text}, degraded_evaluations={self.degraded_evaluations}, "
            f"awaiting_answer={self.awaiting_answer}, "
            f"awaiting_resolved={self.awaiting_resolved}, "
            f"awaiting_requeued={self.awaiting_requeued}, "
            f"posting_review={self.posting_review}, "
            f"questions_created={self.questions_created}, "
            f"questions_reused={self.questions_reused}, "
            f"answer_notifications={len(self.answer_notifications)}, "
            f"by_lane={dict(self.by_lane)}"
            ")"
        )


@dataclass(frozen=True)
class AnswerNotificationItem:
    answer_id: str
    question: str
    status: str
    importance: str
    company: str
    role_title: str
    opportunity_id: str
    opportunity_url: str
    answer_url: str
    action: str


@dataclass(frozen=True)
class _ComputedOpportunity:
    gate: QualificationGate
    base_result: ScoringResult
    persist_gate: bool


def _score_opportunity(
    page: dict,
    criteria: list[Criterion],
    *,
    settings: Settings,
    profile_facts: str = "",
    router: ModelRouter | None,
    client: LMStudioClient | None,
    model_role: str = "primary",
    fast_fit: FastFitAssessment | None = None,
    criterion_progress: Callable[[int, int, str, str], None] | None = None,
    qualification_gate: QualificationGate | None = None,
) -> ScoringResult:
    props = page.get("properties") or {}
    role_title = Pread.read_text(props.get("Role Title"))
    company = Pread.read_text(props.get("Company"))
    description = Pread.read_text(props.get("Description"))
    location = Pread.read_text(props.get("Location"))
    posting_url = Pread.read_url(props.get("URL"))
    source = Pread.read_select(props.get("Source"))
    source_adapter = Pread.read_select(props.get("Source Adapter"))
    posted_at = read_posted_date(props)
    top_matches = Pread.read_text(props.get("Top Matches"))
    comp_min = Pread.read_number(props.get("Compensation Min"))
    comp_max = Pread.read_number(props.get("Compensation Max"))
    comp = comp_max if comp_max is not None else comp_min
    posting = RawPosting(
        source=source,
        source_adapter=source_adapter,
        external_job_id=Pread.read_text(props.get("External Job ID")),
        company=company,
        role_title=role_title,
        url=posting_url,
        description=description,
        location=location,
    )
    preflight = score_preflight(posting, settings)
    if not preflight.accepted:
        return ScoringResult(
            page_id=page.get("id", ""),
            total_score=0.0,
            confidence=1.0,
            lane="Skip",
            hard_excluded=True,
            exclusion_reason=f"Preflight: {preflight.reason}",
            concerns=preflight.reason,
        )

    text = "\n".join(
        [
            f"Company: {company}",
            f"Role: {role_title}",
            f"Source: {source} ({source_adapter})",
            f"URL: {posting_url}",
            f"{DATE_JOB_WAS_POSTED}: {posted_at.isoformat() if posted_at else 'Unknown'}",
            f"Location: {location}",
            f"Matched terms: {top_matches}",
            "",
            "Verified candidate facts:",
            profile_facts or "(no verified facts available)",
            "",
            "Job description:",
            description,
        ]
    )

    components_by_index = {}
    hard_exclude = False
    exclusion_reason = ""
    model_criteria: list[tuple[int, Criterion]] = []

    for criterion_number, criterion in enumerate(criteria, start=1):
        method = criterion.evaluation_method.lower()
        if fast_fit is not None and method in {"llm", "hybrid"}:
            key = criterion.key or criterion.name
            if key in _FAST_FIT_KEYS:
                score = float(getattr(fast_fit, key))
                if key == "required_skill_overlap" and fast_fit.missing_mandatory:
                    score = min(score, 0.2)
                components_by_index[criterion_number - 1] = ComponentResult(
                    criterion_key=key,
                    criterion_name=criterion.name,
                    category=criterion.category,
                    evaluation_method="Fast Fit LLM",
                    score=max(0.0, min(1.0, score)),
                    weight=criterion.weight,
                    direction=criterion.direction,
                    reason=fast_fit.reason,
                )
            else:
                components_by_index[criterion_number - 1] = ComponentResult(
                    criterion_key=key,
                    criterion_name=criterion.name,
                    category=criterion.category,
                    evaluation_method="Fast Fit Neutral",
                    score=0.5,
                    weight=criterion.weight,
                    direction=criterion.direction,
                    reason="no decisive evidence in quick rescore; neutral",
                )
            continue
        if method in {"llm", "hybrid"} and router is not None and client is not None:
            model_criteria.append((criterion_number - 1, criterion))
            continue
        if criterion_progress and criterion_number in {1, len(criteria)}:
            criterion_progress(
                criterion_number,
                len(criteria),
                criterion.name,
                criterion.evaluation_method,
            )
        result = evaluate_criterion(
            criterion,
            posting_text=text,
            posting_comp=comp,
            router=router,
            client=client,
            role_title=role_title,
            description=description,
            location=location,
            posting_url=posting_url,
            posted_at=posted_at,
            settings=settings,
        )
        components_by_index[criterion_number - 1] = result
        if result.triggered_hard_exclusion:
            hard_exclude = True
            exclusion_reason = f"{criterion.name}: {result.reason}"

    if model_criteria:
        if criterion_progress:
            first_index = model_criteria[0][0] + 1
            criterion_progress(
                first_index,
                len(criteria),
                f"{len(model_criteria)} model-driven criteria",
                "Batched LLM",
            )
        batch_results = evaluate_llm_batch(
            [criterion for _, criterion in model_criteria],
            text,
            router=router,
            client=client,
            model_role=model_role,
        )
        for (criterion_index, criterion), result in zip(
            model_criteria,
            batch_results,
            strict=True,
        ):
            components_by_index[criterion_index] = result
            if result.triggered_hard_exclusion:
                hard_exclude = True
                exclusion_reason = f"{criterion.name}: {result.reason}"

    components = [components_by_index[index] for index in range(len(criteria))]

    # Positive criteria establish the score directly. Negative criteria reduce
    # that average; there is no additional baseline that can inflate neutral
    # or unknown evidence to a perfect score.
    positive = [c for c in components if c.direction == "Positive" and c.weight > 0]
    positive_weight = sum(c.weight for c in positive) or 1.0
    total = sum(c.weight * c.score for c in positive) / positive_weight
    non_recency_positive = [c for c in positive if c.criterion_key != POSTING_RECENCY_KEY]
    if non_recency_positive and all(
        c.fallback_used and c.score <= 0.0 for c in non_recency_positive
    ):
        total = 0.0
    negative = [
        c
        for c in components
        if c.direction == "Negative"
        and c.weight > 0
        and c.criterion_key not in {"remoteok_source_quality", "foreign_target_market"}
    ]
    total -= sum(c.weight * c.score for c in negative) / positive_weight

    concerns: list[str] = []
    if preflight.remoteok:
        total -= _REMOTEOK_PENALTY
        concerns.append("RemoteOK source quality penalty")
    if preflight.target_market.level == "foreign":
        total = min(total - _FOREIGN_MARKET_PENALTY, _FOREIGN_MARKET_CAP)
        concerns.append("foreign target market: " + ", ".join(preflight.target_market.countries))
    elif preflight.target_market.level == "mixed":
        total -= _MIXED_MARKET_PENALTY
        concerns.append(
            "mixed US/foreign target market: " + ", ".join(preflight.target_market.countries)
        )

    by_key = {component.criterion_key: component for component in components}
    recency = posting_recency_score(posted_at)
    if recency.days_old is not None:
        if recency.days_old >= 120:
            total = min(total, 0.35)
            concerns.append("posting is 120+ days old")
        elif recency.days_old >= 90:
            total = min(total, 0.45)
            concerns.append("posting is 90+ days old")
        elif recency.days_old >= 60:
            total = min(total, 0.60)
            concerns.append("posting is 60+ days old")
    core_fit = by_key.get("core_role_family_fit")
    required_fit = by_key.get("required_skill_overlap")
    if (core_fit is not None and core_fit.score <= 0.2) or (
        required_fit is not None and required_fit.score <= 0.2
    ):
        total = min(total, _CORE_MISMATCH_CAP)
        concerns.append("missing core role-family fit or mandatory qualifications")
    total = max(0.0, min(1.0, total))
    if qualification_gate is not None:
        required_failures = [
            resolution
            for resolution in qualification_gate.resolutions
            if resolution.requirement.importance == "required"
            and resolution.status == "does_not_meet"
        ]
        preferred_failures = [
            resolution
            for resolution in qualification_gate.resolutions
            if resolution.requirement.importance == "preferred"
            and resolution.status == "does_not_meet"
        ]
        if required_failures:
            total = min(total, _CORE_MISMATCH_CAP)
            concerns.append(
                "does not meet required qualification(s): "
                + ", ".join(r.requirement.text for r in required_failures[:3])
            )
        if preferred_failures:
            total = max(0.0, total - min(0.12, 0.03 * len(preferred_failures)))
            concerns.append(
                "does not meet preferred qualification(s): "
                + ", ".join(r.requirement.text for r in preferred_failures[:3])
            )

    # Confidence: average of component scores normalized by weight.
    weight_total = sum(abs(c.weight) for c in components) or 1.0
    confidence = (
        sum(c.score * abs(c.weight) for c in components) / weight_total if components else 0.0
    )
    if components:
        fallback_ratio = sum(1 for c in components if c.fallback_used) / len(components)
        confidence = max(0.0, confidence * (1.0 - 0.5 * fallback_ratio))

    lane = "Skip" if hard_exclude else assign_lane(total, components)
    if posted_at is None and not hard_exclude:
        if lane != "Skip":
            lane = "High Attention"
        concerns.append("Posting date unknown; review the employer posting before applying")
    top_matches = ""
    if qualification_gate is not None:
        top_matches = ", ".join(
            dict.fromkeys(
                keyword
                for resolution in qualification_gate.resolutions
                if resolution.status == "meets"
                for keyword in resolution.requirement.keywords
            )
        )[:1900]

    return ScoringResult(
        page_id=page.get("id", ""),
        total_score=total,
        confidence=confidence,
        lane=lane,
        hard_excluded=hard_exclude,
        exclusion_reason=exclusion_reason,
        components=components,
        top_matches=top_matches,
        concerns="; ".join(concerns),
    )


def _clear_score_properties() -> dict:
    return {
        "Model Score": Pread.number(None),
        "Confidence": Pread.number(None),
        "Model Lane": Pread.select(None),
        "Model Reason": Pread.rich_text(""),
        "Top Matches": Pread.rich_text(""),
    }


def _qualification_criterion(
    criteria: list[Criterion],
    *,
    required: bool = True,
) -> Criterion | None:
    matches = [
        criterion
        for criterion in criteria
        if (getattr(criterion, "key", "") or "").strip().lower() == "qualification_fit"
    ]
    if not matches and not required:
        return None
    if len(matches) != 1:
        raise RuntimeError(
            "exactly one active Scoring Criteria row with Key=qualification_fit is required"
        )
    criterion = matches[0]
    if criterion.direction != "Positive" or criterion.weight <= 0:
        raise RuntimeError("qualification_fit must be a positive criterion with Weight > 0")
    return criterion


def _base_criteria(criteria: list[Criterion]) -> list[Criterion]:
    return [
        criterion
        for criterion in criteria
        if (criterion.key or "").strip().lower() != "qualification_fit"
    ]


def _ensure_posting_recency_criterion(criteria: list[Criterion]) -> list[Criterion]:
    if any(
        (getattr(criterion, "key", "") or "").strip().lower() == POSTING_RECENCY_KEY
        for criterion in criteria
    ):
        return criteria
    sort_order = (
        max((getattr(criterion, "sort_order", 0) or 0 for criterion in criteria), default=0) + 1
    )
    return [
        *criteria,
        Criterion(
            page_id="",
            name="Posting recency",
            key=POSTING_RECENCY_KEY,
            active=True,
            category="Timing",
            weight=_DEFAULT_POSTING_RECENCY_WEIGHT,
            direction="Positive",
            evaluation_method="Rule",
            hard_exclusion=False,
            lane_override="",
            human_review_trigger=False,
            instructions=(
                "Default deterministic freshness component injected when Notion "
                "does not define posting_recency."
            ),
            minimum_value=None,
            maximum_value=None,
            sort_order=sort_order,
        ),
    ]


def _base_weight(result: ScoringResult) -> float:
    return (
        sum(
            component.weight
            for component in result.components
            if component.direction == "Positive" and component.weight > 0
        )
        or 1.0
    )


def _base_lane_override(result: ScoringResult) -> str:
    return next(
        (
            component.lane_override
            for component in result.components
            if component.lane_override and component.lane_override != "None"
        ),
        "None",
    )


def _base_score_properties(result: ScoringResult) -> dict:
    weight = _base_weight(result)
    return {
        "Base Model Score": Pread.number(round(result.total_score, 4)),
        "Base Score Numerator": Pread.number(round(result.total_score * weight, 6)),
        "Base Score Weight": Pread.number(round(weight, 4)),
        "Base Confidence": Pread.number(round(result.confidence, 4)),
        "Base Model Reason": Pread.rich_text(result.model_reason()),
        "Base Top Matches": Pread.rich_text(result.top_matches),
        "Base Concerns": Pread.rich_text(result.concerns),
        "Base Lane Override": Pread.select(_base_lane_override(result)),
    }


def _published_score_properties(
    *,
    props: dict,
    requirements: list[PersistedQualification],
    qualification_criterion: Criterion,
    my_decision: str,
) -> tuple[dict, float, str]:
    base_score = Pread.read_number(props.get("Base Model Score"))
    base_weight = Pread.read_number(props.get("Base Score Weight"))
    base_numerator = Pread.read_number(props.get("Base Score Numerator"))
    if base_score is None or base_weight is None:
        raise RuntimeError("awaiting opportunity has no persisted base score")
    if base_numerator is None:
        base_numerator = base_score * base_weight
    qual_score = qualification_score(requirements)
    total_weight = base_weight + qualification_criterion.weight
    total = (base_numerator + qual_score * qualification_criterion.weight) / total_weight
    total = max(0.0, min(1.0, total))
    override = Pread.read_select(props.get("Base Lane Override"))
    lane = override if override and override != "None" else assign_lane(total, [])
    base_confidence = Pread.read_number(props.get("Base Confidence")) or 0.0
    confidence = (base_confidence * base_weight + qualification_criterion.weight) / total_weight
    meets_topics = list(
        dict.fromkeys(
            topic
            for requirement in requirements
            if requirement.resolution == "Meets"
            for topic in requirement.topics
        )
    )
    base_matches = Pread.read_text(props.get("Base Top Matches"))
    top_matches = ", ".join(
        dict.fromkeys(
            [
                *[value.strip() for value in base_matches.split(",") if value.strip()],
                *meets_topics,
            ]
        )
    )
    qualification_reason = "; ".join(
        f"{requirement.qualification}: {requirement.resolution}"
        for requirement in requirements
        if requirement.active
    )
    base_reason = Pread.read_text(props.get("Base Model Reason"))
    base_concerns = Pread.read_text(props.get("Base Concerns"))
    qualification_concerns = "; ".join(
        requirement.qualification
        for requirement in requirements
        if requirement.resolution in {"Partial", "Does Not Meet"}
    )
    status = "Rejected" if my_decision == "Skip" or lane == "Skip" else "Review"
    return (
        {
            "Model Score": Pread.number(round(total, 4)),
            "Confidence": Pread.number(round(confidence, 4)),
            "Model Lane": Pread.select(lane),
            "Model Reason": Pread.rich_text(
                "\n".join(
                    value
                    for value in (
                        base_reason,
                        (f"+ qualification_fit: {qual_score:.2f} ({qualification_reason})"),
                    )
                    if value
                )
            ),
            "Top Matches": Pread.rich_text(top_matches[:1900]),
            "Concerns": Pread.rich_text(
                "; ".join(
                    value
                    for value in (
                        base_concerns,
                        qualification_concerns,
                    )
                    if value
                )[:1900]
            ),
            "Qualification Score": Pread.number(round(qual_score, 4)),
            "Hard Exclusion": Pread.checkbox(False),
            "Exclusion Reason": Pread.rich_text(""),
            "Status": Pread.select(status),
            "Last Error": Pread.rich_text(""),
        },
        total,
        lane,
    )


def _run_awaiting_resolution(
    *,
    pages: list[dict],
    criteria: list[Criterion],
    answers: list[CachedAnswer],
    notion: NotionClient,
    settings: Settings,
    store: Store,
    stats: ScoreStats,
    dry_run: bool,
) -> ScoreStats:
    persisted_enabled = bool(settings.notion.data_sources.get("qualification_requirements"))
    qualification_criterion = _qualification_criterion(
        criteria,
        required=persisted_enabled,
    )
    if not persisted_enabled or qualification_criterion is None:
        stats.awaiting_answer += len(pages)
        report_stage_complete(
            StagePhase.SCORE,
            items_completed=len(pages),
            items_failed=0,
        )
        return stats
    answers_by_id = {answer.page_id: answer for answer in answers if answer.page_id}
    facts: list[Fact] = []
    if settings.notion.data_sources.get("resume_facts"):
        try:
            facts = load_facts(notion, settings)
        except Exception as exc:
            log.warning("score-awaiting: could not load resume facts: %s", exc)
    for index, page in enumerate(pages, start=1):
        props = page.get("properties") or {}
        page_id = page.get("id", "")
        company = Pread.read_text(props.get("Company")) or "Unknown company"
        role = Pread.read_text(props.get("Role Title")) or "Unknown role"
        item_label = f"{company} - {role}"
        try:
            requirements = load_requirements(
                notion,
                settings,
                opportunity_id=page_id,
            )
            if not requirements or Pread.read_number(props.get("Base Model Score")) is None:
                if not dry_run:
                    notion.update_page(
                        page_id,
                        {
                            "Status": Pread.select("Scoring"),
                            "Last Error": Pread.rich_text(
                                "Legacy Awaiting Answer row lacks persisted "
                                "qualification context; requeued for scoring."
                            ),
                        },
                    )
                stats.awaiting_requeued += 1
                store.audit(
                    actor="score-awaiting",
                    action="awaiting_requirements_requeued",
                    target_kind="opportunity",
                    target_id=page_id,
                    dry_run=dry_run,
                )
                continue

            proposed: list[tuple[PersistedQualification, str, str]] = []
            pending = False
            for requirement in requirements:
                if not requirement.active:
                    continue
                if not requirement.answer_id:
                    fact_answer = resolve_requirement_from_facts(
                        requirement.as_requirement(),
                        facts,
                    )
                    if fact_answer is not None:
                        resolution, reason = resolve_persisted_requirement(
                            requirement,
                            fact_answer,
                        )
                        proposed.append((requirement, resolution, reason))
                        continue
                    if requirement.resolution in {"Meets", "Partial", "Does Not Meet"}:
                        continue
                    pending = True
                    break
                answer = answers_by_id.get(requirement.answer_id)
                if answer is None or answer.status != "Approved" or not answer.answer.strip():
                    fact_answer = resolve_requirement_from_facts(
                        requirement.as_requirement(),
                        facts,
                    )
                    if fact_answer is not None:
                        resolution, reason = resolve_persisted_requirement(
                            requirement,
                            fact_answer,
                        )
                        proposed.append((requirement, resolution, reason))
                        continue
                    pending = True
                    break
                resolution, reason = resolve_persisted_requirement(
                    requirement,
                    answer,
                )
                proposed.append((requirement, resolution, reason))
            if pending:
                stats.awaiting_answer += 1
                store.audit(
                    actor="score-awaiting",
                    action="awaiting_qualification_answers_unchanged",
                    target_kind="opportunity",
                    target_id=page_id,
                    dry_run=dry_run,
                )
                continue

            for requirement, resolution, reason in proposed:
                requirement.resolution = resolution
                requirement.resolution_reason = reason
            hard_failures = [
                requirement for requirement in requirements if requirement.hard_failure
            ]
            if not dry_run:
                for requirement, resolution, reason in proposed:
                    notion.update_page(
                        requirement.page_id,
                        {
                            "Resolution": Pread.select(resolution),
                            "Resolution Reason": Pread.rich_text(reason),
                            "Last Evaluated": Pread.date_property(datetime.now(UTC)),
                        },
                    )
                if hard_failures:
                    reason = "; ".join(
                        f"{requirement.qualification}: {requirement.resolution_reason}"
                        for requirement in hard_failures
                    )
                    notion.update_page(
                        page_id,
                        {
                            **_clear_score_properties(),
                            "Qualification Score": Pread.number(
                                round(qualification_score(requirements), 4)
                            ),
                            "Hard Exclusion": Pread.checkbox(True),
                            "Exclusion Reason": Pread.rich_text(reason[:1900]),
                            "Concerns": Pread.rich_text(reason[:1900]),
                            "Status": Pread.select("Rejected"),
                            "Last Error": Pread.rich_text(""),
                        },
                    )
                else:
                    public, _total, _lane = _published_score_properties(
                        props=props,
                        requirements=requirements,
                        qualification_criterion=qualification_criterion,
                        my_decision=Pread.read_select(props.get("My Decision")),
                    )
                    notion.update_page(page_id, public)
            if hard_failures:
                stats.excluded += 1
                stats.by_lane["Skip"] = stats.by_lane.get("Skip", 0) + 1
                store.audit(
                    actor="score-awaiting",
                    action="qualification_hard_rejection",
                    target_kind="opportunity",
                    target_id=page_id,
                    dry_run=dry_run,
                )
            else:
                public, total, lane = _published_score_properties(
                    props=props,
                    requirements=requirements,
                    qualification_criterion=qualification_criterion,
                    my_decision=Pread.read_select(props.get("My Decision")),
                )
                stats.scored += 1
                stats.awaiting_resolved += 1
                stats.by_lane[lane] = stats.by_lane.get(lane, 0) + 1
                store.audit(
                    actor="score-awaiting",
                    action="resolved_qualification_score",
                    target_kind="opportunity",
                    target_id=page_id,
                    dry_run=dry_run,
                    payload={"score": total, "lane": lane},
                )
        except Exception as exc:
            log.exception("score-awaiting: failed for page %s", page_id)
            stats.errors.append(str(exc))
            report_item_failed(StagePhase.SCORE)
        finally:
            report_stage_progress(
                StagePhase.SCORE,
                current_item=item_label,
                items_completed=index,
                current_step="awaiting qualification resolution finished",
                force=True,
            )
    report_stage_complete(
        StagePhase.SCORE,
        items_completed=len(pages),
        items_failed=len(stats.errors),
    )
    return stats


def _new_cached_answer(
    *,
    page_id: str,
    question: str,
    question_family: str,
    answer_type: str,
    opportunity_id: str,
    canonical_key: str = "",
    topics: tuple[str, ...] = (),
    question_intent: str = "",
) -> CachedAnswer:
    return CachedAnswer(
        page_id=page_id,
        question=question,
        normalized_question=normalize_question(question),
        question_family=question_family,
        answer_type=answer_type,
        answer="",
        status="Needs Answer",
        approved_for_reuse=False,
        sensitive=False,
        context_specific=False,
        reuse_threshold=0.92,
        canonical_key=canonical_key,
        topics=topics,
        question_intent=question_intent,
        related_opportunity_ids=[opportunity_id],
    )


def _answer_notification_item(
    *,
    page: dict,
    answer: CachedAnswer,
    requirement: str,
    importance: str,
    action: str,
    answer_url: str = "",
) -> AnswerNotificationItem:
    props = page.get("properties") or {}
    return AnswerNotificationItem(
        answer_id=answer.page_id,
        question=answer.question or requirement,
        status=answer.status or "Needs Answer",
        importance=importance,
        company=Pread.read_text(props.get("Company")) or "Unknown company",
        role_title=Pread.read_text(props.get("Role Title")) or "Unknown role",
        opportunity_id=page.get("id", ""),
        opportunity_url=page.get("url", ""),
        answer_url=answer_url,
        action=action,
    )


def _dedupe_answer_notifications(
    items: list[AnswerNotificationItem],
) -> list[AnswerNotificationItem]:
    by_answer: dict[str, AnswerNotificationItem] = {}
    for item in items:
        key = item.answer_id or stable_key(item.question, item.opportunity_id)
        current = by_answer.get(key)
        if current is None or (current.action != "created" and item.action == "created"):
            by_answer[key] = item
    return sorted(
        by_answer.values(),
        key=lambda item: (
            item.company.lower(),
            item.role_title.lower(),
            item.question.lower(),
        ),
    )


def format_answer_digest(items: list[AnswerNotificationItem]) -> str:
    """Format a single organized email for all newly needed answers."""
    items = _dedupe_answer_notifications(items)
    if not items:
        return ""
    opportunity_count = len({item.opportunity_id for item in items if item.opportunity_id})
    created_count = sum(1 for item in items if item.action == "created")
    reused_count = sum(1 for item in items if item.action == "reused")
    lines = [
        "Job Search Hub Answer Bank Digest",
        "",
        "Summary",
        f"- Questions needing attention: {len(items)}",
        f"- Affected opportunities: {opportunity_count}",
        f"- New Answer Bank rows: {created_count}",
        f"- Existing pending rows reused: {reused_count}",
        "",
        "Action Needed",
    ]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                "",
                f"{index}. {item.company} - {item.role_title}",
                f"   Question: {item.question}",
                f"   Importance: {item.importance or '-'}",
                f"   Answer status: {item.status or '-'}",
                f"   Answer Bank: {item.answer_url or item.answer_id or '-'}",
                f"   Opportunity: {item.opportunity_url or item.opportunity_id or '-'}",
            ]
        )
    lines.extend(
        [
            "",
            "What to do next",
            "- Fill in and approve the linked Answer Bank rows.",
            "- Then run `jobhub run`, or let the next tick retry them.",
            "",
            "Notes",
            "- This digest intentionally replaces per-question emails.",
            (
                "- Already answered or resume-backed facts should be resolved by "
                "the scoring pipeline before reaching this list."
            ),
        ]
    )
    return "\n".join(lines)


def send_answer_digest(
    notifier: Notifier | None,
    stats: ScoreStats,
    *,
    dry_run: bool,
) -> bool:
    if notifier is None:
        return False
    items = _dedupe_answer_notifications(stats.answer_notifications)
    if not items:
        return False
    body = format_answer_digest(items)
    key = stable_key(
        "needs_answer_digest",
        *sorted(item.answer_id or item.question for item in items),
    )
    return notifier.send_once(
        kind="needs_answer_digest",
        key=key,
        subject=f"Job Search Hub: Answer Bank digest ({len(items)} question(s))",
        body=body,
    )


def _persist_qualification_gate(
    *,
    page: dict,
    gate: QualificationGate,
    answers: list[CachedAnswer],
    notion: NotionClient,
    settings: Settings,
    store: Store,
    stats: ScoreStats,
    dry_run: bool,
    notifier: Notifier | None = None,
) -> list[str]:
    page_id = page.get("id", "")
    linked_ids = list(
        Pread.read_relation_ids((page.get("properties") or {}).get("Answer Bank Items"))
    )
    created_questions: list[tuple[CachedAnswer, list[float]]] = []
    for resolution in gate.resolutions:
        answer = resolution.answer
        if answer is None and resolution.requirement.canonical_key:
            answer = next(
                (
                    candidate
                    for candidate in answers
                    if candidate.status != "Deprecated"
                    and candidate.canonical_key == resolution.requirement.canonical_key
                ),
                None,
            )
            if answer is not None:
                resolution.answer = answer
        if answer is None and resolution.status in {"unknown", "partial"}:
            answer = next(
                (
                    candidate
                    for candidate, embedding in created_questions
                    if (
                        candidate.canonical_key
                        and candidate.canonical_key == resolution.requirement.canonical_key
                    )
                    or (
                        candidate.question_family == resolution.requirement.question_family
                        and candidate.answer_type == resolution.requirement.answer_type
                        and cosine_similarity(
                            resolution.question_embedding,
                            embedding,
                        )
                        >= candidate.reuse_threshold
                    )
                ),
                None,
            )
            if answer is not None:
                resolution.answer = answer
        if answer is not None:
            if not answer.page_id:
                continue
            linked_ids.append(answer.page_id)
            link_answer_to_opportunity(
                notion,
                answer,
                page_id,
                mark_used=(resolution.status == "meets" and answer.status == "Approved"),
                dry_run=dry_run,
            )
            if resolution.status in {"unknown", "partial"}:
                stats.questions_reused += 1
                stats.answer_notifications.append(
                    _answer_notification_item(
                        page=page,
                        answer=answer,
                        requirement=resolution.requirement.text,
                        importance=resolution.requirement.importance,
                        action="reused",
                    )
                )
                store.audit(
                    actor="score",
                    action="reuse_answer_question",
                    target_kind="answer",
                    target_id=answer.page_id,
                    dry_run=dry_run,
                    payload={"opportunity_id": page_id, "question": answer.question},
                )
            elif answer.status == "Approved":
                store.audit(
                    actor="score",
                    action="reuse_answer",
                    target_kind="answer",
                    target_id=answer.page_id,
                    dry_run=dry_run,
                    payload={"opportunity_id": page_id, "status": resolution.status},
                )
            continue
        if resolution.status not in {"unknown", "partial"}:
            continue
        created_page = create_needs_answer_page(
            notion,
            settings,
            question=resolution.requirement.question,
            question_family=resolution.requirement.question_family,
            answer_type=resolution.requirement.answer_type,
            related_opportunity_id=page_id,
            related_application_id=None,
            sensitive=False,
            dry_run=dry_run,
            canonical_key=resolution.requirement.canonical_key,
            topics=resolution.requirement.topics,
            question_intent=resolution.requirement.question_intent,
        )
        created_id = created_page.get("id", "")
        if not created_id:
            created_id = (
                "dry-run-"
                + sha256(resolution.requirement.question.encode("utf-8")).hexdigest()[:20]
            )
        answer = _new_cached_answer(
            page_id=created_id,
            question=resolution.requirement.question,
            question_family=resolution.requirement.question_family,
            answer_type=resolution.requirement.answer_type,
            opportunity_id=page_id,
            canonical_key=resolution.requirement.canonical_key,
            topics=resolution.requirement.topics,
            question_intent=resolution.requirement.question_intent,
        )
        resolution.answer = answer
        answers.append(answer)
        created_questions.append((answer, resolution.question_embedding))
        linked_ids.append(created_id)
        stats.questions_created += 1
        stats.answer_notifications.append(
            _answer_notification_item(
                page=page,
                answer=answer,
                requirement=resolution.requirement.text,
                importance=resolution.requirement.importance,
                action="created",
                answer_url=created_page.get("url", ""),
            )
        )
        store.audit(
            actor="score",
            action="create_needs_answer",
            target_kind="answer",
            target_id=created_id,
            dry_run=dry_run,
            payload={
                "opportunity_id": page_id,
                "question": resolution.requirement.question,
                "importance": resolution.requirement.importance,
            },
        )
    return list(dict.fromkeys(value for value in linked_ids if value))


def _compact_profile_facts(facts: list[Fact]) -> str:
    approved = [
        fact for fact in facts if fact.active and fact.verified and fact.approved_for_profile
    ]
    skills = [fact.value for fact in approved if fact.fact_type == "Skill"][:40]
    experiences = [fact for fact in approved if fact.fact_type == "Experience Header"]
    bullets = [
        fact.value for fact in approved if fact.fact_type in {"Experience Bullet", "Project Bullet"}
    ][:30]
    lines = []
    if skills:
        lines.append("Skills: " + ", ".join(skills))
    for fact in experiences:
        lines.append(
            "Experience: "
            + " | ".join(
                part for part in (fact.role_label or fact.value, fact.organization) if part
            )
        )
    lines.extend(f"Evidence: {value}" for value in bullets)
    return "\n".join(lines)[:5000]


def _posting_payload(page: dict) -> dict[str, str]:
    props = page.get("properties") or {}
    return {
        "role_title": Pread.read_text(props.get("Role Title")),
        "company": Pread.read_text(props.get("Company")),
        "location": Pread.read_text(props.get("Location")),
        "url": Pread.read_url(props.get("URL")),
        "description": Pread.read_text(props.get("Description")),
    }


def _fast_fit_hard_reject(fast_fit: FastFitAssessment | None) -> bool:
    return bool(
        fast_fit
        and fast_fit.missing_mandatory
        and fast_fit.required_skill_overlap <= 0.2
        and fast_fit.interview_plausibility <= 0.35
    )


def _has_fresh_complete_score(
    page: dict,
    criteria: list[Criterion],
    store: Store,
) -> bool:
    page_id = page.get("id", "")
    if not page_id or not hasattr(store, "latest_score_components"):
        return False
    props = page.get("properties") or {}
    if (
        Pread.read_number(props.get("Model Score")) is None
        and Pread.read_number(props.get("Base Model Score")) is None
    ):
        return False
    expected = {
        criterion.key or criterion.name
        for criterion in criteria
        if (criterion.key or criterion.name)
    }
    if not expected:
        return False
    try:
        rows = store.latest_score_components(page_id)
    except Exception as exc:
        log.warning("could not inspect local score freshness for page %s: %s", page_id, exc)
        return False
    observed = {str(row["criterion_key"]) for row in rows if not bool(row["dry_run"])}
    return observed == expected


def _fast_fit_assessments(
    pages: list[dict],
    *,
    settings: Settings,
    profile_summary: str,
    router: ModelRouter,
    client: LMStudioClient,
) -> dict[str, FastFitAssessment]:
    cache_path = settings.data_dir / "fast_fit_rescore_cache.json"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = loaded
        except (OSError, json.JSONDecodeError):
            log.warning("ignoring unreadable fast-fit cache at %s", cache_path)

    eligible: list[tuple[dict, dict[str, str], str]] = []
    assessments: dict[str, FastFitAssessment] = {}
    for page in pages:
        payload = _posting_payload(page)
        preflight = score_preflight(
            RawPosting(
                source="",
                source_adapter="",
                external_job_id="",
                company=payload["company"],
                role_title=payload["role_title"],
                url=payload["url"],
                description=payload["description"],
                location=payload["location"],
            ),
            settings,
        )
        if preflight.accepted:
            cache_key = hashlib.sha256(
                json.dumps(
                    {
                        "page_id": page.get("id", ""),
                        "profile": profile_summary,
                        "posting": payload,
                    },
                    sort_keys=True,
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                try:
                    assessments[page.get("id", "")] = FastFitAssessment(**cached)
                    continue
                except (TypeError, ValueError):
                    pass
            eligible.append((page, payload, cache_key))

    model_role = "primary"
    try:
        router.get("triage")
        model_role = "triage"
    except Exception:
        pass

    def evaluate_batch(
        batch: list[tuple[dict, dict[str, str], str]],
        *,
        offset: int,
    ) -> list[tuple[tuple[dict, dict[str, str], str], FastFitAssessment]]:
        try:
            results_by_index = evaluate_fast_fit_batch(
                [payload for _, payload, _ in batch],
                profile_facts=profile_summary,
                router=router,
                client=client,
                model_role=model_role,
            )
            return [(batch[index], results_by_index[index]) for index in range(len(batch))]
        except Exception as exc:
            if len(batch) <= 1:
                log.warning(
                    "fast fit row %d/%d skipped after model failure: %s",
                    offset + 1,
                    len(eligible),
                    exc,
                )
                return []
            split = len(batch) // 2
            log.warning(
                "fast fit batch %d-%d/%d failed; retrying as smaller batches: %s",
                offset + 1,
                offset + len(batch),
                len(eligible),
                exc,
            )
            return [
                *evaluate_batch(batch[:split], offset=offset),
                *evaluate_batch(batch[split:], offset=offset + split),
            ]

    for start in range(0, len(eligible), _FAST_FIT_BATCH_SIZE):
        batch = eligible[start : start + _FAST_FIT_BATCH_SIZE]
        log.info(
            "fast fit batch %d-%d/%d using %s",
            start + 1,
            start + len(batch),
            len(eligible),
            model_role,
        )
        evaluated = evaluate_batch(batch, offset=start)
        if not evaluated:
            continue
        for (page, _, cache_key), assessment in evaluated:
            if (
                not assessment.missing_mandatory
                and assessment.required_skill_overlap <= 0.4
                and any(
                    phrase in assessment.reason.lower()
                    for phrase in (
                        "lacks ",
                        "missing ",
                        "no mention",
                        "not mentioned",
                        "does not have",
                    )
                )
            ):
                assessment = FastFitAssessment(
                    transferable_skill_fit=assessment.transferable_skill_fit,
                    required_skill_overlap=min(
                        assessment.required_skill_overlap,
                        0.2,
                    ),
                    interview_plausibility=min(
                        assessment.interview_plausibility,
                        0.35,
                    ),
                    missing_mandatory=True,
                    reason=assessment.reason,
                )
            assessments[page.get("id", "")] = assessment
            cache[cache_key] = {
                "transferable_skill_fit": assessment.transferable_skill_fit,
                "required_skill_overlap": assessment.required_skill_overlap,
                "interview_plausibility": assessment.interview_plausibility,
                "missing_mandatory": assessment.missing_mandatory,
                "reason": assessment.reason,
            }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = Path(f"{cache_path}.tmp")
        temporary_path.write_text(
            json.dumps(cache, ensure_ascii=True, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(cache_path)
    return assessments


def _cancel_unsubmitted_applications(
    notion: NotionClient,
    application_ids: list[str],
    *,
    dry_run: bool,
) -> None:
    for application_id in application_ids:
        application = notion.get_page(application_id)
        status = read_status((application.get("properties") or {}).get("Status"))
        if status == "Submitted":
            continue
        if not dry_run:
            notion.update_page(
                application_id,
                {"Status": Pread.select("Cancelled")},
            )


def _compute_score_candidate(
    page: dict,
    *,
    settings: Settings,
    score_criteria: list[Criterion],
    profile_facts: list[Fact],
    profile_summary: str,
    answers: list[CachedAnswer],
    persisted_enabled: bool,
    store: Store,
    router: ModelRouter | None,
    client: LMStudioClient | None,
    fast_fit: FastFitAssessment | None,
) -> _ComputedOpportunity:
    props = page.get("properties") or {}
    posting = _posting_payload(page)
    preflight = score_preflight(
        RawPosting(
            source=Pread.read_select(props.get("Source")),
            source_adapter=Pread.read_select(props.get("Source Adapter")),
            external_job_id=Pread.read_text(props.get("External Job ID")),
            company=posting["company"],
            role_title=posting["role_title"],
            url=posting["url"],
            description=posting["description"],
            location=posting["location"],
        ),
        settings,
    )
    gate = QualificationGate([], [], [], [], [])
    qualification_enabled = bool(
        settings.notion.data_sources.get("answer_bank")
        and settings.notion.data_sources.get("resume_facts")
        and persisted_enabled
    )
    persist_gate = preflight.accepted and qualification_enabled
    if preflight.accepted and _fast_fit_hard_reject(fast_fit):
        base_result = _score_opportunity(
            page,
            score_criteria,
            settings=settings,
            profile_facts=profile_summary,
            router=router,
            client=client,
            model_role="primary",
            fast_fit=fast_fit,
            criterion_progress=None,
            qualification_gate=None,
        )
        base_result.hard_excluded = True
        base_result.lane = "Skip"
        base_result.exclusion_reason = f"Fast fit hard reject: {fast_fit.reason}"
        base_result.concerns = "; ".join(
            value
            for value in (
                base_result.concerns,
                "fast-fit triage found missing mandatory qualifications",
            )
            if value
        )
        return _ComputedOpportunity(
            gate=gate,
            base_result=base_result,
            persist_gate=False,
        )
    if persist_gate:
        if router is None or client is None:
            raise RuntimeError(
                "qualification extraction requires the primary "
                "LM Studio models; refusing to write a fallback score"
            )
        gate = evaluate_qualification_gate(
            role_title=posting["role_title"],
            description=posting["description"],
            location=posting["location"],
            opportunity_id=page.get("id", ""),
            facts=profile_facts,
            answers=answers,
            router=router,
            client=client,
            store=store,
        )

    base_result = _score_opportunity(
        page,
        score_criteria,
        settings=settings,
        profile_facts="\n".join(value for value in (profile_summary, gate.summary) if value),
        router=router,
        client=client,
        model_role="primary",
        fast_fit=fast_fit,
        criterion_progress=None,
        qualification_gate=None,
    )
    return _ComputedOpportunity(
        gate=gate,
        base_result=base_result,
        persist_gate=persist_gate,
    )


def _iter_computed_opportunities(
    pages: list[dict],
    *,
    workers: int,
    before_submit: Callable[[int, dict], None],
    compute: Callable[[dict, list[CachedAnswer]], _ComputedOpportunity],
    answers: list[CachedAnswer],
) -> Iterator[tuple[int, dict, Future[_ComputedOpportunity]]]:
    def make_future(index: int, page: dict) -> Future[_ComputedOpportunity]:
        future: Future[_ComputedOpportunity] = Future()
        try:
            before_submit(index, page)
            future.set_result(compute(page, list(answers)))
        except Exception as exc:
            future.set_exception(exc)
        return future

    if workers <= 1:
        for index, page in enumerate(pages, start=1):
            yield index, page, make_future(index, page)
        return

    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="score",
    ) as executor:
        pending: deque[tuple[int, dict, Future[_ComputedOpportunity]]] = deque()
        source = iter(enumerate(pages, start=1))

        def fill() -> None:
            while len(pending) < workers:
                try:
                    index, page = next(source)
                except StopIteration:
                    return
                try:
                    before_submit(index, page)
                except Exception as exc:
                    future: Future[_ComputedOpportunity] = Future()
                    future.set_exception(exc)
                else:
                    answer_snapshot = list(answers)
                    future = executor.submit(compute, page, answer_snapshot)
                pending.append((index, page, future))

        fill()
        while pending:
            yield pending.popleft()
            fill()


def run_score(
    settings: Settings,
    *,
    notion: NotionClient,
    store: Store,
    router: ModelRouter | None = None,
    client: LMStudioClient | None = None,
    rescore: bool = False,
    awaiting_only: bool = False,
    limit: int | None = None,
    pages_override: list[dict] | None = None,
    answers_override: list[CachedAnswer] | None = None,
    notifier: Notifier | None = None,
    dry_run: bool = True,
    skip_posting_preflight: bool = False,
) -> ScoreStats:
    """Score fetched opportunities and advance them into the review lifecycle."""
    stats = ScoreStats()
    report_stage_start(
        StagePhase.SCORE,
        current_step="loading active criteria from Notion",
        next_step="sync manual application rows",
    )
    criteria = load_active_criteria(notion, settings)
    stats.criteria_active = len(criteria)
    if not criteria:
        log.warning("no active scoring criteria; skipping score stage")
        report_stage_complete(StagePhase.SCORE)
        return stats
    criteria = _ensure_posting_recency_criterion(criteria)
    stats.criteria_active = len(criteria)

    answers: list[CachedAnswer] = list(answers_override or [])
    if answers_override is None and settings.notion.data_sources.get("answer_bank"):
        answers = load_answer_bank(notion, settings)

    ds_id = settings.notion.data_sources["opportunities"]
    filter_ = (
        {"property": "Status", "select": {"equals": "Awaiting Answer"}}
        if awaiting_only
        else None
        if rescore
        else {
            "or": [
                *[
                    {"property": "Status", "select": {"equals": status}}
                    for status in sorted(_NORMAL_SCORE_STATUSES)
                ],
                *[
                    {
                        "and": [
                            {"property": "Status", "select": {"equals": status}},
                            {
                                "property": "Model Score",
                                "number": {"is_empty": True},
                            },
                        ]
                    }
                    for status in sorted(_SCORELESS_ACTIVE_STATUSES)
                ],
                {
                    "and": [
                        {"property": "Status", "select": {"equals": "Error"}},
                        {
                            "property": "Last Error",
                            "rich_text": {"contains": "LM Studio returned non-JSON content"},
                        },
                    ]
                },
                {
                    "and": [
                        {"property": "Status", "select": {"equals": "Error"}},
                        {
                            "property": "Last Error",
                            "rich_text": {"contains": "LM Studio returned malformed JSON"},
                        },
                    ]
                },
            ]
        }
    )

    report_stage_detail(
        StagePhase.SCORE,
        current_item="Notion opportunities",
        current_step="loading the complete eligible scoring queue",
    )
    with report_stage_heartbeat(
        StagePhase.SCORE,
        current_item="Notion opportunities",
        current_step="loading the complete eligible scoring queue",
        next_step="prepare scoring context and score each opportunity",
    ):
        pages = (
            list(pages_override)
            if pages_override is not None
            else list(
                notion.query_data_source(
                    ds_id,
                    filter_=filter_,
                    sorts=[
                        {"property": "Priority", "direction": "descending"},
                        {"property": "Discovered At", "direction": "descending"},
                    ],
                )
            )
        )
    if limit is not None:
        pages = pages[: max(0, limit)]
    if rescore:
        pages = [
            page
            for page in pages
            if read_status((page.get("properties") or {}).get("Status"))
            not in _RESCORE_PROTECTED_STATUSES
        ]
    if awaiting_only:
        report_stage_queue(
            StagePhase.SCORE,
            len(pages),
            current_step="applying approved answers to persisted requirements",
        )
        return _run_awaiting_resolution(
            pages=pages,
            criteria=criteria,
            answers=answers,
            notion=notion,
            settings=settings,
            store=store,
            stats=stats,
            dry_run=dry_run,
        )

    if not skip_posting_preflight and pages_override is None and pages:
        report_stage_detail(
            StagePhase.SCORE,
            current_item="Posting availability",
            current_step="checking exact scoring queue URLs before model work",
        )
        with report_stage_heartbeat(
            StagePhase.SCORE,
            current_item="Posting availability",
            current_step="checking exact scoring queue URLs before model work",
            next_step="prepare scoring context",
        ):
            verification_stats = verify_active_opportunities(
                settings,
                notion=notion,
                store=store,
                dry_run=dry_run,
                pages=pages,
                force=True,
                max_age_hours=0,
                require_decisive=True,
            )
        if getattr(verification_stats, "errors", []):
            stats.errors.extend(verification_stats.errors)
            return stats
        closed_ids = set(getattr(verification_stats, "closed_page_ids", set()))
        review_ids = set(getattr(verification_stats, "blocked_page_ids", set()))
        stats.posting_review += len(review_ids)
        blocked_ids = closed_ids | review_ids
        if blocked_ids:
            before_count = len(pages)
            pages = [page for page in pages if page.get("id", "") not in blocked_ids]
            log.info(
                "score: skipped %d opportunities after posting availability check",
                before_count - len(pages),
            )
    persisted_enabled = bool(settings.notion.data_sources.get("qualification_requirements"))
    qualification_criterion = _qualification_criterion(
        criteria,
        required=persisted_enabled,
    )
    score_criteria = _base_criteria(criteria) if qualification_criterion else criteria
    if not rescore:
        original_count = len(pages)
        pages = [
            page for page in pages if not _has_fresh_complete_score(page, score_criteria, store)
        ]
        skipped_fresh = original_count - len(pages)
        if skipped_fresh:
            log.info(
                "score: skipped %d opportunities with complete fresh local scores",
                skipped_fresh,
            )
    profile_facts: list[Fact] = []
    if settings.notion.data_sources.get("resume_facts"):
        try:
            profile_facts = load_facts(notion, settings)
        except Exception as exc:
            log.warning("could not load profile facts for scoring context: %s", exc)
    profile_summary = _compact_profile_facts(profile_facts)
    fast_fit_by_page: dict[str, FastFitAssessment] = {}
    if (
        router is not None
        and client is not None
        and getattr(settings, "data_dir", None) is not None
    ):
        with report_stage_heartbeat(
            StagePhase.SCORE,
            current_item="Fast-fit scoring",
            current_step="asking LM Studio for quick fit assessments",
            next_step="score each opportunity against all active criteria",
        ):
            fast_fit_by_page = _fast_fit_assessments(
                pages,
                settings=settings,
                profile_summary=profile_summary,
                router=router,
                client=client,
            )
    report_stage_queue(
        StagePhase.SCORE,
        len(pages),
        current_step=(
            f"scoring each opportunity against {len(criteria)} criteria with "
            f"{min(8, max(1, getattr(settings, 'score_workers', 1)))} worker(s)"
        ),
    )

    workers = min(8, max(1, getattr(settings, "score_workers", 1)))

    def before_submit(index: int, page: dict) -> None:
        props = page.get("properties") or {}
        company = Pread.read_text(props.get("Company")) or "Unknown company"
        role_title = Pread.read_text(props.get("Role Title")) or "Unknown role"
        if not dry_run and not rescore:
            notion.update_page(
                page.get("id", ""),
                {
                    "Processing State": Pread.select("Pending"),
                },
            )
        report_stage_detail(
            StagePhase.SCORE,
            current_item=f"{index}/{len(pages)} {company} - {role_title}",
            current_step="extracting, resolving, and scoring in a bounded worker",
        )

    def compute(
        page: dict,
        answer_snapshot: list[CachedAnswer],
    ) -> _ComputedOpportunity:
        return _compute_score_candidate(
            page,
            settings=settings,
            score_criteria=score_criteria,
            profile_facts=profile_facts,
            profile_summary=profile_summary,
            answers=answer_snapshot,
            persisted_enabled=persisted_enabled,
            store=store,
            router=router,
            client=client,
            fast_fit=fast_fit_by_page.get(page.get("id", "")),
        )

    computed_items = _iter_computed_opportunities(
        pages,
        workers=workers,
        before_submit=before_submit,
        compute=compute,
        answers=answers,
    )
    for index, page, future in computed_items:
        props = page.get("properties") or {}
        company = Pread.read_text(props.get("Company")) or "Unknown company"
        role_title = Pread.read_text(props.get("Role Title")) or "Unknown role"
        original_status = read_status(props.get("Status"))
        preserve_scoreless_status = (
            original_status in _SCORELESS_ACTIVE_STATUSES
            and Pread.read_number(props.get("Model Score")) is None
        )
        item_label = f"{company} - {role_title}"
        try:
            with report_stage_heartbeat(
                StagePhase.SCORE,
                current_item=f"{index}/{len(pages)} {item_label}",
                current_step="waiting for scoring worker to finish",
                next_step="write score results and continue the queue",
            ):
                computed = future.result()
            gate = computed.gate
            base_result = computed.base_result
            if any(c.fallback_used for c in base_result.components):
                stats.degraded_evaluations += sum(c.fallback_used for c in base_result.components)
                stats.errors.append(
                    "Model evaluation pending; existing scores and application records preserved."
                )
                if not dry_run:
                    notion.update_page(
                        page["id"],
                        {
                            "Status": Pread.select(original_status or "Review"),
                            "Processing State": Pread.select("Failed"),
                            "Last Error": Pread.rich_text(
                                "Model output invalid or unavailable. Retry after jobhub doctor."
                            ),
                        },
                    )
                continue
            if computed.persist_gate:
                linked_ids = _persist_qualification_gate(
                    page=page,
                    gate=gate,
                    answers=answers,
                    notion=notion,
                    settings=settings,
                    store=store,
                    stats=stats,
                    dry_run=dry_run,
                    notifier=notifier,
                )
                persisted_requirements = upsert_gate_requirements(
                    notion,
                    settings,
                    opportunity_id=page.get("id", ""),
                    gate=gate,
                    dry_run=dry_run,
                )
            else:
                linked_ids = list(Pread.read_relation_ids(props.get("Answer Bank Items")))
                persisted_requirements = []
            if rescore and base_result.hard_excluded:
                _cancel_unsubmitted_applications(
                    notion,
                    Pread.read_relation_ids(props.get("Application")),
                    dry_run=dry_run,
                )
            weight_total = sum(abs(c.weight) for c in base_result.components) or 1.0
            for component in base_result.components:
                sign = -1.0 if component.direction == "Negative" else 1.0
                weighted_effect = sign * component.weight * component.score / weight_total
                store.record_score_component(
                    opportunity_page_id=base_result.page_id,
                    criterion_key=component.criterion_key,
                    criterion_name=component.criterion_name,
                    category=component.category,
                    weight=component.weight,
                    direction=component.direction,
                    evaluation_method=component.evaluation_method,
                    score=component.score,
                    weighted_effect=weighted_effect,
                    reason=component.reason,
                    triggered_hard_exclusion=component.triggered_hard_exclusion,
                    triggered_human_review=component.triggered_human_review,
                    lane_override=component.lane_override,
                    fallback_used=component.fallback_used,
                    error=component.error,
                    llm_call_id=component.llm_call_id,
                    dry_run=dry_run,
                )
                if component.fallback_used:
                    stats.degraded_evaluations += 1

            if qualification_criterion is None:
                write_scores(
                    notion,
                    page_id=base_result.page_id,
                    result=base_result,
                    my_decision=Pread.read_select(props.get("My Decision")),
                    preserve_status=bool(
                        (
                            rescore
                            and not base_result.hard_excluded
                            and Pread.read_relation_ids(props.get("Application"))
                        )
                        or (preserve_scoreless_status and not base_result.hard_excluded)
                    ),
                    dry_run=dry_run,
                )
                stats.scored += 1
                if base_result.hard_excluded:
                    stats.excluded += 1
                stats.by_lane[base_result.lane] = stats.by_lane.get(base_result.lane, 0) + 1
                continue
            base_properties = _base_score_properties(base_result)
            persisted_properties = {
                "Processing State": Pread.select("Complete"),
                "Last Error": Pread.rich_text(""),
                **base_properties,
                "Answer Bank Items": Pread.relation(linked_ids),
                "Qualification Requirements": Pread.relation(
                    [
                        requirement.page_id
                        for requirement in persisted_requirements
                        if requirement.page_id
                    ]
                ),
            }
            effective_props = {**props, **base_properties}

            if base_result.hard_excluded:
                if not dry_run:
                    notion.update_page(page.get("id", ""), persisted_properties)
                write_scores(
                    notion,
                    page_id=base_result.page_id,
                    result=base_result,
                    my_decision=Pread.read_select(props.get("My Decision")),
                    preserve_status=False,
                    dry_run=dry_run,
                )
                stats.scored += 1
                stats.excluded += 1
                stats.by_lane["Skip"] = stats.by_lane.get("Skip", 0) + 1
                continue

            if gate.hard_failures:
                reason = "; ".join(
                    f"{failure.requirement.text}: {failure.reason}"
                    for failure in gate.hard_failures
                )
                if not dry_run:
                    notion.update_page(
                        page.get("id", ""),
                        {
                            **persisted_properties,
                            **_clear_score_properties(),
                            "Qualification Score": Pread.number(
                                round(qualification_score(persisted_requirements), 4)
                            ),
                            "Hard Exclusion": Pread.checkbox(True),
                            "Exclusion Reason": Pread.rich_text(reason[:1900]),
                            "Concerns": Pread.rich_text(reason[:1900]),
                            "Status": Pread.select("Rejected"),
                            "Last Error": Pread.rich_text(""),
                        },
                    )
                stats.excluded += 1
                stats.by_lane["Skip"] = stats.by_lane.get("Skip", 0) + 1
                store.audit(
                    actor="score",
                    action="qualification_hard_rejection",
                    target_kind="opportunity",
                    target_id=page.get("id", ""),
                    dry_run=dry_run,
                    payload={"reason": reason},
                )
                continue

            if any(
                requirement.is_awaiting
                for requirement in persisted_requirements
                if requirement.active
            ):
                if not dry_run:
                    notion.update_page(
                        page.get("id", ""),
                        {
                            **persisted_properties,
                            **_clear_score_properties(),
                            "Qualification Score": Pread.number(None),
                            "Hard Exclusion": Pread.checkbox(False),
                            "Exclusion Reason": Pread.rich_text(""),
                            "Concerns": Pread.rich_text(
                                "Awaiting answers for: "
                                + "; ".join(
                                    requirement.qualification
                                    for requirement in persisted_requirements
                                    if requirement.is_awaiting
                                )[:1800]
                            ),
                            "Status": Pread.select("Awaiting Answer"),
                            "Last Error": Pread.rich_text(""),
                        },
                    )
                stats.awaiting_answer += 1
                store.audit(
                    actor="score",
                    action="awaiting_qualification_answers",
                    target_kind="opportunity",
                    target_id=page.get("id", ""),
                    dry_run=dry_run,
                    payload={
                        "requirements": [
                            requirement.fingerprint
                            for requirement in persisted_requirements
                            if requirement.is_awaiting
                        ],
                    },
                )
                continue

            public, total, lane = _published_score_properties(
                props=effective_props,
                requirements=persisted_requirements,
                qualification_criterion=qualification_criterion,
                my_decision=Pread.read_select(props.get("My Decision")),
            )
            if (
                rescore
                and Pread.read_relation_ids(props.get("Application"))
                and read_status(props.get("Status"))
            ):
                public["Status"] = Pread.select(read_status(props.get("Status")))
            elif (
                preserve_scoreless_status and Pread.read_select(public.get("Status")) != "Rejected"
            ):
                public["Status"] = Pread.select(original_status)
            if not dry_run:
                notion.update_page(
                    page.get("id", ""),
                    {
                        **persisted_properties,
                        **public,
                    },
                )
            qual_score = qualification_score(persisted_requirements)
            store.record_score_component(
                opportunity_page_id=page.get("id", ""),
                criterion_key="qualification_fit",
                criterion_name=qualification_criterion.name,
                category=qualification_criterion.category,
                weight=qualification_criterion.weight,
                direction=qualification_criterion.direction,
                evaluation_method="Persisted Rule",
                score=qual_score,
                weighted_effect=(
                    qualification_criterion.weight
                    * qual_score
                    / (_base_weight(base_result) + qualification_criterion.weight)
                ),
                reason="deterministic persisted qualification outcomes",
                triggered_hard_exclusion=False,
                triggered_human_review=False,
                lane_override="",
                fallback_used=False,
                error="",
                llm_call_id=None,
                dry_run=dry_run,
            )
            stats.scored += 1
            stats.by_lane[lane] = stats.by_lane.get(lane, 0) + 1
            store.audit(
                actor="score",
                action="score_opportunity",
                target_kind="opportunity",
                target_id=page.get("id", ""),
                dry_run=dry_run,
                payload={"lane": lane, "total": total},
            )
        except Exception as exc:
            log.exception("score: failed for page %s", page.get("id"))
            stats.errors.append(str(exc))
            if not dry_run and page.get("id"):
                try:
                    retryable = isinstance(exc, StructuredOutputError)
                    notion.update_page(
                        page["id"],
                        {
                            "Processing State": Pread.select("Pending" if retryable else "Failed"),
                            "Last Error": Pread.rich_text(
                                f"Scoring pending ({type(exc).__name__}); review jobhub doctor."
                            ),
                        },
                    )
                except Exception:
                    log.exception(
                        "score: failed to record error state for page %s",
                        page.get("id"),
                    )
            report_item_failed(StagePhase.SCORE)
        finally:
            report_stage_progress(
                StagePhase.SCORE,
                current_item=item_label,
                items_completed=index,
                current_step="opportunity finished",
                force=True,
            )
    report_stage_complete(
        StagePhase.SCORE,
        items_completed=len(pages),
        items_failed=len(stats.errors),
        items_total=len(pages),
    )
    log.info("score: %s", stats)
    return stats
