from datetime import UTC, datetime

from job_search_hub.ingest.base import RawPosting
from job_search_hub.pipeline.normalize import (
    below_hard_floor,
    excluded_by_keywords,
    fingerprint,
    normalize_company,
    normalize_url,
)


def _posting(**kw) -> RawPosting:
    base = dict(
        source="Greenhouse",
        source_adapter="greenhouse",
        external_job_id="1",
        company="Acme, Inc.",
        role_title="Senior Backend Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1?utm_source=x",
        description="Build and maintain backend services.",
        discovered_at=datetime.now(UTC),
    )
    base.update(kw)
    return RawPosting(**base)


def test_company_normalization():
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("Acme Corp") == "acme"
    assert normalize_company("Acme LLC") == "acme"


def test_url_normalization_strips_tracking():
    assert normalize_url("https://X.com/a?utm_source=x&q=1") == "https://x.com/a?q=1"


def test_url_normalization_removes_adzuna_search_session():
    first = "https://www.adzuna.com/land/ad/123?se=one&utm_source=x&v=stable"
    second = "https://www.adzuna.com/land/ad/123?se=two&utm_source=x&v=stable"
    assert normalize_url(first) == "https://www.adzuna.com/land/ad/123"
    assert normalize_url(first) == normalize_url(second)


def test_fingerprint_stable():
    a = _posting()
    b = _posting(url=_posting().url + "&utm_campaign=foo")
    assert fingerprint(a) == fingerprint(b)


def test_configured_keyword_excludes():
    p = _posting(
        role_title="Slot Machine Technician",
        description="Maintain equipment on the night shift",
    )
    excluded, reason = excluded_by_keywords(p, ["night shift"])
    assert excluded
    assert "night shift" in reason


def test_unknown_comp_does_not_exclude():
    p = _posting()
    excluded, _ = below_hard_floor(p, annual_floor_usd=60000, hourly_floor_usd=30)
    assert not excluded


def test_confirmed_subfloor_excludes():
    p = _posting(compensation_min=40000, compensation_max=55000, compensation_unit="Annual")
    excluded, reason = below_hard_floor(p, annual_floor_usd=60000, hourly_floor_usd=30)
    assert excluded and "annual" in reason


def test_subfloor_uses_upper_end():
    p = _posting(compensation_min=40000, compensation_max=80000, compensation_unit="Annual")
    excluded, _ = below_hard_floor(p, annual_floor_usd=60000, hourly_floor_usd=30)
    assert not excluded
