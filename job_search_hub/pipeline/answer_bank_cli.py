"""``answer-bank`` CLI operations.

Subcommands:

  list         List all rows currently in ``Needs Answer``.
  show <q>     Print one row in detail (substring match on the question).
  refresh      Recompute embeddings for every Approved row in the bank.
  reuse-test <question>  Show what the reuse search would return for a
                          hypothetical question (no writes).
  propose-yes-no          Ask LM Studio to backfill unanswered Yes/No rows
                          from verified Profile Facts + approved skill answers.

These commands are intentionally simple — the source of truth is Notion.
You write/approve answers there, this module just helps you observe the
local cache and pre-test reuse decisions.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.logging_setup import get_logger
from job_search_hub.models import LMStudioClient, ModelRouter
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.answers import (
    DEFAULT_REUSE_THRESHOLD,
    CachedAnswer,
    find_answer,
    load_answer_bank,
    refresh_answer_embeddings,
)
from job_search_hub.pipeline.profile import Fact, load_facts
from job_search_hub.pipeline.qualification_keys import normalized_answer
from job_search_hub.pipeline.structured_output import (
    rows_by_exact_index,
    validate_exact_indexes,
)
from job_search_hub.topics import normalize_topics

log = get_logger(__name__)
T = TypeVar("T")

_CONTEXT_LENGTH_ERROR_RE = re.compile(
    r"context(?:\s|-|_)?length|context window|token(?:s)? limit|too many tokens|"
    r"maximum context|prompt is too long|exceeds? context|n_ctx|tokens to keep",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]{1,}")
_STOP_WORDS = {
    "about",
    "able",
    "also",
    "and",
    "are",
    "can",
    "candidate",
    "do",
    "does",
    "experience",
    "familiar",
    "for",
    "have",
    "has",
    "in",
    "is",
    "of",
    "or",
    "proficient",
    "skill",
    "skills",
    "the",
    "to",
    "with",
    "you",
    "your",
}

_YES_NO_PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "question_index",
                    "answer",
                    "confidence",
                    "reason",
                    "fact_ids",
                    "answer_ids",
                ],
                "properties": {
                    "question_index": {
                        "type": "integer",
                        "minimum": 0,
                    },
                    "answer": {
                        "type": "string",
                        "enum": [
                            "Yes",
                            "No",
                            "Unknown",
                        ],
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": 300,
                    },
                    "fact_ids": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "string",
                        },
                    },
                    "answer_ids": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "string",
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class YesNoProposal:
    page_id: str
    question: str
    answer: str
    confidence: float
    reason: str
    fact_ids: list[str]
    answer_ids: list[str]
    written: bool = False
    skipped_reason: str = ""


@dataclass
class YesNoProposalStats:
    scanned: int = 0
    candidates: int = 0
    proposed: int = 0
    written: int = 0
    skipped: int = 0
    unknown: int = 0
    errors: list[str] | None = None
    proposals: list[YesNoProposal] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

        if self.proposals is None:
            self.proposals = []


def _canonical_topics(
    topics: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Normalize topic values to the controlled vocabulary."""
    return tuple(
        normalize_topics(
            topics,
            fallback="Other",
            max_topics=4,
        )
    )


def list_needs_answer(
    notion: NotionClient,
    settings: Settings,
) -> list[CachedAnswer]:
    rows = load_answer_bank(
        notion,
        settings,
    )

    return [row for row in rows if row.status == "Needs Answer"]


def show_summary(
    rows: Iterable[CachedAnswer],
) -> str:
    rows = list(rows)

    if not rows:
        return "(no rows)"

    out: list[str] = []

    for row in rows:
        topics = _canonical_topics(row.topics)
        topic_summary = ", ".join(topics) if topics else "?"

        out.append(
            f"- [{row.status}] "
            f"{row.question_family or '?':<22} | "
            f"{row.answer_type or '?':<10} | "
            f"{topic_summary:<24} | "
            f"{row.question[:80]}"
        )

    return "\n".join(out)


def search_question(
    needle: str,
    rows: Iterable[CachedAnswer],
) -> list[CachedAnswer]:
    needle_l = needle.lower()

    return [row for row in rows if needle_l in row.question.lower()]


