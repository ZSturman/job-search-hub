from __future__ import annotations

from types import SimpleNamespace

from job_search_hub.notion import properties as P
from job_search_hub.pipeline.answers import CachedAnswer, find_question, normalize_question
from job_search_hub.pipeline.profile import Fact
from job_search_hub.pipeline.qualification_keys import canonical_key
from job_search_hub.pipeline.qualification_requirements import PersistedQualification
from job_search_hub.pipeline.qualifications import (
    QualificationGate,
    QualificationRequirement,
    QualificationResolution,
    classify_answer,
    evaluate_qualification_gate,
    extract_requirements,
)
from job_search_hub.pipeline.score import Criterion, ScoringResult
from job_search_hub.pipeline.score_stage import _score_opportunity, run_score


def _answer(**overrides) -> CachedAnswer:
    values = {
        "page_id": "answer-1",
        "question": "Have you worked with IGEMS?",
        "normalized_question": normalize_question("Have you worked with IGEMS?"),
        "question_family": "Skills",
        "answer_type": "Yes/No",
        "answer": "",
        "status": "Needs Answer",
        "approved_for_reuse": False,
        "sensitive": False,
        "context_specific": False,
        "reuse_threshold": 0.92,
    }
    values.update(overrides)
    return CachedAnswer(**values)


def _requirement(**overrides) -> QualificationRequirement:
    values = {
        "text": "10 years leading a development team",
        "kind": "years_experience",
        "importance": "hard_required",
        "question": "How many years have you led a development team?",
        "question_family": "Years of Experience",
        "answer_type": "Number",
        "minimum_value": 10,
        "keywords": ("development team leadership",),
    }
    values.update(overrides)
    return QualificationRequirement(**values)


class _ExtractionClient:
    def __init__(self, requirements):
        self.requirements = requirements

    def chat_json(self, **kwargs):
        return {"requirements": self.requirements}


def _extract(requirements, *, role_title="Data Engineer", description=""):
    return extract_requirements(
        role_title=role_title,
        description=description,
        location="United States",
        router=SimpleNamespace(get=lambda _role: "model"),
        client=_ExtractionClient(requirements),
    )


def test_extraction_recovers_explicit_years_threshold() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "Candidate must have 7+ years of Matillion experience",
                "kind": "tool",
                "importance": "hard_required",
                "topics": ["Matillion"],
                "question_intent": "Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": None,
            }
        ],
        description="Candidate must have 7+ years of Matillion experience.",
    )

    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.kind == "years_experience"
    assert requirement.question_intent == "Years of Experience"
    assert requirement.answer_type == "Number"
    assert requirement.expected_operator == "Minimum"
    assert requirement.minimum_value == 7
    assert requirement.importance == "hard_required"


def test_extraction_normalizes_year_range_to_lower_minimum() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "5 to 7 years of experience in Information Technology",
                "kind": "years_experience",
                "importance": "hard_required",
                "topics": ["Information Technology"],
                "question_intent": "Years of Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": 7,
            }
        ],
        description="5 to 7 years of experience in Information Technology.",
    )

    requirement = requirements[0]
    assert requirement.kind == "years_experience"
    assert requirement.question_intent == "Years of Experience"
    assert requirement.answer_type == "Number"
    assert requirement.expected_operator == "Minimum"
    assert requirement.minimum_value == 5


def test_extraction_converts_unbounded_numeric_tool_to_yes_no() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "Matillion",
                "kind": "tool",
                "importance": "hard_required",
                "topics": ["Matillion"],
                "question_intent": "Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": None,
            }
        ],
        description="Experience with Matillion is preferred.",
    )

    requirement = requirements[0]
    assert requirement.answer_type == "Yes/No"
    assert requirement.question_intent == "Experience"
    assert requirement.expected_operator == "Is Yes"
    assert requirement.minimum_value is None
    assert requirement.importance == "required"
    assert requirement.question == "Do you have experience with Matillion?"


