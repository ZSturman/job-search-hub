import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from job_search_hub.models import StructuredOutputError
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.evaluators import (
    FastFitAssessment,
    evaluate_criterion,
    evaluate_fast_fit_batch,
    evaluate_llm_batch,
)
from job_search_hub.pipeline.posting_dates import (
    DATE_JOB_WAS_POSTED,
    POSTING_RECENCY_KEY,
    posting_recency_score,
)
from job_search_hub.pipeline.qualification_requirements import PersistedQualification
from job_search_hub.pipeline.qualifications import QualificationGate
from job_search_hub.pipeline.score import ComponentResult, Criterion, ScoringResult, assign_lane
from job_search_hub.pipeline.score_stage import (
    _fast_fit_assessments,
    _iter_computed_opportunities,
    _score_opportunity,
    run_score,
)


class _Router:
    def get(self, role):
        return "m"


class _BadClient:
    last_call_id = 42

    def chat_json(self, **kwargs):
        raise RuntimeError("not json")


class _CapturingClient:
    last_call_id = 43

    def chat_json(self, **kwargs):
        self.kwargs = kwargs
        return {"score": 0.75, "reason": "good fit"}


class _BatchClient:
    last_call_id = 44

    def chat_json(self, **kwargs):
        self.kwargs = kwargs
        return {
            "evaluations": [
                {
                    "criterion_index": 0,
                    "score": 0.8,
                    "reason": "strong fit",
                    "lane_override": None,
                    "hard_exclude": False,
                    "needs_human_review": False,
                },
                {
                    "criterion_index": 1,
                    "score": 0.2,
                    "reason": "weak mission match",
                    "lane_override": None,
                    "hard_exclude": False,
                    "needs_human_review": False,
                },
            ]
        }


class _FastFitClient:
    last_call_id = 45

    def chat_json(self, **kwargs):
        self.kwargs = kwargs
        return {
            "evaluations": [
                {
                    "posting_index": 0,
                    "transferable_skill_fit": 0.8,
                    "required_skill_overlap": 0.1,
                    "interview_plausibility": 0.2,
                    "missing_mandatory": True,
                    "reason": "Missing a mandatory specialty.",
                },
            ],
        }


def _criterion(**overrides):
    data = dict(
        page_id="c1",
        name="Fit",
        key="fit",
        active=True,
        category="Skills",
        weight=1.0,
        direction="Positive",
        evaluation_method="LLM",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="Score fit.",
        minimum_value=None,
        maximum_value=None,
        sort_order=1,
    )
    data.update(overrides)
    return Criterion(**data)


def test_llm_failure_returns_degraded_neutral_component():
    result = evaluate_criterion(
        _criterion(),
        posting_text="Role text",
        posting_comp=None,
        router=_Router(),
        client=_BadClient(),
    )
    assert result.score == 0.0
    assert result.fallback_used
    assert result.error == "not json"
    assert result.llm_call_id == 42


def test_llm_scoring_limits_structured_response():
    client = _CapturingClient()
    result = evaluate_criterion(
        _criterion(),
        posting_text="Role text",
        posting_comp=None,
        router=_Router(),
        client=client,
    )
    assert result.score == 0.75
    assert client.kwargs["max_tokens"] == 384


def test_posting_recency_curve_penalizes_old_rows():
    now = datetime.now(UTC)
    fresh = posting_recency_score(now - timedelta(days=3), now=now)
    old = posting_recency_score(now - timedelta(days=116), now=now)

    assert fresh.score > 0.95
    assert old.score < 0.12
    assert old.days_old == 116


def test_score_opportunity_records_recency_component_and_caps_old_rows():
    now = datetime.now(UTC)
    settings = SimpleNamespace(
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )
    criteria = [
        _criterion(
            name="Core fit",
            key="core_role_family_fit",
            evaluation_method="Rule",
            weight=1.0,
        ),
        _criterion(
            name="Posting recency",
            key=POSTING_RECENCY_KEY,
            evaluation_method="Rule",
            weight=3.0,
            sort_order=2,
        ),
    ]

    def page(posted_at):
        return {
            "id": f"page-{posted_at.date().isoformat()}",
            "properties": {
                "Company": {"rich_text": [{"plain_text": "Acme"}]},
                "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
                "Description": {"rich_text": [{"plain_text": "Build reliable software systems."}]},
                "URL": {"url": "https://example.com/jobs/123"},
                "Source": {"select": {"name": "Test"}},
                "Source Adapter": {"select": {"name": "test"}},
                DATE_JOB_WAS_POSTED: {"date": {"start": posted_at.isoformat()}},
            },
        }

    fresh = _score_opportunity(
        page(now - timedelta(days=3)),
        criteria,
        settings=settings,
        router=None,
        client=None,
    )
    old = _score_opportunity(
        page(now - timedelta(days=116)),
        criteria,
        settings=settings,
        router=None,
        client=None,
    )

    assert any(c.criterion_key == POSTING_RECENCY_KEY for c in old.components)
    assert fresh.total_score > old.total_score
    assert old.total_score <= 0.45


