from __future__ import annotations

from types import SimpleNamespace

from job_search_hub.commands.operations import cmd_migrate_qualifications
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.answers import CachedAnswer, normalize_question
from job_search_hub.pipeline.lifecycle_sync import meeting_answer_ids_by_opportunity
from job_search_hub.pipeline.profile import Fact
from job_search_hub.pipeline.qualification_migration import (
    QualificationMigrationStats,
    defer_backfill_candidates,
    find_awaiting_backfill_candidates,
    migrate_answer_bank,
)
from job_search_hub.pipeline.qualification_requirements import PersistedQualification
from job_search_hub.pipeline.score import Criterion
from job_search_hub.pipeline.score_stage import run_score


def _criterion() -> Criterion:
    return Criterion(
        page_id="qualification-fit",
        name="Qualification Fit",
        key="qualification_fit",
        active=True,
        category="Skills",
        weight=2,
        direction="Positive",
        evaluation_method="Rule",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="persisted deterministic fit",
        minimum_value=None,
        maximum_value=None,
        sort_order=7.5,
    )


def _answer(*, page_id: str, value: str, status: str = "Approved") -> CachedAnswer:
    question = "Have you worked with Matillion?"
    return CachedAnswer(
        page_id=page_id,
        question=question,
        normalized_question=normalize_question(question),
        question_family="Skills",
        answer_type="Yes/No",
        answer=value,
        status=status,
        approved_for_reuse=status == "Approved",
        sensitive=False,
        context_specific=False,
        reuse_threshold=0.92,
        canonical_key="experience|tool:matillion|yes-no",
        topics=("Matillion",),
        question_intent="Experience",
    )


def _requirement(
    *,
    answer_id: str,
    importance: str = "Required",
    failure_action: str = "Score Only",
) -> PersistedQualification:
    return PersistedQualification(
        page_id="requirement-1",
        opportunity_id="opportunity-1",
        answer_id=answer_id,
        qualification="Experience with Matillion",
        canonical_key="experience|tool:matillion|yes-no",
        topics=("Matillion",),
        question_intent="Experience",
        requirement_kind="Tool",
        importance=importance,
        answer_type="Yes/No",
        expected_operator="Is Yes",
        expected_answer="yes",
        numeric_threshold=None,
        meets_score=1,
        partial_score=0.5,
        does_not_meet_score=0,
        score_weight=3 if importance == "Hard Required" else 2,
        failure_action=failure_action,
        resolution="Awaiting Answer",
        resolution_reason="",
        source_requirement="Experience with Matillion",
        fingerprint="fingerprint-1",
        active=True,
    )


def _opportunity() -> dict:
    return {
        "id": "opportunity-1",
        "properties": {
            "Status": P.select("Awaiting Answer"),
            "Base Model Score": P.number(0.8),
            "Base Score Numerator": P.number(8),
            "Base Score Weight": P.number(10),
            "Base Confidence": P.number(0.7),
            "Base Model Reason": P.rich_text("base reason"),
            "Base Top Matches": P.rich_text("Python"),
            "Base Concerns": P.rich_text(""),
            "Base Lane Override": P.select("None"),
        },
    }


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        notion=SimpleNamespace(
            data_sources={
                "opportunities": "opportunities",
                "answer_bank": "answers",
                "qualification_requirements": "requirements",
            }
        ),
    )


class _Store:
    def audit(self, **kwargs):
        return None


def test_awaiting_resolution_publishes_stored_base_plus_qualification(
    monkeypatch,
) -> None:
    updates = []
    answer = _answer(page_id="answer-yes", value="Yes")
    requirement = _requirement(answer_id=answer.page_id)

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [_opportunity()]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda *args, **kwargs: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_answer_bank",
        lambda *args, **kwargs: [answer],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_requirements",
        lambda *args, **kwargs: [requirement],
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=_Store(),
        awaiting_only=True,
        dry_run=False,
    )

    requirement_update = next(props for page_id, props in updates if page_id == "requirement-1")
    opportunity_update = next(props for page_id, props in updates if page_id == "opportunity-1")
    assert P.read_select(requirement_update["Resolution"]) == "Meets"
    assert P.read_number(opportunity_update["Model Score"]) == 0.8333
    assert P.read_number(opportunity_update["Qualification Score"]) == 1
    assert P.read_select(opportunity_update["Status"]) == "Review"
    assert stats.awaiting_resolved == 1
    assert stats.questions_created == 0