def reuse_test(
    *,
    question: str,
    answer_type: str,
    rows: list[CachedAnswer],
    router: ModelRouter,
    client: LMStudioClient,
    store: Store,
) -> dict:
    """Run the same reuse search used by qualification and application checks."""
    embeddings = {}
    question_embedding = None
    if router is not None and client is not None:
        embeddings = refresh_answer_embeddings(
            answers=rows, router=router, client=client, store=store
        )
        with contextlib.suppress(KeyError, ValueError, RuntimeError):
            question_embedding = client.embed(
                model=router.get("embedding"),
                inputs=[question],
                stage="answer-bank",
                role="embedding",
                task="reuse_test",
            )[0]

    lookup = find_answer(
        question=question,
        answer_type=answer_type,
        candidates=rows,
        question_embedding=question_embedding,
        candidate_embeddings=embeddings,
    )

    if lookup.found and lookup.answer is not None:
        return {
            "found": True,
            "method": lookup.method,
            "similarity": lookup.similarity,
            "matched_question": lookup.answer.question,
            "answer": lookup.answer.answer,
            "threshold": (lookup.answer.reuse_threshold or DEFAULT_REUSE_THRESHOLD),
            "topics": list(_canonical_topics(lookup.answer.topics)),
            "question_intent": (lookup.answer.question_intent),
            "canonical_key": (lookup.answer.canonical_key),
        }

    return {
        "found": False,
        "method": lookup.method,
        "similarity": lookup.similarity,
        "matched_question": (lookup.answer.question if lookup.answer else ""),
        "topics": (list(_canonical_topics(lookup.answer.topics)) if lookup.answer else []),
        "question_intent": (lookup.answer.question_intent if lookup.answer else ""),
        "canonical_key": (lookup.answer.canonical_key if lookup.answer else ""),
    }


def _chunks(
    items: list[T],
    size: int,
) -> Iterable[list[T]]:
    size = max(
        1,
        size,
    )

    for start in range(
        0,
        len(items),
        size,
    ):
        yield items[start : start + size]


def _clip(
    value: str | None,
    limit: int,
) -> str:
    value = " ".join(str(value or "").split())

    return value[:limit]


def _is_context_length_error(
    exc: BaseException,
) -> bool:
    return bool(_CONTEXT_LENGTH_ERROR_RE.search(str(exc)))


def _is_structured_batch_error(
    exc: BaseException,
) -> bool:
    text = str(exc).lower()

    return any(
        marker in text
        for marker in (
            "malformed json",
            "schema-invalid",
            "logically invalid json",
            "structured response failed",
        )
    )


def _tokens(
    *values: object,
) -> set[str]:
    text = " ".join(str(value or "") for value in values)

    return {
        token
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOP_WORDS and not token.isdigit()
    }


def _yes_no_token(
    value: str,
) -> str:
    normalized = normalized_answer(
        value,
        "Yes/No",
    )

    if normalized == "yes":
        return "Yes"

    if normalized == "no":
        return "No"

    return ""


def _candidate_rows(
    rows: list[CachedAnswer],
    *,
    include_sensitive: bool,
    limit: int | None,
) -> list[CachedAnswer]:
    skill_intents = {
        "",
        "Experience",
        "Proficiency",
        "Other",
    }

    candidates = [
        row
        for row in rows
        if row.status == "Needs Answer"
        and row.answer_type == "Yes/No"
        and row.question.strip()
        and not row.answer.strip()
        and (
            (row.question_family == "Skills" and row.question_intent in skill_intents)
            or row.question_intent
            in {
                "Experience",
                "Proficiency",
            }
        )
        and (include_sensitive or not row.sensitive)
    ]

    return candidates[:limit] if limit else candidates