def test_extraction_never_renders_numeric_location_question() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "United States",
                "kind": "location",
                "importance": "hard_required",
                "topics": ["United States"],
                "question_intent": "Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": None,
            }
        ],
        description="The candidate must be located in the United States.",
    )

    requirement = requirements[0]
    assert requirement.answer_type == "Yes/No"
    assert requirement.question_intent == "Location"
    assert requirement.expected_operator == "Is Yes"
    assert requirement.question == "Are you located in or able to work from United States?"
    assert "How many years" not in requirement.question


def test_extraction_drops_role_title_only_requirement() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "Lead Data Engineer (GCP & Matillion)",
                "kind": "skill",
                "importance": "hard_required",
                "topics": ["Data Engineering"],
                "question_intent": "Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": None,
            }
        ],
        role_title="Lead Data Engineer (GCP & Matillion)",
        description="Build data pipelines on Google Cloud Platform.",
    )

    assert requirements == []


def test_non_year_experience_requirement_becomes_general_experience() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": ("Extensive web application penetration testing experience"),
                "kind": "years_experience",
                "importance": "required",
                "topics": ["Web Application Penetration Testing"],
                "question_intent": "Years of Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": None,
            }
        ],
        description="Extensive web application penetration testing experience is desired.",
    )

    requirement = requirements[0]
    assert requirement.kind == "other"
    assert requirement.question_intent == "Experience"
    assert requirement.answer_type == "Yes/No"
    assert requirement.expected_operator == "Is Yes"
    assert requirement.minimum_value is None


def test_percentages_do_not_create_numeric_years_requirement() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "UI 60% Java 40%",
                "kind": "skill",
                "importance": "required",
                "topics": ["UI", "Java"],
                "question_intent": "Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": None,
            }
        ],
        description="UI 60% Java 40%",
    )

    requirement = requirements[0]
    assert requirement.question_intent == "Experience"
    assert requirement.answer_type == "Yes/No"
    assert requirement.minimum_value is None


def test_extraction_source_text_overrides_conflicting_operator() -> None:
    requirements = _extract(
        [
            {
                "source_requirement": "At most 3 years of Matillion experience",
                "kind": "years_experience",
                "importance": "required",
                "topics": ["Matillion"],
                "question_intent": "Years of Experience",
                "answer_type": "Number",
                "expected_operator": "Minimum",
                "expected_answer": None,
                "numeric_threshold": 3,
            }
        ],
        description="At most 3 years of Matillion experience.",
    )

    assert requirements[0].expected_operator == "Maximum"
    assert requirements[0].minimum_value == 3


def test_pending_question_is_reused_by_semantic_match() -> None:
    answer = _answer(normalized_question="different wording")
    lookup = find_question(
        question="Do you have experience using IGEMS?",
        question_family="Skills",
        answer_type="Yes/No",
        opportunity_id="opportunity-2",
        candidates=[answer],
        question_embedding=[1.0, 0.0],
        candidate_embeddings={"answer-1": [0.99, 0.01]},
    )
    assert lookup.found
    assert lookup.answer is answer


def test_context_specific_pending_question_is_not_reused_across_jobs() -> None:
    answer = _answer(
        context_specific=True,
        related_opportunity_ids=["opportunity-1"],
    )
    lookup = find_question(
        question=answer.question,
        question_family="Skills",
        answer_type="Yes/No",
        opportunity_id="opportunity-2",
        candidates=[answer],
        question_embedding=None,
        candidate_embeddings=None,
    )
    assert not lookup.found


def test_canonical_question_prefers_approved_answered_duplicate() -> None:
    key = canonical_key(
        intent="Experience",
        kind="tool",
        topics=("Matillion",),
        answer_type="Yes/No",
    )
    pending = _answer(
        page_id="pending",
        question="Do you have experience with Matillion?",
        canonical_key=key,
        topics=("Matillion",),
        question_intent="Experience",
    )
    approved = _answer(
        page_id="approved",
        question="Have you worked with Matillion?",
        answer="Yes",
        status="Approved",
        approved_for_reuse=True,
        canonical_key=key,
        topics=("Matillion",),
        question_intent="Experience",
    )

    lookup = find_question(
        question=pending.question,
        question_family="Skills",
        answer_type="Yes/No",
        opportunity_id="opportunity-2",
        candidates=[pending, approved],
        question_embedding=None,
        candidate_embeddings=None,
        canonical_key=key,
        topics=("Matillion",),
        question_intent="Experience",
    )

    assert lookup.found
    assert lookup.answer is approved
    assert lookup.method == "canonical_key"


