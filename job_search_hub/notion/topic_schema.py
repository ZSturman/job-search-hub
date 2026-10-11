"""Repair and enforce the bounded Notion topic schema.

Notion treats every multi-select value written by a page update as a schema
option when the value is new.  Topic fields therefore need an explicit,
bounded vocabulary.  The Opportunity topic columns are obsolete mirrors and
must retain no options at all.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from job_search_hub.config import Settings
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.topics import (
    ACTIVE_NOTION_TOPIC_PROPERTIES,
    CANONICAL_TOPIC_SET,
    LEGACY_OPPORTUNITY_TOPIC_PROPERTIES,
    canonical_topic_options,
)


@dataclass(frozen=True)
class TopicPropertyPolicy:
    data_source_key: str
    property_name: str
    desired_option_names: frozenset[str]


TOPIC_PROPERTY_POLICIES: tuple[TopicPropertyPolicy, ...] = (
    *(
        TopicPropertyPolicy(
            data_source_key=data_source_key,
            property_name=property_name,
            desired_option_names=CANONICAL_TOPIC_SET,
        )
        for data_source_key, property_name in sorted(ACTIVE_NOTION_TOPIC_PROPERTIES)
    ),
    *(
        TopicPropertyPolicy(
            data_source_key="opportunities",
            property_name=property_name,
            desired_option_names=frozenset(),
        )
        for property_name in LEGACY_OPPORTUNITY_TOPIC_PROPERTIES
    ),
)


@dataclass
class TopicSchemaRepair:
    data_source_key: str
    property_name: str
    options_before: int = 0
    options_after: int = 0
    options_removed: int = 0
    options_added: int = 0
    rows_repaired: int = 0
    skipped: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.options_removed or self.options_added or self.rows_repaired)


@dataclass
class TopicSchemaRepairReport:
    repairs: list[TopicSchemaRepair] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(repair.changed for repair in self.repairs)

    @property
    def options_removed(self) -> int:
        return sum(repair.options_removed for repair in self.repairs)

    @property
    def options_added(self) -> int:
        return sum(repair.options_added for repair in self.repairs)

    @property
    def rows_repaired(self) -> int:
        return sum(repair.rows_repaired for repair in self.repairs)


def _option_objects(property_schema: dict[str, Any]) -> list[dict[str, Any]]:
    config = property_schema.get("multi_select") or {}
    options = config.get("options") or []
    if not isinstance(options, list):
        raise ValueError("Notion returned an invalid multi-select options collection.")
    return [option for option in options if isinstance(option, dict)]


def _option_names(options: Iterable[dict[str, Any]]) -> set[str]:
    return {
        name.strip()
        for option in options
        if isinstance((name := option.get("name")), str) and name.strip()
    }


def _desired_options(
    policy: TopicPropertyPolicy,
    current_options: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Keep existing option IDs for active topics and remove all others."""
    if not policy.desired_option_names:
        return []

    current_by_name = {
        option["name"].strip(): option
        for option in current_options
        if isinstance(option.get("name"), str) and option["name"].strip()
    }
    desired: list[dict[str, str]] = []
    for option in canonical_topic_options():
        name = option["name"]
        current = current_by_name.get(name)
        option_id = current.get("id") if current else None
        desired.append({"id": option_id, "name": name} if option_id else option)
    return desired


def _multi_select_values(page: dict[str, Any], property_name: str) -> list[str]:
    properties = page.get("properties") or {}
    property_value = properties.get(property_name)
    return P.read_multi_select(property_value if isinstance(property_value, dict) else None)


def _policies_for(data_source_keys: Iterable[str] | None) -> tuple[TopicPropertyPolicy, ...]:
    if data_source_keys is None:
        return TOPIC_PROPERTY_POLICIES
    requested = set(data_source_keys)
    return tuple(
        policy for policy in TOPIC_PROPERTY_POLICIES if policy.data_source_key in requested
    )