def test_llm_batch_scores_multiple_criteria_in_one_call():
    client = _BatchClient()
    criteria = [
        _criterion(),
        _criterion(page_id="c2", name="Mission", key="mission", sort_order=2),
    ]
    results = evaluate_llm_batch(
        criteria,
        "Role text",
        router=_Router(),
        client=client,
    )

    assert [result.score for result in results] == [0.8, 0.2]
    assert all(result.llm_call_id == 44 for result in results)
    assert client.kwargs["task"] == "criteria_batch:2"
    assert client.kwargs["max_tokens"] == 384


def test_llm_batch_duplicate_indexes_fall_back_safely():
    class Client:
        last_call_id = 50

        def chat_json(self, **kwargs):
            return {
                "evaluations": [
                    {
                        "criterion_index": 0,
                        "score": 0.9,
                        "reason": "first",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    },
                    {
                        "criterion_index": 0,
                        "score": 0.1,
                        "reason": "duplicate",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    },
                ],
            }

    results = evaluate_llm_batch(
        [_criterion(), _criterion(page_id="c2", name="Risk", key="risk", direction="Negative")],
        "Role text",
        router=_Router(),
        client=Client(),
    )

    assert [result.score for result in results] == [0.0, 1.0]
    assert all(result.fallback_used for result in results)
    assert all("duplicate criterion_index" in result.error for result in results)


def test_llm_batch_out_of_range_indexes_fall_back_safely():
    class Client:
        last_call_id = 51

        def chat_json(self, **kwargs):
            return {
                "evaluations": [
                    {
                        "criterion_index": 0,
                        "score": 0.9,
                        "reason": "first",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    },
                    {
                        "criterion_index": 2,
                        "score": 0.1,
                        "reason": "out",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    },
                ],
            }

    results = evaluate_llm_batch(
        [_criterion(), _criterion(page_id="c2")],
        "Role text",
        router=_Router(),
        client=Client(),
    )

    assert [result.score for result in results] == [0.0, 0.0]
    assert all(result.fallback_used for result in results)
    assert all("out-of-range criterion_index" in result.error for result in results)


def test_llm_batch_missing_evaluations_fall_back_safely():
    class Client:
        last_call_id = 52

        def chat_json(self, **kwargs):
            return {
                "evaluations": [
                    {
                        "criterion_index": 0,
                        "score": 0.9,
                        "reason": "first",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    },
                ],
            }

    results = evaluate_llm_batch(
        [_criterion(), _criterion(page_id="c2")],
        "Role text",
        router=_Router(),
        client=Client(),
    )

    assert [result.score for result in results] == [0.0, 0.0]
    assert all(result.fallback_used for result in results)
    assert all("missing criterion_index" in result.error for result in results)


def test_llm_batch_can_use_fast_triage_model():
    client = _BatchClient()
    evaluate_llm_batch(
        [_criterion(), _criterion(page_id="c2")],
        "Role text",
        router=_Router(),
        client=client,
        model_role="triage",
    )

    assert client.kwargs["role"] == "triage"


def test_fast_fit_batch_compares_posting_to_verified_facts():
    client = _FastFitClient()
    results = evaluate_fast_fit_batch(
        [
            {
                "role_title": "Platform Engineer",
                "company": "Example",
                "location": "Remote US",
                "url": "https://example.com/jobs/1",
                "description": "Requires Kubernetes.",
            }
        ],
        profile_facts="Skills: Python, React",
        router=_Router(),
        client=client,
    )

    assert results[0].missing_mandatory
    assert results[0].required_skill_overlap == 0.1
    assert "Verified candidate facts" in client.kwargs["messages"][1]["content"]


