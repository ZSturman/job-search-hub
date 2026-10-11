"""Load active profile facts from Notion for scoring context.

Resume generation has been removed from the project. The remaining profile
loader keeps the existing Profile Facts database available as a human-curated
source of candidate context for scoring and qualification checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from job_search_hub.config import Settings
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient

log = get_logger(__name__)


@dataclass
class Fact:
    page_id: str
    fact_id: str
    fact_type: str
    parent_fact_id: str
    value: str
    organization: str
    role_label: str
    relationship_type: str
    start_date: str | None
    end_date: str | None
    current: bool
    category: str
    proficiency: str
    emphasis_tags: list[str]
    verified: bool
    approved_for_profile: bool
    approved_for_applications: bool
    metrics_verified: bool
    sensitive: bool
    sort_order: int
    active: bool


def load_facts(notion: NotionClient, settings: Settings) -> list[Fact]:
    ds_id = settings.notion.data_sources["resume_facts"]
    out: list[Fact] = []
    for page in notion.query_data_source(ds_id):
        props = page.get("properties") or {}
        if not P.read_checkbox(props.get("Active")):
            continue
        out.append(
            Fact(
                page_id=page.get("id", ""),
                fact_id=P.read_text(props.get("Fact ID")),
                fact_type=P.read_select(props.get("Fact Type")),
                parent_fact_id=(P.read_relation_ids(props.get("Parent Fact")) or [""])[0],
                value=P.read_text(props.get("Value")) or P.read_title(props.get("Fact")),
                organization=P.read_text(props.get("Organization")),
                role_label=P.read_text(props.get("Role Label")),
                relationship_type=P.read_select(props.get("Relationship Type")),
                start_date=P.read_date(props.get("Start Date")),
                end_date=P.read_date(props.get("End Date")),
                current=P.read_checkbox(props.get("Current")),
                category=P.read_select(props.get("Category")),
                proficiency=P.read_select(props.get("Proficiency")),
                emphasis_tags=P.read_multi_select(props.get("Emphasis Tags")),
                verified=P.read_checkbox(props.get("Verified")),
                approved_for_profile=P.read_checkbox(props.get("Approved for Profile")),
                approved_for_applications=P.read_checkbox(props.get("Approved for Applications")),
                metrics_verified=P.read_checkbox(props.get("Metrics Verified")),
                sensitive=P.read_checkbox(props.get("Sensitive")),
                sort_order=int(P.read_number(props.get("Sort Order")) or 0),
                active=True,
            )
        )
    log.info("loaded %d active profile facts", len(out))
    return out