def test_canonical_key_keeps_experience_and_years_questions_separate() -> None:
    experience = canonical_key(
        intent="Experience",
        kind="tool",
        topics=("Matillion",),
        answer_type="Yes/No",
    )
    years = canonical_key(
        intent="Years of Experience",
        kind="tool",
        topics=("Matillion",),
        answer_type="Number",
    )
    assert experience != years


def test_canonical_qualification_matching_does_not_request_embeddings(
    monkeypatch,
) -> None:
    requirement = _requirement(
        text="Experience with Matillion",
        kind="tool",
        importance="required",
        question="Do you have experience with Matillion?",
        question_family="Skills",
        answer_type="Yes/No",
        minimum_value=None,
        keywords=("Matillion",),
        topics=("Matillion",),
        question_intent="Experience",
        expected_operator="Is Yes",
        expected_answer="yes",
        source_requirement="Experience with Matillion",
        canonical_key="experience|tool:matillion|yes-no",
    )
    answer = _answer(
        page_id="answer-matillion",
        question="Have you worked with Matillion?",
        normalized_question=normalize_question("Have you worked with Matillion?"),
        answer="Yes",
        status="Approved",
        approved_for_reuse=True,
        canonical_key=requirement.canonical_key,
        topics=("Matillion",),
        question_intent="Experience",
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.extract_requirements",
        lambda **kwargs: [requirement],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.refresh_answer_embeddings",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("canonical matching must not refresh embeddings")
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications._resolve_with_model",
        lambda *args, **kwargs: {
            0: ("unknown", "unused model fallback", []),
        },
    )

    class Client:
        def embed(self, **kwargs):
            raise AssertionError("canonical matching must not embed questions")

    gate = evaluate_qualification_gate(
        role_title="Lead Data Engineer",
        description="Experience with Matillion is required.",
        location="Remote",
        opportunity_id="opportunity-1",
        facts=[],
        answers=[answer],
        router=SimpleNamespace(),
        client=Client(),
        store=SimpleNamespace(),
    )

    assert gate.resolutions[0].status == "meets"
    assert gate.resolutions[0].answer is answer


def test_compound_skill_requirement_splits_and_resolves_from_resume_facts(
    monkeypatch,
) -> None:
    requirement = _requirement(
        text="Experience with React and Python",
        kind="tool",
        importance="required",
        question="Do you have experience with React and Python?",
        question_family="Skills",
        answer_type="Yes/No",
        minimum_value=None,
        keywords=("React", "Python"),
        topics=("React", "Python"),
        question_intent="Experience",
        expected_operator="Is Yes",
        expected_answer="yes",
        source_requirement="Experience with React and Python",
        canonical_key="experience|tool:python+react|yes-no",
    )
    facts = [
        Fact(
            page_id=f"fact-{value.lower()}",
            fact_id="",
            fact_type="Skill",
            parent_fact_id="",
            value=value,
            organization="",
            role_label="",
            relationship_type="Not Applicable",
            start_date=None,
            end_date=None,
            current=False,
            category="Frontend" if value == "React" else "Language",
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
        for value in ("React", "Python")
    ]
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.extract_requirements",
        lambda **kwargs: [requirement],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications._resolve_with_model",
        lambda *args, **kwargs: {},
    )

    gate = evaluate_qualification_gate(
        role_title="Software Engineer",
        description="Experience with React and Python is required.",
        location="Remote",
        opportunity_id="opportunity-1",
        facts=facts,
        answers=[],
        router=SimpleNamespace(get=lambda role: "model"),
        client=SimpleNamespace(),
        store=SimpleNamespace(),
    )

    assert [resolution.requirement.topics for resolution in gate.resolutions] == [
        ("React",),
        ("Python",),
    ]
    assert [resolution.status for resolution in gate.resolutions] == ["meets", "meets"]
    assert gate.unresolved == []


