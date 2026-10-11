"""Structured event stream for live operator visibility.

SQLite keeps the durable audit tables. This module writes compact JSONL events
that are easy to tail from the CLI while a tick is running.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _safe_payload(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _safe_payload(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_payload(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def emit_event(
    log_dir: Path,
    *,
    event: str,
    stage: str,
    message: str,
    payload: dict | None = None,
) -> None:
    """Append one JSON event to ``logs/events.jsonl``.

    This helper intentionally never raises; event logging should not break the
    job-search pipeline.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "events.jsonl"
        row = {
            "occurred_at": datetime.now(UTC).isoformat(),
            "event": event,
            "stage": stage,
            "message": message,
            "payload": _safe_payload(payload or {}),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
    except Exception:
        return


def read_events(log_dir: Path, *, limit: int = 50) -> list[dict]:
    path = log_dir / "events.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