def repair_topic_schemas(
    settings: Settings,
    *,
    notion: NotionClient,
    dry_run: bool,
    data_source_keys: Iterable[str] | None = None,
) -> TopicSchemaRepairReport:
    """Converge known topic fields to their bounded schema policies.

    Schema options are reduced before any page rows are touched. This order is
    intentional: an oversized schema can reject normal page writes, while a
    schema update that omits an option removes it from the data source.
    """
    report = TopicSchemaRepairReport()
    policies_by_source: dict[str, list[TopicPropertyPolicy]] = {}
    for policy in _policies_for(data_source_keys):
        policies_by_source.setdefault(policy.data_source_key, []).append(policy)

    for data_source_key, policies in policies_by_source.items():
        data_source_id = settings.notion.data_sources.get(data_source_key, "")
        if not data_source_id:
            report.repairs.extend(
                TopicSchemaRepair(
                    data_source_key=data_source_key,
                    property_name=policy.property_name,
                    skipped=True,
                )
                for policy in policies
            )
            continue

        schema = notion.get_data_source(data_source_id)
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            raise ValueError(f"{data_source_key} returned an invalid Notion property schema.")

        pending: list[tuple[TopicPropertyPolicy, TopicSchemaRepair, list[dict[str, str]]]] = []
        for policy in policies:
            property_schema = properties.get(policy.property_name)
            if not isinstance(property_schema, dict):
                report.repairs.append(
                    TopicSchemaRepair(
                        data_source_key=data_source_key,
                        property_name=policy.property_name,
                        skipped=True,
                    )
                )
                continue
            if property_schema.get("type") != "multi_select":
                raise ValueError(
                    f"{data_source_key}.{policy.property_name} is not a multi-select property."
                )

            current_options = _option_objects(property_schema)
            current_names = _option_names(current_options)
            desired_names = set(policy.desired_option_names)
            repair = TopicSchemaRepair(
                data_source_key=data_source_key,
                property_name=policy.property_name,
                options_before=len(current_names),
                options_after=len(desired_names),
                options_removed=len(current_names - desired_names),
                options_added=len(desired_names - current_names),
            )
            report.repairs.append(repair)
            pending.append((policy, repair, _desired_options(policy, current_options)))

        page_repair_candidates = [
            pending_repair
            for pending_repair in pending
            if pending_repair[1].options_removed or pending_repair[1].options_added
        ]
        if not page_repair_candidates:
            # When the schema is already exact, Notion cannot expose a page
            # value outside it. Avoid re-scanning every Opportunity on each
            # ingest just to prove that invariant again.
            continue

        # Shrink first so an oversized schema cannot keep page-level recovery
        # from succeeding. Notion removes options omitted from this payload.
        if not dry_run:
            for policy, _repair, desired_options in page_repair_candidates:
                property_schema = properties[policy.property_name]
                property_id = property_schema.get("id") or policy.property_name
                notion.update_data_source(
                    data_source_id,
                    properties={str(property_id): {"multi_select": {"options": desired_options}}},
                )

            verified_schema = notion.get_data_source(data_source_id)
            verified_properties = verified_schema.get("properties") or {}
            if not isinstance(verified_properties, dict):
                raise ValueError(
                    f"{data_source_key} returned an invalid Notion property schema after repair."
                )
            for policy, _repair, _desired_options_for_property in page_repair_candidates:
                verified_property = verified_properties.get(policy.property_name)
                if not isinstance(verified_property, dict):
                    raise ValueError(
                        f"{data_source_key}.{policy.property_name} disappeared during repair."
                    )
                actual_names = _option_names(_option_objects(verified_property))
                if actual_names != set(policy.desired_option_names):
                    raise ValueError(
                        f"{data_source_key}.{policy.property_name} schema repair did not "
                        "converge; "
                        f"expected={sorted(policy.desired_option_names)!r} "
                        f"actual={sorted(actual_names)!r}"
                    )

        for page in notion.query_data_source(data_source_id):
            page_id = str(page.get("id") or "")
            if not page_id:
                continue
            for policy, repair, _desired_options_for_property in page_repair_candidates:
                current_values = _multi_select_values(page, policy.property_name)
                desired_values: list[str] = []
                for value in current_values:
                    if value in policy.desired_option_names and value not in desired_values:
                        desired_values.append(value)
                if current_values == desired_values:
                    continue
                repair.rows_repaired += 1
                if not dry_run:
                    notion.update_page(
                        page_id,
                        {policy.property_name: P.multi_select(desired_values)},
                    )

    return report


def repair_legacy_opportunity_topic_schema(
    settings: Settings,
    *,
    notion: NotionClient,
    dry_run: bool,
) -> TopicSchemaRepairReport:
    """Remove stale options from the three deprecated Opportunity mirrors."""
    return repair_topic_schemas(
        settings,
        notion=notion,
        dry_run=dry_run,
        data_source_keys=("opportunities",),
    )