def test_awaiting_resolution_rejects_known_hard_failure(monkeypatch) -> None:
    updates = []
    answer = _answer(page_id="answer-no", value="No")
    requirement = _requirement(
        answer_id=answer.page_id,
        importance="Hard Required",
        failure_action="Reject",
    )

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [_opportunity()]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda *args, **kwargs: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_answer_bank",
        lambda *args, **kwargs: [answer],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_requirements",
        lambda *args, **kwargs: [requirement],
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=_Store(),
        awaiting_only=True,
        dry_run=False,
    )

    opportunity_update = next(props for page_id, props in updates if page_id == "opportunity-1")
    assert P.read_select(opportunity_update["Status"]) == "Rejected"
    assert P.read_checkbox(opportunity_update["Hard Exclusion"])
    assert P.read_number(opportunity_update["Model Score"]) is None
    assert stats.excluded == 1


def test_awaiting_resolution_requeues_requirementless_legacy_row(
    monkeypatch,
) -> None:
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [_opportunity()]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda *args, **kwargs: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_answer_bank",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_requirements",
        lambda *args, **kwargs: [],
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=_Store(),
        awaiting_only=True,
        dry_run=False,
    )

    assert stats.awaiting_answer == 0
    assert stats.awaiting_requeued == 1
    assert stats.questions_created == 0
    assert updates == [
        (
            "opportunity-1",
            {
                "Status": P.select("Scoring"),
                "Last Error": P.rich_text(
                    "Legacy Awaiting Answer row lacks persisted "
                    "qualification context; requeued for scoring."
                ),
            },
        )
    ]


def test_awaiting_resolution_uses_resume_facts_when_answer_is_missing(
    monkeypatch,
) -> None:
    updates = []
    requirement = _requirement(answer_id="")
    fact = Fact(
        page_id="fact-matillion",
        fact_id="",
        fact_type="Skill",
        parent_fact_id="",
        value="Matillion",
        organization="",
        role_label="",
        relationship_type="Not Applicable",
        start_date=None,
        end_date=None,
        current=False,
        category="Data",
        proficiency="Strong",
        emphasis_tags=[],
        verified=True,
        approved_for_profile=True,
        approved_for_applications=True,
        metrics_verified=False,
        sensitive=False,
        sort_order=0,
        active=True,
    )

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [_opportunity()]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda *args, **kwargs: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_answer_bank",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_requirements",
        lambda *args, **kwargs: [requirement],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_facts",
        lambda *args, **kwargs: [fact],
    )
    settings = _settings()
    settings.notion.data_sources["resume_facts"] = "facts"

    stats = run_score(
        settings,
        notion=Notion(),
        store=_Store(),
        awaiting_only=True,
        dry_run=False,
    )

    requirement_update = next(props for page_id, props in updates if page_id == "requirement-1")
    opportunity_update = next(props for page_id, props in updates if page_id == "opportunity-1")
    assert P.read_select(requirement_update["Resolution"]) == "Meets"
    assert P.read_number(opportunity_update["Model Score"]) == 0.8333
    assert stats.awaiting_resolved == 1


def test_backfill_candidate_discovery_defers_incomplete_descriptions(
    monkeypatch,
) -> None:
    complete = _opportunity()
    complete["id"] = "complete"
    complete["properties"]["Description"] = P.rich_text("Complete job description.")
    truncated = _opportunity()
    truncated["id"] = "truncated"
    truncated["properties"]["Description"] = P.rich_text("Truncated description...&nbsp;")
    empty = _opportunity()
    empty["id"] = "empty"
    empty["properties"]["Description"] = P.rich_text("")

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [complete, truncated, empty]

    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_requirements",
        lambda *args, **kwargs: [],
    )
    stats = QualificationMigrationStats()

    candidates = find_awaiting_backfill_candidates(
        _settings(),
        notion=Notion(),
        store=_Store(),
        stats=stats,
        dry_run=False,
    )

    assert [page["id"] for page in candidates] == ["complete"]
    assert stats.awaiting_candidates == 3
    assert stats.awaiting_deferred == 2
    assert any("truncated" in value for value in stats.deferred)
    assert any("empty" in value for value in stats.deferred)
    assert stats.errors == []