def test_fast_fit_assessments_split_malformed_batches(tmp_path):
    class SplitClient:
        last_call_id = 46

        def __init__(self):
            self.batch_sizes = []

        def chat_json(self, **kwargs):
            content = kwargs["messages"][1]["content"]
            size = content.count('"posting_index"')
            self.batch_sizes.append(size)
            if size > 1:
                raise StructuredOutputError("LM Studio returned malformed JSON")
            return {
                "evaluations": [
                    {
                        "posting_index": 0,
                        "transferable_skill_fit": 0.7,
                        "required_skill_overlap": 0.6,
                        "interview_plausibility": 0.5,
                        "missing_mandatory": False,
                        "reason": "Reasonable adjacent fit.",
                    }
                ],
            }

    def page(page_id: str, title: str) -> dict:
        return {
            "id": page_id,
            "properties": {
                "Company": P.rich_text("Acme"),
                "Role Title": P.rich_text(title),
                "Location": P.rich_text("Remote US"),
                "URL": P.url(f"https://example.com/jobs/{page_id}"),
                "Description": P.rich_text(
                    "Build reliable Python and React software for production teams."
                ),
            },
        }

    settings = SimpleNamespace(
        data_dir=tmp_path,
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )
    client = SplitClient()

    assessments = _fast_fit_assessments(
        [page("opportunity-1", "Software Engineer"), page("opportunity-2", "Platform Engineer")],
        settings=settings,
        profile_summary="Skills: Python, React",
        router=_Router(),
        client=client,
    )

    assert client.batch_sizes == [2, 1, 1]
    assert set(assessments) == {"opportunity-1", "opportunity-2"}
    assert all(not assessment.missing_mandatory for assessment in assessments.values())


def test_bounded_scoring_is_parallel_ordered_and_refreshes_answer_snapshots():
    active = 0
    max_active = 0
    lock = threading.Lock()
    answers = []

    def compute(page, answer_snapshot):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02 if page["id"] == "first" else 0.005)
        with lock:
            active -= 1
        return len(answer_snapshot)

    items = _iter_computed_opportunities(
        [{"id": "first"}, {"id": "second"}, {"id": "third"}],
        workers=2,
        before_submit=lambda _index, _page: None,
        compute=compute,
        answers=answers,
    )
    results = []
    for index, page, future in items:
        results.append((index, page["id"], future.result()))
        if page["id"] == "first":
            answers.append(SimpleNamespace(page_id="new-answer"))

    assert max_active == 2
    assert results == [
        (1, "first", 0),
        (2, "second", 0),
        (3, "third", 1),
    ]


def test_fresh_complete_scores_are_skipped_unless_rescore(monkeypatch):
    page = {
        "id": "opportunity-1",
        "properties": {
            "Status": {"select": {"name": "Scoring"}},
            "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
            "Description": {"rich_text": [{"plain_text": "Build reliable software."}]},
            "URL": {"url": "https://example.com/jobs/123"},
            "Model Score": {"number": 0.8},
        },
    }
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    class Store:
        def latest_score_components(self, opportunity_page_id):
            return [
                {"criterion_key": "fit", "dry_run": 0},
                {"criterion_key": POSTING_RECENCY_KEY, "dry_run": 0},
            ]

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._compute_score_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fresh row should not be rescored")
        ),
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
        score_workers=1,
    )

    stats = run_score(
        settings,
        notion=Notion(),
        store=Store(),
        router=_Router(),
        client=_BadClient(),
        pages_override=[page],
        dry_run=False,
    )

    assert stats.scored == 0
    assert stats.errors == []
    assert updates == []


def test_transient_structured_output_failure_remains_retryable(monkeypatch):
    updates = []
    page = {
        "id": "opportunity-1",
        "properties": {
            "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
            "Description": {"rich_text": [{"plain_text": "Build reliable software."}]},
            "URL": {"url": "https://example.com/jobs/123"},
        },
    }

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._compute_score_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            StructuredOutputError("malformed JSON after retry")
        ),
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
        score_workers=1,
    )

    stats = run_score(
        settings,
        notion=Notion(),
        store=SimpleNamespace(),
        router=_Router(),
        client=_BadClient(),
        dry_run=False,
    )

    assert stats.errors == ["malformed JSON after retry"]
    assert updates[-1][1]["Processing State"]["select"]["name"] == "Pending"


