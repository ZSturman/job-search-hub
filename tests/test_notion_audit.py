from __future__ import annotations

from copy import deepcopy

from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.notion.audit import run_notion_audit
from job_search_hub.notion.schema import ALL_DATA_SOURCES


def _live_property(prop_spec):
    payload = {"type": prop_spec.type}
    if prop_spec.type in {"select", "multi_select", "status"}:
        payload[prop_spec.type] = {"options": [{"name": option} for option in prop_spec.options]}
    elif prop_spec.type == "relation":
        payload["relation"] = {}
    elif prop_spec.type == "formula":
        payload["formula"] = {"expression": prop_spec.formula or ""}
    elif prop_spec.type == "rollup":
        payload["rollup"] = {}
    else:
        payload[prop_spec.type] = {}
    return payload


class _Notion:
    def __init__(self):
        self.sources = {}
        for spec in ALL_DATA_SOURCES:
            self.sources[spec.key] = {
                "id": spec.key,
                "properties": {prop.name: _live_property(prop) for prop in spec.properties},
            }
        self.sources["opportunities"]["properties"]["Stale Column"] = {
            "type": "rich_text",
            "rich_text": {},
        }
        status = deepcopy(self.sources["opportunities"]["properties"]["Status"])
        status["select"]["options"].append({"name": "Approved to Submit"})
        self.sources["opportunities"]["properties"]["Status"] = status

    def get_data_source(self, data_source_id):
        return self.sources[data_source_id]

    def query_data_source_page(self, data_source_id, **kwargs):
        if data_source_id != "opportunities":
            return {"results": [], "has_more": False}
        return {
            "results": [
                {
                    "id": "op-1",
                    "properties": {
                        "Status": P.select("Approved to Submit"),
                        "My Decision": P.select("Review"),
                    },
                }
            ],
            "has_more": False,
        }


def test_audit_flags_extra_columns_options_and_row_values(monkeypatch, tmp_path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources.update({spec.key: spec.key for spec in ALL_DATA_SOURCES})
    monkeypatch.setattr("job_search_hub.notion.audit.validate_all", lambda *args, **kwargs: [])

    audit = run_notion_audit(
        settings,
        notion=_Notion(),  # type: ignore[arg-type]
        sample_size=1,
    )

    issues = {(issue.data_source, issue.property_name, issue.code) for issue in audit.issues}
    assert ("opportunities", "Stale Column", "extra_live_property") in issues
    assert ("opportunities", "Status", "extra_options") in issues
    assert ("opportunities", "Status", "unexpected_row_value") in issues
    assert not audit.ok()
