from __future__ import annotations

from types import SimpleNamespace

from job_search_hub.notion.topic_schema import repair_topic_schemas
from job_search_hub.topics import CANONICAL_TOPICS, LEGACY_OPPORTUNITY_TOPIC_PROPERTIES


def _multi_select_property(property_id: str, *options: str) -> dict:
    return {
        "id": property_id,
        "type": "multi_select",
        "multi_select": {"options": [{"id": f"id-{name}", "name": name} for name in options]},
    }


class _FakeNotion:
    def __init__(self, schemas: dict[str, dict], pages: dict[str, list[dict]]) -> None:
        self.schemas = schemas
        self.pages = pages
        self.schema_updates: list[tuple[str, dict]] = []
        self.page_updates: list[tuple[str, dict]] = []

    def get_data_source(self, data_source_id: str) -> dict:
        return self.schemas[data_source_id]

    def update_data_source(self, data_source_id: str, *, properties: dict) -> dict:
        self.schema_updates.append((data_source_id, properties))
        live_properties = self.schemas[data_source_id]["properties"]
        for property_id, patch in properties.items():
            property_schema = next(
                property_schema
                for property_schema in live_properties.values()
                if property_schema["id"] == property_id
            )
            property_schema["multi_select"]["options"] = patch["multi_select"]["options"]
        return self.schemas[data_source_id]

    def query_data_source(self, data_source_id: str):
        yield from self.pages[data_source_id]

    def update_page(self, page_id: str, properties: dict) -> dict:
        self.page_updates.append((page_id, properties))
        for pages in self.pages.values():
            for page in pages:
                if page["id"] == page_id:
                    page["properties"].update(properties)
                    return page
        raise KeyError(page_id)


def _settings(**data_sources: str):
    return SimpleNamespace(notion=SimpleNamespace(data_sources=data_sources))


def test_legacy_opportunity_topics_are_reported_but_not_changed_in_dry_run() -> None:
    properties = {
        name: _multi_select_property(f"prop-{index}", "unbounded topic")
        for index, name in enumerate(LEGACY_OPPORTUNITY_TOPIC_PROPERTIES)
    }
    notion = _FakeNotion(
        {"opportunities": {"properties": properties}},
        {
            "opportunities": [
                {
                    "id": "page-1",
                    "properties": {
                        name: {"multi_select": [{"name": "unbounded topic"}]}
                        for name in LEGACY_OPPORTUNITY_TOPIC_PROPERTIES
                    },
                }
            ]
        },
    )

    report = repair_topic_schemas(
        _settings(opportunities="opportunities"),
        notion=notion,
        dry_run=True,
        data_source_keys=("opportunities",),
    )

    assert report.options_removed == 3
    assert report.rows_repaired == 3
    assert not notion.schema_updates
    assert not notion.page_updates


def test_legacy_opportunity_topics_are_removed_before_page_values_are_cleared() -> None:
    properties = {
        name: _multi_select_property(f"prop-{index}", "unbounded topic")
        for index, name in enumerate(LEGACY_OPPORTUNITY_TOPIC_PROPERTIES)
    }
    notion = _FakeNotion(
        {"opportunities": {"properties": properties}},
        {
            "opportunities": [
                {
                    "id": "page-1",
                    "properties": {
                        name: {"multi_select": [{"name": "unbounded topic"}]}
                        for name in LEGACY_OPPORTUNITY_TOPIC_PROPERTIES
                    },
                }
            ]
        },
    )

    report = repair_topic_schemas(
        _settings(opportunities="opportunities"),
        notion=notion,
        dry_run=False,
        data_source_keys=("opportunities",),
    )

    assert report.options_removed == 3
    assert report.rows_repaired == 3
    assert [update[0] for update in notion.schema_updates] == ["opportunities"] * 3
    assert len(notion.page_updates) == 3
    assert all(prop["multi_select"]["options"] == [] for prop in properties.values())
    assert all(
        value == {"multi_select": []}
        for _page_id, patch in notion.page_updates
        for value in patch.values()
    )


def test_active_topic_schema_is_converged_to_the_canonical_vocabulary() -> None:
    notion = _FakeNotion(
        {
            "answer_bank": {
                "properties": {
                    "Topics": _multi_select_property("topics-id", "Python", "unbounded topic")
                }
            }
        },
        {
            "answer_bank": [
                {
                    "id": "answer-1",
                    "properties": {
                        "Topics": {
                            "multi_select": [
                                {"name": "Python"},
                                {"name": "unbounded topic"},
                            ]
                        }
                    },
                }
            ]
        },
    )

    report = repair_topic_schemas(
        _settings(answer_bank="answer_bank"),
        notion=notion,
        dry_run=False,
        data_source_keys=("answer_bank",),
    )

    assert report.options_removed == 1
    assert report.options_added == len(CANONICAL_TOPICS) - 1
    assert report.rows_repaired == 1
    options = notion.schemas["answer_bank"]["properties"]["Topics"]["multi_select"]["options"]
    assert {option["name"] for option in options} == set(CANONICAL_TOPICS)
    assert notion.page_updates == [("answer-1", {"Topics": {"multi_select": [{"name": "Python"}]}})]