def test_country_relocation_answer_resolves_city_relocation_question(
    monkeypatch,
) -> None:
    requirement = _requirement(
        text="Must be willing to relocate to Toronto",
        kind="relocation",
        importance="hard_required",
        question="Are you willing to relocate to Toronto?",
        question_family="Relocation",
        answer_type="Yes/No",
        minimum_value=None,
        keywords=("Toronto",),
        topics=("Toronto",),
        question_intent="Relocation",
        expected_operator="Is Yes",
        expected_answer="yes",
        source_requirement="Must be willing to relocate to Toronto",
        canonical_key="relocation|relocation:toronto|yes-no",
    )
    answer = _answer(
        page_id="answer-canada",
        question="Are you willing to relocate to Canada?",
        normalized_question=normalize_question("Are you willing to relocate to Canada?"),
        question_family="Relocation",
        answer_type="Yes/No",
        answer="No",
        status="Approved",
        approved_for_reuse=True,
        topics=("Canada",),
        question_intent="Relocation",
        canonical_key="relocation|relocation:canada|yes-no",
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.extract_requirements",
        lambda **kwargs: [requirement],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications._resolve_with_model",
        lambda *args, **kwargs: {},
    )

    gate = evaluate_qualification_gate(
        role_title="Software Engineer",
        description="Must be willing to relocate to Toronto.",
        location="Toronto",
        opportunity_id="opportunity-1",
        facts=[],
        answers=[answer],
        router=SimpleNamespace(get=lambda role: "model"),
        client=SimpleNamespace(),
        store=SimpleNamespace(),
    )

    assert gate.resolutions[0].answer is answer
    assert gate.resolutions[0].status == "does_not_meet"
    assert gate.hard_failures == [gate.resolutions[0]]


def test_context_caps_are_applied_before_resolution_model_call(monkeypatch) -> None:
    requirements = [
        _requirement(
            text=f"Requirement {index} " + ("x" * 2000),
            kind="tool",
            minimum_value=None,
            answer_type="Yes/No",
            expected_operator="Is Yes",
            topics=(f"Tool {index}",),
            question_intent="Experience",
            canonical_key=f"experience|tool:tool-{index}|yes-no",
        )
        for index in range(20)
    ]
    facts = [
        Fact(
            page_id=f"fact-{index}",
            fact_id="",
            fact_type="Skill",
            parent_fact_id="",
            value="v" * 2000,
            organization="o" * 2000,
            role_label="r" * 2000,
            relationship_type="",
            start_date=None,
            end_date=None,
            current=False,
            category="",
            proficiency="p" * 2000,
            emphasis_tags=[],
            verified=True,
            approved_for_profile=True,
            approved_for_applications=True,
            metrics_verified=False,
            sensitive=False,
            sort_order=0,
            active=True,
        )
        for index in range(100)
    ]
    captured = {}

    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.extract_requirements",
        lambda **kwargs: requirements,
    )

    class Client:
        def chat_json(self, **kwargs):
            captured["content"] = kwargs["messages"][1]["content"]
            return {
                "resolutions": [
                    {
                        "requirement_index": index,
                        "status": "unknown",
                        "reason": "no verified evidence",
                        "fact_ids": [],
                    }
                    for index in range(16)
                ]
            }

    gate = evaluate_qualification_gate(
        role_title="Role",
        description="Description",
        location="Remote",
        opportunity_id="opportunity-1",
        facts=facts,
        answers=[],
        router=SimpleNamespace(get=lambda role: "model"),
        client=Client(),
        store=SimpleNamespace(),
    )

    content = captured["content"]
    assert "Requirement 16" not in content
    assert "x" * 700 not in content
    assert "fact-80" not in content
    assert "v" * 600 not in content
    assert len(gate.resolutions) == 20
    assert gate.resolutions[17].status == "unknown"


