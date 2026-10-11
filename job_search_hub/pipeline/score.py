"""Scoring stage.

Reads `Scoring Criteria` from Notion each tick. Each criterion has:
- Active, Weight, Direction, Evaluation Method, Hard Exclusion,
  Lane Override, Human Review Trigger, Instructions.

For evaluation methods:
- `Rule` / `Keyword` / `Numeric`: deterministic Python evaluators (built-ins).
- `LLM`: handed to the primary model with the criterion's instructions and a
  strict JSON schema.
- `Hybrid`: deterministic first, LLM for the residual.
- `Manual`: skip.

The router lives in `evaluators.py` and is intentionally small here; this
file orchestrates loading, running, and writing back to Notion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from job_search_hub.config import Settings
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion import properties as P
from job_search_hub.notion import properties as Pread
from job_search_hub.notion.client import NotionClient

log = get_logger(__name__)


@dataclass
class Criterion:
    page_id: str
    name: str
    key: str
    active: bool
    category: str
    weight: float
    direction: str
    evaluation_method: str
    hard_exclusion: bool
    lane_override: str
    human_review_trigger: bool
    instructions: str
    minimum_value: float | None
    maximum_value: float | None
    sort_order: int


def load_active_criteria(notion: NotionClient, settings: Settings) -> list[Criterion]:
    ds_id = settings.notion.data_sources["scoring_criteria"]
    rows: list[Criterion] = []
    for page in notion.query_data_source(ds_id):
        props = page.get("properties") or {}
        active = Pread.read_checkbox(props.get("Active"))
        if not active:
            continue
        rows.append(
            Criterion(
                page_id=page.get("id", ""),
                name=Pread.read_title(props.get("Criterion")),
                key=Pread.read_text(props.get("Key")),
                active=active,
                category=Pread.read_select(props.get("Category")),
                weight=float(Pread.read_number(props.get("Weight")) or 0.0),
                direction=Pread.read_select(props.get("Direction")),
                evaluation_method=Pread.read_select(props.get("Evaluation Method")),
                hard_exclusion=Pread.read_checkbox(props.get("Hard Exclusion")),
                lane_override=Pread.read_select(props.get("Lane Override")),
                human_review_trigger=Pread.read_checkbox(props.get("Human Review Trigger")),
                instructions=Pread.read_text(props.get("Instructions")),
                minimum_value=Pread.read_number(props.get("Minimum Value")),
                maximum_value=Pread.read_number(props.get("Maximum Value")),
                sort_order=int(Pread.read_number(props.get("Sort Order")) or 0),
            )
        )
    rows.sort(key=lambda c: c.sort_order)
    log.info("loaded %d active scoring criteria", len(rows))
    return rows


@dataclass
class ComponentResult:
    criterion_key: str
    score: float  # 0..1
    weight: float
    direction: str
    triggered_hard_exclusion: bool = False
    triggered_human_review: bool = False
    lane_override: str = ""
    reason: str = ""
    fallback_used: bool = False
    error: str = ""
    llm_call_id: int | None = None
    criterion_name: str = ""
    category: str = ""
    evaluation_method: str = ""


@dataclass
class ScoringResult:
    page_id: str
    total_score: float
    confidence: float
    lane: str
    hard_excluded: bool
    exclusion_reason: str
    components: list[ComponentResult] = field(default_factory=list)
    top_matches: str = ""
    concerns: str = ""

    def model_reason(self) -> str:
        parts = []
        for c in self.components:
            arrow = (
                "+" if c.direction == "Positive" else ("-" if c.direction == "Negative" else "*")
            )
            suffix = " [fallback]" if c.fallback_used else ""
            parts.append(f"{arrow} {c.criterion_key}: {c.score:.2f}{suffix} ({c.reason or '-'})")
        return "\n".join(parts)


def assign_lane(total: float, components: list[ComponentResult]) -> str:
    """Default lane assignment.

    Criteria with `Direction = Lane Override` and a non-None `Lane Override`
    take precedence in the order they appear. If none fired, fall back to
    score thresholds.
    """
    for c in components:
        if c.lane_override and c.lane_override != "None":
            return c.lane_override
    if total >= 0.75:
        return "High Attention"
    if total >= 0.5:
        return "Fast Review"
    if total >= 0.25:
        return "Worth a Look"
    return "Skip"


def write_scores(
    notion: NotionClient,
    *,
    page_id: str,
    result: ScoringResult,
    my_decision: str = "",
    preserve_status: bool = False,
    dry_run: bool,
) -> None:
    if dry_run:
        log.info(
            "[dry-run] would write scores page=%s lane=%s total=%.2f",
            page_id,
            result.lane,
            result.total_score,
        )
        return
    properties = {
        "Processing State": P.select("Complete"),
        "Last Error": P.rich_text(""),
        "Model Score": P.number(round(result.total_score, 4)),
        "Confidence": P.number(round(result.confidence, 4)),
        "Model Lane": P.select(result.lane),
        "Model Reason": P.rich_text(result.model_reason()),
        "Top Matches": P.rich_text(result.top_matches),
        "Concerns": P.rich_text(result.concerns),
    }
    if result.hard_excluded:
        properties["Hard Exclusion"] = P.checkbox(True)
        properties["Exclusion Reason"] = P.rich_text(result.exclusion_reason)
        properties["Status"] = P.select("Rejected")
    else:
        properties["Hard Exclusion"] = P.checkbox(False)
        properties["Exclusion Reason"] = P.rich_text("")
    if not result.hard_excluded and not preserve_status:
        if my_decision == "Skip" or result.lane == "Skip":
            properties["Status"] = P.select("Rejected")
        else:
            properties["Status"] = P.select("Review")
    notion.update_page(page_id, properties)
