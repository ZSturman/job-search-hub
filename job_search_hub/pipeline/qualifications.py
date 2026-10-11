"""Qualification extraction and Answer Bank resolution before scoring."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from job_search_hub.db import Store
from job_search_hub.logging_setup import get_logger
from job_search_hub.models import LMStudioClient, ModelRouter
from job_search_hub.pipeline.answers import (
    CachedAnswer,
    find_question,
    refresh_answer_embeddings,
)
from job_search_hub.pipeline.profile import Fact
from job_search_hub.pipeline.qualification_keys import (
    canonical_key,
    question_family_for_intent,
    render_question,
)
from job_search_hub.pipeline.structured_output import (
    rows_by_exact_index,
    validate_exact_indexes,
)
from job_search_hub.topics import normalize_topics

log = get_logger(__name__)

_NEGATIVE_ANSWERS = {
    "no",
    "none",
    "never",
    "not familiar",
    "no experience",
    "i do not",
    "i don't",
    "do not have",
    "don't have",
}
_POSITIVE_ANSWERS = {
    "yes",
    "y",
    "true",
    "i am",
    "i have",
    "familiar",
    "experienced",
}
_YEAR_RANGE_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*(?:\+|\s+plus)?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_YEAR_AT_LEAST_RE = re.compile(
    r"\b(?:at\s+least|minimum(?:\s+of)?|min(?:imum)?|more\s+than)\s*"
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:\+|\s+plus)?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_YEAR_VALUE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<plus>\+|plus)?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_HARD_LANGUAGE_RE = re.compile(
    r"\b(?:must|required|requires|mandatory|shall|minimum|at least|"
    r"need(?:s)?\s+to)\b|\d+(?:\.\d+)?\s*\+\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_MAXIMUM_LANGUAGE_RE = re.compile(
    r"\b(?:maximum|at most|no more than|up to)\b",
    re.IGNORECASE,
)
_MAX_POSTING_EXCERPT_CHARS = 8_000
_MAX_RESOLUTION_REQUIREMENTS = 16
_MAX_REQUIREMENT_CHARS = 600
_MAX_FACTS_FOR_RESOLUTION = 80
_MAX_FACT_FIELD_CHARS = 500
_MAX_APPROVED_ANSWER_CHARS = 500
_FACT_MATCH_INTENTS = {"Experience", "Proficiency"}
_LOCATION_CHILD_COUNTRIES = {
    "toronto": "Canada",
    "vancouver": "Canada",
    "montreal": "Canada",
    "ottawa": "Canada",
    "calgary": "Canada",
    "mexico city": "Mexico",
    "guadalajara": "Mexico",
    "mumbai": "India",
    "bengaluru": "India",
    "bangalore": "India",
    "delhi": "India",
    "hyderabad": "India",
    "sao paulo": "Brazil",
    "rio de janeiro": "Brazil",
    "london": "United Kingdom",
    "singapore": "Singapore",
    "kuala lumpur": "Malaysia",
    "bangkok": "Thailand",
}
_CONTEXT_LENGTH_ERROR_RE = re.compile(
    r"context(?:\s|-|_)?length|context window|token(?:s)? limit|too many tokens|"
    r"maximum context|prompt is too long|exceeds? context|n_ctx",
    re.IGNORECASE,
)

_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requirements"],
    "properties": {
        "requirements": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_requirement",
                    "kind",
                    "importance",
                    "topics",
                    "question_intent",
                    "answer_type",
                    "expected_operator",
                    "expected_answer",
                    "numeric_threshold",
                ],
                "properties": {
                    "source_requirement": {"type": "string", "maxLength": 600},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "skill",
                            "tool",
                            "credential",
                            "years_experience",
                            "location",
                            "relocation",
                            "work_authorization",
                            "other",
                        ],
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["hard_required", "required", "preferred"],
                    },
                    "topics": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {"type": "string"},
                    },
                    "question_intent": {
                        "type": "string",
                        "enum": [
                            "Experience",
                            "Years of Experience",
                            "Proficiency",
                            "Credential",
                            "Location",
                            "Relocation",
                            "Work Authorization",
                            "Sponsorship",
                            "Availability",
                            "Compensation",
                            "Other",
                        ],
                    },
                    "answer_type": {
                        "type": "string",
                        "enum": ["Yes/No", "Number", "Short Text"],
                    },
                    "expected_operator": {
                        "type": "string",
                        "enum": [
                            "Is Yes",
                            "Minimum",
                            "Maximum",
                            "Equals",
                            "One Of",
                            "Contains",
                            "Human Review",
                        ],
                    },
                    "expected_answer": {"type": ["string", "null"]},
                    "numeric_threshold": {"type": ["number", "null"]},
                },
            },
        },
    },
}

_RESOLUTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resolutions"],
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "requirement_index",
                    "status",
                    "reason",
                    "fact_ids",
                ],
                "properties": {
                    "requirement_index": {
                        "type": "integer",
                        "minimum": 0,
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "meets",
                            "partial",
                            "does_not_meet",
                            "unknown",
                        ],
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": 500,
                    },
                    "fact_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

_NORMALIZATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "kind",
        "question_intent",
        "answer_type",
        "expected_operator",
        "expected_answer",
        "numeric_threshold",
        "reason",
    ],
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "skill",
                "tool",
                "credential",
                "years_experience",
                "location",
                "relocation",
                "work_authorization",
                "other",
            ],
        },
        "question_intent": {
            "type": "string",
            "enum": [
                "Experience",
                "Years of Experience",
                "Proficiency",
                "Credential",
                "Location",
                "Relocation",
                "Work Authorization",
                "Sponsorship",
                "Availability",
                "Compensation",
                "Other",
            ],
        },
        "answer_type": {
            "type": "string",
            "enum": ["Yes/No", "Number", "Short Text"],
        },
        "expected_operator": {
            "type": "string",
            "enum": [
                "Is Yes",
                "Minimum",
                "Maximum",
                "Equals",
                "One Of",
                "Contains",
                "Human Review",
            ],
        },
        "expected_answer": {
            "type": ["string", "null"],
            "maxLength": 120,
        },
        "numeric_threshold": {
            "type": ["number", "null"],
        },
        "reason": {
            "type": "string",
            "maxLength": 240,
        },
    },
}


@dataclass(frozen=True)
class QualificationRequirement:
    text: str
    kind: str
    importance: str
    question: str
    question_family: str
    answer_type: str
    minimum_value: float | None = None
    keywords: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    question_intent: str = "Other"
    expected_operator: str = "Human Review"
    expected_answer: str = ""
    source_requirement: str = ""
    canonical_key: str = ""


@dataclass
class QualificationResolution:
    requirement: QualificationRequirement
    status: str
    reason: str
    answer: CachedAnswer | None = None
    fact_ids: list[str] = field(default_factory=list)
    question_embedding: list[float] = field(
        default_factory=list,
        repr=False,
    )


@dataclass
class QualificationGate:
    requirements: list[QualificationRequirement]
    resolutions: list[QualificationResolution]
    unresolved: list[QualificationResolution]
    hard_failures: list[QualificationResolution]
    answer_ids: list[str]

    @property
    def summary(self) -> str:
        parts = []
        for resolution in self.resolutions:
            parts.append(
                f"{resolution.requirement.importance} "
                f"{resolution.requirement.text}: {resolution.status} "
                f"({resolution.reason})"
            )
        return "\n".join(parts)

    @property
    def satisfactory_answers(self) -> list[CachedAnswer]:
        return [
            resolution.answer
            for resolution in self.resolutions
            if resolution.status == "meets"
            and resolution.answer is not None
            and resolution.answer.status == "Approved"
            and not resolution.answer.sensitive
        ]


def _canonicalize_requirement(
    requirement: QualificationRequirement,
) -> QualificationRequirement:
    """Synchronize a requirement with the controlled topic vocabulary."""
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


def _normalized_phrase(value: str) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        value.lower(),
    ).strip()


def _clip(value: object, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit]


def _is_context_length_error(exc: BaseException) -> bool:
    return bool(_CONTEXT_LENGTH_ERROR_RE.search(str(exc)))


def _requirement_context(
    source_requirement: str,
    description: str,
) -> str:
    source = _normalized_phrase(source_requirement)
    if not source:
        return source_requirement

    lines = [line.strip() for line in description.splitlines()]

    for index, line in enumerate(lines):
        if source not in _normalized_phrase(line):
            continue

        context = [line]
        previous = index - 1

        while previous >= 0 and not lines[previous]:
            previous -= 1

        if previous >= 0 and (
            lines[previous].endswith(":") or _HARD_LANGUAGE_RE.search(lines[previous])
        ):
            context.insert(0, lines[previous])

        return "\n".join(context)

    return source_requirement


def _explicit_year_requirement(
    source_requirement: str,
) -> tuple[float, str] | None:
    range_match = _YEAR_RANGE_RE.search(source_requirement)

    if range_match:
        low = float(range_match.group("low"))
        high = float(range_match.group("high"))
        value = min(low, high)

        if value < 0:
            raise RuntimeError(
                f"qualification has an invalid negative years threshold: {source_requirement}"
            )

        return value, "Minimum"

    at_least_match = _YEAR_AT_LEAST_RE.search(source_requirement)

    if at_least_match:
        value = float(at_least_match.group("value"))

        if value < 0:
            raise RuntimeError(
                f"qualification has an invalid negative years threshold: {source_requirement}"
            )

        return value, "Minimum"

    match = _YEAR_VALUE_RE.search(source_requirement)

    if not match:
        return None

    value = float(match.group("value"))

    if value < 0:
        raise RuntimeError(
            f"qualification has an invalid negative years threshold: {source_requirement}"
        )

    operator = "Maximum" if _MAXIMUM_LANGUAGE_RE.search(source_requirement) else "Minimum"

    return value, operator


def _model_normalize_requirement(
    item: dict,
    *,
    router: ModelRouter,
    client: LMStudioClient,
) -> dict | None:
    source_requirement = str(item.get("source_requirement", "")).strip()

    if not source_requirement:
        return None

    system = (
        "Normalize one job qualification requirement. If it contains a clear "
        "years-of-experience threshold, return kind=years_experience, "
        "question_intent='Years of Experience', answer_type=Number, "
        "expected_operator=Minimum or Maximum, and numeric_threshold. For ranges "
        "such as '5 to 7 years', use Minimum with the lower number. If no clean "
        "deterministic comparison is possible, use expected_operator=Human Review "
        "and numeric_threshold=null. Return only strict JSON."
    )

    try:
        payload = client.chat_json(
            model=router.get("primary"),
            messages=[
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_requirement": source_requirement,
                            "kind": item.get("kind"),
                            "question_intent": item.get("question_intent"),
                            "answer_type": item.get("answer_type"),
                            "expected_operator": item.get("expected_operator"),
                            "expected_answer": item.get("expected_answer"),
                            "numeric_threshold": item.get("numeric_threshold"),
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
            json_schema=_NORMALIZATION_SCHEMA,
            max_tokens=256,
            stage="score",
            role="primary",
            task="normalize_qualification_requirement",
        )
    except Exception as exc:
        log.warning(
            "qualification normalization model fallback failed: %s",
            exc,
        )
        return None

    normalized = dict(item)
    normalized.update(payload)
    normalized["source_requirement"] = source_requirement
    return normalized


def _human_review_requirement(item: dict) -> dict:
    normalized = dict(item)
    normalized.update(
        {
            "kind": str(normalized.get("kind") or "other"),
            "question_intent": "Other",
            "answer_type": "Short Text",
            "expected_operator": "Human Review",
            "expected_answer": "",
            "numeric_threshold": None,
        }
    )
    return normalized


def _normalize_extracted_requirement(
    item: dict,
    *,
    role_title: str,
    description: str,
    router: ModelRouter | None = None,
    client: LMStudioClient | None = None,
) -> dict | None:
    source_requirement = str(item.get("source_requirement", "")).strip()

    if not source_requirement:
        return None

    if _normalized_phrase(source_requirement) == _normalized_phrase(role_title):
        return None

    normalized = dict(item)
    normalized["source_requirement"] = source_requirement

    kind = str(normalized.get("kind", "other"))
    intent = str(normalized.get("question_intent", "Other"))
    answer_type = str(normalized.get("answer_type", "Yes/No"))
    operator = str(
        normalized.get(
            "expected_operator",
            "Human Review",
        )
    )
    supplied_threshold = normalized.get("numeric_threshold")

    fixed_intents = {
        "location": "Location",
        "relocation": "Relocation",
        "work_authorization": "Work Authorization",
        "credential": "Credential",
    }

    if kind in fixed_intents:
        intent = fixed_intents[kind]
        answer_type = "Yes/No"
        operator = "Is Yes"
        supplied_threshold = None
        normalized["expected_answer"] = "yes"

    else:
        numeric_shape = (
            answer_type == "Number"
            or intent == "Years of Experience"
            or kind == "years_experience"
            or operator in {"Minimum", "Maximum"}
        )

        explicit_years = _explicit_year_requirement(source_requirement)

        if numeric_shape and explicit_years is not None:
            parsed_threshold, parsed_operator = explicit_years
            kind = "years_experience"
            intent = "Years of Experience"
            answer_type = "Number"
            operator = parsed_operator
            supplied_threshold = parsed_threshold
            normalized["expected_answer"] = ""

        elif numeric_shape:
            model_normalized = (
                _model_normalize_requirement(
                    item,
                    router=router,
                    client=client,
                )
                if router is not None
                and client is not None
                and re.search(
                    r"\b(?:years?|yrs?)\b",
                    source_requirement,
                    re.IGNORECASE,
                )
                else None
            )

            if model_normalized is not None:
                return _normalize_extracted_requirement(
                    model_normalized,
                    role_title=role_title,
                    description=description,
                )

            if re.search(
                r"\b(?:years?|yrs?)\b",
                source_requirement,
                re.IGNORECASE,
            ):
                normalized = _human_review_requirement(normalized)
                kind = str(normalized["kind"])
                intent = str(normalized["question_intent"])
                answer_type = str(normalized["answer_type"])
                operator = str(normalized["expected_operator"])
                supplied_threshold = normalized["numeric_threshold"]

            else:
                kind = kind if kind in {"skill", "tool"} else "other"
                intent = "Experience"
                answer_type = "Yes/No"
                operator = "Is Yes"
                supplied_threshold = None
                normalized["expected_answer"] = "yes"

        elif answer_type == "Yes/No":
            operator = "Is Yes"
            normalized["expected_answer"] = "yes"

    importance = str(normalized.get("importance", "required"))
    hard_context = _requirement_context(
        source_requirement,
        description,
    )

    if importance == "hard_required" and not _HARD_LANGUAGE_RE.search(hard_context):
        importance = "required"

    normalized.update(
        {
            "kind": kind,
            "importance": importance,
            "question_intent": intent,
            "answer_type": answer_type,
            "expected_operator": operator,
            "numeric_threshold": supplied_threshold,
        }
    )

    return normalized


def extract_requirements(
    *,
    role_title: str,
    description: str,
    location: str,
    router: ModelRouter,
    client: LMStudioClient,
) -> list[QualificationRequirement]:
    system = (
        "Extract only concrete candidate qualifications from a job posting: "
        "skills, named tools/systems, credentials, explicit years of experience, "
        "and location or relocation constraints. Ignore communication, teamwork, "
        "attention to detail, critical thinking, and other generic soft skills. "
        "Split compound lists into atomic requirements when one answer could differ. "
        "Use hard_required for must/shall/mandatory/minimum requirements whose "
        "failure makes the candidate ineligible, required for other stated required "
        "capabilities, and preferred for desired/preferred/familiarity items. "
        "Return short, broad technical or qualification topics. Do not include full "
        "sentences, company-specific phrasing, or duplicated variants. Topic strings "
        "will be normalized to the application's controlled vocabulary. "
        "Use Number plus Minimum for explicit minimum years. Use Is Yes for binary "
        "experience, tool, credential, location, relocation, or authorization needs. "
        "Use Human Review only when no deterministic comparison is possible."
    )

    payload = client.chat_json(
        model=router.get("primary"),
        messages=[
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": (
                    f"Role: {role_title}\n"
                    f"Location: {location}\n\n"
                    "Description:\n"
                    f"{_clip(description, _MAX_POSTING_EXCERPT_CHARS)}"
                ),
            },
        ],
        json_schema=_EXTRACTION_SCHEMA,
        max_tokens=3072,
        stage="score",
        role="primary",
        task="extract_qualifications",
    )

    raw_requirements = payload.get("requirements")

    if not isinstance(raw_requirements, list):
        raise RuntimeError("qualification extraction omitted requirements")

    requirements: list[QualificationRequirement] = []
    seen: set[str] = set()

    for raw_item in raw_requirements:
        if not isinstance(raw_item, dict):
            continue

        item = _normalize_extracted_requirement(
            raw_item,
            role_title=role_title,
            description=description,
            router=router,
            client=client,
        )

        if item is None:
            continue

        source_requirement = str(item.get("source_requirement", "")).strip()

        raw_topics = tuple(str(value) for value in item.get("topics", []) if str(value).strip())
        topics = tuple(
            normalize_topics(
                raw_topics,
                fallback="Other",
                max_topics=4,
            )
        )

        if not source_requirement or not topics:
            continue

        intent = str(item.get("question_intent", "Other"))
        answer_type = str(item.get("answer_type", "Yes/No"))
        kind = str(item.get("kind", "other"))

        key = canonical_key(
            intent=intent,
            kind=kind,
            topics=topics,
            answer_type=answer_type,
        )

        if key in seen:
            continue

        seen.add(key)

        threshold = item.get("numeric_threshold")

        requirement = _canonicalize_requirement(
            QualificationRequirement(
                text=source_requirement,
                kind=kind,
                importance=str(item.get("importance", "")),
                question=render_question(
                    intent=intent,
                    topics=raw_topics or topics,
                    answer_type=answer_type,
                ),
                question_family=question_family_for_intent(intent),
                answer_type=answer_type,
                minimum_value=(float(threshold) if threshold is not None else None),
                keywords=raw_topics or topics,
                topics=topics,
                question_intent=intent,
                expected_operator=str(
                    item.get(
                        "expected_operator",
                        "Human Review",
                    )
                ),
                expected_answer=str(item.get("expected_answer") or ""),
                source_requirement=source_requirement,
                canonical_key=key,
            )
        )

        requirements.append(requirement)

    return requirements


def _split_atomic_requirements(
    requirements: list[QualificationRequirement],
) -> list[QualificationRequirement]:
    out: list[QualificationRequirement] = []

    for raw_requirement in requirements:
        requirement = _canonicalize_requirement(raw_requirement)

        if (
            requirement.answer_type == "Yes/No"
            and requirement.question_intent in _FACT_MATCH_INTENTS
            and requirement.kind in {"skill", "tool", "other"}
            and len(requirement.topics) > 1
        ):
            for topic in requirement.topics:
                topics = (topic,)

                key = canonical_key(
                    intent=requirement.question_intent,
                    kind=requirement.kind,
                    topics=topics,
                    answer_type=requirement.answer_type,
                )

                out.append(
                    replace(
                        requirement,
                        text=(f"{requirement.source_requirement or requirement.text} [{topic}]"),
                        question=render_question(
                            intent=requirement.question_intent,
                            topics=topics,
                            answer_type=requirement.answer_type,
                        ),
                        question_family=question_family_for_intent(requirement.question_intent),
                        keywords=topics,
                        topics=topics,
                        canonical_key=key,
                    )
                )

            continue

        out.append(requirement)

    return out


def _topic_token(value: str) -> str:
    return re.sub(
        r"[^a-z0-9+#]+",
        " ",
        value.lower(),
    ).strip()


def _fact_matches_topic(
    fact: Fact,
    topic: str,
) -> bool:
    if not (
        fact.active
        and fact.verified
        and (fact.approved_for_profile or fact.approved_for_applications)
    ):
        return False

    needle = _topic_token(topic)

    if not needle:
        return False

    fields = [
        fact.value,
        fact.category,
        fact.proficiency,
        " ".join(fact.emphasis_tags),
        fact.organization,
        fact.role_label,
    ]

    haystack = " ".join(_topic_token(value) for value in fields if value)

    if not haystack:
        return False

    if needle == haystack:
        return True

    return bool(
        re.search(
            rf"(?<![a-z0-9+#])"
            rf"{re.escape(needle)}"
            rf"(?![a-z0-9+#])",
            haystack,
        )
    )


def _answer_from_fact(
    requirement: QualificationRequirement,
    fact: Fact,
) -> CachedAnswer:
    return CachedAnswer(
        page_id="",
        question=requirement.question,
        normalized_question="",
        question_family=requirement.question_family,
        answer_type=requirement.answer_type,
        answer="Yes",
        status="Approved",
        approved_for_reuse=True,
        sensitive=fact.sensitive,
        context_specific=False,
        reuse_threshold=1.0,
        canonical_key=requirement.canonical_key,
        topics=requirement.topics,
        question_intent=requirement.question_intent,
        normalized_answer="yes",
        fact_source_ids=[fact.page_id or fact.fact_id],
    )


def _resolve_from_facts(
    requirement: QualificationRequirement,
    facts: list[Fact],
) -> CachedAnswer | None:
    if (
        requirement.answer_type != "Yes/No"
        or requirement.question_intent not in _FACT_MATCH_INTENTS
    ):
        return None

    match_terms = requirement.keywords or requirement.topics
    for topic in match_terms:
        fact = next(
            (
                candidate
                for candidate in facts
                if _fact_matches_topic(
                    candidate,
                    topic,
                )
            ),
            None,
        )

        if fact is None:
            return None

    fact = next(
        candidate
        for candidate in facts
        if _fact_matches_topic(
            candidate,
            match_terms[0],
        )
    )

    return _answer_from_fact(
        requirement,
        fact,
    )


def resolve_requirement_from_facts(
    requirement: QualificationRequirement,
    facts: list[Fact],
) -> CachedAnswer | None:
    """Return a synthetic approved answer when verified facts satisfy a requirement."""
    return _resolve_from_facts(
        _canonicalize_requirement(requirement),
        facts,
    )


def _topic_country(topic: str) -> str:
    cleaned = _topic_token(topic)

    if cleaned in {"canada", "ca"}:
        return "Canada"

    if cleaned == "mexico":
        return "Mexico"

    if cleaned == "india":
        return "India"

    if cleaned == "brazil":
        return "Brazil"

    if cleaned in {
        "united kingdom",
        "uk",
        "great britain",
    }:
        return "United Kingdom"

    if cleaned == "singapore":
        return "Singapore"

    if cleaned == "malaysia":
        return "Malaysia"

    if cleaned == "thailand":
        return "Thailand"

    return _LOCATION_CHILD_COUNTRIES.get(
        cleaned,
        "",
    )


def _find_location_parent_answer(
    requirement: QualificationRequirement,
    answers: list[CachedAnswer],
    *,
    opportunity_id: str,
) -> CachedAnswer | None:
    if requirement.question_intent not in {
        "Location",
        "Relocation",
    }:
        return None

    countries = {_topic_country(topic) for topic in requirement.topics}
    countries.discard("")

    if not countries:
        return None

    for answer in answers:
        if answer.status != "Approved" or not answer.answer.strip():
            continue

        if not (answer.approved_for_reuse or opportunity_id in answer.related_opportunity_ids):
            continue

        if answer.context_specific and opportunity_id not in answer.related_opportunity_ids:
            continue

        if answer.answer_type != "Yes/No" or answer.question_intent != requirement.question_intent:
            continue

        answer_countries = {_topic_country(topic) for topic in answer.topics}
        answer_countries.discard("")

        if countries & answer_countries:
            return answer

    return None


def classify_answer(
    requirement: QualificationRequirement,
    answer: CachedAnswer,
) -> tuple[str, str] | None:
    """Deterministically classify unambiguous yes/no and numeric answers."""
    value = answer.answer.strip()
    lower = value.lower()
    operator = requirement.expected_operator

    if operator == "Is Yes" or (operator == "Human Review" and requirement.answer_type == "Yes/No"):
        if lower in {"yes", "y", "true"}:
            return (
                "meets",
                "approved affirmative answer",
            )

        if lower in {"no", "n", "false"}:
            return (
                "does_not_meet",
                "approved negative answer",
            )

        return (
            "unknown",
            "approved answer is not a recognized yes/no value",
        )

    if operator in {"Minimum", "Maximum"} or (
        operator == "Human Review"
        and requirement.answer_type == "Number"
        and requirement.minimum_value is not None
    ):
        operator = "Minimum" if operator == "Human Review" else operator

        match = re.search(
            r"-?\d+(?:\.\d+)?",
            value.replace(",", ""),
        )

        if not match:
            return (
                "unknown",
                "approved answer is not numeric",
            )

        actual = float(match.group(0))
        threshold = requirement.minimum_value

        if threshold is None:
            return (
                "unknown",
                "numeric requirement has no stored threshold",
            )

        meets = actual >= threshold if operator == "Minimum" else actual <= threshold

        if meets:
            return (
                "meets",
                f"{actual:g} meets {operator.lower()} {threshold:g}",
            )

        return (
            "does_not_meet",
            (
                f"{actual:g} is below minimum {threshold:g}"
                if operator == "Minimum"
                else f"{actual:g} exceeds maximum {threshold:g}"
            ),
        )

    if operator == "Equals":
        return (
            (
                "meets",
                "approved answer equals expected value",
            )
            if lower == requirement.expected_answer.strip().lower()
            else (
                "does_not_meet",
                "approved answer differs from expected value",
            )
        )

    if operator == "One Of":
        expected = {
            part.strip().lower()
            for part in re.split(
                r"[,|;]",
                requirement.expected_answer,
            )
            if part.strip()
        }

        return (
            (
                "meets",
                "approved answer is an accepted value",
            )
            if lower in expected
            else (
                "does_not_meet",
                "approved answer is not an accepted value",
            )
        )

    if operator == "Contains":
        expected = requirement.expected_answer.strip().lower()

        return (
            (
                "meets",
                "approved answer contains expected value",
            )
            if expected and expected in lower
            else (
                "does_not_meet",
                "approved answer lacks expected value",
            )
        )

    if operator == "Human Review" and value:
        return (
            "partial",
            "approved answer requires human interpretation",
        )

    if any(phrase in lower for phrase in _NEGATIVE_ANSWERS):
        return (
            "does_not_meet",
            "approved answer explicitly denies the qualification",
        )

    if lower in _POSITIVE_ANSWERS:
        return (
            "meets",
            "approved affirmative answer",
        )

    return None


def _verified_fact_payload(
    facts: list[Fact],
) -> list[dict]:
    return [
        {
            "id": fact.page_id or fact.fact_id,
            "type": fact.fact_type,
            "value": _clip(
                fact.value,
                _MAX_FACT_FIELD_CHARS,
            ),
            "role": _clip(
                fact.role_label,
                _MAX_FACT_FIELD_CHARS,
            ),
            "organization": _clip(
                fact.organization,
                _MAX_FACT_FIELD_CHARS,
            ),
            "proficiency": _clip(
                fact.proficiency,
                _MAX_FACT_FIELD_CHARS,
            ),
            "dates": [
                _clip(fact.start_date, 40),
                _clip(fact.end_date, 40),
            ],
        }
        for fact in facts
        if fact.active
        and fact.verified
        and (fact.approved_for_profile or fact.approved_for_applications)
        and (fact.page_id or fact.fact_id)
    ][:_MAX_FACTS_FOR_RESOLUTION]


def _resolve_with_model(
    requirements: list[QualificationRequirement],
    facts: list[Fact],
    matched_answers: dict[int, CachedAnswer],
    *,
    router: ModelRouter,
    client: LMStudioClient,
) -> dict[int, tuple[str, str, list[str]]]:
    if not requirements:
        return {}

    system = (
        "Resolve concrete job requirements using only the supplied verified facts "
        "and approved answers. Absence of evidence is unknown, never a negative. "
        "A related but weaker capability is partial. An explicit denial or numeric "
        "shortfall is does_not_meet. Do not infer years, credentials, tools, or "
        "location willingness. Return one resolution per requirement_index. Keep "
        "each reason to 20 words or fewer."
    )

    req_payload = []
    capped_requirements = requirements[:_MAX_RESOLUTION_REQUIREMENTS]

    if len(capped_requirements) < len(requirements):
        log.warning(
            "qualification resolution capped requirements from %d to %d before LM call",
            len(requirements),
            len(capped_requirements),
        )

    for index, requirement in enumerate(capped_requirements):
        answer = matched_answers.get(index)

        req_payload.append(
            {
                "requirement_index": index,
                "text": _clip(
                    requirement.text,
                    _MAX_REQUIREMENT_CHARS,
                ),
                "kind": requirement.kind,
                "importance": requirement.importance,
                "minimum_value": requirement.minimum_value,
                "approved_answer": (
                    {
                        "question": _clip(
                            answer.question,
                            _MAX_REQUIREMENT_CHARS,
                        ),
                        "answer": _clip(
                            answer.answer,
                            _MAX_APPROVED_ANSWER_CHARS,
                        ),
                    }
                    if answer
                    else None
                ),
            }
        )

    payload = client.chat_json(
        model=router.get("primary"),
        messages=[
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": (
                    "Requirements:\n"
                    f"{json.dumps(req_payload, ensure_ascii=True)}"
                    "\n\nVerified facts:\n"
                    f"{json.dumps(_verified_fact_payload(facts), ensure_ascii=True)}"
                ),
            },
        ],
        json_schema=_RESOLUTION_SCHEMA,
        max_tokens=max(
            512,
            min(
                2048,
                len(capped_requirements) * 96 + 256,
            ),
        ),
        stage="score",
        role="primary",
        task="resolve_qualifications",
        post_validator=lambda data: validate_exact_indexes(
            data,
            array_key="resolutions",
            index_key="requirement_index",
            expected_indexes=range(len(capped_requirements)),
        ),
    )

    rows = rows_by_exact_index(
        payload,
        array_key="resolutions",
        index_key="requirement_index",
        expected_indexes=range(len(capped_requirements)),
    )

    result: dict[
        int,
        tuple[str, str, list[str]],
    ] = {}

    for index, row in rows.items():
        result[index] = (
            str(
                row.get(
                    "status",
                    "unknown",
                )
            ),
            str(
                row.get(
                    "reason",
                    "",
                )
            ).strip(),
            [
                str(value)
                for value in row.get(
                    "fact_ids",
                    [],
                )
                if str(value)
            ],
        )

    return result


def evaluate_qualification_gate(
    *,
    role_title: str,
    description: str,
    location: str,
    opportunity_id: str,
    facts: list[Fact],
    answers: list[CachedAnswer],
    router: ModelRouter,
    client: LMStudioClient,
    store: Store,
) -> QualificationGate:
    try:
        requirements = extract_requirements(
            role_title=role_title,
            description=description,
            location=location,
            router=router,
            client=client,
        )
    except Exception as exc:
        log.warning(
            "qualification extraction skipped after model failure: %s",
            exc,
        )
        requirements = []

    requirements = _split_atomic_requirements(requirements)

    if not requirements:
        return QualificationGate(
            [],
            [],
            [],
            [],
            [],
        )

    legacy_answers = [
        answer
        for answer in answers
        if answer.status != "Deprecated"
        and not (answer.canonical_key or answer.topics or answer.question_intent)
    ]

    if legacy_answers:
        answer_embeddings = refresh_answer_embeddings(
            answers=legacy_answers,
            router=router,
            client=client,
            store=store,
        )

        try:
            question_vectors = client.embed(
                model=router.get("embedding"),
                inputs=[r.question for r in requirements],
                stage="score",
                role="embedding",
                task="qualification_questions",
            )
        except Exception:
            answer_embeddings = {}
            question_vectors = [[] for _ in requirements]

    else:
        answer_embeddings = {}
        question_vectors = [[] for _requirement in requirements]

    matched: dict[int, CachedAnswer] = {}
    approved: dict[int, CachedAnswer] = {}

    for index, requirement in enumerate(requirements):
        fact_answer = _resolve_from_facts(
            requirement,
            facts,
        )

        if fact_answer is not None:
            matched[index] = fact_answer
            approved[index] = fact_answer
            continue

        lookup = find_question(
            question=requirement.question,
            question_family=(requirement.question_family),
            answer_type=requirement.answer_type,
            opportunity_id=opportunity_id,
            candidates=answers,
            question_embedding=(question_vectors[index]),
            candidate_embeddings=answer_embeddings,
            canonical_key=requirement.canonical_key,
            topics=requirement.topics,
            question_intent=(requirement.question_intent),
        )

        if (not lookup.found or lookup.answer is None) and requirement.question_intent in {
            "Location",
            "Relocation",
        }:
            parent_answer = _find_location_parent_answer(
                requirement,
                answers,
                opportunity_id=opportunity_id,
            )

            if parent_answer is not None:
                lookup.found = True
                lookup.answer = parent_answer
                lookup.method = "location_parent"

        if not lookup.found or lookup.answer is None:
            continue

        matched[index] = lookup.answer
        answer = lookup.answer

        reusable = answer.approved_for_reuse or opportunity_id in answer.related_opportunity_ids

        if answer.status == "Approved" and reusable and answer.answer.strip():
            approved[index] = answer

    try:
        model_resolutions = _resolve_with_model(
            requirements,
            facts,
            approved,
            router=router,
            client=client,
        )
    except Exception as exc:
        if _is_context_length_error(exc):
            log.warning(
                "qualification resolution skipped after LM Studio context/token limit: %s",
                exc,
            )
        else:
            log.warning(
                "qualification resolution skipped after model failure: %s",
                exc,
            )

        model_resolutions = {}

    resolutions: list[QualificationResolution] = []

    for index, requirement in enumerate(requirements):
        answer = approved.get(index)
        deterministic = (
            classify_answer(
                requirement,
                answer,
            )
            if answer
            else None
        )

        if deterministic:
            status, reason = deterministic
            fact_ids = answer.fact_source_ids

        else:
            status, reason, fact_ids = model_resolutions.get(
                index,
                (
                    "unknown",
                    "model qualification resolution unavailable",
                    [],
                ),
            )

        if answer is None and status == "does_not_meet":
            status = "unknown"
            reason = "no verified evidence"
            fact_ids = []

        if index in matched and index not in approved and status == "unknown":
            reason = "matching Answer Bank row still needs an approved answer"

        resolutions.append(
            QualificationResolution(
                requirement=requirement,
                status=status,
                reason=(reason or "no verified evidence"),
                answer=matched.get(index),
                fact_ids=fact_ids,
                question_embedding=(question_vectors[index]),
            )
        )

    unresolved = [
        resolution for resolution in resolutions if resolution.status in {"unknown", "partial"}
    ]

    hard_failures = [
        resolution
        for resolution in resolutions
        if (
            resolution.requirement.importance == "hard_required"
            and resolution.status == "does_not_meet"
        )
    ]

    return QualificationGate(
        requirements=requirements,
        resolutions=resolutions,
        unresolved=unresolved,
        hard_failures=hard_failures,
        answer_ids=list(
            dict.fromkeys(answer.page_id for answer in matched.values() if answer.page_id)
        ),
    )
