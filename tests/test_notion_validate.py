"""Validator unit tests using a stub NotionClient (no network)."""

from __future__ import annotations

from job_search_hub.config import load_settings
from job_search_hub.notion.client import NotionError
from job_search_hub.notion.schema import ANSWER_BANK, OPPORTUNITIES
from job_search_hub.notion.validate import (
    SchemaMismatch,
    ValidationReport,
    summarize_reports,
    validate_all,
    validate_one,
)


class _Stub:
    """Mimics just enough of NotionClient.get_data_source for the validator."""

    def __init__(
        self,
        payload: dict,
        *,
        by_id: dict[str, dict] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.by_id = by_id or {}
        self.errors = errors or {}

    def get_data_source(self, data_source_id: str) -> dict:
        if data_source_id in self.errors:
            raise NotionError(self.errors[data_source_id])
        if data_source_id in self.by_id:
            return self.by_id[data_source_id]
        return self.payload


def _select_options(*names: str) -> list[dict]:
    return [{"name": n} for n in names]


def _build_payload(spec) -> dict:
    """Build a minimal valid payload from a DataSourceSpec."""
    properties: dict[str, dict] = {}
    for p in spec.properties:
        prop: dict = {"type": p.type}
        if p.type in {"select", "multi_select", "status"}:
            prop[p.type] = {"options": _select_options(*p.options)}
        elif p.type == "relation":
            prop["relation"] = {"data_source_id": "fake_target"}
        elif p.type == "formula":
            prop["formula"] = {"expression": p.formula or ""}
        elif p.type == "rollup":
            prop["rollup"] = {}
        else:
            prop[p.type] = {}
        properties[p.name] = prop
    return {"properties": properties}


def test_valid_schema_passes():
    payload = _build_payload(ANSWER_BANK)
    report = validate_one(_Stub(payload), ANSWER_BANK, "ds1", relation_targets={})
    assert report.ok, report.mismatches


def test_missing_property_caught():
    payload = _build_payload(ANSWER_BANK)
    del payload["properties"]["Question Family"]
    report = validate_one(_Stub(payload), ANSWER_BANK, "ds1", relation_targets={})
    assert not report.ok
    codes = [m.code for m in report.mismatches]
    assert "missing_property" in codes


def test_type_mismatch_caught():
    payload = _build_payload(OPPORTUNITIES)
    payload["properties"]["Compensation Min"] = {"type": "rich_text", "rich_text": {}}
    report = validate_one(_Stub(payload), OPPORTUNITIES, "ds1", relation_targets={})
    codes = [m.code for m in report.mismatches]
    assert "type_mismatch" in codes


def test_missing_status_option_caught():
    payload = _build_payload(OPPORTUNITIES)
    # Drop one expected status option.
    payload["properties"]["Status"]["select"]["options"] = _select_options(
        "Fetched",
        "Scoring",
        "Rejected",
        "Closed",
    )
    report = validate_one(_Stub(payload), OPPORTUNITIES, "ds1", relation_targets={})
    codes = [m.code for m in report.mismatches]
    assert "missing_options" in codes


def test_missing_id_reported():
    payload = _build_payload(ANSWER_BANK)
    report = validate_one(_Stub(payload), ANSWER_BANK, "", relation_targets={})
    assert not report.ok
    assert any(m.code == "missing_id" for m in report.mismatches)


def test_relation_diagnostic_exact_data_source_match_has_no_mismatch():
    payload = _build_payload(OPPORTUNITIES)
    payload["properties"]["Answer Bank Items"] = {
        "type": "relation",
        "relation": {"data_source_id": "ds_expected_target"},
    }
    report = validate_one(
        _Stub(payload),
        OPPORTUNITIES,
        "ds_opportunities",
        relation_targets={"answer_bank": "ds_expected_target"},
    )
    assert not any(m.code == "relation_mismatch" for m in report.mismatches)


def test_relation_configured_parent_database_id_resolves_to_child_data_source(
    monkeypatch,
    tmp_path,
) -> None:
    payload = _build_payload(OPPORTUNITIES)
    payload["id"] = "ds_opportunities"
    payload["parent"] = {"database_id": "db_opportunities"}
    payload["properties"]["Answer Bank Items"] = {
        "type": "relation",
        "relation": {"data_source_id": "ds_answer_bank"},
    }
    monkeypatch.setenv("JOBHUB_NOTION_DS_OPPORTUNITIES", "db_opportunities")
    monkeypatch.setenv("JOBHUB_NOTION_DS_ANSWER_BANK", "db_answer_bank")
    settings = load_settings(env_file=tmp_path / "missing.env")

    client = _Stub(
        payload,
        by_id={
            "db_opportunities": payload,
            "ds_opportunities": payload,
            "db_answer_bank": {
                "id": "ds_answer_bank",
                "parent": {"database_id": "db_answer_bank"},
                "properties": {},
            },
            "ds_answer_bank": {
                "id": "ds_answer_bank",
                "parent": {"database_id": "db_answer_bank"},
                "properties": {},
            },
        },
    )

    reports = validate_all(settings, client=client)
    report = next(r for r in reports if r.data_source == "opportunities")

    assert report.data_source_id == "ds_opportunities"
    assert not any(m.code == "relation_mismatch" for m in report.mismatches)


def test_relation_different_child_ids_under_same_container_pass() -> None:
    payload = _build_payload(OPPORTUNITIES)
    payload["properties"]["Answer Bank Items"] = {
        "type": "relation",
        "relation": {"data_source_id": "ds_live_target"},
    }
    client = _Stub(
        payload,
        by_id={
            "ds_opportunities": payload,
            "ds_expected_target": {
                "id": "ds_expected_target",
                "parent": {"database_id": "db_shared"},
            },
            "ds_live_target": {"id": "ds_live_target", "parent": {"database_id": "db_shared"}},
        },
    )

    report = validate_one(
        client,
        OPPORTUNITIES,
        "ds_opportunities",
        relation_targets={"answer_bank": "ds_expected_target"},
    )

    assert report.ok, report.mismatches
    assert not any(m.code == "relation_mismatch" for m in report.mismatches)


def test_relation_diagnostic_unrelated_database_containers() -> None:
    payload = _build_payload(OPPORTUNITIES)
    payload["properties"]["Answer Bank Items"] = {
        "type": "relation",
        "relation": {"data_source_id": "ds_live_target"},
    }
    client = _Stub(
        payload,
        by_id={
            "ds_opportunities": payload,
            "ds_expected_target": {
                "id": "ds_expected_target",
                "parent": {"database_id": "db_expected"},
            },
            "ds_live_target": {"id": "ds_live_target", "parent": {"database_id": "db_live"}},
        },
    )

    report = validate_one(
        client,
        OPPORTUNITIES,
        "ds_opportunities",
        relation_targets={"answer_bank": "ds_expected_target"},
    )

    relation_mismatches = [m for m in report.mismatches if m.code == "relation_mismatch"]
    assert relation_mismatches
    details = relation_mismatches[0].details or {}
    assert details["same_database_container"] is False


def test_relation_diagnostic_handles_missing_live_database_id() -> None:
    payload = _build_payload(OPPORTUNITIES)
    payload["properties"]["Answer Bank Items"] = {
        "type": "relation",
        "relation": {"data_source_id": "ds_live_target"},
    }
    client = _Stub(
        payload,
        by_id={
            "ds_opportunities": payload,
            "ds_expected_target": {
                "id": "ds_expected_target",
                "parent": {"database_id": "db_expected"},
            },
            "ds_live_target": {"id": "ds_live_target", "parent": {"database_id": "db_live"}},
        },
    )

    report = validate_one(
        client,
        OPPORTUNITIES,
        "ds_opportunities",
        relation_targets={"answer_bank": "ds_expected_target"},
    )
    relation_mismatches = [m for m in report.mismatches if m.code == "relation_mismatch"]
    assert relation_mismatches
    details = relation_mismatches[0].details or {}
    assert details["live_relation_database_id"] is None
    summary = summarize_reports([report])
    assert "live_relation_database_id" not in summary


def test_formula_equivalent_when_notion_uses_property_id_reference() -> None:
    payload = _build_payload(ANSWER_BANK)
    payload["properties"]["Status"]["id"] = "status_prop_internal"
    payload["properties"]["Needs Answer"] = {
        "type": "formula",
        "formula": {"expression": 'prop("status_prop_internal") == "Needs Answer"'},
    }
    report = validate_one(_Stub(payload), ANSWER_BANK, "ds_answer_bank", relation_targets={})
    assert not any(m.code == "formula_mismatch" for m in report.mismatches)


def test_formula_equivalent_when_notion_uses_encoded_property_id_reference() -> None:
    payload = _build_payload(ANSWER_BANK)
    payload["properties"]["Status"]["id"] = "status%20prop%2Finternal"
    payload["properties"]["Needs Answer"] = {
        "type": "formula",
        "formula": {"expression": 'prop("status%2520prop%252Finternal") == "Needs Answer"'},
    }
    report = validate_one(_Stub(payload), ANSWER_BANK, "ds_answer_bank", relation_targets={})
    assert not any(m.code == "formula_mismatch" for m in report.mismatches)


def test_formula_equivalent_when_notion_uses_block_property_reference() -> None:
    payload = _build_payload(ANSWER_BANK)
    payload["properties"]["Status"]["id"] = "%7Ckso"
    payload["properties"]["Needs Answer"] = {
        "type": "formula",
        "formula": {
            "expression": (
                "{{notion:block_property:%7Ckso:372aec94-4d3c-80d9-bca0-000bbeb12465:"
                'dfeaec94-4d3c-81cd-bb42-00038fa91f24}} == "Needs Answer"'
            ),
        },
    }
    report = validate_one(_Stub(payload), ANSWER_BANK, "ds_answer_bank", relation_targets={})
    assert not any(m.code == "formula_mismatch" for m in report.mismatches)


def test_formula_mismatch_includes_live_expression_diagnostic() -> None:
    payload = _build_payload(ANSWER_BANK)
    payload["properties"]["Needs Answer"] = {
        "type": "formula",
        "formula": {"expression": 'prop("Status") == "Drafted"'},
    }
    report = validate_one(_Stub(payload), ANSWER_BANK, "ds_answer_bank", relation_targets={})
    formula_mismatches = [m for m in report.mismatches if m.code == "formula_mismatch"]
    assert formula_mismatches
    details = formula_mismatches[0].details or {}
    assert details["live_expression"] == 'prop("Status") == "Drafted"'
    summary = summarize_reports([report])
    assert "live_formula_expression" in summary
    assert "manual" in summary.lower()


def test_summary_adds_access_hint_for_object_not_found_fetch_failures():
    msg = (
        "Notion GET /data_sources/abc -> 404: "
        '{"object":"error","status":404,"code":"object_not_found",'
        '"message":"Could not find database with ID: abc. Make sure the relevant pages '
        'and databases are shared with your integration "social-posts-automation"."}'
    )
    reports = [
        ValidationReport(
            data_source="opportunities",
            data_source_id="abc",
            ok=False,
            mismatches=[
                SchemaMismatch(data_source="opportunities", code="fetch_failed", message=msg)
            ],
        )
    ]
    text = summarize_reports(reports)
    assert "Likely cause: Notion access mismatch" in text
    assert "social-posts-automation" in text


def test_summary_no_access_hint_for_non_fetch_mismatch():
    reports = [
        ValidationReport(
            data_source="opportunities",
            data_source_id="abc",
            ok=False,
            mismatches=[
                SchemaMismatch(
                    data_source="opportunities",
                    code="missing_property",
                    message="property 'Status' not found",
                )
            ],
        )
    ]
    text = summarize_reports(reports)
    assert "Likely cause: Notion access mismatch" not in text