def test_model_unavailable_backfill_deferral_is_not_an_error() -> None:
    stats = QualificationMigrationStats(awaiting_candidates=1)

    defer_backfill_candidates(
        [{"id": "opportunity-1"}],
        reason="LM Studio unavailable",
        store=_Store(),
        stats=stats,
        dry_run=False,
    )

    assert stats.awaiting_deferred == 1
    assert stats.errors == []
    assert stats.deferred == ["opportunity-1: LM Studio unavailable"]


def test_migration_command_succeeds_when_model_backfill_is_deferred(
    monkeypatch,
) -> None:
    stats = QualificationMigrationStats(
        answers_scanned=1,
        answers_canonicalized=1,
        awaiting_candidates=1,
    )

    class NotionContext:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class Router:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(
        "job_search_hub.commands.operations._bootstrap",
        lambda args: (
            SimpleNamespace(
                sqlite_path="test.sqlite",
                lmstudio=SimpleNamespace(),
                llm_audit_mode="metadata",
            ),
            False,
        ),
    )
    monkeypatch.setattr("job_search_hub.commands.operations.open_store", lambda path: _Store())
    monkeypatch.setattr(
        "job_search_hub.commands.operations._open_notion", lambda settings: NotionContext()
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.migrate_answer_bank",
        lambda *args, **kwargs: stats,
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.find_awaiting_backfill_candidates",
        lambda *args, **kwargs: [{"id": "opportunity-1"}],
    )
    monkeypatch.setattr("job_search_hub.models.LMStudioClient", Client)
    monkeypatch.setattr("job_search_hub.models.ModelRouter", Router)

    exit_code = cmd_migrate_qualifications(
        SimpleNamespace(commit=True, dry_run=False, subcommand="migrate-qualifications")
    )

    assert exit_code == 0
    assert stats.awaiting_deferred == 1
    assert stats.errors == []


def test_duplicate_migration_relinks_and_deprecates_pending_wording(
    monkeypatch,
) -> None:
    approved = _answer(page_id="approved", value="Yes")
    approved.related_opportunity_ids = ["opportunity-1"]
    approved.related_application_ids = ["application-1"]
    approved.created_time = "2025-01-01T00:00:00Z"
    pending = _answer(page_id="pending", value="", status="Needs Answer")
    pending.question = "Do you have experience with Matillion?"
    pending.normalized_question = normalize_question(pending.question)
    pending.related_opportunity_ids = ["opportunity-1"]
    pending.related_application_ids = ["application-1"]
    pending.created_time = "2026-01-01T00:00:00Z"
    years = CachedAnswer(
        **{
            **pending.__dict__,
            "page_id": "years",
            "question": "How many years of experience do you have with Matillion?",
            "normalized_question": normalize_question(
                "How many years of experience do you have with Matillion?"
            ),
            "answer_type": "Number",
            "canonical_key": "years-of-experience|tool:matillion|number",
            "question_intent": "Years of Experience",
        }
    )
    requirement = _requirement(answer_id="pending")
    pages = {
        "opportunity-1": {
            "properties": {
                "Answer Bank Items": P.relation(["pending", "approved"]),
            },
        },
        "application-1": {
            "properties": {"Answer Bank Items": P.relation(["pending"])},
        },
    }
    updates = []

    class Notion:
        def get_page(self, page_id):
            return pages[page_id]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))
            pages.setdefault(page_id, {"properties": {}})
            pages[page_id]["properties"].update(properties)

    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_answer_bank",
        lambda *args, **kwargs: [pending, years, approved],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_requirements",
        lambda *args, **kwargs: [requirement],
    )

    stats = migrate_answer_bank(
        _settings(),
        notion=Notion(),
        store=_Store(),
        dry_run=False,
    )

    pending_update = next(
        props for page_id, props in reversed(updates) if page_id == "pending" and "Status" in props
    )
    requirement_update = next(props for page_id, props in updates if page_id == "requirement-1")
    assert stats.duplicate_groups == 1
    assert stats.duplicates_deprecated == 1
    assert P.read_select(pending_update["Status"]) == "Deprecated"
    assert P.read_relation_ids(pending_update["Superseded By"]) == ["approved"]
    assert P.read_relation_ids(pages["opportunity-1"]["properties"]["Answer Bank Items"]) == [
        "approved"
    ]
    assert P.read_relation_ids(requirement_update["Answer Bank Item"]) == ["approved"]
    canonical_by_id = {answer.page_id: answer for answer in stats.canonical_answers}
    assert canonical_by_id["pending"].status == "Deprecated"
    assert canonical_by_id["approved"].related_opportunity_ids == ["opportunity-1"]