def test_score_payload_links_requirements_without_deprecated_topic_columns(monkeypatch):
    updates = []
    page = {
        "id": "opportunity-1",
        "properties": {
            "Status": P.select("Fetched"),
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("Software Engineer"),
            "Description": P.rich_text("Build reliable Python and React software."),
            "URL": P.url("https://example.com/jobs/123"),
        },
    }

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page] if data_source_id == "opportunities" else []

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    class Store:
        def record_score_component(self, **kwargs):
            return None

        def audit(self, **kwargs):
            return None

    base_result = ScoringResult(
        page_id="opportunity-1",
        total_score=0.8,
        confidence=0.9,
        lane="High Attention",
        hard_excluded=False,
        exclusion_reason="",
        components=[
            ComponentResult(
                criterion_key="fit",
                criterion_name="Fit",
                category="Skills",
                evaluation_method="Rule",
                score=0.8,
                weight=1.0,
                direction="Positive",
                reason="good fit",
            )
        ],
        top_matches="Python",
        concerns="",
    )
    persisted_requirement = PersistedQualification(
        page_id="requirement-1",
        opportunity_id="opportunity-1",
        answer_id="answer-1",
        qualification="Build with Python and React",
        canonical_key="experience|tools:python-react|yes-no",
        topics=("Python", "React"),
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
        resolution="Meets",
        resolution_reason="approved evidence",
        source_requirement="Build with Python and React",
        fingerprint="fingerprint-1",
        active=True,
    )

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [
            _criterion(),
            _criterion(
                page_id="qualification-fit",
                name="Qualification Fit",
                key="qualification_fit",
                evaluation_method="Rule",
                weight=2.0,
                sort_order=2,
            ),
        ],
    )
    monkeypatch.setattr("job_search_hub.pipeline.score_stage.load_answer_bank", lambda *a, **k: [])
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._compute_score_candidate",
        lambda *a, **k: SimpleNamespace(
            gate=QualificationGate([], [], [], [], []),
            persist_gate=True,
            base_result=base_result,
        ),
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._persist_qualification_gate",
        lambda *a, **k: ["answer-1"],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.upsert_gate_requirements",
        lambda *a, **k: [persisted_requirement],
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(
            data_sources={
                "opportunities": "opportunities",
                "answer_bank": "answers",
                "qualification_requirements": "requirements",
            }
        ),
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
        score_workers=1,
    )

    stats = run_score(
        settings,
        notion=Notion(),
        store=Store(),
        router=None,
        client=None,
        dry_run=False,
        skip_posting_preflight=True,
    )

    final_properties = next(
        properties
        for page_id, properties in updates
        if page_id == "opportunity-1" and "Qualification Requirements" in properties
    )
    assert stats.scored == 1
    assert P.read_relation_ids(final_properties["Qualification Requirements"]) == ["requirement-1"]
    assert "Answer Bank Items" in final_properties
    assert "Qualification Topics" not in final_properties
    assert "Hard Qualification Topics" not in final_properties
    assert "Preferred Qualification Topics" not in final_properties


def test_score_query_requeues_only_structured_output_errors(monkeypatch):
    captured = {}

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
        score_workers=1,
    )

    run_score(
        settings,
        notion=Notion(),
        store=SimpleNamespace(),
        router=_Router(),
        client=_BadClient(),
    )

    filters = captured["filter_"]["or"]
    assert {"property": "Status", "select": {"equals": "Fetched"}} in filters
    assert {"property": "Status", "select": {"equals": "Scoring"}} in filters
    serialized = str(filters)
    assert "LM Studio returned non-JSON content" in serialized
    assert "LM Studio returned malformed JSON" in serialized


def test_total_llm_failure_preserves_scores_and_leaves_pending_review(monkeypatch):
    writes = []

    class _Notion:
        def update_page(self, page_id, properties):
            return None

        def query_data_source(self, data_source_id, **kwargs):
            return [
                {
                    "id": "opportunity-1",
                    "properties": {
                        "Role Title": {
                            "rich_text": [{"plain_text": "Software Engineer"}],
                        },
                        "Description": {
                            "rich_text": [
                                {
                                    "plain_text": (
                                        "Build reliable Python and React software, write tests, "
                                        "review code, and maintain production services."
                                    ),
                                }
                            ],
                        },
                        "URL": {"url": "https://example.com/jobs/123"},
                    },
                }
            ]

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.write_scores",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    class Store:
        def record_score_component(self, **kwargs):
            return None

    settings = SimpleNamespace(
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )
    stats = run_score(
        settings,
        notion=_Notion(),
        store=Store(),
        router=_Router(),
        client=_BadClient(),
        dry_run=False,
    )

    assert stats.scored == 0
    assert stats.errors
    assert writes == []


