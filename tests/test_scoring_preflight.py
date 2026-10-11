from types import SimpleNamespace

from job_search_hub.notion import properties as P
from job_search_hub.pipeline.score import Criterion, ScoringResult
from job_search_hub.pipeline.score_stage import _score_opportunity, run_score


def _settings():
    return SimpleNamespace(
        search_terms=SimpleNamespace(role_terms=["Engineer", "Developer"]),
        discovery=SimpleNamespace(
            allowed_languages=["en"],
            language_min_confidence=0.7,
        ),
        notion=SimpleNamespace(data_sources={"opportunities": "opportunities"}),
    )


def _criterion(*, key="fit", weight=1.0):
    return Criterion(
        page_id=key,
        name=key.replace("_", " ").title(),
        key=key,
        active=True,
        category="Skills",
        weight=weight,
        direction="Positive",
        evaluation_method="LLM",
        hard_exclusion=False,
        lane_override="None",
        human_review_trigger=False,
        instructions="Score conservatively against verified facts.",
        minimum_value=None,
        maximum_value=None,
        sort_order=1,
    )


def _page(*, title, description, url, status="Fetched", application_ids=None):
    return {
        "id": "opportunity-1",
        "properties": {
            "Role Title": P.rich_text(title),
            "Company": P.rich_text("Example"),
            "Description": P.rich_text(description),
            "URL": P.url(url),
            "Status": P.select(status),
            "Application": P.relation(application_ids or []),
        },
    }


class _FailIfCalled:
    def chat_json(self, **kwargs):
        raise AssertionError("preflight rejection must not call the model")


class _Router:
    def get(self, role):
        return "model"


class _StaticClient:
    last_call_id = 1

    def __init__(self, scores):
        self.scores = scores

    def chat_json(self, **kwargs):
        return {
            "evaluations": [
                {
                    "criterion_index": index,
                    "score": score,
                    "reason": "test",
                    "lane_override": None,
                    "hard_exclude": False,
                    "needs_human_review": False,
                }
                for index, score in enumerate(self.scores)
            ],
        }


def test_supplied_voice_actor_is_rejected_before_lm_studio():
    page = _page(
        title="British English Voice Actor Specialist Lancaster",
        url=(
            "https://remoteOK.com/remote-jobs/"
            "remote-british-english-voice-actor-specialist-lancaster-"
            "opsarmy-careers-1132748"
        ),
        description=(
            "Record voice scripts with clear pronunciation and natural delivery. "
            "Native British English and previous voice acting experience are required. "
        )
        * 4,
    )

    result = _score_opportunity(
        page,
        [_criterion()],
        settings=_settings(),
        router=_Router(),
        client=_FailIfCalled(),
    )

    assert result.hard_excluded
    assert result.total_score == 0
    assert "does not match the configured search preferences" in result.exclusion_reason


def test_supplied_non_english_generic_remoteok_row_is_rejected_before_model():
    page = _page(
        title="å…µå™¨å²",
        url="https://remoteOK.com/remote-jobs/",
        description=(
            "职位来源于智联招聘。兵器发射理论类相关专业。"
            "以担保或任何理由索要财物，扣押证照，均涉嫌违法。"
        )
        * 4,
    )

    result = _score_opportunity(
        page,
        [_criterion()],
        settings=_settings(),
        router=_Router(),
        client=_FailIfCalled(),
    )

    assert result.hard_excluded
    assert result.total_score == 0
    assert "generic RemoteOK job-list URL" in result.exclusion_reason


