"""Focused Notion workflow views. Core data operations retain their API pin."""

from job_search_hub.notion.hub import STEPS, children

# Views are a newer, separately versioned API; do not upgrade ordinary page CRUD.
VIEW_HEADERS = {"Notion-Version": "2026-03-11"}
VISIBLE = {
    "search_profiles": {
        "Profile",
        "Enabled",
        "Role Terms",
        "Skill Terms",
        "Mission Terms",
        "Excluded Terms",
        "Location",
        "Languages",
        "Remote Only",
        "Max New Per Run",
        "Minimum Annual Pay",
        "Minimum Hourly Pay",
    },
    "sources": {"Source", "Type", "Target", "Enabled", "Cadence", "Status", "Last Error"},
    "scoring_criteria": {
        "Criterion",
        "Active",
        "Weight",
        "Direction",
        "Evaluation Method",
        "Instructions",
    },
    "opportunities": {
        "Opportunity",
        "Company",
        "Role Title",
        "Model Score",
        "Model Lane",
        "Top Matches",
        "Concerns",
        "URL",
        "My Decision",
        "Next Action",
        "Suggested Resume",
    },
    "answer_bank": {
        "Question",
        "My Answer",
        "Status",
        "Approved for Reuse",
        "Affected Opportunities",
        "Source Notes",
    },
    "qualification_requirements": {
        "Requirement",
        "Opportunity",
        "Question",
        "Answer",
        "Resolution",
        "Evidence",
    },
    "resume_facts": {
        "Fact",
        "Value",
        "Verified",
        "Approved for Profile",
        "Approved for Applications",
        "Source",
        "Notes",
    },
    "applications": {
        "Application",
        "Status",
        "Opportunity",
        "Job URL",
        "Submitted At",
        "My Notes",
        "Missing Answer Count",
    },
    "resume_library": {"Resume", "Availability", "Tags", "Recommendation", "Jobs"},
    "organizations": {"Organization", "Website", "Careers URL", "Relationship", "Notes"},
}


def _filter(step, key):
    if key == "opportunities":
        return {
            "and": [
                {"property": "Status", "select": {"does_not_equal": s}}
                for s in ("Closed", "Rejected", "Submitted", "Interviewing", "Offer")
            ]
        }
    if key == "answer_bank":
        return {
            "and": [
                {"property": "Status", "select": {"does_not_equal": s}}
                for s in ("Approved", "Deprecated")
            ]
        }
    if key == "applications":
        statuses = (
            ("Planned",)
            if step.startswith("4")
            else ("Submitted", "Interviewing", "Offer", "Rejected", "Cancelled", "Closed")
        )
        return {"or": [{"property": "Status", "select": {"equals": s}} for s in statuses]}
    return None


def provision_views(notion, root, ids):
    """Restartable views beneath the five pages, without changing user-created views."""
    pages = {
        b.get("child_page", {}).get("title"): b["id"]
        for b in children(notion, root)
        if b.get("type") == "child_page"
    }
    for title, _, keys in STEPS:
        parent = pages[title]
        existing = set()
        for block in children(notion, parent):
            if block.get("type") != "child_database":
                continue
            cursor = None
            while True:
                params = {"database_id": block["id"], "page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                result = notion._request("GET", "/views", params=params, headers=VIEW_HEADERS)
                existing.update(
                    (v.get("data_source_id"), v.get("name")) for v in result.get("results", [])
                )
                if not result.get("has_more"):
                    break
                cursor = result["next_cursor"]
        for key in keys:
            name = title.split(" — ")[1] + " • " + key.replace("_", " ").title()
            if (ids[key], name) in existing:
                continue
            schema = notion.get_data_source(ids[key])
            properties = [
                {"property_id": prop["id"], "visible": n in VISIBLE[key]}
                for n, prop in schema["properties"].items()
            ]
            payload = {
                "data_source_id": ids[key],
                "name": name,
                "type": "table",
                "create_database": {"parent": {"type": "page_id", "page_id": parent}},
                "configuration": {"type": "table", "properties": properties, "wrap_cells": True},
            }
            condition = _filter(title, key)
            if condition:
                payload["filter"] = condition
            if key == "opportunities":
                payload["sorts"] = [{"property": "Model Score", "direction": "descending"}]
            notion._request("POST", "/views", json=payload, headers=VIEW_HEADERS)
    return {"workflow_pages": len(STEPS)}
