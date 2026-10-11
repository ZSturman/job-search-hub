"""Helpers for reading and writing Notion properties.

Notion's payload shape is verbose; these helpers keep the rest of the
codebase concise. All readers tolerate missing/null properties.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# ---------- readers ----------


def read_title(prop: dict | None) -> str:
    if not prop:
        return ""
    parts = prop.get("title") or []
    return "".join(
        p.get("plain_text", "") or ((p.get("text") or {}).get("content") or "") for p in parts
    ).strip()


def read_text(prop: dict | None) -> str:
    if not prop:
        return ""
    parts = prop.get("rich_text") or []
    return "".join(
        p.get("plain_text", "") or ((p.get("text") or {}).get("content") or "") for p in parts
    ).strip()


def read_url(prop: dict | None) -> str:
    if not prop:
        return ""
    return prop.get("url") or ""


def read_email(prop: dict | None) -> str:
    if not prop:
        return ""
    return prop.get("email") or ""


def read_select(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return (sel or {}).get("name") or ""


def read_status(prop: dict | None) -> str:
    if not prop:
        return ""
    sel = prop.get("status")
    return (sel or {}).get("name") or ""


def read_multi_select(prop: dict | None) -> list[str]:
    if not prop:
        return []
    return [o.get("name", "") for o in (prop.get("multi_select") or [])]


def read_checkbox(prop: dict | None) -> bool:
    if not prop:
        return False
    return bool(prop.get("checkbox"))


def read_number(prop: dict | None) -> float | None:
    if not prop:
        return None
    return prop.get("number")


def read_date(prop: dict | None) -> str | None:
    if not prop:
        return None
    d = prop.get("date")
    if not d:
        return None
    return d.get("start")


def read_relation_ids(prop: dict | None) -> list[str]:
    if not prop:
        return []
    return [r.get("id", "") for r in (prop.get("relation") or [])]


def read_formula_value(prop: dict | None) -> Any:
    if not prop:
        return None
    f = prop.get("formula") or {}
    t = f.get("type")
    if t is None:
        return None
    return f.get(t)


def read_rollup_number(prop: dict | None) -> float | None:
    if not prop:
        return None
    r = prop.get("rollup") or {}
    return r.get("number")


# ---------- writers ----------


def title(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text or ""}}]}


def rich_text(text: str | None) -> dict:
    if not text:
        return {"rich_text": []}
    # Notion limits a single rich_text chunk to 2000 characters
    chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
    return {"rich_text": [{"type": "text", "text": {"content": c}} for c in chunks]}


def url(value: str | None) -> dict:
    return {"url": value or None}


def email(value: str | None) -> dict:
    return {"email": value or None}


def select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def multi_select(names: list[str]) -> dict:
    return {"multi_select": [{"name": n} for n in names if n]}


def checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


def number(value: float | int | None) -> dict:
    return {"number": value}


def date_property(start: str | date | datetime | None, end: str | None = None) -> dict:
    if start is None:
        return {"date": None}
    if isinstance(start, (date, datetime)):
        start = start.isoformat()
    payload: dict[str, Any] = {"start": start}
    if end:
        payload["end"] = end
    return {"date": payload}


def relation(ids: list[str]) -> dict:
    return {"relation": [{"id": i} for i in ids if i]}
