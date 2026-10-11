from __future__ import annotations

from types import SimpleNamespace

from job_search_hub.notion import properties as P
from job_search_hub.pipeline.qualifications import (
    QualificationGate,
    QualificationRequirement,
    QualificationResolution,
)
from job_search_hub.pipeline.score_stage import (
    ScoreStats,
    _persist_qualification_gate,
    send_answer_digest,
)


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_once(self, kind, key, subject, body):
        self.sent.append((kind, key, subject, body))
        return True


class _Store:
    def __init__(self):
        self.audit_rows = []

    def audit(self, **kwargs):
        self.audit_rows.append(kwargs)


class _Notion:
    def __init__(self):
        self.created = []

    def create_page(self, **kwargs):
        self.created.append(kwargs)
        return {"id": "answer-1", "url": "https://notion.local/answer-1"}


def test_missing_answer_creation_is_sent_in_single_digest() -> None:
    requirement = QualificationRequirement(
        text="Experience with Matillion",
        kind="Tool",
        importance="Required",
        question="Do you have experience with Matillion?",
        question_family="Experience",
        answer_type="Yes/No",
        topics=("Matillion",),
        question_intent="Experience",
        expected_operator="Is Yes",
        expected_answer="yes",
        canonical_key="experience|tool:matillion|yes-no",
    )
    resolution = QualificationResolution(
        requirement=requirement,
        status="unknown",
        reason="No approved answer",
        question_embedding=[1.0, 0.0],
    )
    gate = QualificationGate(
        requirements=[requirement],
        resolutions=[resolution],
        unresolved=[resolution],
        hard_failures=[],
        answer_ids=[],
    )
    notifier = _Notifier()
    stats = ScoreStats()

    linked = _persist_qualification_gate(
        page={
            "id": "opportunity-1",
            "url": "https://notion.local/opportunity-1",
            "properties": {
                "Company": P.rich_text("Acme"),
                "Role Title": P.rich_text("Engineer"),
                "Answer Bank Items": P.relation([]),
            },
        },
        gate=gate,
        answers=[],
        notion=_Notion(),  # type: ignore[arg-type]
        settings=SimpleNamespace(notion=SimpleNamespace(data_sources={"answer_bank": "answers"})),
        store=_Store(),  # type: ignore[arg-type]
        stats=stats,
        notifier=notifier,  # type: ignore[arg-type]
        dry_run=False,
    )

    assert linked == ["answer-1"]
    assert stats.questions_created == 1
    assert notifier.sent == []

    assert send_answer_digest(notifier, stats, dry_run=False)
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "needs_answer_digest"
    assert notifier.sent[0][2] == "Job Search Hub: Answer Bank digest (1 question(s))"
    assert "Summary" in notifier.sent[0][3]
    assert "Action Needed" in notifier.sent[0][3]
    assert "Do you have experience with Matillion?" in notifier.sent[0][3]


def test_score_stats_string_summarizes_answer_notifications() -> None:
    stats = ScoreStats()
    stats.answer_notifications.append(
        SimpleNamespace(
            answer_id="answer-1",
            question="Do you have experience with a very specific internal tool?",
        )
    )
    stats.errors.extend(["first", "second", "third", "fourth"])

    text = str(stats)

    assert "answer_notifications=1" in text
    assert "very specific internal tool" not in text
    assert "errors=4" in text
    assert "errors_more=1" in text
