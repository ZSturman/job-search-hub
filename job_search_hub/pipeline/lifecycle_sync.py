"""Application tracking follows human decisions; documents are advisory."""

from __future__ import annotations

from dataclasses import dataclass, field

from job_search_hub.config import Settings
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.lifecycle import read_status

PROTECTED = {"Submitted", "Interviewing", "Offer", "Closed"}


@dataclass
class LifecycleSyncStats:
    queued: int = 0
    rejected: int = 0
    review: int = 0
    manual_applications_created: int = 0
    ready: int = 0
    submitted: int = 0
    errors: list[str] = field(default_factory=list)


def meeting_answer_ids_by_opportunity(requirements):
    result = {}
    for req in requirements:
        if req.active and req.resolution == "Meets" and req.opportunity_id and req.answer_id:
            result.setdefault(req.opportunity_id, set()).add(req.answer_id)
    return result


def _next_action(props):
    missing = (P.read_rollup_number(props.get("Unresolved Qualification Count")) or 0) + (
        P.read_rollup_number(props.get("Missing Answer Count")) or 0
    )
    if missing:
        return "Review and approve qualification answers"
    if not P.read_text(props.get("Resume to use")):
        return "Choose a resume; apply manually on the employer site"
    return "Review the employer posting and apply manually"


def create_manual_applications(
    settings: Settings, *, notion, store=None, status_options=None, notifier=None, dry_run: bool
) -> LifecycleSyncStats:
    stats = LifecycleSyncStats()
    ds = settings.notion.data_sources["opportunities"]
    # Load application identities first: a failed relation update must not duplicate an application.
    existing = {}
    for app in notion.query_data_source(settings.notion.data_sources["applications"]):
        for oid in P.read_relation_ids((app.get("properties") or {}).get("Opportunity")):
            existing.setdefault(oid, app["id"])
    for page in notion.query_data_source(
        ds, filter_={"property": "My Decision", "select": {"equals": "Apply"}}
    ):
        props = page.get("properties") or {}
        oid = page.get("id")
        if not oid or read_status(props.get("Status")) in PROTECTED:
            continue
        linked = P.read_relation_ids(props.get("Application"))
        if linked:
            continue
        try:
            app_id = existing.get(oid)
            if not app_id:
                title = P.read_title(props.get("Opportunity")) or "Planned application"
                if not dry_run:
                    app = notion.create_page(
                        parent={"data_source_id": settings.notion.data_sources["applications"]},
                        properties={
                            "Application": P.title(title),
                            "Opportunity": P.relation([oid]),
                            "Job URL": P.url(P.read_url(props.get("URL"))),
                            "Status": P.select("Planned"),
                            "Submission Mode": P.select("Manual"),
                        },
                    )
                    app_id = app["id"]
                    existing[oid] = app_id
                    if store is not None:
                        store.audit(
                            actor="lifecycle_sync",
                            action="manual_application",
                            target_kind="application",
                            target_id=app_id,
                            payload={"opportunity": oid},
                        )
                stats.manual_applications_created += 1
            if not dry_run and app_id:
                notion.update_page(
                    oid,
                    {
                        "Application": P.relation([app_id]),
                        "Next Action": P.rich_text(_next_action(props)),
                    },
                )
            stats.ready += 1
        except Exception as exc:
            stats.errors.append(f"Application tracking for {oid}: {type(exc).__name__}")
    return stats


def reconcile_lifecycles(
    settings: Settings, *, notion, store=None, notifier=None, dry_run: bool
) -> LifecycleSyncStats:
    stats = create_manual_applications(
        settings, notion=notion, store=store, notifier=notifier, dry_run=dry_run
    )
    apps = list(notion.query_data_source(settings.notion.data_sources["applications"]))
    app_by_op = {}
    for app in apps:
        for oid in P.read_relation_ids((app.get("properties") or {}).get("Opportunity")):
            app_by_op[oid] = app
    for page in notion.query_data_source(settings.notion.data_sources["opportunities"]):
        oid = page.get("id")
        props = page.get("properties") or {}
        current = read_status(props.get("Status"))
        if current in {"Offer", "Closed"}:
            continue
        decision = P.read_select(props.get("My Decision"))
        target = None
        if app := app_by_op.get(oid):
            app_status = read_status((app.get("properties") or {}).get("Status"))
            if app_status in {"Submitted", "Interviewing", "Offer"}:
                target = app_status
        if target is None and current not in PROTECTED | {"Verification Needed", "Rejected"}:
            target = (
                "Rejected"
                if decision == "Skip"
                else "Shortlisted"
                if decision in {"Apply", "Shortlist"}
                else None
            )
        if target and target != current:
            if not dry_run:
                notion.update_page(oid, {"Status": P.select(target)})
                if store is not None:
                    store.audit(
                        actor="lifecycle_sync",
                        action="status",
                        target_kind="opportunity",
                        target_id=oid,
                        payload={"status": target},
                    )
            stats.submitted += target == "Submitted"
            stats.rejected += target == "Rejected"
    return stats