def test_model_assisted_answer_bank_consolidation_merges_compatible_rows(
    monkeypatch,
) -> None:
    approved = _answer(page_id="approved", value="Yes")
    approved.question = "Do you have experience with Python?"
    approved.normalized_question = normalize_question(approved.question)
    approved.canonical_key = "experience|tool:python|yes-no"
    approved.topics = ("Python",)
    alternate = _answer(page_id="alternate", value="", status="Needs Answer")
    alternate.question = "Have you built software using Python?"
    alternate.normalized_question = normalize_question(alternate.question)
    alternate.canonical_key = "experience|tool:software-using-python|yes-no"
    alternate.topics = ("Software Using Python",)
    alternate.related_opportunity_ids = ["opportunity-1"]
    pages = {
        "opportunity-1": {
            "properties": {"Answer Bank Items": P.relation(["alternate"])},
        },
    }
    updates = []

    class Notion:
        def get_page(self, page_id):
            return pages[page_id]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))
            pages.setdefault(page_id, {"properties": {}})
            pages[page_id]["properties"].update(properties)

    class Client:
        def chat_json(self, **kwargs):
            return {"duplicate_groups": [[0, 1]]}

    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_answer_bank",
        lambda *args, **kwargs: [approved, alternate],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_requirements",
        lambda *args, **kwargs: [],
    )

    stats = migrate_answer_bank(
        _settings(),
        notion=Notion(),
        store=_Store(),
        router=SimpleNamespace(get=lambda role: "model"),
        client=Client(),
        dry_run=False,
    )

    assert stats.model_duplicate_groups == 1
    assert stats.duplicate_groups == 1
    assert stats.duplicates_deprecated == 1
    alternate_update = next(
        props
        for page_id, props in reversed(updates)
        if page_id == "alternate" and "Status" in props
    )
    assert P.read_select(alternate_update["Status"]) == "Deprecated"
    assert P.read_relation_ids(pages["opportunity-1"]["properties"]["Answer Bank Items"]) == [
        "approved"
    ]


def test_resume_answer_filter_includes_only_fully_met_requirements() -> None:
    meets = _requirement(answer_id="answer-meets")
    meets.resolution = "Meets"
    partial = _requirement(answer_id="answer-partial")
    partial.page_id = "requirement-2"
    partial.resolution = "Partial"
    failed = _requirement(answer_id="answer-failed")
    failed.page_id = "requirement-3"
    failed.resolution = "Does Not Meet"

    answer_ids = meeting_answer_ids_by_opportunity([meets, partial, failed])

    assert answer_ids == {"opportunity-1": {"answer-meets"}}


def test_duplicate_migration_refuses_conflicting_approved_answers(
    monkeypatch,
) -> None:
    yes = _answer(page_id="answer-yes", value="Yes")
    no = _answer(page_id="answer-no", value="No")
    updates = []

    class Notion:
        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_answer_bank",
        lambda *args, **kwargs: [yes, no],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualification_migration.load_requirements",
        lambda *args, **kwargs: [],
    )

    stats = migrate_answer_bank(
        _settings(),
        notion=Notion(),
        store=_Store(),
        dry_run=False,
    )

    review_updates = [
        properties
        for _page_id, properties in updates
        if P.read_select(properties.get("Status")) == "Needs Review"
    ]
    assert stats.conflicts == 1
    assert stats.duplicates_deprecated == 0
    assert len(review_updates) == 2
