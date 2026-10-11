"""Explain an opportunity's scoring inputs and latest score components."""

from __future__ import annotations

from job_search_hub.config import Settings
from job_search_hub.db import Store
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.score import load_active_criteria


def _looks_like_page_id(value: str) -> bool:
    cleaned = value.replace("-", "")
    return len(cleaned) == 32 and all(c in "0123456789abcdefABCDEF" for c in cleaned)


def find_opportunity(notion: NotionClient, settings: Settings, query: str) -> dict:
    if _looks_like_page_id(query):
        return notion.get_page(query)
    ds_id = settings.notion.data_sources["opportunities"]
    filters = [
        {"property": "URL", "url": {"equals": query}},
        {"property": "Fingerprint", "rich_text": {"equals": query}},
    ]
    for filter_ in filters:
        for page in notion.query_data_source(ds_id, filter_=filter_):
            return page
    raise RuntimeError(f"No opportunity matched {query!r}")


def explain_opportunity(
    *,
    notion: NotionClient,
    settings: Settings,
    store: Store,
    query: str,
) -> str:
    page = find_opportunity(notion, settings, query)
    props = page.get("properties") or {}
    page_id = page.get("id", "")
    fields = {
        "Company": P.read_text(props.get("Company")),
        "Role Title": P.read_text(props.get("Role Title")),
        "URL": P.read_url(props.get("URL")),
        "Source": P.read_select(props.get("Source")),
        "Source Adapter": P.read_select(props.get("Source Adapter")),
        "Location": P.read_text(props.get("Location")),
        "Remote Status": P.read_select(props.get("Remote Status")),
        "Schedule": P.read_select(props.get("Schedule")),
        "Compensation Text": P.read_text(props.get("Compensation Text")),
        "Model Score": str(P.read_number(props.get("Model Score")) or ""),
        "Confidence": str(P.read_number(props.get("Confidence")) or ""),
        "Model Lane": P.read_select(props.get("Model Lane")),
        "Status": read_status(props.get("Status")),
    }
    criteria = load_active_criteria(notion, settings)
    components = store.latest_score_components(page_id)

    lines = [f"# Explain Opportunity: {fields['Company']} - {fields['Role Title']}", ""]
    lines.append("## Notion Fields Used")
    for k, v in fields.items():
        lines.append(f"- {k}: {v or '-'}")
    lines.append("")
    lines.append("## Active Scoring Criteria")
    for c in sorted(criteria, key=lambda c: abs(c.weight), reverse=True):
        lines.append(
            f"- {c.key or c.name}: weight={c.weight}, method={c.evaluation_method}, "
            f"direction={c.direction}, lane_override={c.lane_override or 'None'}"
        )
    lines.append("")
    lines.append("## Latest Score Components")
    if not components:
        lines.append("- No local score components found. Run `jobhub score --dry-run` first.")
    else:
        for row in components:
            flags = []
            if row["triggered_hard_exclusion"]:
                flags.append("hard_exclusion")
            if row["triggered_human_review"]:
                flags.append("human_review")
            if row["lane_override"]:
                flags.append(f"lane={row['lane_override']}")
            if row["fallback_used"]:
                flags.append("fallback")
            if row["error"]:
                flags.append(f"error={row['error'][:80]}")
            lines.append(
                f"- {row['criterion_key']}: score={row['score']:.2f}, "
                f"weight={row['weight']:.2f}, effect={row['weighted_effect']:.4f}, "
                f"method={row['evaluation_method']}, reason={row['reason'] or '-'}"
                f"{' [' + ', '.join(flags) + ']' if flags else ''}"
            )
    return "\n".join(lines)