def _fact_payload(
    facts: list[Fact],
    *,
    include_sensitive: bool,
) -> list[dict]:
    payload: list[dict] = []

    for fact in facts:
        if not fact.active or not fact.verified or not fact.approved_for_applications:
            continue

        if fact.sensitive and not include_sensitive:
            continue

        if not (fact.page_id or fact.fact_id):
            continue

        fact_topics = tuple(
            normalize_topics(
                [
                    fact.value,
                    fact.category,
                    fact.proficiency,
                    *fact.emphasis_tags,
                ],
                fallback="Other",
                max_topics=4,
            )
        )

        payload.append(
            {
                "id": (fact.page_id or fact.fact_id),
                "type": fact.fact_type,
                "value": _clip(
                    fact.value,
                    140,
                ),
                "category": _clip(
                    fact.category,
                    60,
                ),
                "proficiency": _clip(
                    fact.proficiency,
                    60,
                ),
                "role": _clip(
                    fact.role_label,
                    120,
                ),
                "organization": _clip(
                    fact.organization,
                    120,
                ),
                "topics": list(fact_topics),
            }
        )

    payload.sort(
        key=lambda row: (
            row.get("type") != "Skill",
            row.get("value", ""),
        )
    )

    return payload


def _approved_skill_answer_payload(
    rows: list[CachedAnswer],
    *,
    include_sensitive: bool,
) -> list[dict]:
    payload: list[dict] = []

    for row in rows:
        answer = _yes_no_token(row.answer)

        if not answer:
            continue

        if row.status != "Approved" or row.answer_type != "Yes/No":
            continue

        if not row.approved_for_reuse or row.context_specific:
            continue

        if row.sensitive and not include_sensitive:
            continue

        if row.question_family != "Skills" and row.question_intent not in {
            "Experience",
            "Proficiency",
            "Credential",
        }:
            continue

        topics = _canonical_topics(row.topics)

        payload.append(
            {
                "id": row.page_id,
                "question": _clip(
                    row.question,
                    180,
                ),
                "answer": answer,
                "topics": list(topics),
                "intent": (row.question_intent),
                "family": (row.question_family),
                "canonical_key": _clip(
                    row.canonical_key,
                    160,
                ),
            }
        )

    return payload


def _question_payload(
    rows: list[CachedAnswer],
) -> list[dict]:
    return [
        {
            "question_index": index,
            "question": _clip(
                row.question,
                300,
            ),
            "family": (row.question_family),
            "intent": (row.question_intent),
            "topics": list(_canonical_topics(row.topics)),
            "canonical_key": _clip(
                row.canonical_key,
                160,
            ),
        }
        for index, row in enumerate(rows)
    ]


def _batch_terms(
    rows: list[CachedAnswer],
) -> set[str]:
    terms: set[str] = set()

    for row in rows:
        topics = _canonical_topics(row.topics)

        terms.update(
            _tokens(
                row.question,
                row.question_family,
                row.question_intent,
                row.canonical_key,
                " ".join(topics),
            )
        )

    return terms


def _scored_context(
    context: list[dict],
    terms: set[str],
    *,
    limit: int,
    fields: tuple[str, ...],
) -> list[dict]:
    if not context or not terms:
        return []

    scored: list[tuple[int, str, dict]] = []

    for item in context:
        item_terms = _tokens(
            *(
                item.get(
                    field,
                    "",
                )
                for field in fields
            )
        )

        item_terms.update(
            _tokens(
                " ".join(
                    str(value)
                    for value in item.get(
                        "topics",
                        [],
                    )
                )
            )
        )

        score = len(terms & item_terms)

        if score <= 0:
            continue

        scored.append(
            (
                score,
                str(item.get("value") or item.get("question") or ""),
                item,
            )
        )

    scored.sort(
        key=lambda row: (
            -row[0],
            row[1],
        )
    )

    return [item for _score, _label, item in scored[:limit]]


def _select_context_for_batch(
    batch: list[CachedAnswer],
    *,
    fact_context: list[dict],
    answer_context: list[dict],
) -> tuple[list[dict], list[dict]]:
    terms = _batch_terms(batch)

    fact_limit = max(
        8,
        min(
            28,
            len(batch) * 5,
        ),
    )
    answer_limit = max(
        4,
        min(
            16,
            len(batch) * 3,
        ),
    )

    facts = _scored_context(
        fact_context,
        terms,
        limit=fact_limit,
        fields=(
            "value",
            "category",
            "proficiency",
            "role",
            "organization",
        ),
    )

    answers = _scored_context(
        answer_context,
        terms,
        limit=answer_limit,
        fields=(
            "question",
            "intent",
            "family",
            "canonical_key",
        ),
    )

    return facts, answers