def test_context_length_resolution_error_does_not_fail_gate(monkeypatch) -> None:
    requirement = _requirement(
        text="Experience with Matillion",
        kind="tool",
        importance="required",
        minimum_value=None,
        answer_type="Yes/No",
        expected_operator="Is Yes",
        topics=("Matillion",),
        question_intent="Experience",
        canonical_key="experience|tool:matillion|yes-no",
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.extract_requirements",
        lambda **kwargs: [requirement],
    )

    class Client:
        def chat_json(self, **kwargs):
            raise RuntimeError("LM Studio context length exceeded token limit")

    gate = evaluate_qualification_gate(
        role_title="Role",
        description="Description",
        location="Remote",
        opportunity_id="opportunity-1",
        facts=[],
        answers=[],
        router=SimpleNamespace(get=lambda role: "model"),
        client=Client(),
        store=SimpleNamespace(),
    )

    assert gate.resolutions[0].status == "unknown"
    assert gate.hard_failures == []


def test_context_length_resolution_error_does_not_fail_scoring_run(monkeypatch) -> None:
    requirement = _requirement(
        text="Experience with Matillion",
        kind="tool",
        importance="required",
        minimum_value=None,
        answer_type="Yes/No",
        expected_operator="Is Yes",
        topics=("Matillion",),
        question_intent="Experience",
        canonical_key="experience|tool:matillion|yes-no",
    )
    page = {
        "id": "opportunity-1",
        "properties": {
            "Role Title": P.rich_text("Software Engineer"),
            "Company": P.rich_text("Example"),
            "Description": P.rich_text("Build production software with Matillion."),
            "Location": P.rich_text("Remote"),
            "URL": P.url("https://example.com/jobs/1"),
        },
    }
    fit = Criterion(
        page_id="fit",
        name="Fit",
        key="fit",
        active=True,
        category="Skills",
        weight=1,
        direction="Positive",
        evaluation_method="Rule",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="software",
        minimum_value=None,
        maximum_value=None,
        sort_order=1,
    )
    qualification_fit = Criterion(
        page_id="qualification-fit",
        name="Qualification Fit",
        key="qualification_fit",
        active=True,
        category="Skills",
        weight=1,
        direction="Positive",
        evaluation_method="Rule",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="persisted",
        minimum_value=None,
        maximum_value=None,
        sort_order=2,
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [fit, qualification_fit],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_facts",
        lambda notion, settings: [],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.qualifications.extract_requirements",
        lambda **kwargs: [requirement],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.upsert_gate_requirements",
        lambda *args, **kwargs: [],
    )

    class Client:
        def chat_json(self, **kwargs):
            raise RuntimeError("LM Studio context length exceeded token limit")

    class Store:
        def audit(self, **kwargs):
            return None

        def record_score_component(self, **kwargs):
            return None

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return []

    settings = SimpleNamespace(
        score_workers=1,
        notion=SimpleNamespace(
            data_sources={
                "opportunities": "opportunities",
                "answer_bank": "answers",
                "resume_facts": "facts",
                "qualification_requirements": "requirements",
            }
        ),
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )

    stats = run_score(
        settings,
        notion=Notion(),
        store=Store(),
        router=SimpleNamespace(get=lambda role: "model"),
        client=Client(),
        pages_override=[page],
        dry_run=True,
    )

    assert stats.scored == 1
    assert stats.errors == []


def test_numeric_answer_below_hard_minimum_is_explicit_failure() -> None:
    answer = _answer(
        answer_type="Number",
        answer="4",
        status="Approved",
        approved_for_reuse=True,
    )
    status, reason = classify_answer(_requirement(), answer)
    assert status == "does_not_meet"
    assert "below minimum 10" in reason


