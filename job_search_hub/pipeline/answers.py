"""Answer Bank workflow.

When the system encounters an application question that cannot be answered
confidently from verified Notion data, we:
  1. normalize the question text;
  2. search the Answer Bank by exact + embedding similarity;
  3. propose or use an approved answer when safe;
  4. otherwise create a `Needs Answer` row, link it to the opportunity
     and application, and notify the user.

This module exposes pure functions for the steps that don't need Notion (so
they're testable) plus the wiring that talks to Notion + the embedding model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.logging_setup import get_logger
from job_search_hub.models import LMStudioClient, ModelRouter
from job_search_hub.notion import properties as P
from job_search_hub.notion import properties as Pread
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.qualification_keys import normalized_answer
from job_search_hub.topics import normalize_topics

log = get_logger(__name__)

DEFAULT_REUSE_THRESHOLD = 0.92


def normalize_question(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, and expand contractions.

    Used for cheap exact-match lookups before the embedding pass.
    """
    s = (text or "").lower().strip()
    s = re.sub(r"[\u2018\u2019\u201c\u201d]", "'", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 '?]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0

    dot = 0.0
    na = 0.0
    nb = 0.0

    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y

    if na == 0.0 or nb == 0.0:
        return 0.0

    return dot / ((na**0.5) * (nb**0.5))


@dataclass
class CachedAnswer:
    page_id: str
    question: str
    normalized_question: str
    question_family: str
    answer_type: str
    answer: str
    status: str
    approved_for_reuse: bool
    sensitive: bool
    context_specific: bool
    reuse_threshold: float
    canonical_key: str = ""
    topics: tuple[str, ...] = ()
    question_intent: str = ""
    normalized_answer: str = ""
    superseded_by_ids: list[str] = field(default_factory=list)
    related_opportunity_ids: list[str] = field(default_factory=list)
    related_application_ids: list[str] = field(default_factory=list)
    fact_source_ids: list[str] = field(default_factory=list)
    use_count: int = 0
    created_time: str = ""


@dataclass
class AnswerLookup:
    found: bool = False
    answer: CachedAnswer | None = None
    similarity: float = 0.0
    method: str = ""  # "exact" | "embedding" | "none"


def find_answer(
    *,
    question: str,
    answer_type: str,
    candidates: list[CachedAnswer],
    question_embedding: list[float] | None,
    candidate_embeddings: dict[str, list[float]] | None,
    threshold_default: float = DEFAULT_REUSE_THRESHOLD,
) -> AnswerLookup:
    """Pure search over a list of candidate answers.

    Exact normalized match first. Then embedding similarity, gated by
    per-row `reuse_threshold` (falling back to `threshold_default`),
    `Approved for Reuse`, matching `Answer Type`, and not `Context Specific`.
    """
    norm = normalize_question(question)

    for candidate in candidates:
        if not candidate.approved_for_reuse:
            continue

        if candidate.context_specific:
            continue

        if answer_type and candidate.answer_type and candidate.answer_type != answer_type:
            continue

        if candidate.normalized_question == norm:
            return AnswerLookup(
                found=True,
                answer=candidate,
                similarity=1.0,
                method="exact",
            )

    if question_embedding is None or not candidate_embeddings:
        return AnswerLookup(method="none")

    best: tuple[CachedAnswer, float] | None = None

    for candidate in candidates:
        if not candidate.approved_for_reuse or candidate.context_specific:
            continue

        if answer_type and candidate.answer_type and candidate.answer_type != answer_type:
            continue

        embedding = candidate_embeddings.get(candidate.page_id)

        if not embedding:
            continue

        similarity = cosine_similarity(
            question_embedding,
            embedding,
        )

        if best is None or similarity > best[1]:
            best = (candidate, similarity)

    if best is None:
        return AnswerLookup(method="none")

    answer, similarity = best
    threshold = answer.reuse_threshold or threshold_default

    if similarity >= threshold:
        return AnswerLookup(
            found=True,
            answer=answer,
            similarity=similarity,
            method="embedding",
        )

    return AnswerLookup(
        found=False,
        answer=answer,
        similarity=similarity,
        method="embedding",
    )


def find_question(
    *,
    question: str,
    question_family: str,
    answer_type: str,
    opportunity_id: str,
    candidates: list[CachedAnswer],
    question_embedding: list[float] | None,
    candidate_embeddings: dict[str, list[float]] | None,
    canonical_key: str = "",
    topics: tuple[str, ...] | list[str] = (),
    question_intent: str = "",
    threshold_default: float = DEFAULT_REUSE_THRESHOLD,
) -> AnswerLookup:
    """Find an existing approved or pending row for one qualification question."""
    eligible: list[CachedAnswer] = []

    for candidate in candidates:
        if candidate.status == "Deprecated":
            continue

        if answer_type and candidate.answer_type != answer_type:
            continue

        if candidate.context_specific and opportunity_id not in candidate.related_opportunity_ids:
            continue

        eligible.append(candidate)

    def preferred(values: list[CachedAnswer]) -> CachedAnswer:
        return min(
            values,
            key=lambda candidate: (
                -int(candidate.status == "Approved" and bool(candidate.answer.strip())),
                -int(candidate.approved_for_reuse),
                -candidate.use_count,
                candidate.created_time or "9999-12-31T23:59:59Z",
                candidate.page_id,
            ),
        )

    if canonical_key:
        matches = [candidate for candidate in eligible if candidate.canonical_key == canonical_key]

        if matches:
            return AnswerLookup(
                found=True,
                answer=preferred(matches),
                similarity=1.0,
                method="canonical_key",
            )

    normalized_topics = set(
        normalize_topics(
            topics,
            fallback="Other",
            max_topics=4,
        )
    )

    if normalized_topics and question_intent:
        matches: list[CachedAnswer] = []

        for candidate in eligible:
            candidate_topics = set(
                normalize_topics(
                    candidate.topics,
                    fallback="Other",
                    max_topics=4,
                )
            )

            if (
                candidate_topics == normalized_topics
                and candidate.question_intent == question_intent
                and candidate.answer_type == answer_type
            ):
                matches.append(candidate)

        if matches:
            return AnswerLookup(
                found=True,
                answer=preferred(matches),
                similarity=1.0,
                method="metadata",
            )

    norm = normalize_question(question)

    legacy_eligible = [
        candidate
        for candidate in eligible
        if not question_family or candidate.question_family == question_family
    ]

    exact_matches = [
        candidate for candidate in legacy_eligible if candidate.normalized_question == norm
    ]

    if exact_matches:
        return AnswerLookup(
            found=True,
            answer=preferred(exact_matches),
            similarity=1.0,
            method="exact",
        )

    if question_embedding is None or not candidate_embeddings:
        return AnswerLookup(method="none")

    best: tuple[CachedAnswer, float] | None = None

    for candidate in legacy_eligible:
        if candidate.canonical_key or candidate.topics or candidate.question_intent:
            continue

        embedding = candidate_embeddings.get(candidate.page_id)

        if not embedding:
            continue

        similarity = cosine_similarity(
            question_embedding,
            embedding,
        )

        if best is None or similarity > best[1]:
            best = (candidate, similarity)

    if best is None:
        return AnswerLookup(method="none")

    candidate, similarity = best
    threshold = candidate.reuse_threshold or threshold_default

    return AnswerLookup(
        found=similarity >= threshold,
        answer=candidate,
        similarity=similarity,
        method="embedding",
    )


# ---- Notion-facing helpers ----


def load_answer_bank(
    notion: NotionClient,
    settings: Settings,
) -> list[CachedAnswer]:
    ds_id = settings.notion.data_sources["answer_bank"]
    rows: list[CachedAnswer] = []

    for page in notion.query_data_source(ds_id):
        props = page.get("properties") or {}

        question = Pread.read_title(props.get("Question"))
        answer_type = Pread.read_select(props.get("Answer Type"))
        answer = Pread.read_text(props.get("My Answer"))
        topics = tuple(
            normalize_topics(
                Pread.read_multi_select(props.get("Topics")),
                fallback="Other",
                max_topics=4,
            )
        )

        rows.append(
            CachedAnswer(
                page_id=page.get("id", ""),
                question=question,
                normalized_question=(
                    Pread.read_text(props.get("Normalized Question"))
                    or normalize_question(question)
                ),
                question_family=Pread.read_select(props.get("Question Family")),
                answer_type=answer_type,
                answer=answer,
                status=read_status(props.get("Status")),
                approved_for_reuse=Pread.read_checkbox(props.get("Approved for Reuse")),
                sensitive=Pread.read_checkbox(props.get("Sensitive")),
                context_specific=Pread.read_checkbox(props.get("Context Specific")),
                reuse_threshold=float(
                    Pread.read_number(props.get("Reuse Threshold")) or DEFAULT_REUSE_THRESHOLD
                ),
                canonical_key=Pread.read_text(props.get("Canonical Key")),
                topics=topics,
                question_intent=Pread.read_select(props.get("Question Intent")),
                normalized_answer=(
                    Pread.read_text(props.get("Normalized Answer"))
                    or normalized_answer(
                        answer,
                        answer_type,
                    )
                ),
                superseded_by_ids=Pread.read_relation_ids(props.get("Superseded By")),
                related_opportunity_ids=Pread.read_relation_ids(props.get("Related Opportunities")),
                related_application_ids=Pread.read_relation_ids(props.get("Related Applications")),
                fact_source_ids=Pread.read_relation_ids(props.get("Fact Sources")),
                use_count=int(Pread.read_number(props.get("Use Count")) or 0),
                created_time=page.get(
                    "created_time",
                    "",
                ),
            )
        )

    return rows


def create_needs_answer_page(
    notion: NotionClient,
    settings: Settings,
    *,
    question: str,
    question_family: str,
    answer_type: str,
    related_opportunity_id: str | None,
    related_application_id: str | None,
    sensitive: bool,
    dry_run: bool,
    canonical_key: str = "",
    topics: tuple[str, ...] | list[str] = (),
    question_intent: str = "",
) -> dict:
    """Create an Answer Bank row in `Needs Answer` status. Returns the page."""
    canonical_topics = normalize_topics(
        topics,
        fallback="Other",
        max_topics=4,
    )

    if dry_run:
        log.info(
            "[dry-run] would create Answer Bank row q=%r family=%s type=%s topics=%s",
            question,
            question_family,
            answer_type,
            canonical_topics,
        )
        return {}

    relations: dict = {}

    if related_opportunity_id:
        relations["Related Opportunities"] = P.relation([related_opportunity_id])

    if related_application_id:
        relations["Related Applications"] = P.relation([related_application_id])

    properties = {
        "Question": P.title(question),
        "Normalized Question": P.rich_text(normalize_question(question)),
        "Question Family": P.select(question_family or "Other"),
        "Answer Type": P.select(answer_type or "Short Text"),
        "Canonical Key": P.rich_text(canonical_key),
        "Topics": P.multi_select(canonical_topics),
        "Question Intent": P.select(question_intent or "Other"),
        "Normalized Answer": P.rich_text(""),
        "Status": P.select("Needs Answer"),
        "Approved for Reuse": P.checkbox(False),
        "Sensitive": P.checkbox(sensitive),
        "Context Specific": P.checkbox(False),
        "First Seen": P.date_property(datetime.now(UTC)),
        "Use Count": P.number(0),
        "Draft Source": P.select("LLM"),
        **relations,
    }

    return notion.create_page(
        parent={"data_source_id": settings.notion.data_sources["answer_bank"]},
        properties=properties,
    )


def create_needs_answer(
    notion: NotionClient,
    settings: Settings,
    *,
    question: str,
    question_family: str,
    answer_type: str,
    related_opportunity_id: str | None,
    related_application_id: str | None,
    sensitive: bool,
    dry_run: bool,
    canonical_key: str = "",
    topics: tuple[str, ...] | list[str] = (),
    question_intent: str = "",
) -> str:
    """Create an Answer Bank row in `Needs Answer` status. Returns page id."""
    page = create_needs_answer_page(
        notion,
        settings,
        question=question,
        question_family=question_family,
        answer_type=answer_type,
        related_opportunity_id=related_opportunity_id,
        related_application_id=related_application_id,
        sensitive=sensitive,
        dry_run=dry_run,
        canonical_key=canonical_key,
        topics=topics,
        question_intent=question_intent,
    )

    return page.get("id", "")


def link_answer_to_opportunity(
    notion: NotionClient,
    answer: CachedAnswer,
    opportunity_id: str,
    *,
    mark_used: bool,
    dry_run: bool,
) -> None:
    """Idempotently add an opportunity relation and optional reuse metadata."""
    related = list(
        dict.fromkeys(
            [
                *answer.related_opportunity_ids,
                opportunity_id,
            ]
        )
    )

    if dry_run:
        return

    properties = {"Related Opportunities": P.relation(related)}

    if mark_used:
        properties.update(
            {
                "Last Used": P.date_property(datetime.now(UTC)),
                "Use Count": P.number(answer.use_count + 1),
            }
        )
        answer.use_count += 1

    notion.update_page(
        answer.page_id,
        properties,
    )

    answer.related_opportunity_ids = related


def refresh_answer_embeddings(
    *,
    answers: list[CachedAnswer],
    router: ModelRouter,
    client: LMStudioClient,
    store: Store,
) -> dict[str, list[float]]:
    """Compute or load cached embeddings for each answer's question."""
    try:
        model = router.get("embedding")
    except Exception:
        return {}

    cached = dict(
        store.load_embeddings(
            namespace="answer_question",
            model=model,
        )
    )

    needed = [answer for answer in answers if answer.page_id and answer.page_id not in cached]

    if needed:
        vectors = client.embed(
            model=model,
            inputs=[answer.question for answer in needed],
            stage="answer-bank",
            role="embedding",
            task="refresh_answer_embeddings",
        )

        for answer, vector in zip(
            needed,
            vectors,
            strict=True,
        ):
            store.store_embedding(
                namespace="answer_question",
                key=answer.page_id,
                model=model,
                vector=vector,
                text_preview=answer.question[:200],
            )

            cached[answer.page_id] = vector

    return cached
