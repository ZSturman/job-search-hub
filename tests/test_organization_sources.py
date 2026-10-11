from __future__ import annotations

from datetime import UTC, datetime, timedelta

from job_search_hub.config import load_settings
from job_search_hub.ingest.organization_sources import (
    OrganizationSourceTarget,
    due_organization_source_targets,
    parse_organization_careers_url,
    read_organization_source_targets,
    settings_with_organization_targets,
)
from job_search_hub.ingest.registry import build_adapters
from job_search_hub.notion import properties as P


class _Notion:
    def __init__(self):
        self.rows = []

    def query_data_source(self, data_source_id, **kwargs):
        assert data_source_id == "organizations"
        return [row for row in self.rows if P.read_checkbox(row["properties"].get("Active"))]


def _settings(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["organizations"] = "organizations"
    return settings


def test_parse_known_ats_career_urls() -> None:
    assert parse_organization_careers_url("https://job-boards.greenhouse.io/acme") == (
        "greenhouse",
        "acme",
    )
    assert parse_organization_careers_url("https://jobs.lever.co/acme") == (
        "lever",
        "acme",
    )
    assert parse_organization_careers_url("https://jobs.ashbyhq.com/acme") == (
        "ashby",
        "acme",
    )
    assert parse_organization_careers_url("https://acme.recruitee.com/o/role") == (
        "recruitee",
        "acme",
    )
    assert parse_organization_careers_url("https://jobs.smartrecruiters.com/Acme/role") == (
        "smartrecruiters",
        "Acme",
    )
    assert parse_organization_careers_url("https://example.com/careers") == (
        "direct_discovery",
        "https://example.com/careers",
    )


def test_active_organizations_expand_adapter_targets_and_direct_discovery(tmp_path) -> None:
    notion = _Notion()
    notion.rows = [
        {
            "id": "org-1",
            "properties": {
                "Organization": P.title("Acme"),
                "Careers URL": P.url("https://job-boards.greenhouse.io/acme"),
                "Active": P.checkbox(True),
            },
        },
        {
            "id": "org-2",
            "properties": {
                "Organization": P.title("Plain"),
                "Careers URL": P.url("https://plain.example/careers"),
                "Active": P.checkbox(True),
            },
        },
        {
            "id": "org-3",
            "properties": {
                "Organization": P.title("Inactive"),
                "Careers URL": P.url("https://jobs.lever.co/inactive"),
                "Active": P.checkbox(False),
            },
        },
    ]

    targets = read_organization_source_targets(notion, _settings(tmp_path))  # type: ignore[arg-type]
    settings = settings_with_organization_targets(_settings(tmp_path), targets)

    assert settings.adapters.greenhouse_boards == ["acme"]
    assert settings.adapters.watchlist_urls == []
    assert settings.adapters.lever_companies == []


def test_organization_targets_are_deduped_against_env_targets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JOBHUB_GREENHOUSE_BOARDS", "acme")
    settings = load_settings(env_file=tmp_path / "missing.env")
    targets = [
        type(
            "Target",
            (),
            {
                "adapter_key": "greenhouse",
                "target": "acme",
            },
        )(),
        type(
            "Target",
            (),
            {
                "adapter_key": "direct_discovery",
                "target": "https://plain.example/careers",
            },
        )(),
    ]

    merged = settings_with_organization_targets(settings, targets)  # type: ignore[arg-type]
    adapters = {adapter.key: adapter for adapter in build_adapters(merged)}

    assert adapters["greenhouse"].boards == ["acme"]
    direct = build_adapters(settings, organization_targets=[targets[1]])
    direct_adapter = next(adapter for adapter in direct if adapter.key == "direct_discovery")
    assert direct_adapter.organization_targets == [targets[1]]


def test_manual_organization_targets_honor_daily_and_weekly_source_history(tmp_store) -> None:
    now = datetime.now(UTC)
    daily = OrganizationSourceTarget(
        "daily",
        "Daily",
        "https://daily.example/careers",
        "direct_discovery",
        "https://daily.example/careers",
        "Daily",
    )
    weekly = OrganizationSourceTarget(
        "weekly",
        "Weekly",
        "https://weekly.example/careers",
        "direct_discovery",
        "https://weekly.example/careers",
        "Weekly",
    )
    automated = OrganizationSourceTarget(
        "ashby",
        "Ashby",
        "https://jobs.ashbyhq.com/acme",
        "ashby",
        "acme",
        "Weekly",
    )
    for target in (daily, weekly):
        tmp_store.record_source_run(
            source_key="direct_discovery",
            adapter_key="direct_discovery",
            target=target.careers_url,
            status="unchanged",
            configured=True,
        )

    due = due_organization_source_targets(
        [daily, weekly, automated],
        tmp_store,
        now=now + timedelta(days=1, seconds=1),
    )

    assert daily in due
    assert weekly not in due
    assert automated in due