def test_required_and_preferred_failures_change_score() -> None:
    criterion = Criterion(
        page_id="fit",
        name="Fit",
        key="fit",
        active=True,
        category="Skills",
        weight=1,
        direction="Positive",
        evaluation_method="LLM",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="fit",
        minimum_value=None,
        maximum_value=None,
        sort_order=1,
    )

    class Client:
        last_call_id = 1

        def chat_json(self, **kwargs):
            return {
                "evaluations": [
                    {
                        "criterion_index": 0,
                        "score": 0.9,
                        "reason": "fit",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    }
                ],
            }

    class Router:
        def get(self, role):
            return "model"

    settings = SimpleNamespace(
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        )
    )
    page = {
        "id": "opportunity-1",
        "properties": {
            "Role Title": P.rich_text("Software Engineer"),
            "Company": P.rich_text("Example"),
            "Description": P.rich_text("Build production software."),
            "URL": P.url("https://example.com/jobs/1"),
        },
    }
    required = QualificationResolution(
        requirement=_requirement(
            text="IGEMS experience",
            kind="tool",
            importance="required",
            minimum_value=None,
            answer_type="Yes/No",
        ),
        status="does_not_meet",
        reason="No",
    )
    preferred = QualificationResolution(
        requirement=_requirement(
            text="Local to Dayton",
            kind="location",
            importance="preferred",
            minimum_value=None,
            answer_type="Yes/No",
        ),
        status="does_not_meet",
        reason="Not local",
    )
    gate = QualificationGate(
        requirements=[required.requirement, preferred.requirement],
        resolutions=[required, preferred],
        unresolved=[],
        hard_failures=[],
        answer_ids=[],
    )
    result = _score_opportunity(
        page,
        [criterion],
        settings=settings,
        router=Router(),
        client=Client(),
        qualification_gate=gate,
    )
    assert result.total_score <= 0.24
    assert result.lane == "Skip"
    assert "required qualification" in result.concerns


def test_awaiting_row_does_not_stop_later_scoring(monkeypatch) -> None:
    pages = [
        {"id": "waiting", "properties": {"Status": P.select("Fetched")}},
        {"id": "scored", "properties": {"Status": P.select("Fetched")}},
    ]
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            if data_source_id == "answers":
                return []
            if data_source_id == "facts":
                return []
            return pages

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

        def create_page(self, **kwargs):
            return {"id": "new-answer"}

    class Store:
        def audit(self, **kwargs):
            return None

        def record_score_component(self, **kwargs):
            return None

        def load_embeddings(self, **kwargs):
            return []

        def store_embedding(self, **kwargs):
            return None

    requirement = _requirement(
        text="IGEMS",
        kind="tool",
        minimum_value=None,
        answer_type="Yes/No",
    )
    unresolved = QualificationResolution(
        requirement=requirement,
        status="unknown",
        reason="unknown",
    )
    calls = {"count": 0}

    def fake_gate(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return QualificationGate(
                [requirement],
                [unresolved],
                [unresolved],
                [],
                [],
            )
        return QualificationGate([], [], [], [], [])

    fit = Criterion(
        page_id="fit",
        name="Fit",
        key="fit",
        active=True,
        category="Skills",
        weight=1,
        direction="Positive",
        evaluation_method="Rule",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="fit",
        minimum_value=None,
        maximum_value=None,
        sort_order=1,
    )
    qualification_fit = Criterion(
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
        instructions="persisted",
        minimum_value=None,
        maximum_value=None,
        sort_order=2,
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [fit, qualification_fit],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_facts",
        lambda notion, settings: [],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_answer_bank",
        lambda notion, settings: [],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.score_preflight",
        lambda *args, **kwargs: SimpleNamespace(accepted=True),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.evaluate_qualification_gate",
        fake_gate,
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._score_opportunity",
        lambda page, *args, **kwargs: ScoringResult(
            page_id=page["id"],
            components=[],
            hard_excluded=False,
            lane="High Attention",
            total_score=0.9,
            confidence=0.9,
            exclusion_reason="",
        ),
    )
    persisted = PersistedQualification(
        page_id="requirement-1",
        opportunity_id="waiting",
        answer_id="new-answer",
        qualification="IGEMS",
        canonical_key="experience|tool:igems|yes-no",
        topics=("IGEMS",),
        question_intent="Experience",
        requirement_kind="Tool",
        importance="Hard Required",
        answer_type="Yes/No",
        expected_operator="Is Yes",
        expected_answer="",
        numeric_threshold=None,
        meets_score=1,
        partial_score=0.5,
        does_not_meet_score=0,
        score_weight=3,
        failure_action="Reject",
        resolution="Awaiting Answer",
        resolution_reason="unknown",
        source_requirement="IGEMS",
        fingerprint="fingerprint",
        active=True,
    )
    persist_calls = {"count": 0}

    def fake_upsert(*args, **kwargs):
        persist_calls["count"] += 1
        return [persisted] if persist_calls["count"] == 1 else []

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.upsert_gate_requirements",
        fake_upsert,
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(
            data_sources={
                "opportunities": "opportunities",
                "answer_bank": "answers",
                "resume_facts": "facts",
                "qualification_requirements": "requirements",
            }
        ),
    )
    stats = run_score(
        settings,
        notion=Notion(),
        store=Store(),
        router=SimpleNamespace(),
        client=SimpleNamespace(),
        dry_run=False,
    )
    assert stats.awaiting_answer == 1
    assert stats.scored == 1
    assert any(
        page_id == "scored" and properties.get("Model Score", {}).get("number") is not None
        for page_id, properties in updates
    )


