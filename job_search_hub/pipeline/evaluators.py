"""Component evaluators for the scoring engine.

Each evaluator returns a `ComponentResult`. We provide a small set of
deterministic built-ins; LLM-driven criteria delegate to the primary model
with the criterion's instructions as the system prompt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from job_search_hub.config import Settings
from job_search_hub.logging_setup import get_logger
from job_search_hub.models import LMStudioClient, ModelRouter
from job_search_hub.pipeline.discovery import (
    detect_language,
    generic_listing_url_reason,
    is_remoteok_url,
    target_market_signal,
)
from job_search_hub.pipeline.posting_dates import POSTING_RECENCY_KEY, posting_recency_score
from job_search_hub.pipeline.score import ComponentResult, Criterion
from job_search_hub.pipeline.structured_output import rows_by_exact_index, validate_exact_indexes

log = get_logger(__name__)


_LLM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "reason"],
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 500},
        "lane_override": {
            "type": ["string", "null"],
            "enum": [None, "None", "Worth a Look", "Fast Review", "High Attention", "Skip"],
        },
        "hard_exclude": {"type": "boolean"},
        "needs_human_review": {"type": "boolean"},
    },
}

_FAST_FIT_KEYS = (
    "transferable_skill_fit",
    "required_skill_overlap",
    "interview_plausibility",
)


@dataclass(frozen=True)
class FastFitAssessment:
    transferable_skill_fit: float
    required_skill_overlap: float
    interview_plausibility: float
    missing_mandatory: bool
    reason: str


def _fast_fit_batch_schema(posting_count: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["evaluations"],
        "properties": {
            "evaluations": {
                "type": "array",
                "minItems": posting_count,
                "maxItems": posting_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "posting_index",
                        *_FAST_FIT_KEYS,
                        "missing_mandatory",
                        "reason",
                    ],
                    "properties": {
                        "posting_index": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": max(0, posting_count - 1),
                        },
                        **{
                            key: {"type": "number", "minimum": 0, "maximum": 1}
                            for key in _FAST_FIT_KEYS
                        },
                        "missing_mandatory": {"type": "boolean"},
                        "reason": {"type": "string", "maxLength": 180},
                    },
                },
            },
        },
    }


def _llm_batch_schema(criterion_count: int) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["evaluations"],
        "properties": {
            "evaluations": {
                "type": "array",
                "minItems": criterion_count,
                "maxItems": criterion_count,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "criterion_index",
                        "score",
                        "reason",
                        "lane_override",
                        "hard_exclude",
                        "needs_human_review",
                    ],
                    "properties": {
                        "criterion_index": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": max(0, criterion_count - 1),
                        },
                        "score": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string", "maxLength": 500},
                        "lane_override": {
                            "type": ["string", "null"],
                            "enum": [
                                None,
                                "None",
                                "Worth a Look",
                                "Fast Review",
                                "High Attention",
                                "Skip",
                            ],
                        },
                        "hard_exclude": {"type": "boolean"},
                        "needs_human_review": {"type": "boolean"},
                    },
                },
            },
        },
    }


def _result(criterion: Criterion, *, score: float, reason: str = "") -> ComponentResult:
    r = ComponentResult(
        criterion_key=criterion.key or criterion.name,
        criterion_name=criterion.name,
        category=criterion.category,
        evaluation_method=criterion.evaluation_method,
        score=max(0.0, min(1.0, score)),
        weight=criterion.weight,
        direction=criterion.direction,
        reason=reason,
    )
    return _apply_criterion_flags(criterion, r)


def _safe_llm_failure_result(
    criterion: Criterion,
    exc: BaseException | str,
    *,
    call_id: int | None,
) -> ComponentResult:
    direction = (criterion.direction or "").lower()
    score = 1.0 if direction == "negative" else 0.0
    result = _result(criterion, score=score, reason=f"llm invalid output: {exc}")
    result.fallback_used = True
    result.error = str(exc)
    result.llm_call_id = call_id
    result.triggered_hard_exclusion = False
    result.triggered_human_review = bool(criterion.human_review_trigger)
    result.lane_override = ""
    return result


def _apply_criterion_flags(criterion: Criterion, result: ComponentResult) -> ComponentResult:
    triggered = result.score > 0.0
    direction = (criterion.direction or "").lower()
    method = (criterion.evaluation_method or "").lower()
    if triggered and (criterion.hard_exclusion or direction == "hard exclusion"):
        result.triggered_hard_exclusion = method != "numeric"
    if method == "numeric" and (criterion.hard_exclusion or direction == "hard exclusion"):
        result.triggered_hard_exclusion = " < min " in result.reason
    if triggered and (criterion.human_review_trigger or direction == "human review trigger"):
        result.triggered_human_review = True
    if triggered and criterion.lane_override and criterion.lane_override != "None":
        result.lane_override = criterion.lane_override
    return result


def evaluate_numeric_min(criterion: Criterion, value: float | None) -> ComponentResult:
    if value is None:
        return _result(criterion, score=0.5, reason="value unknown")
    floor = criterion.minimum_value or 0.0
    ceiling = criterion.maximum_value or max(floor + 1.0, 1.0)
    if value < floor:
        return _result(criterion, score=0.0, reason=f"value {value} < min {floor}")
    if value >= ceiling:
        return _result(criterion, score=1.0, reason=f"value {value} >= max {ceiling}")
    span = ceiling - floor
    return _result(
        criterion, score=(value - floor) / span, reason=f"value {value} within [{floor},{ceiling}]"
    )


def evaluate_keyword_match(
    criterion: Criterion,
    text: str,
    *,
    keywords: list[str],
) -> ComponentResult:
    if not keywords:
        return _result(criterion, score=0.0, reason="no keywords configured")
    lower = text.lower()
    hits = [k for k in keywords if k.lower() in lower]
    score = min(1.0, len(hits) / max(1.0, len(keywords) / 2.0))
    return _result(
        criterion, score=score, reason=f"matched {len(hits)}/{len(keywords)} keywords: {hits[:5]}"
    )


def evaluate_structured_rule(
    criterion: Criterion,
    *,
    role_title: str,
    description: str,
    location: str,
    posting_url: str,
    posted_at=None,
    settings: Settings | None,
) -> ComponentResult | None:
    key = (criterion.key or "").lower()
    if key == POSTING_RECENCY_KEY:
        recency = posting_recency_score(posted_at)
        return _result(criterion, score=recency.score, reason=recency.reason)
    if key == "night_shift":
        schedule_text = "\n".join([role_title, description]).lower()
        patterns = (
            r"\bovernight(?:s)?\b",
            r"\bgraveyard\b",
            r"\bnight shift\b",
            r"\bthird shift\b",
            r"\b3rd shift\b",
            r"\b(?:10|11|12)\s*(?:p\.?m\.?|pm)\s*(?:-|–|to)\s*"
            r"(?:[1-9]|10|11|12)\s*(?:a\.?m\.?|am)\b",
        )
        matched = next(
            (pattern for pattern in patterns if re.search(pattern, schedule_text, re.I)),
            "",
        )
        return _result(
            criterion,
            score=1.0 if matched else 0.0,
            reason=(
                "confirmed overnight or night-shift schedule"
                if matched
                else "no confirmed overnight or night-shift schedule"
            ),
        )
    if key == "core_role_family_fit":
        roles = list(getattr(getattr(settings, "search_terms", None), "role_terms", []))
        if not roles:
            return _result(criterion, score=0.5, reason="role preferences are unknown")
        matched = any(term.casefold() in role_title.casefold() for term in roles)
        return _result(
            criterion,
            score=1.0 if matched else 0.0,
            reason="configured role family matched" if matched else "role family mismatch",
        )
    if key == "english_language_validity":
        if settings is None:
            return _result(criterion, score=0.0, reason="language settings unavailable")
        language, confidence, mixed = detect_language(
            f"{role_title}\n{description}",
            minimum_confidence=settings.discovery.language_min_confidence,
        )
        invalid = mixed or (
            language
            and confidence >= settings.discovery.language_min_confidence
            and language not in settings.discovery.allowed_languages
        )
        return _result(
            criterion,
            score=1.0 if invalid else 0.0,
            reason=(
                f"invalid language: {language or 'mixed'} confidence={confidence:.2f}"
                if invalid
                else f"English/insufficient contrary evidence confidence={confidence:.2f}"
            ),
        )
    if key == "direct_application_url_validity":
        reason = generic_listing_url_reason(posting_url)
        return _result(
            criterion,
            score=1.0 if reason else 0.0,
            reason=reason or "individual posting URL",
        )
    if key == "remoteok_source_quality":
        matched = is_remoteok_url(posting_url)
        return _result(
            criterion,
            score=1.0 if matched else 0.0,
            reason="RemoteOK source penalty" if matched else "not a RemoteOK URL",
        )
    if key == "foreign_target_market":
        signal = target_market_signal(role_title, description, location)
        score = 1.0 if signal.level == "foreign" else (0.5 if signal.level == "mixed" else 0.0)
        return _result(
            criterion,
            score=score,
            reason=(
                f"{signal.level} target market: {', '.join(signal.countries)}"
                if signal.level != "none"
                else "no foreign employment targeting"
            ),
        )
    return None


def evaluate_llm(
    criterion: Criterion,
    posting_text: str,
    *,
    router: ModelRouter,
    client: LMStudioClient,
) -> ComponentResult:
    system = (
        "You evaluate a job posting against a single scoring criterion. "
        "Return strict JSON. Score is in [0,1] where 1 means strong fit for "
        "the Positive direction, or strong negative signal for the Negative "
        "direction. Be conservative: if you are unsure, score 0.5 and explain. "
        "If the criterion's instructions specify a hard exclusion, set "
        "hard_exclude=true and score=1."
    )
    user = (
        f"Criterion: {criterion.name}\n"
        f"Direction: {criterion.direction}\n"
        f"Instructions: {criterion.instructions}\n\n"
        f"Posting:\n{posting_text[:6000]}\n"
    )
    try:
        data = client.chat_json(
            model=router.get("primary"),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_schema=_LLM_SCHEMA,
            max_tokens=384,
            stage="score",
            role="primary",
            task=criterion.key or criterion.name,
        )
    except Exception as exc:
        log.warning("LLM evaluation failed for criterion %s: %s", criterion.name, exc)
        return _safe_llm_failure_result(
            criterion,
            exc,
            call_id=getattr(client, "last_call_id", None),
        )
    score = float(data.get("score", 0.5))
    reason = str(data.get("reason", ""))
    r = _result(criterion, score=score, reason=reason)
    r.llm_call_id = getattr(client, "last_call_id", None)
    r.triggered_hard_exclusion = bool(data.get("hard_exclude")) or r.triggered_hard_exclusion
    r.triggered_human_review = bool(data.get("needs_human_review")) or r.triggered_human_review
    lo = data.get("lane_override")
    if lo and lo != "None":
        r.lane_override = lo
    elif criterion.lane_override and criterion.lane_override != "None":
        r.lane_override = criterion.lane_override
    return r


def evaluate_llm_batch(
    criteria: list[Criterion],
    posting_text: str,
    *,
    router: ModelRouter,
    client: LMStudioClient,
    model_role: str = "primary",
) -> list[ComponentResult]:
    """Evaluate all model-driven criteria in one inference request."""
    if not criteria:
        return []

    system = (
        "You evaluate one job posting against multiple scoring criteria. "
        "Return exactly one strict-JSON evaluation for every criterion_index. "
        "Scores are in [0,1]: 1 means strong fit for Positive criteria or a "
        "strong negative signal for Negative criteria. Be conservative; use "
        "0.5 when evidence is unclear. Keep each reason concise and specific "
        "to the posting. Only set hard_exclude or a lane_override when that "
        "criterion's instructions support it."
    )
    criterion_payload = [
        {
            "criterion_index": index,
            "name": criterion.name,
            "direction": criterion.direction,
            "instructions": criterion.instructions,
        }
        for index, criterion in enumerate(criteria)
    ]
    user = (
        "Criteria:\n"
        f"{json.dumps(criterion_payload, ensure_ascii=True)}\n\n"
        f"Posting:\n{posting_text[:6000]}\n"
    )
    try:
        data = client.chat_json(
            model=router.get(model_role),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_schema=_llm_batch_schema(len(criteria)),
            max_tokens=max(384, min(2048, len(criteria) * 128 + 128)),
            stage="score",
            role=model_role,
            task=f"criteria_batch:{len(criteria)}",
            post_validator=lambda payload: validate_exact_indexes(
                payload,
                array_key="evaluations",
                index_key="criterion_index",
                expected_indexes=range(len(criteria)),
            ),
        )
    except Exception as exc:
        log.warning("batched LLM evaluation failed for %d criteria: %s", len(criteria), exc)
        call_id = getattr(client, "last_call_id", None)
        return [_safe_llm_failure_result(criterion, exc, call_id=call_id) for criterion in criteria]

    call_id = getattr(client, "last_call_id", None)
    try:
        by_index = rows_by_exact_index(
            data,
            array_key="evaluations",
            index_key="criterion_index",
            expected_indexes=range(len(criteria)),
        )
    except Exception as exc:
        log.warning("batched LLM evaluation failed post-validation: %s", exc)
        return [_safe_llm_failure_result(criterion, exc, call_id=call_id) for criterion in criteria]
    results = []
    for index, criterion in enumerate(criteria):
        item = by_index.get(index)
        if item is None:
            result = _result(criterion, score=0.5, reason="batch response omitted criterion")
            result.fallback_used = True
            result.error = "batch response omitted criterion"
            result.llm_call_id = call_id
            result.triggered_hard_exclusion = False
            result.triggered_human_review = bool(criterion.human_review_trigger)
            result.lane_override = ""
            results.append(result)
            continue

        result = _result(
            criterion,
            score=float(item.get("score", 0.5)),
            reason=str(item.get("reason", "")),
        )
        result.llm_call_id = call_id
        result.triggered_hard_exclusion = (
            bool(item.get("hard_exclude")) or result.triggered_hard_exclusion
        )
        result.triggered_human_review = (
            bool(item.get("needs_human_review")) or result.triggered_human_review
        )
        lane_override = item.get("lane_override")
        if lane_override and lane_override != "None":
            result.lane_override = lane_override
        elif criterion.lane_override and criterion.lane_override != "None":
            result.lane_override = criterion.lane_override
        results.append(result)
    return results


def evaluate_fast_fit_batch(
    postings: list[dict[str, str]],
    *,
    profile_facts: str,
    router: ModelRouter,
    client: LMStudioClient,
    model_role: str = "triage",
) -> dict[int, FastFitAssessment]:
    """Score the decisive fit dimensions for several postings in one request."""
    if not postings:
        return {}
    system = (
        "Quickly screen jobs against approved candidate facts. "
        "Evaluate only evidence actually present. Generic soft skills cannot "
        "substitute for a missing required language, platform, credential, "
        "specialty, seniority level, or years of experience. Set "
        "missing_mandatory=true and required_skill_overlap at 0.2 or lower "
        "when a core must-have is absent. Interview plausibility is the "
        "realistic chance of an initial interview, not whether the job sounds "
        "interesting. Return one evaluation per posting_index and keep each "
        "reason to 18 words or fewer."
    )
    compact_postings = [
        {
            "posting_index": index,
            "role": posting.get("role_title", ""),
            "company": posting.get("company", ""),
            "location": posting.get("location", ""),
            "url": posting.get("url", ""),
            "description": posting.get("description", "")[:1200],
        }
        for index, posting in enumerate(postings)
    ]
    user = (
        "Verified candidate facts:\n"
        f"{profile_facts[:5000]}\n\n"
        "Postings:\n"
        f"{json.dumps(compact_postings, ensure_ascii=True)}"
    )
    data = client.chat_json(
        model=router.get(model_role),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        json_schema=_fast_fit_batch_schema(len(postings)),
        max_tokens=max(256, min(2048, len(postings) * 80 + 128)),
        stage="score",
        role=model_role,
        task=f"fast_fit_batch:{len(postings)}",
        post_validator=lambda payload: validate_exact_indexes(
            payload,
            array_key="evaluations",
            index_key="posting_index",
            expected_indexes=range(len(postings)),
        ),
    )
    rows = rows_by_exact_index(
        data,
        array_key="evaluations",
        index_key="posting_index",
        expected_indexes=range(len(postings)),
    )
    by_index: dict[int, FastFitAssessment] = {}
    for index, item in rows.items():
        by_index[index] = FastFitAssessment(
            transferable_skill_fit=float(item.get("transferable_skill_fit", 0.0)),
            required_skill_overlap=float(item.get("required_skill_overlap", 0.0)),
            interview_plausibility=float(item.get("interview_plausibility", 0.0)),
            missing_mandatory=bool(item.get("missing_mandatory")),
            reason=str(item.get("reason", "")),
        )
    return by_index


def evaluate_criterion(
    criterion: Criterion,
    *,
    posting_text: str,
    posting_comp: float | None,
    router: ModelRouter | None,
    client: LMStudioClient | None,
    role_title: str = "",
    description: str = "",
    location: str = "",
    posting_url: str = "",
    posted_at=None,
    settings: Settings | None = None,
) -> ComponentResult:
    method = (criterion.evaluation_method or "").lower()
    structured = evaluate_structured_rule(
        criterion,
        role_title=role_title,
        description=description,
        location=location,
        posting_url=posting_url,
        posted_at=posted_at,
        settings=settings,
    )
    if structured is not None:
        return structured
    if method == "manual":
        return _result(criterion, score=0.5, reason="manual; awaiting human input")
    if method == "numeric":
        return evaluate_numeric_min(criterion, posting_comp)
    if method in {"keyword", "rule"}:
        # Pull keywords from instructions: comma-separated list.
        keywords = [k.strip() for k in re.split(r"[,;\n]+", criterion.instructions) if k.strip()]
        return evaluate_keyword_match(criterion, posting_text, keywords=keywords)
    if method in {"llm", "hybrid"}:
        if router is None or client is None:
            return _result(criterion, score=0.5, reason="llm method but no router/client provided")
        return evaluate_llm(criterion, posting_text, router=router, client=client)
    return _result(criterion, score=0.5, reason=f"unknown evaluation method: {method!r}")
