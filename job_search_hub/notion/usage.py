"""Schema coverage, column ownership, and bounded writer value contracts."""

from dataclasses import dataclass

from job_search_hub.notion.schema import ALL_DATA_SOURCES

SYSTEM_READ = "system_read"
SYSTEM_WRITE = "system_write"
OPERATOR_INPUT = "operator_input"
OPERATOR_OUTPUT = "operator_output"
FORMULA_ROLLUP = "formula_rollup"
DEPRECATED = "deprecated"


@dataclass(frozen=True)
class UsageIssue:
    data_source: str
    property_name: str
    code: str
    message: str


@dataclass(frozen=True)
class ColumnUsage:
    purposes: tuple[str, ...]
    note: str = ""


COLUMN_USAGE = {
    s.key: {
        p.name: ColumnUsage(
            (SYSTEM_READ, OPERATOR_OUTPUT, FORMULA_ROLLUP)
            if p.type in {"formula", "rollup", "last_edited_time"}
            else (SYSTEM_READ, SYSTEM_WRITE, OPERATOR_INPUT)
        )
        for p in s.properties
    }
    for s in ALL_DATA_SOURCES
}
# Old topic mirrors may remain in imported schemas; the relational model owns them.
for name in ("Qualification Topics", "Hard Qualification Topics", "Preferred Qualification Topics"):
    COLUMN_USAGE["opportunities"][name] = ColumnUsage((SYSTEM_READ, DEPRECATED))


def usage_for(data_source_key, name):
    return COLUMN_USAGE.get(data_source_key, {}).get(name)


def registry_issues():
    issues = []
    for spec in ALL_DATA_SOURCES:
        expected = {p.name for p in spec.properties}
        declared = COLUMN_USAGE.get(spec.key, {})
        for name in expected - declared.keys():
            issues.append(UsageIssue(spec.key, name, "missing_usage", "No ownership declaration"))
        for name in declared.keys() - expected:
            if DEPRECATED not in declared[name].purposes:
                issues.append(
                    UsageIssue(spec.key, name, "unknown_usage", "Not in the active schema")
                )
    return issues


def declared_property_names(data_source_key):
    return set(COLUMN_USAGE.get(data_source_key, {}))


def allowed_select_options(data_source_key, property_name):
    for spec in ALL_DATA_SOURCES:
        if spec.key == data_source_key:
            return next((p.options for p in spec.properties if p.name == property_name), ())
    return ()


def validate_written_properties(data_source_key, properties):
    issues = []
    for name, payload in properties.items():
        usage = usage_for(data_source_key, name)
        if usage is None:
            issues.append(
                UsageIssue(
                    data_source_key,
                    name,
                    "write_unknown_property",
                    "Not declared in the active schema",
                )
            )
            continue
        if SYSTEM_WRITE not in usage.purposes:
            issues.append(
                UsageIssue(
                    data_source_key,
                    name,
                    "write_to_non_output_property",
                    "Computed or deprecated property",
                )
            )
        allowed = allowed_select_options(data_source_key, name)
        values = [payload["select"]["name"]] if payload.get("select") else []
        values += [item["name"] for item in payload.get("multi_select", [])]
        # Only bounded enum fields reject new values; open tags/source identifiers stay extensible.
        if allowed:
            for value in values:
                if value not in allowed:
                    issues.append(
                        UsageIssue(
                            data_source_key,
                            name,
                            "unsupported_written_value",
                            f"Unsupported value {value!r}",
                        )
                    )
    return issues


def usage_issues(data_source_key, properties):
    return [
        UsageIssue(data_source_key, n, "unknown_property", "Not part of the active schema")
        for n in properties
        if n not in COLUMN_USAGE.get(data_source_key, {})
    ]
