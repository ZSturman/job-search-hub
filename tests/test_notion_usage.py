from __future__ import annotations

from datetime import UTC, datetime

from job_search_hub.ingest.base import RawPosting
from job_search_hub.notion import properties as P
from job_search_hub.notion.schema import ALL_DATA_SOURCES
from job_search_hub.notion.usage import (
    COLUMN_USAGE,
    registry_issues,
    validate_written_properties,
)
from job_search_hub.pipeline.ingest_stage import _assert_valid_opportunity_write, _build_properties


def test_usage_registry_covers_every_schema_property() -> None:
    assert registry_issues() == []
    assert set(COLUMN_USAGE) == {spec.key for spec in ALL_DATA_SOURCES}


def test_ingest_writer_payload_uses_declared_columns_and_values() -> None:
    posting = RawPosting(
        source="Test",
        source_adapter="test",
        external_job_id="job-1",
        company="Acme",
        role_title="Engineer",
        url="https://example.com/job",
        description="Build reliable Python systems.",
        discovered_at=datetime.now(UTC),
        remote_status="Remote",
        schedule="Daytime",
        employment_type=["Full-time"],
        compensation_unit="Annual",
    )

    issues = validate_written_properties("opportunities", _build_properties(posting))

    assert issues == []


def test_writer_value_contract_rejects_legacy_status_values() -> None:
    issues = validate_written_properties(
        "opportunities",
        {"Status": P.select("Approved to Submit")},
    )

    assert [issue.code for issue in issues] == ["unsupported_written_value"]
    assert "Approved to Submit" in issues[0].message


def test_writer_contract_rejects_unknown_columns() -> None:
    issues = validate_written_properties(
        "opportunities",
        {"Legacy Column": P.rich_text("stale")},
    )

    assert [issue.code for issue in issues] == ["write_unknown_property"]


def test_writer_contract_rejects_deprecated_qualification_topic_columns() -> None:
    issues = validate_written_properties(
        "opportunities",
        {
            "Qualification Topics": P.multi_select(["Python"]),
            "Hard Qualification Topics": P.multi_select(["Visa"]),
            "Preferred Qualification Topics": P.multi_select(["React"]),
        },
    )

    assert [issue.code for issue in issues] == [
        "write_to_non_output_property",
        "write_to_non_output_property",
        "write_to_non_output_property",
    ]
    assert {issue.property_name for issue in issues} == {
        "Qualification Topics",
        "Hard Qualification Topics",
        "Preferred Qualification Topics",
    }


def test_writer_contract_rejects_noncanonical_active_topic_values() -> None:
    issues = validate_written_properties(
        "answer_bank",
        {"Topics": P.multi_select(["unbounded topic"])},
    )

    assert [issue.code for issue in issues] == ["unsupported_written_value"]


def test_ingest_blocks_deprecated_topic_mirrors_at_runtime() -> None:
    try:
        _assert_valid_opportunity_write(
            {"Qualification Topics": P.multi_select(["unbounded topic"])}
        )
    except ValueError as exc:
        assert "Qualification Topics" in str(exc)
    else:  # pragma: no cover - makes a regression unmistakable
        raise AssertionError("ingest accepted a deprecated qualification topic mirror")
