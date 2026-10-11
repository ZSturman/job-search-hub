from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from job_search_hub.config import load_settings
from job_search_hub.db import open_store
from job_search_hub.ingest.base import Adapter, RawPosting
from job_search_hub.pipeline.discovery import (
    GlobalIntakeState,
    evaluate_posting,
    plausible_fit_title,
    select_postings,
)
from job_search_hub.pipeline.ingest_stage import run_ingest
from job_search_hub.pipeline.posting_verification import PostingVerificationResult

ENGLISH = (
    "Build reliable React and Python software with a collaborative engineering "
    "team. Write automated tests, review code, improve security, and optimize "
    "application performance for customers."
)
SPANISH = (
    "Desarrolla aplicaciones confiables con React y Python junto a un equipo de "
    "ingeniería. Escribe pruebas automatizadas, revisa código y mejora la "
    "seguridad y el rendimiento del producto."
)
MIXED = (
    "Build reliable React and Python software with an engineering team. "
    "Escribe pruebas automatizadas, revisa código y mejora la seguridad y el "
    "rendimiento del producto para nuestros clientes."
)
JAPANESE = (
    "ReactとPythonを使用して信頼性の高いソフトウェアを開発するエンジニアを募集しています。"
    "チームと協力し、自動テストを作成し、製品のセキュリティを改善します。"
)


def _posting(
    index: int = 1,
    *,
    title: str = "React Developer",
    description: str = ENGLISH,
    bucket: str = "react",
    posted_days_ago: int = 0,
) -> RawPosting:
    return RawPosting(
        source="Test Source",
        source_adapter="test",
        external_job_id=str(index),
        company=f"Company {index}",
        role_title=title,
        url=f"https://example.com/jobs/{index}",
        description=description,
        posted_at=datetime.now(UTC) - timedelta(days=posted_days_ago),
        extras={"search_bucket": bucket},
    )


