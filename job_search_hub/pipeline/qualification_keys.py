"""Canonical qualification topics, intents, keys, and question templates."""

from __future__ import annotations

import re

from job_search_hub.topics import normalize_topic, normalize_topics

_QUESTION_PREFIXES = (
    r"have you (?:ever )?worked (?:specifically )?with ",
    r"do you have (?:any )?experience (?:working )?(?:specifically )?with ",
    r"are you familiar with ",
    r"how many years of experience do you have (?:working )?(?:specifically )?with ",
    r"how many years have you (?:worked|been working) with ",
)


def canonical_topic(value: str) -> str:
    """Return one controlled-vocabulary topic for legacy callers."""
    topics = normalize_topic(
        value,
        fallback="Other",
    )
    return topics[0] if topics else "Other"


def _canonical_topics(
    topics: tuple[str, ...] | list[str],
) -> list[str]:
    """Normalize and deduplicate a qualification's complete topic collection."""
    if not topics:
        return []

    return normalize_topics(
        topics,
        fallback="Other",
        max_topics=4,
    )


def topic_from_question(question: str, family: str = "") -> str:
    """Extract one controlled-vocabulary subject from legacy question wording."""
    lower = question.strip().lower()

    for pattern in _QUESTION_PREFIXES:
        match = re.match(pattern, lower)
        if match:
            original_tail = question[match.end() :]
            return canonical_topic(original_tail)

    if "lead data engineering" in lower or "lead data engineer" in lower:
        return "Data Engineering"

    if family == "Work Authorization":
        return "Work Authorization"

    if family == "Sponsorship":
        return "Employment Sponsorship"

    if family == "Relocation":
        return "Relocation"

    if family == "Location":
        location_text = re.sub(
            r"^(?:are you|do you|would you be)\s+",
            "",
            question,
            flags=re.IGNORECASE,
        )
        return canonical_topic(location_text)

    if family == "Salary":
        return "Compensation"

    if family == "Availability":
        return "Availability"

    core = re.sub(
        r"\b(?:are|do|have|has|you|your|with|experience|worked|working|"
        r"familiar|qualified|ever|specifically|how|many|years|of|to|be)\b",
        " ",
        question,
        flags=re.IGNORECASE,
    )
    return canonical_topic(core)


def infer_question_intent(
    question: str,
    family: str,
    answer_type: str,
) -> str:
    lower = question.lower()

    if answer_type == "Number" or "how many years" in lower:
        return "Years of Experience"

    if family == "Work Authorization":
        return "Work Authorization"

    if family == "Sponsorship":
        return "Sponsorship"

    if family == "Relocation":
        return "Relocation"

    if family == "Location":
        return "Location"

    if family == "Availability":
        return "Availability"

    if family == "Salary":
        return "Compensation"

    if "certif" in lower or "credential" in lower or "license" in lower:
        return "Credential"

    if "proficien" in lower or "qualified" in lower:
        return "Proficiency"

    if any(word in lower for word in ("experience", "worked", "familiar")):
        return "Experience"

    return "Other"


def answer_type_slug(answer_type: str) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        "-",
        answer_type.lower(),
    ).strip("-")


def topic_slug(topic: str) -> str:
    return re.sub(
        r"[^a-z0-9+#]+",
        "-",
        topic.lower(),
    ).strip("-")


def canonical_key(
    *,
    intent: str,
    kind: str,
    topics: tuple[str, ...] | list[str],
    answer_type: str,
) -> str:
    canonical_topics = _canonical_topics(topics)

    topic_part = "+".join(sorted(topic_slug(topic) for topic in canonical_topics))

    return "|".join(
        (
            topic_slug(intent),
            f"{topic_slug(kind)}:{topic_part or 'other'}",
            answer_type_slug(answer_type),
        )
    )


def render_question(
    *,
    intent: str,
    topics: tuple[str, ...] | list[str],
    answer_type: str,
) -> str:
    canonical_topics = _canonical_topics(topics)
    raw_topics = [str(topic).strip() for topic in topics if str(topic).strip()]
    # Keep a named tool/skill legible in the operator-facing question even
    # when it deliberately maps to the bounded Notion topic "Other".  The
    # controlled topic remains available for schema-safe storage and keys.
    subject_topics = (
        raw_topics
        if canonical_topics == ["Other"]
        and any(topic.casefold() != "other" for topic in raw_topics)
        else canonical_topics
    )
    subject = " and ".join(subject_topics) or "this qualification"

    if intent == "Years of Experience" or answer_type == "Number":
        return f"How many years of experience do you have with {subject}?"

    if intent == "Proficiency":
        return f"Are you proficient with {subject}?"

    if intent == "Credential":
        return f"Do you hold the required {subject} credential?"

    if intent == "Location":
        return f"Are you located in or able to work from {subject}?"

    if intent == "Relocation":
        return f"Are you willing to relocate to {subject}?"

    if intent == "Work Authorization":
        return f"Are you authorized to work in {subject}?"

    if intent == "Sponsorship":
        return f"Would you require {subject}?"

    if intent == "Availability":
        return f"Does your availability meet the {subject} requirement?"

    if intent == "Compensation":
        return f"Does your compensation expectation match {subject}?"

    return f"Do you have experience with {subject}?"


def question_family_for_intent(intent: str) -> str:
    return {
        "Years of Experience": "Years of Experience",
        "Location": "Location",
        "Relocation": "Relocation",
        "Work Authorization": "Work Authorization",
        "Sponsorship": "Sponsorship",
        "Availability": "Availability",
        "Compensation": "Salary",
    }.get(
        intent,
        "Skills",
    )


def normalized_answer(value: str, answer_type: str) -> str:
    lower = value.strip().lower()

    if answer_type == "Yes/No":
        if lower in {"yes", "y", "true"}:
            return "yes"

        if lower in {"no", "n", "false"}:
            return "no"

    if answer_type == "Number":
        match = re.search(
            r"-?\d+(?:\.\d+)?",
            value.replace(",", ""),
        )
        if match:
            return f"{float(match.group(0)):g}"

    return re.sub(
        r"\s+",
        " ",
        lower,
    )