def test_awaiting_retry_leaves_unresolved_row_untouched(monkeypatch) -> None:
    pending = _answer(
        page_id="answer-pending",
        related_opportunity_ids=["opportunity-awaiting"],
    )
    page = {
        "id": "opportunity-awaiting",
        "properties": {
            "Status": P.select("Awaiting Answer"),
            "Answer Bank Items": P.relation(["answer-pending"]),
            "Base Model Score": P.number(0.8),
            "Base Score Numerator": P.number(8),
            "Base Score Weight": P.number(10),
        },
    }
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    class Store:
        def audit(self, **kwargs):
            return None

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [
            Criterion(
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
                instructions="persisted",
                minimum_value=None,
                maximum_value=None,
                sort_order=1,
            )
        ],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_answer_bank",
        lambda notion, settings: [pending],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.evaluate_qualification_gate",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unresolved retry must not re-extract")
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._score_opportunity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("awaiting retry must not score descriptions")
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.create_needs_answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("awaiting retry must not create questions")
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_requirements",
        lambda *args, **kwargs: [
            PersistedQualification(
                page_id="requirement-pending",
                opportunity_id="opportunity-awaiting",
                answer_id="answer-pending",
                qualification="Matillion experience",
                canonical_key="experience|tool:matillion|yes-no",
                topics=("Matillion",),
                question_intent="Experience",
                requirement_kind="Tool",
                importance="Required",
                answer_type="Yes/No",
                expected_operator="Is Yes",
                expected_answer="yes",
                numeric_threshold=None,
                meets_score=1,
                partial_score=0.5,
                does_not_meet_score=0,
                score_weight=2,
                failure_action="Score Only",
                resolution="Awaiting Answer",
                resolution_reason="",
                source_requirement="Experience with Matillion",
                fingerprint="pending-fingerprint",
                active=True,
            )
        ],
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(
            data_sources={
                "opportunities": "opportunities",
                "answer_bank": "answers",
                "resume_facts": "facts",
                "qualification_requirements": "requirements",
            }
        ),
    )

    stats = run_score(
        settings,
        notion=Notion(),
        store=Store(),
        router=SimpleNamespace(),
        client=SimpleNamespace(),
        awaiting_only=True,
        dry_run=False,
    )

    assert stats.awaiting_answer == 1
    assert stats.questions_created == 0
    assert stats.questions_reused == 0
    assert updates == []
