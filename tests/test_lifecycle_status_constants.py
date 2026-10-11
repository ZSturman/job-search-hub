from job_search_hub.notion.schema import APPLICATIONS, OPPORTUNITIES
from job_search_hub.pipeline.lifecycle import APPLICATION_STATUSES, OPPORTUNITY_STATUSES


def _options(spec, property_name: str) -> tuple[str, ...]:
    for prop in spec.properties:
        if prop.name == property_name:
            return prop.options
    raise AssertionError(f"missing property {property_name}")


def test_lifecycle_status_constants_match_notion_schema() -> None:
    assert _options(OPPORTUNITIES, "Status") == OPPORTUNITY_STATUSES
    assert _options(APPLICATIONS, "Status") == APPLICATION_STATUSES


def test_application_statuses_do_not_include_legacy_drafted_state() -> None:
    assert "Drafted" not in APPLICATION_STATUSES
