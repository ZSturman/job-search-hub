"""Read-only schema validation against the live Notion data sources.

We compare each configured data source against `schema.ALL_DATA_SOURCES`. The
validator never modifies Notion. On mismatch it produces a structured report
and the caller decides whether to halt the affected operation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote

from job_search_hub.config import Settings
from job_search_hub.logging_setup import get_logger
from job_search_hub.notion.client import NotionClient, NotionError
from job_search_hub.notion.schema import ALL_DATA_SOURCES, DataSourceSpec, PropertySpec

log = get_logger(__name__)


class _DataSourceReader(Protocol):
    def get_data_source(self, data_source_id: str) -> dict[str, Any]: ...


@dataclass
class SchemaMismatch:
    data_source: str
    code: str
    message: str
    property_name: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class ValidationReport:
    data_source: str
    data_source_id: str
    ok: bool
    mismatches: list[SchemaMismatch] = field(default_factory=list)


_PROP_REF_RE = re.compile(r'prop\("([^"]+)"\)')
_BLOCK_PROPERTY_REF_RE = re.compile(r"\{\{notion:block_property:([^}:]+)(?::[^}]*)?\}\}")


def _options_from_property(prop: dict[str, Any]) -> set[str]:
    pt = prop.get("type")
    if pt in {"select", "multi_select", "status"}:
        block = prop.get(pt) or {}
        opts = block.get("options") or []
        return {o.get("name", "") for o in opts}
    return set()


def _validate_property(
    spec: PropertySpec,
    live_props: dict[str, dict[str, Any]],
    *,
    relation_targets: dict[str, str],
    data_source_key: str,
    client: _DataSourceReader,
    ds_context_cache: dict[str, dict[str, Any]],
) -> list[SchemaMismatch]:
    mismatches: list[SchemaMismatch] = []
    live = live_props.get(spec.name)
    if live is None:
        if spec.required:
            mismatches.append(
                SchemaMismatch(
                    data_source=data_source_key,
                    code="missing_property",
                    message=f"property {spec.name!r} not found",
                    property_name=spec.name,
                )
            )
        return mismatches

    actual_type = live.get("type")
    if actual_type != spec.type:
        mismatches.append(
            SchemaMismatch(
                data_source=data_source_key,
                code="type_mismatch",
                message=f"property {spec.name!r} expected type {spec.type!r}, got {actual_type!r}",
                property_name=spec.name,
            )
        )
        return mismatches  # nothing further to compare

    # Option presence (we require the expected options to exist; extras are fine)
    if spec.options:
        live_options = _options_from_property(live)
        missing = [o for o in spec.options if o not in live_options]
        if missing:
            mismatches.append(
                SchemaMismatch(
                    data_source=data_source_key,
                    code="missing_options",
                    message=f"property {spec.name!r} missing options: {missing}",
                    property_name=spec.name,
                )
            )

    # Relation target check
    if spec.type == "relation" and spec.relation_target:
        rel = live.get("relation") or {}
        live_relation_ds_id = rel.get("data_source_id")
        live_relation_db_id = rel.get("database_id")
        target_id = live_relation_ds_id or live_relation_db_id
        expected_id = relation_targets.get(spec.relation_target)
        if expected_id and target_id and expected_id != target_id:
            relation_details = _build_relation_diagnostics(
                client,
                expected_target_id=expected_id,
                live_relation_ds_id=live_relation_ds_id,
                live_relation_db_id=live_relation_db_id,
                source_data_source_key=data_source_key,
                property_name=spec.name,
                cache=ds_context_cache,
            )
            if not _relation_target_matches(expected_id, target_id, relation_details):
                container_note = ""
                same_container = relation_details.get("same_database_container")
                if same_container is True:
                    container_note = " (same database container)"
                elif same_container is False:
                    container_note = " (different database containers)"
                mismatches.append(
                    SchemaMismatch(
                        data_source=data_source_key,
                        code="relation_mismatch",
                        message=(
                            f"relation {spec.name!r} expected target {spec.relation_target!r} "
                            f"({expected_id}), got {target_id}{container_note}"
                        ),
                        property_name=spec.name,
                        details=relation_details,
                    )
                )

    # Formula text check (best-effort: Notion returns the expression)
    if spec.type == "formula" and spec.formula:
        live_expr = (live.get("formula") or {}).get("expression", "")
        expected_expr = spec.formula.strip()
        live_expr_stripped = live_expr.strip()
        aliases = _formula_aliases(live_props)
        expected_norm = _normalize_formula_expression(expected_expr, aliases)
        live_norm = _normalize_formula_expression(live_expr_stripped, aliases)
        if live_norm != expected_norm:
            mismatches.append(
                SchemaMismatch(
                    data_source=data_source_key,
                    code="formula_mismatch",
                    message=(f"formula {spec.name!r} expected {spec.formula!r}, got {live_expr!r}"),
                    property_name=spec.name,
                    details={
                        "expected_expression": expected_expr,
                        "live_expression": live_expr_stripped,
                        "normalized_expected": expected_norm,
                        "normalized_live": live_norm,
                        "manual_verification_recommended": True,
                    },
                )
            )

    return mismatches


def validate_one(
    client: _DataSourceReader,
    spec: DataSourceSpec,
    data_source_id: str,
    *,
    relation_targets: dict[str, str],
    ds_context_cache: dict[str, dict[str, Any]] | None = None,
) -> ValidationReport:
    report = ValidationReport(
        data_source=spec.key, data_source_id=data_source_id, ok=True, mismatches=[]
    )
    if not data_source_id:
        report.ok = False
        report.mismatches.append(
            SchemaMismatch(
                data_source=spec.key,
                code="missing_id",
                message=(
                    f"NOTION_DS_{spec.key.upper()} is missing; reconnect the root "
                    + "with jobhub setup"
                ),
            )
        )
        return report

    try:
        live = client.get_data_source(data_source_id)
    except NotionError as exc:
        report.ok = False
        report.mismatches.append(
            SchemaMismatch(
                data_source=spec.key,
                code="fetch_failed",
                message=str(exc),
            )
        )
        return report

    live_props: dict[str, dict[str, Any]] = live.get("properties") or {}
    if ds_context_cache is None:
        ds_context_cache = {}
    for prop_spec in spec.properties:
        report.mismatches.extend(
            _validate_property(
                prop_spec,
                live_props,
                relation_targets=relation_targets,
                data_source_key=spec.key,
                client=client,
                ds_context_cache=ds_context_cache,
            )
        )
    report.ok = not report.mismatches
    return report


def _relation_target_matches(
    expected_id: str,
    target_id: str,
    details: dict[str, Any],
) -> bool:
    expected_resolved = details.get("expected_target_data_source_id") or expected_id
    live_resolved = details.get("live_target_data_source_id") or target_id
    if expected_resolved == live_resolved:
        return True
    return details.get("same_database_container") is True


def _fetch_data_source_context(
    client: _DataSourceReader,
    raw_id: str | None,
    *,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not raw_id:
        return {}
    if raw_id in cache:
        return cache[raw_id]

    context: dict[str, Any] = {
        "input_id": raw_id,
        "resolved_data_source_id": None,
        "parent_database_id": None,
        "fetch_error": None,
    }
    try:
        data = client.get_data_source(raw_id)
        context["resolved_data_source_id"] = data.get("id") or raw_id
        parent = data.get("parent") or {}
        context["parent_database_id"] = parent.get("database_id")
    except Exception as exc:  # best-effort diagnostics only
        context["fetch_error"] = str(exc)

    cache[raw_id] = context
    return context


def _build_relation_diagnostics(
    client: _DataSourceReader,
    *,
    expected_target_id: str,
    live_relation_ds_id: str | None,
    live_relation_db_id: str | None,
    source_data_source_key: str,
    property_name: str,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected_ctx = _fetch_data_source_context(client, expected_target_id, cache=cache)
    live_lookup_id = live_relation_ds_id or live_relation_db_id
    live_ctx = _fetch_data_source_context(client, live_lookup_id, cache=cache)

    expected_resolved = expected_ctx.get("resolved_data_source_id") or expected_target_id
    live_resolved = live_ctx.get("resolved_data_source_id") or live_lookup_id

    expected_parent = expected_ctx.get("parent_database_id")
    live_parent = live_ctx.get("parent_database_id")
    same_container: bool | None = None
    if expected_parent and live_parent:
        same_container = expected_parent == live_parent

    return {
        "source_data_source": source_data_source_key,
        "property_name": property_name,
        "expected_target_input_id": expected_target_id,
        "expected_target_data_source_id": expected_resolved,
        "expected_target_parent_database_id": expected_parent,
        "live_relation_data_source_id": live_relation_ds_id,
        "live_relation_database_id": live_relation_db_id,
        "live_target_data_source_id": live_resolved,
        "live_target_parent_database_id": live_parent,
        "same_database_container": same_container,
        "expected_target_fetch_error": expected_ctx.get("fetch_error"),
        "live_target_fetch_error": live_ctx.get("fetch_error"),
    }


def _id_variants(raw: str) -> set[str]:
    variants: set[str] = set()
    current = raw
    for _ in range(4):
        variants.add(current)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return variants


def _formula_aliases(live_props: dict[str, dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for prop_name, prop in live_props.items():
        aliases[prop_name] = prop_name
        prop_id = prop.get("id")
        if not prop_id:
            continue
        pid = str(prop_id)
        for variant in _id_variants(pid):
            aliases[variant] = prop_name
    return aliases


def _normalize_formula_expression(expr: str, aliases: dict[str, str]) -> str:
    def _canonical_prop(token: str) -> str | None:
        for variant in _id_variants(token):
            canonical = aliases.get(variant)
            if canonical:
                return f'prop("{canonical}")'
        return None

    def _replace_prop(match: re.Match[str]) -> str:
        token = match.group(1)
        canonical = _canonical_prop(token)
        if canonical:
            return canonical
        return match.group(0)

    def _replace_block_property(match: re.Match[str]) -> str:
        token = match.group(1)
        canonical = _canonical_prop(token)
        if canonical:
            return canonical
        return match.group(0)

    normalized = _BLOCK_PROPERTY_REF_RE.sub(_replace_block_property, expr)
    return _PROP_REF_RE.sub(_replace_prop, normalized).strip()


def _canonicalize_configured_data_source_ids(
    client: _DataSourceReader,
    configured_ids: dict[str, str],
    *,
    cache: dict[str, dict[str, Any]],
) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for key, raw_id in configured_ids.items():
        if not raw_id:
            canonical[key] = raw_id
            continue
        context = _fetch_data_source_context(client, raw_id, cache=cache)
        canonical[key] = context.get("resolved_data_source_id") or raw_id
    return canonical


def validate_all(
    settings: Settings, client: _DataSourceReader | None = None
) -> list[ValidationReport]:
    """Run validation across every configured data source.

    The caller owns NotionClient lifecycle when it provides one; otherwise we
    create and close one here.
    """
    owned_client: NotionClient | None = None
    if client is None:
        owned_client = NotionClient(settings.notion)
        client = owned_client

    ds_context_cache: dict[str, dict[str, Any]] = {}
    relation_targets = _canonicalize_configured_data_source_ids(
        client,
        settings.notion.data_sources,
        cache=ds_context_cache,
    )

    try:
        reports: list[ValidationReport] = []
        for spec in ALL_DATA_SOURCES:
            configured_ds_id = settings.notion.data_sources.get(spec.key, "")
            ds_id = relation_targets.get(spec.key) or configured_ds_id
            reports.append(
                validate_one(
                    client,
                    spec,
                    ds_id,
                    relation_targets=relation_targets,
                    ds_context_cache=ds_context_cache,
                )
            )
        return reports
    finally:
        if owned_client is not None:
            owned_client.close()


def summarize_reports(reports: list[ValidationReport]) -> str:
    lines = []
    fetch_failed_count = 0
    object_not_found_count = 0
    integration_names: set[str] = set()

    def _extract_hint_data(msg: str) -> None:
        nonlocal object_not_found_count
        if '"code":"object_not_found"' not in msg:
            return
        object_not_found_count += 1

        marker = "-> 404:"
        idx = msg.find(marker)
        if idx == -1:
            return
        payload = msg[idx + len(marker) :].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return

        raw = data.get("message", "")
        token = 'shared with your integration "'
        start = raw.find(token)
        if start == -1:
            return
        start += len(token)
        end = raw.find('"', start)
        if end == -1:
            return
        name = raw[start:end].strip()
        if name:
            integration_names.add(name)

    for r in reports:
        status = "OK" if r.ok else "MISMATCH"
        lines.append(f"[{status}] {r.data_source} ({r.data_source_id or 'missing-id'})")
        for m in r.mismatches:
            target = f"{m.property_name}: " if m.property_name else ""
            lines.append(f"    - {m.code}: {target}{m.message}")
            if m.code == "relation_mismatch":
                details = m.details or {}
                lines.append(
                    "      source_data_source.property: "
                    f"{details.get('source_data_source', m.data_source)}."
                    f"{details.get('property_name', m.property_name or '(unknown)')}"
                )
                lines.append(
                    "      expected_target_data_source_id: "
                    f"{details.get('expected_target_data_source_id') or '(unknown)'}"
                )
                expected_input = details.get("expected_target_input_id")
                expected_resolved = details.get("expected_target_data_source_id")
                if expected_input and expected_input != expected_resolved:
                    lines.append(
                        f"      expected_target_input_id (from NOTION_DS_*): {expected_input}"
                    )
                lines.append(
                    "      live_relation_data_source_id: "
                    f"{details.get('live_relation_data_source_id') or '(missing)'}"
                )
                live_target_id = details.get("live_target_data_source_id")
                if live_target_id and live_target_id != details.get("live_relation_data_source_id"):
                    lines.append(f"      live_target_data_source_id: {live_target_id}")
                if details.get("live_relation_database_id"):
                    lines.append(
                        "      live_relation_database_id: "
                        f"{details.get('live_relation_database_id')}"
                    )
                lines.append(
                    "      expected_target_parent_database_id: "
                    f"{details.get('expected_target_parent_database_id') or '(unknown)'}"
                )
                lines.append(
                    "      live_target_parent_database_id: "
                    f"{details.get('live_target_parent_database_id') or '(unknown)'}"
                )
                same_container = details.get("same_database_container")
                if same_container is True:
                    container_text = "yes"
                elif same_container is False:
                    container_text = "no"
                else:
                    container_text = "unknown"
                lines.append(f"      same_database_container: {container_text}")
            if m.code == "formula_mismatch":
                details = m.details or {}
                if details.get("live_expression"):
                    lines.append(f"      live_formula_expression: {details.get('live_expression')}")
                if details.get("normalized_expected") or details.get("normalized_live"):
                    lines.append(
                        "      normalized_formula_expected: "
                        f"{details.get('normalized_expected') or '(empty)'}"
                    )
                    lines.append(
                        "      normalized_formula_live: "
                        f"{details.get('normalized_live') or '(empty)'}"
                    )
                if details.get("manual_verification_recommended"):
                    lines.append(
                        "      note: Notion formula serialization can differ by internal "
                        "property references; verify manually if this is expected."
                    )
            if m.code == "fetch_failed":
                fetch_failed_count += 1
                _extract_hint_data(m.message)

    if reports and fetch_failed_count == len(reports) and object_not_found_count == len(reports):
        lines.append("")
        lines.append(
            "Likely cause: Notion access mismatch (token/integration share), not schema drift."
        )
        if integration_names:
            names = ", ".join(sorted(integration_names))
            lines.append(f"Notion says to share these databases with integration: {names}")
        lines.append("Checks:")
        lines.append(
            "    - Ensure NOTION_TOKEN belongs to the intended integration for this project."
        )
        lines.append(
            "    - Open each database in Notion -> Share -> Connections and add that integration."
        )
        lines.append(
            "    - Confirm each NOTION_DS_* ID points to an existing database in that workspace."
        )
        lines.append("    - Re-run: python -m job_search_hub.tick validate")
    return "\n".join(lines)
