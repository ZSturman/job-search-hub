"""Read-only operational audit for Notion tables, columns, and values."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from job_search_hub.config import Settings
from job_search_hub.notion import properties as P
from job_search_hub.notion.client import NotionClient
from job_search_hub.notion.schema import ALL_DATA_SOURCES, DataSourceSpec, PropertySpec
from job_search_hub.notion.usage import (
    COLUMN_USAGE,
    DEPRECATED,
    FORMULA_ROLLUP,
    OPERATOR_INPUT,
    OPERATOR_OUTPUT,
    SYSTEM_READ,
    SYSTEM_WRITE,
    registry_issues,
    usage_for,
)
from job_search_hub.notion.validate import validate_all


@dataclass(frozen=True)
class AuditIssue:
    data_source: str
    code: str
    message: str
    property_name: str = ""
    severity: str = "error"


@dataclass
class TableAudit:
    data_source: str
    rows_sampled: int = 0
    issues: list[AuditIssue] = field(default_factory=list)
    write_only: list[str] = field(default_factory=list)
    read_only: list[str] = field(default_factory=list)
    manual_only: list[str] = field(default_factory=list)


@dataclass
class NotionAudit:
    tables: list[TableAudit] = field(default_factory=list)
    global_issues: list[AuditIssue] = field(default_factory=list)

    @property
    def issues(self) -> list[AuditIssue]:
        result = list(self.global_issues)
        for table in self.tables:
            result.extend(table.issues)
        return result

    def ok(self, *, strict_columns: bool = False) -> bool:
        for issue in self.issues:
            if issue.severity == "error":
                return False
            if strict_columns and issue.code in {"deprecated_live_property", "extra_live_property"}:
                return False
        return True


def _options_from_live_property(prop: dict[str, Any]) -> set[str]:
    prop_type = prop.get("type")
    if prop_type not in {"select", "multi_select", "status"}:
        return set()
    block = prop.get(prop_type) or {}
    return {
        str(option.get("name") or "") for option in block.get("options") or [] if option.get("name")
    }


def _row_values(page: dict[str, Any], spec: PropertySpec) -> Iterable[str]:
    prop = (page.get("properties") or {}).get(spec.name)
    if spec.type == "select":
        value = P.read_select(prop)
        if value:
            yield value
    elif spec.type == "multi_select":
        yield from (value for value in P.read_multi_select(prop) if value)


def _sample_pages(
    notion: NotionClient,
    data_source_id: str,
    *,
    sample_size: int,
    full: bool,
) -> list[dict[str, Any]]:
    if full:
        return list(notion.query_data_source(data_source_id))
    if hasattr(notion, "query_data_source_page"):
        data = notion.query_data_source_page(data_source_id, page_size=max(1, sample_size))
        return list(data.get("results") or [])[:sample_size]
    return list(notion.query_data_source(data_source_id))[:sample_size]


def _table_usage_lists(data_source_key: str) -> tuple[list[str], list[str], list[str]]:
    write_only: list[str] = []
    read_only: list[str] = []
    manual_only: list[str] = []
    for name, usage in sorted(COLUMN_USAGE.get(data_source_key, {}).items()):
        purposes = set(usage.purposes)
        writes = SYSTEM_WRITE in purposes
        reads = SYSTEM_READ in purposes
        output = OPERATOR_OUTPUT in purposes
        manual = OPERATOR_INPUT in purposes and not writes
        if writes and not reads and not output and FORMULA_ROLLUP not in purposes:
            write_only.append(name)
        if reads and not writes and not manual:
            read_only.append(name)
        if manual and not writes:
            manual_only.append(name)
    return write_only, read_only, manual_only


def _audit_live_columns(
    spec: DataSourceSpec,
    live_props: dict[str, dict[str, Any]],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    expected = {prop.name for prop in spec.properties}
    declared = set(COLUMN_USAGE.get(spec.key, {}))
    for name in sorted(set(live_props) - declared):
        severity = "warning" if name in expected else "error"
        issues.append(
            AuditIssue(
                data_source=spec.key,
                property_name=name,
                code="extra_live_property",
                severity=severity,
                message=f"{spec.key}.{name} exists in Notion but is not declared in usage registry",
            )
        )
    for name in sorted(declared - expected):
        usage = usage_for(spec.key, name)
        if usage and DEPRECATED in usage.purposes:
            continue
        issues.append(
            AuditIssue(
                data_source=spec.key,
                property_name=name,
                code="usage_without_schema",
                message=f"{spec.key}.{name} is declared in usage registry but not schema",
            )
        )
    for name in sorted(declared & set(live_props)):
        usage = usage_for(spec.key, name)
        if usage and DEPRECATED in usage.purposes:
            issues.append(
                AuditIssue(
                    data_source=spec.key,
                    property_name=name,
                    code="deprecated_live_property",
                    severity="warning",
                    message=f"{spec.key}.{name} is marked deprecated but still exists in Notion",
                )
            )
    return issues


def _audit_options(
    spec: DataSourceSpec,
    live_props: dict[str, dict[str, Any]],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    for prop_spec in spec.properties:
        if not prop_spec.options:
            continue
        live = live_props.get(prop_spec.name)
        if not live:
            continue
        extras = sorted(_options_from_live_property(live) - set(prop_spec.options))
        if extras:
            issues.append(
                AuditIssue(
                    data_source=spec.key,
                    property_name=prop_spec.name,
                    code="extra_options",
                    severity="warning",
                    message=(f"{spec.key}.{prop_spec.name} has extra live option(s): {extras}"),
                )
            )
    return issues


def _audit_sampled_values(
    spec: DataSourceSpec,
    pages: list[dict[str, Any]],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    for prop_spec in spec.properties:
        if not prop_spec.options:
            continue
        allowed = set(prop_spec.options)
        unexpected: set[str] = set()
        for page in pages:
            unexpected.update(
                value for value in _row_values(page, prop_spec) if value not in allowed
            )
        if unexpected:
            issues.append(
                AuditIssue(
                    data_source=spec.key,
                    property_name=prop_spec.name,
                    code="unexpected_row_value",
                    message=(
                        f"{spec.key}.{prop_spec.name} contains unsupported sampled value(s): "
                        f"{sorted(unexpected)}"
                    ),
                )
            )
    return issues


def run_notion_audit(
    settings: Settings,
    *,
    notion: NotionClient,
    sample_size: int = 25,
    full: bool = False,
) -> NotionAudit:
    audit = NotionAudit()
    for issue in registry_issues():
        audit.global_issues.append(
            AuditIssue(
                data_source=issue.data_source,
                property_name=issue.property_name,
                code=issue.code,
                message=issue.message,
            )
        )

    schema_reports = validate_all(settings, client=notion)
    schema_by_key = {report.data_source: report for report in schema_reports}
    for report in schema_reports:
        for mismatch in report.mismatches:
            audit.global_issues.append(
                AuditIssue(
                    data_source=report.data_source,
                    property_name=mismatch.property_name or "",
                    code=f"schema_{mismatch.code}",
                    message=mismatch.message,
                )
            )

    for spec in ALL_DATA_SOURCES:
        table = TableAudit(data_source=spec.key)
        audit.tables.append(table)
        report = schema_by_key.get(spec.key)
        ds_id = settings.notion.data_sources.get(spec.key, "")
        if not ds_id or (report is not None and not report.data_source_id):
            continue
        try:
            live = notion.get_data_source(ds_id)
            live_props = live.get("properties") or {}
            table.issues.extend(_audit_live_columns(spec, live_props))
            table.issues.extend(_audit_options(spec, live_props))
            pages = _sample_pages(notion, ds_id, sample_size=sample_size, full=full)
            table.rows_sampled = len(pages)
            table.issues.extend(_audit_sampled_values(spec, pages))
            table.write_only, table.read_only, table.manual_only = _table_usage_lists(spec.key)
        except Exception as exc:
            table.issues.append(
                AuditIssue(
                    data_source=spec.key,
                    code="audit_fetch_failed",
                    message=f"{spec.key} audit failed: {exc}",
                )
            )
    return audit


def format_notion_audit(audit: NotionAudit, *, strict_columns: bool = False) -> str:
    lines = [
        "Notion operational audit:",
        f"status={'OK' if audit.ok(strict_columns=strict_columns) else 'ISSUES'}",
    ]
    for issue in audit.global_issues:
        lines.append(
            f"[{issue.severity.upper()}] {issue.data_source}"
            f"{'.' + issue.property_name if issue.property_name else ''}: "
            f"{issue.code}: {issue.message}"
        )
    for table in audit.tables:
        errors = sum(1 for issue in table.issues if issue.severity == "error")
        warnings = sum(1 for issue in table.issues if issue.severity == "warning")
        lines.append(
            f"[TABLE] {table.data_source}: sampled={table.rows_sampled} "
            f"errors={errors} warnings={warnings} "
            f"manual_only={len(table.manual_only)} read_only={len(table.read_only)} "
            f"write_only={len(table.write_only)}"
        )
        for issue in table.issues:
            lines.append(
                f"  [{issue.severity.upper()}] "
                f"{issue.property_name or table.data_source}: "
                f"{issue.code}: {issue.message}"
            )
        if table.manual_only:
            lines.append("  manual-only: " + ", ".join(table.manual_only[:20]))
        if table.write_only:
            lines.append("  written-not-read: " + ", ".join(table.write_only[:20]))
        if table.read_only:
            lines.append("  read-not-written: " + ", ".join(table.read_only[:20]))
    return "\n".join(lines)
