"""Private document catalog, factual review queue, and recommendations only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from job_search_hub.notion import properties as P
from job_search_hub.storage import app_paths, private_write, read_json, write_json


def catalog() -> list[dict]:
    return read_json(app_paths().data / "resumes/index.json", [])


def add_resume(path: Path, title: str, tags=()) -> dict:
    path = path.expanduser().resolve()
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise ValueError("Choose an existing PDF file.")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("The file is not a PDF.")
    if not title.strip():
        raise ValueError("Give the resume a descriptive title.")
    doc_id = hashlib.sha256(data).hexdigest()
    rows = catalog()
    row = next((r for r in rows if r["id"] == doc_id), None)
    if row is None:
        action = "created"
        row = {
            "id": doc_id,
            "title": title.strip(),
            "tags": list(tags),
            "file": f"{doc_id}/resume.pdf",
        }
        private_write(app_paths().data / "resumes" / row["file"], data)
        rows.append(row)
    else:
        action = "updated"
        row.update(title=title.strip(), tags=list(tags))
    write_json(app_paths().data / "resumes/index.json", rows)
    return {**row, "action": action}


def sync_library(notion, settings):
    ds = settings.notion.data_sources["resume_library"]
    existing = {
        P.read_text(p["properties"].get("Document ID")): p for p in notion.query_data_source(ds)
    }
    for row in catalog():
        props = {
            "Resume": P.title(row["title"]),
            "Document ID": P.rich_text(row["id"]),
            "Tags": P.multi_select(row["tags"]),
            "Availability": P.select(
                "Available" if (app_paths().data / "resumes" / row["file"]).is_file() else "Missing"
            ),
        }
        old = existing.get(row["id"])
        if old:
            update_changed(notion, old, props)
        else:
            notion.create_page(parent={"data_source_id": ds}, properties=props)


def update_changed(notion, page, props):
    old = page.get("properties", {})

    def value(p):
        typ = p.get("type") or next(
            (
                k
                for k in (
                    "title",
                    "rich_text",
                    "select",
                    "multi_select",
                    "checkbox",
                    "number",
                    "relation",
                    "date",
                    "url",
                )
                if k in p
            ),
            "",
        )
        if typ in {"title", "rich_text"}:
            return P.read_title(p) if typ == "title" else P.read_text(p)
        if typ == "select":
            return P.read_select(p)
        if typ == "multi_select":
            return P.read_multi_select(p)
        if typ == "relation":
            return P.read_relation_ids(p)
        return p.get(typ)

    changed = {k: v for k, v in props.items() if value(v) != value(old.get(k, {}))}
    if changed:
        notion.update_page(page["id"], changed)


def stage_facts(doc_id, *, client=None, router=None):
    matches = [r for r in catalog() if r["id"].startswith(doc_id)]
    if len(matches) != 1:
        raise ValueError("Use an unambiguous document ID from jobhub resumes list.")
    row = matches[0]
    from job_search_hub.pipeline.resume_facts_sync import (
        _deterministic_facts_from_text,
        extract_facts_from_text,
        extract_pdf_text,
    )

    path = app_paths().data / "resumes" / row["file"]
    text = extract_pdf_text(path)
    if client is not None and router is not None:
        facts, _ = extract_facts_from_text(
            resume_title=row["title"], pdf_path=path, text=text, router=router, client=client
        )
    else:
        facts = _deterministic_facts_from_text(resume_title=row["title"], pdf_path=path, text=text)
    queue = read_json(app_paths().data / "profile/pending.json", [])
    known = {r["id"] for r in queue}
    for fact in facts:
        fid = hashlib.sha256(
            (row["id"] + fact.fact_type + fact.value + fact.evidence).encode()
        ).hexdigest()
        if fid not in known:
            queue.append(
                {
                    "id": fid,
                    "document_id": row["id"],
                    "value": fact.value,
                    "evidence": fact.evidence,
                    "fact_type": fact.fact_type,
                    "category": fact.category,
                    "sensitive": fact.sensitive,
                    "approved": False,
                }
            )
    write_json(app_paths().data / "profile/pending.json", queue)
    return [r for r in queue if r["document_id"] == row["id"]]


def approve_fact(notion, settings, fid):
    queue = read_json(app_paths().data / "profile/pending.json", [])
    matches = [r for r in queue if r["id"].startswith(fid)]
    if len(matches) != 1:
        raise ValueError("Use an unambiguous fact ID from jobhub profile pending.")
    row = matches[0]
    ds = settings.notion.data_sources["resume_facts"]
    fact_id = "document:" + row["id"]
    existing = list(
        notion.query_data_source(
            ds, filter_={"property": "Fact ID", "rich_text": {"equals": fact_id}}
        )
    )
    props = {
        "Fact": P.title(row["value"]),
        "Fact ID": P.rich_text(fact_id),
        "Value": P.rich_text(row["value"]),
        "Fact Type": P.select(row["fact_type"]),
        "Category": P.select(row["category"]),
        "Verified": P.checkbox(True),
        "Approved for Profile": P.checkbox(True),
        "Approved for Applications": P.checkbox(True),
        "Sensitive": P.checkbox(row["sensitive"]),
        "Active": P.checkbox(True),
        "Source": P.rich_text("document:" + row["document_id"]),
        "Notes": P.rich_text("Approved evidence: " + row["evidence"]),
    }
    if existing:
        update_changed(notion, existing[0], props)
    else:
        notion.create_page(parent={"data_source_id": ds}, properties=props)
    row["approved"] = True
    write_json(app_paths().data / "profile/pending.json", queue)


def suggest(notion, settings, *, client=None, router=None, store=None, preview=False):
    pages = list(
        notion.query_data_source(
            settings.notion.data_sources["opportunities"],
            filter_={
                "or": [
                    {"property": "My Decision", "select": {"equals": "Shortlist"}},
                    {"property": "My Decision", "select": {"equals": "Apply"}},
                ]
            },
        )
    )
    jobs = [
        {
            "id": p["id"],
            "role": P.read_text(p["properties"].get("Role Title")),
            "description": P.read_text(p["properties"].get("Description"))[:8000],
        }
        for p in pages
        if P.read_select(p["properties"].get("Status"))
        not in {"Submitted", "Interviewing", "Offer", "Closed", "Rejected"}
    ]
    if not jobs:
        return []
    from job_search_hub.pipeline.profile import load_facts

    facts = [
        {"value": f.value, "type": f.fact_type}
        for f in load_facts(notion, settings)
        if f.verified and f.approved_for_profile and not f.sensitive
    ]
    files = [
        {k: v for k, v in r.items() if k != "file"}
        for r in catalog()
        if (app_paths().data / "resumes" / r["file"]).is_file()
    ]
    key = hashlib.sha256(
        json.dumps(
            {
                "jobs": jobs,
                "files": files,
                "facts": facts,
                "model": router.get("primary") if router else None,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cache = read_json(app_paths().data / "recommendations.json", {})
    if cache.get("key") == key:
        groups = cache["groups"]
    elif client is None or router is None:
        groups = []
        for job in jobs:
            match = next(
                (
                    r
                    for r in files
                    if r["title"].casefold() in job["role"].casefold()
                    or any(t.casefold() in job["role"].casefold() for t in r["tags"])
                ),
                None,
            )
            title = match["title"] if match else (job["role"] or "General application")
            group = next((g for g in groups if g["title"] == title), None)
            if not group:
                group = {
                    "title": title,
                    "document_id": match["id"] if match else "",
                    "job_ids": [],
                    "rationale": "Existing library match."
                    if match
                    else (
                        "Consider a reusable resume for this role family; create it "
                        "outside this tool."
                    ),
                }
                groups.append(group)
            group["job_ids"].append(job["id"])
    else:
        schema = {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 120,
                                "pattern": r"^[^\r\n]*$",
                            },
                            "document_id": {"type": "string"},
                            "job_ids": {"type": "array", "items": {"type": "string"}},
                            "rationale": {
                                "type": "string",
                                "maxLength": 400,
                                "pattern": r"^[^\r\n]*$",
                            },
                        },
                        "required": ["title", "document_id", "job_ids", "rationale"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["groups"],
            "additionalProperties": False,
        }
        payload = json.loads(
            client.chat(
                model=router.get("primary"),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Recommend reusable resume TYPES only. Prefer an existing document"
                            " ID where suitable. Return a short title, matching job IDs, and a"
                            " brief reason. Never draft resume content, sections, bullet "
                            "points, cover letters, or document-building instructions. Cover "
                            "each job exactly once. Use an empty document_id for a proposed "
                            "type."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"jobs": jobs, "existing_resumes": files, "approved_facts": facts}
                        ),
                    },
                ],
                json_schema=schema,
                stage="resume_suggestions",
                role="primary",
                task="recommend_types",
            )
        )
        from jsonschema import ValidationError, validate

        try:
            validate(payload, schema)
        except ValidationError:
            raise ValueError(
                "Invalid recommendation metadata; selections were preserved."
            ) from None
        groups = payload["groups"]
    assigned = [i for g in groups for i in g["job_ids"]]
    if sorted(assigned) != sorted(j["id"] for j in jobs):
        raise ValueError(
            "Model recommendation coverage is invalid; existing selections were preserved."
        )
    ids = {r["id"] for r in files}
    if any(g["document_id"] and g["document_id"] not in ids for g in groups):
        raise ValueError("Model selected an unknown document; existing selections were preserved.")
    combined = {}
    for group in groups:
        identity = group["document_id"] or "suggestion:" + group["title"].casefold()
        if identity not in combined:
            combined[identity] = dict(group, job_ids=[])
        combined[identity]["job_ids"].extend(group["job_ids"])
    groups = list(combined.values())
    if not preview:
        write_json(app_paths().data / "recommendations.json", {"key": key, "groups": groups})
        ds = settings.notion.data_sources["resume_library"]
        existing = {
            P.read_text(p["properties"].get("Document ID")): p for p in notion.query_data_source(ds)
        }
        for g in groups:
            gid = (
                g["document_id"]
                or "suggestion:" + hashlib.sha256(g["title"].casefold().encode()).hexdigest()
            )
            props = {
                "Resume": P.title(g["title"]),
                "Document ID": P.rich_text(gid),
                "Jobs": P.relation(g["job_ids"]),
                "Recommendation": P.rich_text(g["rationale"]),
                "Availability": P.select("Available" if g["document_id"] else "Suggested"),
            }
            if gid in existing:
                update_changed(notion, existing[gid], props)
            else:
                existing[gid] = notion.create_page(parent={"data_source_id": ds}, properties=props)
            for job_id in g["job_ids"]:
                page = next(p for p in pages if p["id"] == job_id)
                update_changed(notion, page, {"Suggested Resume": P.rich_text(g["title"])})
    return groups
