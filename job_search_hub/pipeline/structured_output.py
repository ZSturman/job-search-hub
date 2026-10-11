"""Post-parse validation helpers for structured model output."""

from __future__ import annotations

from typing import Any


def _expected_set(expected_indexes: range | set[int] | list[int] | tuple[int, ...]) -> set[int]:
    return set(expected_indexes)


def validate_exact_indexes(
    data: Any,
    *,
    array_key: str,
    index_key: str,
    expected_indexes: range | set[int] | list[int] | tuple[int, ...],
) -> None:
    """Require an indexed response array to match the requested index set exactly."""
    expected = _expected_set(expected_indexes)
    if not isinstance(data, dict):
        raise ValueError("response root must be an object")
    rows = data.get(array_key)
    if not isinstance(rows, list):
        raise ValueError(f"response omitted {array_key}")
    seen: set[int] = set()
    duplicates: list[int] = []
    out_of_range: list[int] = []
    malformed = 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        index = row.get(index_key)
        if isinstance(index, bool) or not isinstance(index, int):
            malformed += 1
            continue
        if index in seen:
            duplicates.append(index)
        seen.add(index)
        if index not in expected:
            out_of_range.append(index)
    missing = sorted(expected - seen)
    problems = []
    if malformed:
        problems.append(f"{malformed} malformed item(s)")
    if duplicates:
        problems.append(f"duplicate {index_key}: {sorted(set(duplicates))}")
    if out_of_range:
        problems.append(f"out-of-range {index_key}: {sorted(set(out_of_range))}")
    if missing:
        problems.append(f"missing {index_key}: {missing}")
    if len(rows) != len(expected):
        problems.append(f"expected {len(expected)} item(s), got {len(rows)}")
    if problems:
        raise ValueError("; ".join(problems))


def rows_by_exact_index(
    data: Any,
    *,
    array_key: str,
    index_key: str,
    expected_indexes: range | set[int] | list[int] | tuple[int, ...],
) -> dict[int, dict]:
    """Return response rows keyed by index after exact-index validation."""
    validate_exact_indexes(
        data,
        array_key=array_key,
        index_key=index_key,
        expected_indexes=expected_indexes,
    )
    return {row[index_key]: row for row in data[array_key]}