def _proposal_note(
    proposal: YesNoProposal,
    *,
    approved: bool,
) -> str:
    status = "Approved" if approved else "Needs Review"

    return "\n".join(
        [
            (f"LM Studio proposed {proposal.answer} with confidence {proposal.confidence:.2f}."),
            f"Status written: {status}.",
            f"Reason: {proposal.reason}",
            ("Fact sources: " + (", ".join(proposal.fact_ids) if proposal.fact_ids else "(none)")),
            (
                "Answer sources: "
                + (", ".join(proposal.answer_ids) if proposal.answer_ids else "(none)")
            ),
        ]
    )


def _write_proposal(
    *,
    notion: NotionClient,
    row: CachedAnswer,
    proposal: YesNoProposal,
    approve: bool,
) -> None:
    properties = {
        "My Answer": P.rich_text(proposal.answer),
        "Normalized Answer": P.rich_text(
            normalized_answer(
                proposal.answer,
                "Yes/No",
            )
        ),
        "Status": P.select("Approved" if approve else "Needs Review"),
        "Approved for Reuse": P.checkbox(
            approve and not row.sensitive and not row.context_specific
        ),
        "Draft Source": P.select("LLM"),
        "Notes": P.rich_text(
            _proposal_note(
                proposal,
                approved=approve,
            )
        ),
    }

    if proposal.fact_ids:
        properties["Fact Sources"] = P.relation(proposal.fact_ids)

    notion.update_page(
        row.page_id,
        properties,
    )