def test_one_failed_opportunity_does_not_stop_scoring_batch(monkeypatch):
    pages = [
        {
            "id": "failed",
            "properties": {
                "Role Title": {"rich_text": [{"plain_text": "Broken Role"}]},
                "Description": {"rich_text": [{"plain_text": "Broken."}]},
                "URL": {"url": "https://example.com/jobs/broken"},
            },
        },
        {
            "id": "scored",
            "properties": {
                "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
                "Description": {"rich_text": [{"plain_text": "Build software."}]},
                "URL": {"url": "https://example.com/jobs/scored"},
            },
        },
    ]
    writes = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return pages

        def update_page(self, page_id, properties):
            return None

    class Store:
        def record_score_component(self, **kwargs):
            return None

    calls = {"count": 0}

    def compute(page, **kwargs):
        calls["count"] += 1
        if page["id"] == "failed":
            raise RuntimeError("first row failed")
        return SimpleNamespace(
            gate=SimpleNamespace(),
            persist_gate=False,
            base_result=ScoringResult(
                page_id=page["id"],
                total_score=0.8,
                confidence=0.8,
                lane="High Attention",
                hard_excluded=False,
                exclusion_reason="",
                components=[
                    ComponentResult(
                        criterion_key="fit",
                        criterion_name="Fit",
                        category="Skills",
                        evaluation_method="LLM",
                        score=0.8,
                        weight=1,
                        direction="Positive",
                        reason="fit",
                    )
                ],
            ),
        )

    from job_search_hub.pipeline.score import ComponentResult, ScoringResult

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._compute_score_candidate",
        compute,
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.write_scores",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    settings = SimpleNamespace(
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
        score_workers=1,
    )

    stats = run_score(
        settings,
        notion=Notion(),
        store=Store(),
        router=_Router(),
        client=_BadClient(),
        dry_run=False,
    )

    assert calls["count"] == 2
    assert stats.errors == ["first row failed"]
    assert stats.scored == 1
    assert writes[0][1]["result"].page_id == "scored"


def test_fast_fit_hard_mismatch_avoids_detailed_llm():
    class Client:
        def chat_json(self, **kwargs):
            raise AssertionError("detailed LLM should not run")

    page = {
        "id": "opportunity-1",
        "properties": {
            "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
            "Description": {
                "rich_text": [
                    {
                        "plain_text": (
                            "Build reliable software with Python and React for a production "
                            "engineering team. Write tests and review code."
                        )
                    }
                ]
            },
            "URL": {"url": "https://example.com/jobs/123"},
        },
    }
    settings = SimpleNamespace(
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )
    result = _score_opportunity(
        page,
        [_criterion(key="required_skill_overlap"), _criterion(key="mission")],
        settings=settings,
        profile_facts="Skills: Python, React",
        router=_Router(),
        client=Client(),
        fast_fit=FastFitAssessment(
            transferable_skill_fit=0.2,
            required_skill_overlap=0.1,
            interview_plausibility=0.2,
            missing_mandatory=True,
            reason="Missing mandatory mobile specialty.",
        ),
    )

    assert result.total_score <= 0.24
    assert result.lane == "Skip"
    assert all(
        component.evaluation_method.startswith("Fast Fit") for component in result.components
    )


def test_neutral_positive_criteria_produce_neutral_total():
    class Client:
        last_call_id = 1

        def chat_json(self, **kwargs):
            return {
                "evaluations": [
                    {
                        "criterion_index": 0,
                        "score": 0.5,
                        "reason": "unknown",
                        "lane_override": None,
                        "hard_exclude": False,
                        "needs_human_review": False,
                    }
                ],
            }

    page = {
        "id": "opportunity-1",
        "properties": {
            "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
            "Description": {
                "rich_text": [
                    {
                        "plain_text": (
                            "Build reliable software with Python and React for a production "
                            "engineering team. Write tests and review code."
                        )
                    }
                ]
            },
            "URL": {"url": "https://example.com/jobs/123"},
        },
    }
    settings = SimpleNamespace(
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )
    result = _score_opportunity(
        page,
        [_criterion()],
        settings=settings,
        profile_facts="Skills: Python, React",
        router=_Router(),
        client=Client(),
    )

    assert result.total_score == 0.5


