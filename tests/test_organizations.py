from __future__ import annotations

from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.organizations import (
    ensure_organization,
    organization_is_noisy,
    sync_opportunity_organizations,
)


def _settings(tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources.update(
        {
            "organizations": "organizations",
            "opportunities": "opportunities",
        }
    )
    return settings


class _Notion:
    def __init__(self):
        self.organizations = {}
        self.opportunities = {}
        self.created = []
        self.updated = []

    def query_data_source(self, data_source_id, **kwargs):
        if data_source_id == "organizations":
            name = ((kwargs.get("filter_") or {}).get("title") or {}).get("equals")
            rows = list(self.organizations.values())
            if name:
                rows = [
                    page
                    for page in rows
                    if P.read_title(page["properties"].get("Organization")) == name
                ]
            return rows
        return list(self.opportunities.values())

    def create_page(self, **kwargs):
        page_id = f"org-{len(self.organizations) + 1}"
        page = {"id": page_id, "properties": kwargs["properties"]}
        self.organizations[page_id] = page
        self.created.append(kwargs)
        return page

    def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        if page_id in self.organizations:
            self.organizations[page_id]["properties"].update(properties)
        if page_id in self.opportunities:
            self.opportunities[page_id]["properties"].update(properties)


def test_ensure_organization_creates_and_caches(tmp_path) -> None:
    notion = _Notion()
    settings = _settings(tmp_path)
    cache = {}

    first = ensure_organization(
        notion,  # type: ignore[arg-type]
        settings,
        company=" Acme  Inc ",
        source_type="adzuna",
        dry_run=False,
        cache=cache,
    )
    second = ensure_organization(
        notion,  # type: ignore[arg-type]
        settings,
        company="Acme Inc",
        source_type="adzuna",
        dry_run=False,
        cache=cache,
    )

    assert first == second == "org-1"
    assert len(notion.created) == 1
    props = notion.organizations["org-1"]["properties"]
    assert P.read_title(props["Organization"]) == "Acme Inc"
    assert P.read_multi_select(props["Source Types"]) == ["adzuna"]


def test_org_sync_links_unlinked_opportunities(tmp_path) -> None:
    notion = _Notion()
    notion.opportunities["op-1"] = {
        "id": "op-1",
        "properties": {
            "Company": P.rich_text("Acme"),
            "Source Adapter": P.select("greenhouse"),
            "Organization": P.relation([]),
        },
    }

    stats = sync_opportunity_organizations(
        _settings(tmp_path),
        notion=notion,  # type: ignore[arg-type]
        dry_run=False,
    )

    assert stats.scanned == 1
    assert stats.linked == 1
    assert P.read_relation_ids(notion.opportunities["op-1"]["properties"]["Organization"]) == [
        "org-1"
    ]


def test_noisy_organization_labels_are_skipped(tmp_path) -> None:
    notion = _Notion()
    notion.opportunities["op-1"] = {
        "id": "op-1",
        "properties": {
            "Company": P.rich_text(
                "We Work Remotely: Remote jobs in design, programming, marketing and more"
            ),
            "Source Adapter": P.select("rss"),
            "Organization": P.relation([]),
        },
    }

    stats = sync_opportunity_organizations(
        _settings(tmp_path),
        notion=notion,  # type: ignore[arg-type]
        dry_run=False,
    )

    assert stats.linked == 0
    assert stats.skipped_noisy == 1
    assert notion.organizations == {}


def test_link_only_sources_can_link_existing_but_not_create(tmp_path) -> None:
    notion = _Notion()
    notion.organizations["org-1"] = {
        "id": "org-1",
        "properties": {
            "Organization": P.title("Acme"),
            "Source Types": P.multi_select([]),
        },
    }
    notion.opportunities["op-1"] = {
        "id": "op-1",
        "properties": {
            "Company": P.rich_text("Acme"),
            "Source Adapter": P.select("rss"),
            "Organization": P.relation([]),
        },
    }
    notion.opportunities["op-2"] = {
        "id": "op-2",
        "properties": {
            "Company": P.rich_text("Missing Co"),
            "Source Adapter": P.select("rss"),
            "Organization": P.relation([]),
        },
    }

    stats = sync_opportunity_organizations(
        _settings(tmp_path),
        notion=notion,  # type: ignore[arg-type]
        dry_run=False,
    )

    assert stats.linked == 1
    assert stats.link_existing == 1
    assert stats.skipped_link_only_missing == 1
    assert len(notion.organizations) == 1


def test_create_missing_modes_change_org_sync_behavior(tmp_path) -> None:
    settings = _settings(tmp_path)

    never = _Notion()
    never.opportunities["op-1"] = {
        "id": "op-1",
        "properties": {
            "Company": P.rich_text("Acme"),
            "Source Adapter": P.select("greenhouse"),
            "Organization": P.relation([]),
        },
    }
    never_stats = sync_opportunity_organizations(
        settings,
        notion=never,  # type: ignore[arg-type]
        dry_run=False,
        create_missing="never",
    )
    assert never_stats.linked == 0
    assert never_stats.skipped_link_only_missing == 1

    all_mode = _Notion()
    all_mode.opportunities["op-1"] = {
        "id": "op-1",
        "properties": {
            "Company": P.rich_text("Acme"),
            "Source Adapter": P.select("rss"),
            "Organization": P.relation([]),
        },
    }
    all_stats = sync_opportunity_organizations(
        settings,
        notion=all_mode,  # type: ignore[arg-type]
        dry_run=False,
        create_missing="all",
    )
    assert all_stats.linked == 1
    assert all_stats.created == 1


def test_organization_noise_detector_flags_source_labels() -> None:
    noisy, reason = organization_is_noisy("[G]Sources")
    assert noisy
    assert "source" in reason

    noisy, _ = organization_is_noisy("We Work Remotely: Full-Stack Programming Jobs")
    assert noisy

    noisy, _ = organization_is_noisy("Acme Inc")
    assert not noisy