def propose_yes_no_answers(
    *,
    notion: NotionClient,
    settings: Settings,
    store: Store,
    router: ModelRouter,
    client: LMStudioClient,
    dry_run: bool,
    limit: int | None = None,
    batch_size: int = 20,
    min_confidence: float = 0.82,
    include_sensitive: bool = False,
    approve: bool = False,
) -> YesNoProposalStats:
    """Backfill unanswered Yes/No Answer Bank rows using compact evidence batches."""
    stats = YesNoProposalStats()

    rows = load_answer_bank(
        notion,
        settings,
    )
    stats.scanned = len(rows)

    candidates = _candidate_rows(
        rows,
        include_sensitive=include_sensitive,
        limit=limit,
    )
    stats.candidates = len(candidates)

    if not candidates:
        return stats

    facts = load_facts(
        notion,
        settings,
    )

    fact_context = _fact_payload(
        facts,
        include_sensitive=include_sensitive,
    )

    answer_context = _approved_skill_answer_payload(
        rows,
        include_sensitive=include_sensitive,
    )

    valid_fact_ids = {str(row["id"]) for row in fact_context if row.get("id")}

    valid_answer_ids = {str(row["id"]) for row in answer_context if row.get("id")}

    system = (
        "Backfill reusable job application Yes/No answers for a candidate. Use only "
        "the supplied approved profile facts and approved reusable skill answers. "
        "Answer Yes only when evidence directly supports the question. Answer No "
        "only when an approved source answer explicitly says No or directly denies "
        "the same capability. Absence of evidence is Unknown, never No. Topic labels "
        "use a controlled vocabulary and should be treated as the primary semantic "
        "match signal. Return one proposal per question_index. Reasons must be "
        "one-line strings without line breaks."
    )

    model = router.get("primary")

    def call_batch(
        batch: list[CachedAnswer],
        *,
        fact_limit: int | None = None,
        answer_limit: int | None = None,
    ) -> list[tuple[CachedAnswer, dict]]:
        batch_facts, batch_answers = _select_context_for_batch(
            batch,
            fact_context=fact_context,
            answer_context=answer_context,
        )

        if fact_limit is not None:
            batch_facts = batch_facts[:fact_limit]

        if answer_limit is not None:
            batch_answers = batch_answers[:answer_limit]

        payload = client.chat_json(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": (
                        "Questions:\n"
                        f"{json.dumps(_question_payload(batch), ensure_ascii=True)}"
                        "\n\nRelevant approved profile facts:\n"
                        f"{json.dumps(batch_facts, ensure_ascii=True)}"
                        "\n\nRelevant approved reusable Yes/No skill answers:\n"
                        f"{json.dumps(batch_answers, ensure_ascii=True)}"
                    ),
                },
            ],
            json_schema=_YES_NO_PROPOSAL_SCHEMA,
            max_tokens=max(
                384,
                min(
                    1536,
                    len(batch) * 110 + 384,
                ),
            ),
            stage="answer-bank",
            role="primary",
            task="propose_yes_no_answers",
            post_validator=(
                lambda data: validate_exact_indexes(
                    data,
                    array_key="proposals",
                    index_key="question_index",
                    expected_indexes=range(len(batch)),
                )
            ),
        )

        indexed = rows_by_exact_index(
            payload,
            array_key="proposals",
            index_key="question_index",
            expected_indexes=range(len(batch)),
        )

        return [
            (
                batch[index],
                raw,
            )
            for index, raw in indexed.items()
        ]

    def call_batch_adaptive(
        batch: list[CachedAnswer],
    ) -> list[tuple[CachedAnswer, dict]]:
        try:
            return call_batch(batch)
        except Exception as exc:
            retryable = _is_context_length_error(exc) or _is_structured_batch_error(exc)

            if not retryable:
                raise

            if len(batch) > 1:
                midpoint = max(
                    1,
                    len(batch) // 2,
                )

                log.info(
                    "answer-bank proposal batch failed retryably; retrying as %d + %d",
                    midpoint,
                    len(batch) - midpoint,
                )

                return [
                    *call_batch_adaptive(batch[:midpoint]),
                    *call_batch_adaptive(batch[midpoint:]),
                ]

            log.info("answer-bank proposal single-row retryable failure; retrying minimal context")

            return call_batch(
                batch,
                fact_limit=3,
                answer_limit=2,
            )

    for batch in _chunks(
        candidates,
        batch_size,
    ):
        try:
            batch_rows = call_batch_adaptive(batch)
        except Exception as exc:
            stats.errors.append(str(exc))

            log.warning(
                "answer-bank yes/no proposal batch failed: %s",
                exc,
            )
            continue

        for row, raw in batch_rows:
            answer = str(raw.get("answer") or "Unknown")

            confidence = float(raw.get("confidence") or 0.0)

            fact_ids = list(
                dict.fromkeys(
                    str(value)
                    for value in (raw.get("fact_ids") or [])
                    if str(value) in valid_fact_ids
                )
            )

            answer_ids = list(
                dict.fromkeys(
                    str(value)
                    for value in (raw.get("answer_ids") or [])
                    if str(value) in valid_answer_ids
                )
            )

            proposal = YesNoProposal(
                page_id=row.page_id,
                question=row.question,
                answer=answer,
                confidence=confidence,
                reason=str(raw.get("reason") or "").strip(),
                fact_ids=fact_ids,
                answer_ids=answer_ids,
            )

            if answer == "Unknown":
                stats.unknown += 1
                proposal.skipped_reason = "unknown"

            elif answer not in {
                "Yes",
                "No",
            }:
                stats.skipped += 1
                proposal.skipped_reason = "invalid answer"

            elif confidence < min_confidence:
                stats.skipped += 1
                proposal.skipped_reason = f"confidence below {min_confidence:.2f}"

            elif not fact_ids and not answer_ids:
                stats.skipped += 1
                proposal.skipped_reason = "missing cited evidence"

            elif answer == "No" and not answer_ids:
                stats.skipped += 1
                proposal.skipped_reason = "No requires explicit approved answer evidence"

            else:
                stats.proposed += 1

                if not dry_run:
                    _write_proposal(
                        notion=notion,
                        row=row,
                        proposal=proposal,
                        approve=approve,
                    )

                    store.audit(
                        actor="answer-bank",
                        action=("propose_yes_no_answer"),
                        target_kind="answer_bank",
                        target_id=row.page_id,
                        dry_run=dry_run,
                        payload={
                            "answer": (proposal.answer),
                            "confidence": (proposal.confidence),
                            "approved": approve,
                            "fact_ids": (proposal.fact_ids),
                            "answer_ids": (proposal.answer_ids),
                            "topics": list(_canonical_topics(row.topics)),
                            "canonical_key": (row.canonical_key),
                        },
                    )

                proposal.written = not dry_run
                stats.written += int(not dry_run)

            stats.proposals.append(proposal)

    return stats
