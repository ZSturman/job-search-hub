from job_search_hub.config import load_settings


def test_defaults_load_without_env(tmp_path):
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.dry_run_default is True
    assert s.hard_floor_annual_usd == 0
    assert s.hard_floor_hourly_usd == 0
    assert s.notion.api_version == "2025-09-03"
    assert s.llm_audit_mode == "off"
    assert s.score_workers == 2
    assert s.source_catalog_file.name == "source_catalog.yaml"
    # Notion ids start empty until provided.
    assert all(v == "" for v in s.notion.data_sources.values())


def test_csv_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_GREENHOUSE_BOARDS", "acme,otherco")
    monkeypatch.setenv("JOBHUB_ADZUNA_REQUESTS_PER_MINUTE", "18")
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.adapters.greenhouse_boards == ["acme", "otherco"]
    assert s.adapters.adzuna_requests_per_minute == 18


def test_score_workers_are_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_SCORE_WORKERS", "99")
    assert load_settings(env_file=tmp_path / "missing.env").score_workers == 8

    monkeypatch.setenv("JOBHUB_SCORE_WORKERS", "0")
    assert load_settings(env_file=tmp_path / "missing.env").score_workers == 1


def test_auto_model_values_do_not_pin_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_MODEL_PRIMARY", "auto")
    monkeypatch.setenv("JOBHUB_MODEL_EMBEDDING", "best")
    monkeypatch.setenv("JOBHUB_MODEL_REVIEW", "google/gemma-4-e4b")

    s = load_settings(env_file=tmp_path / "missing.env")

    assert s.lmstudio.overrides == {"review": "google/gemma-4-e4b"}


def test_pipe_terms_and_usajobs_plural_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_JOB_SEARCH_ROLE_TERMS", "software engineer|data engineer")
    monkeypatch.setenv("JOBHUB_JOB_SEARCH_SKILL_TERMS", "Python|React")
    monkeypatch.setenv("JOBHUB_JOB_SEARCH_MISSION_TERMS", "animal welfare|climate")
    monkeypatch.setenv("JOBHUB_USAJOBS_KEYWORDS", "software engineer|data analyst")
    monkeypatch.setenv("JOBHUB_USAJOBS_JOB_CATEGORY_CODES", "2210;1550;1560")
    monkeypatch.setenv("JOBHUB_USAJOBS_REMOTE", "true")
    monkeypatch.setenv("JOBHUB_USAJOBS_RADIUS_MILES", "25")

    s = load_settings(env_file=tmp_path / "missing.env")

    assert s.search_terms.role_terms[:2] == ["software engineer", "data engineer"]
    assert s.search_terms.profiles[0].name == "My search"
    assert s.search_terms.profiles[0].role_terms == ["software engineer", "data engineer"]
    assert s.search_terms.skill_terms == ["Python", "React"]
    assert s.search_terms.mission_terms == ["animal welfare", "climate"]
    assert s.adapters.usajobs_keywords == ["software engineer", "data analyst"]
    assert s.adapters.usajobs_job_category_codes == ["2210", "1550", "1560"]
    assert s.adapters.usajobs_remote is True
    assert s.adapters.usajobs_radius_miles == 25


def test_usajobs_legacy_keyword_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUB_USAJOBS_KEYWORD", "software developer|IT specialist")
    s = load_settings(env_file=tmp_path / "missing.env")
    assert s.adapters.usajobs_keywords == ["software developer", "IT specialist"]