def test_language_and_uncertainty_rejections(tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")

    assert evaluate_posting(_posting(description=ENGLISH), settings).accepted
    assert evaluate_posting(_posting(description=SPANISH), settings).reason == "non_english"
    assert evaluate_posting(_posting(description=MIXED), settings).reason == "non_english"
    assert evaluate_posting(_posting(description=JAPANESE), settings).reason == "non_english"
    assert (
        evaluate_posting(_posting(description="React developer"), settings).reason == "non_english"
    )


def test_title_exclusions_follow_user_preferences(tmp_path):
    from dataclasses import replace

    settings = load_settings(env_file=tmp_path / "missing.env")
    settings = replace(settings, excluded_terms=("Sales", "President"))
    assert (
        evaluate_posting(_posting(title="Sales Engineer"), settings).reason == "excluded_preference"
    )
    assert evaluate_posting(_posting(title="Frontend Engineer"), settings).accepted


def test_relevance_and_deterministic_bucket_source_caps(tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    irrelevant = _posting(
        title="Operations Coordinator",
        description=ENGLISH.replace("React and Python software", "office processes"),
    )
    assert evaluate_posting(irrelevant, settings).reason == "irrelevant"

    postings = [
        _posting(i, bucket="react" if i < 5 else "python", posted_days_ago=i) for i in range(1, 9)
    ]
    first = select_postings(postings, settings, max_per_bucket=2, max_per_source=3)
    second = select_postings(
        list(reversed(postings)),
        settings,
        max_per_bucket=2,
        max_per_source=3,
    )
    assert [p.external_job_id for p in first.accepted] == [
        p.external_job_id for p in second.accepted
    ]
    assert len(first.accepted) == 3
    assert first.stats.cap_overflow == 5


def test_global_company_and_cross_source_quotas_keep_intake_diverse(tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    state = GlobalIntakeState()
    first_source = [_posting(i, title=f"Software Engineer {i}") for i in range(1, 4)]
    for posting in first_source:
        posting.company = "Repeated Co"
    first = select_postings(first_source, settings, global_state=state)

    assert len(first.accepted) == 2
    assert first.stats.company_quota == 1

    duplicate = _posting(20, title=first.accepted[0].role_title)
    duplicate.company = "Repeated Co"
    duplicate.source_adapter = "different-source"
    second = select_postings([duplicate], settings, global_state=state)

    assert second.accepted == []
    assert second.stats.cross_source_duplicates == 1


def test_named_media_profile_is_not_hard_capped_before_global_intake(tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    from dataclasses import replace

    from job_search_hub.config import SearchProfile

    settings = replace(
        settings,
        search_terms=replace(
            settings.search_terms,
            role_terms=["Video Editor"],
            profiles=(SearchProfile("media", ["Video Editor"], 5),),
        ),
    )
    state = GlobalIntakeState()
    postings = [_posting(i, title=f"Video Editor {i}") for i in range(1, 7)]
    selected = select_postings(postings, settings, max_per_bucket=10, global_state=state)

    assert len(selected.accepted) == 6
    assert selected.stats.profile_quota == 0
    assert state.company_counts


def test_cleanup_plausible_title_classifier_covers_resume_aligned_roles() -> None:
    for title in (
        "Senior Software Engineer",
        "Staff Product Security Engineer - Customer Platform",
        "Senior Software Development Engineer in Test II - Automation",
        "QA Automation Engineer",
        "Developer Relations Manager, AI Platform and Tools - Data Platform Engineering",
        "Apple DFM Automation & AI Tools Development Engineer",
        "AI Engineer, Evaluation",
        "Scientific Software Engineer - Compiler",
        "DevOps and Tools Engineer",
        "Computer Vision Engineer",
        "BI Engineer",
        "Member of Technical Staff - Product Eng",
        "Tech Lead Full-Stack Rails Engineer",
        "Principal AI Safety Engineer for Autonomous Vehicles",
        "AI Security Evaluation Engineer",
        "Computer Vision Algorithm Engineer",
        "Research Engineer",
        "ML Ops Engineer",
        "Data Analytic Engineer I",
    ):
        assert plausible_fit_title(title), title

    for title in (
        "Product Marketing Manager",
        "Staff UI/UX Product Designer",
        "Office Assistant",
        "Structural Engineer",
        "Inside Sales Contractor",
    ):
        assert not plausible_fit_title(title), title


def test_rejected_posting_never_reaches_notion_create(monkeypatch, tmp_path) -> None:
    class SpanishAdapter(Adapter):
        key = "spanish"
        label = "Spanish"

        def fetch(self) -> Iterable[RawPosting]:
            yield _posting(description=SPANISH)

    class Notion:
        def query_data_source(self, data_source_id, **kwargs):
            return []

        def create_page(self, **kwargs):
            raise AssertionError("rejected posting must never be written")

    settings = load_settings(env_file=tmp_path / "missing.env")
    store = open_store(tmp_path / "store.sqlite")
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.build_adapters",
        lambda settings, only=None, organization_targets=None: [SpanishAdapter()],
    )

    stats = run_ingest(
        settings,
        notion=Notion(),
        store=store,
        dry_run=False,
    )
    assert stats.non_english == 1
    assert stats.new_opportunities == 0


def test_unresolved_aggregator_does_not_consume_global_intake(monkeypatch, tmp_path) -> None:
    class Aggregator(Adapter):
        key = "adzuna"
        label = "Adzuna"

        def fetch(self) -> Iterable[RawPosting]:
            posting = _posting(1, title="Software Engineer")
            posting.source_adapter = "adzuna"
            posting.source = "Adzuna"
            yield posting

    class Official(Adapter):
        key = "greenhouse"
        label = "Greenhouse"

        def fetch(self) -> Iterable[RawPosting]:
            posting = _posting(2, title="Software Engineer")
            posting.source_adapter = "greenhouse"
            posting.source = "Greenhouse"
            return [posting]

    class Notion:
        def __init__(self) -> None:
            self.created = []

        def query_data_source(self, *_args, **_kwargs):
            return []

        def create_page(self, **kwargs):
            self.created.append(kwargs["properties"])
            return {"id": "new"}

    settings = load_settings(env_file=tmp_path / "missing.env")
    store = open_store(tmp_path / "store.sqlite")
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.build_adapters",
        lambda *_args, **_kwargs: [Aggregator(), Official()],
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage._prepare_canonical_posting",
        lambda posting, **_kwargs: None if posting.source_adapter == "adzuna" else posting,
    )
    monkeypatch.setattr(
        "job_search_hub.pipeline.ingest_stage.verify_posting_url",
        lambda *_args, **_kwargs: PostingVerificationResult("available", "ok", datetime.now(UTC)),
    )
    notion = Notion()

    stats = run_ingest(settings, notion=notion, store=store, dry_run=False)

    assert stats.unresolved_supplemental == 1
    assert stats.new_opportunities == 1
    assert len(notion.created) == 1