def test_scoring_prompt_contains_url_source_and_verified_facts():
    client = _BatchClient()
    page = {
        "id": "opportunity-1",
        "properties": {
            "Role Title": {"rich_text": [{"plain_text": "Software Engineer"}]},
            "Description": {
                "rich_text": [
                    {
                        "plain_text": (
                            "Build reliable software with Python and React for a production "
                            "engineering team. Write tests and review code."
                        )
                    }
                ]
            },
            "URL": {"url": "https://example.com/jobs/123"},
            "Source": {"select": {"name": "RSS"}},
            "Source Adapter": {"select": {"name": "rss"}},
        },
    }
    settings = SimpleNamespace(
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
    )
    _score_opportunity(
        page,
        [_criterion()],
        settings=settings,
        profile_facts="Skills: Python, React\nExperience: Software Engineer",
        router=_Router(),
        client=client,
    )

    prompt = client.kwargs["messages"][1]["content"]
    assert "URL: https://example.com/jobs/123" in prompt
    assert "Source: RSS (rss)" in prompt
    assert "Verified candidate facts:" in prompt
    assert "Skills: Python, React" in prompt


def test_lane_override_applies_when_rule_fires():
    result = evaluate_criterion(
        _criterion(
            direction="Lane Override",
            evaluation_method="Keyword",
            lane_override="High Attention",
            instructions="custom prompt",
        ),
        posting_text="This role has a custom prompt.",
        posting_comp=None,
        router=None,
        client=None,
    )
    assert result.lane_override == "High Attention"
    assert assign_lane(0.1, [result]) == "High Attention"


def test_gambling_rule_does_not_treat_instruction_prose_as_keywords():
    criterion = _criterion(
        key="custom_exclusion",
        name="User exclusion",
        evaluation_method="Keyword",
        direction="Hard Exclusion",
        hard_exclusion=True,
        lane_override="Skip",
        instructions=("casinos,wagering"),
    )

    result = evaluate_criterion(
        criterion,
        posting_text="Build a useful software product for healthcare teams.",
        posting_comp=None,
        router=None,
        client=None,
        role_title="Software Engineer",
        description="Build a useful software product for healthcare teams.",
    )

    assert result.score == 0.0
    assert not result.triggered_hard_exclusion


def test_night_shift_rule_requires_explicit_schedule_language():
    criterion = _criterion(
        key="night_shift",
        name="Night shift exclusion",
        evaluation_method="Rule",
        direction="Hard Exclusion",
        hard_exclusion=True,
        lane_override="Skip",
    )

    daytime = evaluate_criterion(
        criterion,
        posting_text="Flexible schedule with occasional after-hours support.",
        posting_comp=None,
        router=None,
        client=None,
        role_title="Software Engineer",
        description="Flexible schedule with occasional after-hours support.",
    )
    overnight = evaluate_criterion(
        criterion,
        posting_text="This is a permanent overnight position.",
        posting_comp=None,
        router=None,
        client=None,
        role_title="Software Engineer",
        description="This is a permanent overnight position.",
    )

    assert daytime.score == 0.0
    assert not daytime.triggered_hard_exclusion
    assert overnight.score == 1.0
    assert overnight.triggered_hard_exclusion


def test_numeric_hard_exclusion_only_triggers_on_known_below_minimum():
    unknown = evaluate_criterion(
        _criterion(
            direction="Hard Exclusion",
            evaluation_method="Numeric",
            minimum_value=30,
        ),
        posting_text="",
        posting_comp=None,
        router=None,
        client=None,
    )
    below = evaluate_criterion(
        _criterion(
            direction="Hard Exclusion",
            evaluation_method="Numeric",
            minimum_value=30,
            maximum_value=100,
        ),
        posting_text="",
        posting_comp=20,
        router=None,
        client=None,
    )
    assert not unknown.triggered_hard_exclusion
    assert below.triggered_hard_exclusion
