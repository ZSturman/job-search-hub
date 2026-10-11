from __future__ import annotations

from pathlib import Path

from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles


def _opportunity(
    page_id: str,
    status: str,
    *,
    decision: str = "Undecided",
    resume_to_use: str = "",
    application_ids: list[str] | None = None,
    exclusion: bool = False,
    missing_answers: int = 0,
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Opportunity": P.title(page_id),
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("Engineer"),
            "URL": P.url("https://example.com/job"),
            "Status": P.select(status),
            "My Decision": P.select(decision),
            "Resume to use": P.rich_text(resume_to_use),
            "Hard Exclusion": P.checkbox(exclusion),
            "Application": P.relation(application_ids or []),
            "Missing Answer Count": {"rollup": {"number": missing_answers}},
            "Unresolved Qualification Count": {"rollup": {"number": 0}},
        },
    }


def _application(page_id: str, status: str, opportunity_ids: list[str]) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Application": P.title(page_id),
            "Status": P.select(status),
            "Opportunity": P.relation(opportunity_ids),
            "Missing Answer Count": {"rollup": {"number": 0}},
        },
    }


def _settings(tmp_path: Path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources.update(
        {
            "opportunities": "opportunities",
            "applications": "applications",
        }
    )
    return settings


def _matches(page: dict, filter_: dict) -> bool:
    props = page.get("properties") or {}
    if "and" in filter_:
        return all(_matches(page, item) for item in filter_["and"])
    if "or" in filter_:
        return any(_matches(page, item) for item in filter_["or"])
    prop = filter_.get("property")
    if prop == "My Decision":
        return P.read_select(props.get(prop)) == filter_["select"]["equals"]
    if prop == "Status":
        return P.read_select(props.get(prop)) == filter_["select"]["equals"]
    if prop == "Application" and filter_.get("relation", {}).get("is_empty"):
        return not P.read_relation_ids(props.get(prop))
    return True


class _Notion:
    def __init__(self, *, opportunities, applications=None, status_options=None):
        self.opportunities = {page["id"]: page for page in opportunities}
        self.applications = {page["id"]: page for page in applications or []}
        self.status_options = set(
            status_options
            or {
                "Review",
                "Queued",
                "Needs Input",
                "Shortlisted",
                "Rejected",
                "Submitted",
            }
        )
        self.updated = []
        self.created = []

    def get_data_source(self, data_source_id):
        return {
            "properties": {
                "Status": {
                    "type": "select",
                    "select": {
                        "options": [{"name": option} for option in sorted(self.status_options)]
                    },
                }
            }
        }

    def query_data_source(self, data_source_id, **kwargs):
        filter_ = kwargs.get("filter_") or {}
        rows = (
            self.applications.values()
            if data_source_id == "applications"
            else self.opportunities.values()
        )
        return [page for page in rows if _matches(page, filter_)]

    def get_page(self, page_id):
        return self.opportunities[page_id]

    def update_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        if page_id in self.opportunities:
            self.opportunities[page_id]["properties"].update(properties)
        if page_id in self.applications:
            self.applications[page_id]["properties"].update(properties)

    def create_page(self, **kwargs):
        app_id = f"app-{len(self.created) + 1}"
        self.created.append(kwargs)
        self.applications[app_id] = {
            "id": app_id,
            "properties": kwargs["properties"],
        }
        return {"id": app_id}


class _Notifier:
    def __init__(self):
        self.sent = []

    def send_once(self, kind, key, subject, body):
        self.sent.append((kind, key, subject, body))
        return True


def test_apply_tracks_without_a_resume_or_answers(tmp_path):
    settings = _settings(tmp_path)
    notion = _Notion(
        opportunities=[_opportunity("job", "Review", decision="Apply", missing_answers=3)]
    )
    stats = reconcile_lifecycles(settings, notion=notion, dry_run=False)
    assert stats.manual_applications_created == 1
    assert len(notion.applications) == 1
    app = next(iter(notion.applications.values()))
    assert P.read_select(app["properties"]["Status"]) == "Planned"
    assert P.read_select(notion.opportunities["job"]["properties"]["Status"]) == "Shortlisted"
    assert "answers" in P.read_text(notion.opportunities["job"]["properties"]["Next Action"])
    assert (
        reconcile_lifecycles(settings, notion=notion, dry_run=False).manual_applications_created
        == 0
    )


def test_preview_tracks_intent_without_writes(tmp_path):
    notion = _Notion(opportunities=[_opportunity("job", "Review", decision="Apply")])
    stats = reconcile_lifecycles(_settings(tmp_path), notion=notion, dry_run=True)
    assert stats.manual_applications_created == 1
    assert notion.created == [] and notion.updated == []


def test_tracking_preserves_submitted_history(tmp_path):
    notion = _Notion(
        opportunities=[_opportunity("job", "Submitted", decision="Skip")],
        applications=[_application("app", "Submitted", ["job"])],
    )
    reconcile_lifecycles(_settings(tmp_path), notion=notion, dry_run=False)
    assert P.read_select(notion.opportunities["job"]["properties"]["Status"]) == "Submitted"
    assert notion.created == []


def test_tracking_does_not_advance_failed_verification(tmp_path):
    notion = _Notion(opportunities=[_opportunity("job", "Verification Needed", decision="Apply")])
    reconcile_lifecycles(_settings(tmp_path), notion=notion, dry_run=False)
    assert len(notion.applications) == 1
    assert (
        P.read_select(notion.opportunities["job"]["properties"]["Status"]) == "Verification Needed"
    )


def test_restart_after_relation_failure_does_not_duplicate(tmp_path):
    class Failing(_Notion):
        fail = True

        def update_page(self, page_id, properties):
            if "Application" in properties and self.fail:
                self.fail = False
                raise RuntimeError("interrupted relation write")
            return super().update_page(page_id, properties)

    notion = Failing(opportunities=[_opportunity("job", "Review", decision="Apply")])
    settings = _settings(tmp_path)
    assert reconcile_lifecycles(settings, notion=notion, dry_run=False).errors
    reconcile_lifecycles(settings, notion=notion, dry_run=False)
    assert len(notion.applications) == 1
    assert P.read_relation_ids(notion.opportunities["job"]["properties"]["Application"])