def test_source_quality_penalty_does_not_impose_geographic_preferences():
    description = (
        "Build reliable Python and React software, write automated tests, and "
        "maintain production APIs with an engineering team."
    )
    direct = _score_opportunity(
        _page(
            title="Software Engineer",
            description=description,
            url="https://example.com/jobs/123",
        ),
        [_criterion()],
        settings=_settings(),
        router=_Router(),
        client=_StaticClient([0.8]),
    )
    remoteok = _score_opportunity(
        _page(
            title="Software Engineer",
            description=description,
            url="https://remoteok.com/remote-jobs/software-engineer-example-123",
        ),
        [_criterion()],
        settings=_settings(),
        router=_Router(),
        client=_StaticClient([0.8]),
    )
    foreign = _score_opportunity(
        _page(
            title="Software Engineer - Canada Remote",
            description=description + " Applicants must reside in Canada.",
            url="https://example.com/jobs/456",
        ),
        [_criterion()],
        settings=_settings(),
        router=_Router(),
        client=_StaticClient([0.8]),
    )

    assert remoteok.total_score == direct.total_score - 0.08
    assert foreign.total_score == direct.total_score


def test_missing_required_qualifications_caps_score_below_skip_threshold():
    result = _score_opportunity(
        _page(
            title="Software Engineer",
            description=(
                "Build production software. A specialized mandatory qualification "
                "is required for this role."
            ),
            url="https://example.com/jobs/123",
        ),
        [_criterion(key="fit"), _criterion(key="required_skill_overlap")],
        settings=_settings(),
        router=_Router(),
        client=_StaticClient([0.95, 0.1]),
    )

    assert result.total_score <= 0.24
    assert result.lane == "Skip"


def test_rescore_rejects_invalid_row_and_cancels_unsubmitted_application(monkeypatch):
    page = _page(
        title="Software Engineer",
        description="Build software.",
        url="https://example.com/jobs/123",
        status="Needs Input",
        application_ids=["application-1"],
    )
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def get_page(self, page_id):
            return {
                "id": page_id,
                "properties": {"Status": P.select("Needs Review")},
            }

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    class Store:
        def audit(self, **kwargs):
            return None

        def record_score_component(self, **kwargs):
            return None

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._score_opportunity",
        lambda *args, **kwargs: ScoringResult(
            page_id="opportunity-1",
            total_score=0,
            confidence=1,
            lane="Skip",
            hard_excluded=True,
            exclusion_reason="Preflight: invalid",
        ),
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=Store(),
        rescore=True,
        dry_run=False,
        skip_posting_preflight=True,
    )

    assert stats.excluded == 1
    assert ("application-1", {"Status": P.select("Cancelled")}) in updates
    opportunity_updates = [props for page_id, props in updates if page_id == "opportunity-1"]
    assert opportunity_updates[-1]["Status"] == P.select("Rejected")


def test_normal_scoring_query_includes_scoreless_active_statuses(monkeypatch):
    captured = {}

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            captured["filter"] = kwargs.get("filter_")
            return []

    class Store:
        def audit(self, **kwargs):
            return None

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=Store(),
        dry_run=True,
    )

    assert stats.errors == []
    serialized = str(captured["filter"])
    assert "Review" in serialized
    assert "Needs Input" in serialized
    assert "is_empty" in serialized


def test_scoreless_application_status_is_preserved_when_score_is_written(monkeypatch):
    page = _page(
        title="Software Engineer",
        description="Build software.",
        url="https://example.com/jobs/123",
        status="Needs Input",
    )
    page["properties"]["Model Score"] = P.number(None)
    updates = []

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return []

        def update_page(self, page_id, properties):
            updates.append((page_id, properties))

    class Store:
        def audit(self, **kwargs):
            return None

        def record_score_component(self, **kwargs):
            return None

    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage.load_active_criteria",
        lambda notion, settings: [_criterion()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.score_stage._score_opportunity",
        lambda *args, **kwargs: ScoringResult(
            page_id="opportunity-1",
            total_score=0.7,
            confidence=0.8,
            lane="Fast Review",
            hard_excluded=False,
            exclusion_reason="",
        ),
    )

    stats = run_score(
        _settings(),
        notion=Notion(),
        store=Store(),
        pages_override=[page],
        dry_run=False,
    )

    assert stats.scored == 1
    opportunity_update = updates[-1][1]
    assert opportunity_update["Model Score"] == P.number(0.7)
    assert "Status" not in opportunity_update
