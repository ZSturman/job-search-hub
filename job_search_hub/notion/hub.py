"""Scoped hub discovery, shared editable preferences, and template provisioning."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from job_search_hub.config import SearchProfile, SearchTermSettings, Settings
from job_search_hub.notion import properties as P
from job_search_hub.notion.schema import ALL_DATA_SOURCES, DataSourceSpec, PropertySpec
from job_search_hub.storage import private_write

SEARCH_PROFILES = DataSourceSpec(
    "search_profiles",
    "Search Profiles",
    "Profile",
    (
        PropertySpec("Profile", "title"),
        PropertySpec("Enabled", "checkbox"),
        *(
            PropertySpec(n, "multi_select")
            for n in ("Role Terms", "Skill Terms", "Mission Terms", "Excluded Terms", "Languages")
        ),
        PropertySpec("Location", "rich_text"),
        PropertySpec("Remote Only", "checkbox"),
        PropertySpec("Max New Per Run", "number"),
        PropertySpec("Minimum Annual Pay", "number"),
        PropertySpec("Minimum Hourly Pay", "number"),
    ),
)
SOURCES = DataSourceSpec(
    "sources",
    "Sources",
    "Source",
    (
        PropertySpec("Source", "title"),
        PropertySpec("Type", "rich_text"),
        PropertySpec("Target", "rich_text"),
        PropertySpec("Enabled", "checkbox"),
        PropertySpec("Cadence", "select", options=("Daily", "Weekly", "Monthly", "Manual")),
        PropertySpec("Status", "rich_text"),
        PropertySpec("Last Checked", "date"),
        PropertySpec("Next Check", "date"),
        PropertySpec("Last Error", "rich_text"),
    ),
)
RESUME_LIBRARY = DataSourceSpec(
    "resume_library",
    "Resume Library",
    "Resume",
    (
        PropertySpec("Resume", "title"),
        PropertySpec("Document ID", "rich_text"),
        PropertySpec("Tags", "multi_select"),
        PropertySpec("Availability", "select", options=("Available", "Suggested", "Missing")),
        PropertySpec("Recommendation", "rich_text"),
        PropertySpec("Jobs", "relation", relation_target="opportunities"),
    ),
)
HUB_SPECS = tuple(
    replace(s, properties=s.properties + (PropertySpec("Import ID", "rich_text", required=False),))
    for s in ALL_DATA_SOURCES + (SEARCH_PROFILES, SOURCES, RESUME_LIBRARY)
)
STEPS = (
    (
        "1 — My search",
        "Edit profiles, sources, and scoring rules.",
        ("search_profiles", "sources", "scoring_criteria"),
    ),
    ("2 — Review jobs", "Review matches and choose Shortlist, Apply, or Skip.", ("opportunities",)),
    (
        "3 — Resolve questions",
        "Approve reusable answers and review their supporting evidence.",
        ("answer_bank", "qualification_requirements", "resume_facts"),
    ),
    (
        "4 — Apply",
        "Apply manually on the employer site. Documents and answers are advisory tasks.",
        ("applications", "resume_library"),
    ),
    (
        "5 — Track progress",
        "Record Submitted, Interviewing, Offer, or an outcome, and add notes.",
        ("applications", "organizations"),
    ),
)


def page_id(value: str) -> str:
    matches = re.findall(
        r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value.split("?")[0]
    )
    if not matches:
        raise ValueError("Enter a Notion page URL or UUID.")
    return str(UUID(matches[-1]))


def children(notion, block_id):
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        result = notion._request("GET", f"/blocks/{block_id}/children", params=params)
        yield from result.get("results", [])
        if not result.get("has_more"):
            break
        cursor = result["next_cursor"]


def discover(notion, root: str) -> tuple[dict[str, str], dict[str, str]]:
    """Search only descendants of the chosen root, never the entire workspace."""
    pending = [page_id(root)]
    seen = set()
    databases = {}
    sources = {}
    names = {s.title: s for s in HUB_SPECS}
    while pending:
        parent = pending.pop()
        if parent in seen:
            continue
        seen.add(parent)
        if len(seen) > 200:
            raise ValueError("Hub contains too many nested pages; select a dedicated root.")
        for block in children(notion, parent):
            kind = block.get("type")
            if kind == "child_database":
                title = (block.get(kind) or {}).get("title", "")
                if title not in names:
                    continue
                spec = names[title]
                if spec.key in sources:
                    raise ValueError(f"Multiple {title} databases below the root.")
                db = notion._request("GET", f"/databases/{block['id']}")
                candidates = db.get("data_sources", [])
                if len(candidates) != 1:
                    raise ValueError(f"{title} must have exactly one template data source.")
                databases[spec.key] = block["id"]
                sources[spec.key] = candidates[0]["id"]
            elif (
                kind == "child_page"
                or block.get("has_children")
                and kind not in {"child_database", "link_to_page"}
            ):
                if (block.get("child_page") or {}).get("title") not in {step[0] for step in STEPS}:
                    pending.append(block["id"])
    return sources, databases


def bind(settings: Settings, notion, root: str) -> Settings:
    ids, _ = discover(notion, root)
    missing = [s.title for s in HUB_SPECS if s.key not in ids]
    if missing:
        raise ValueError(
            "The hub is missing: "
            + ", ".join(missing)
            + ". Duplicate a complete template or run jobhub setup --create-template."
        )
    return replace(settings, notion=replace(settings.notion, data_sources=ids))


def snapshot(
    settings: Settings, notion, *, preview=False, due_only=True
) -> tuple[Settings, list[dict]]:
    profiles = []
    for page in notion.query_data_source(settings.notion.data_sources["search_profiles"]):
        p = page["properties"]
        if not P.read_checkbox(p.get("Enabled")):
            continue
        if any(
            (P.read_number(p.get(field)) or 0) < 0
            for field in ("Max New Per Run", "Minimum Annual Pay", "Minimum Hourly Pay")
        ):
            raise ValueError("Search profile limits must be non-negative. Edit 1 — My search.")
        cap = P.read_number(p.get("Max New Per Run"))
        if cap is not None and cap < 0:
            raise ValueError("Profile intake limits must be non-negative.")
        profiles.append(
            SearchProfile(
                P.read_title(p.get("Profile")),
                P.read_multi_select(p.get("Role Terms")),
                int(cap if cap is not None else 50),
                skill_terms=P.read_multi_select(p.get("Skill Terms")),
                mission_terms=P.read_multi_select(p.get("Mission Terms")),
                excluded_terms=P.read_multi_select(p.get("Excluded Terms")),
                location=P.read_text(p.get("Location")),
                allowed_languages=P.read_multi_select(p.get("Languages")) or ["en"],
                remote_only=P.read_checkbox(p.get("Remote Only")),
                min_annual=P.read_number(p.get("Minimum Annual Pay")) or 0,
                min_hourly=P.read_number(p.get("Minimum Hourly Pay")) or 0,
            )
        )

    def flatten(attr):
        return list(dict.fromkeys(t for p in profiles for t in getattr(p, attr)))

    terms = SearchTermSettings(
        flatten("role_terms"), flatten("skill_terms"), flatten("mission_terms"), tuple(profiles)
    )
    rows = list(notion.query_data_source(settings.notion.data_sources["sources"]))
    targets = {}
    enabled = []
    now = datetime.now(UTC)
    for row in rows:
        p = row["properties"]
        kind = P.read_text(p.get("Type"))
        target = P.read_text(p.get("Target"))
        if not P.read_checkbox(p.get("Enabled")) or P.read_select(p.get("Cadence")) == "Manual":
            continue
        next_check = P.read_date(p.get("Next Check"))
        if next_check and due_only:
            due = datetime.fromisoformat(next_check.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=UTC)
            if due > now:
                continue
        if kind == "careers":
            kind = "direct_discovery"
        if kind == "manual":
            continue
        enabled.append(kind)
        targets.setdefault(kind, []).append(target)
    updates = {}
    for kind, field in [
        ("greenhouse", "greenhouse_boards"),
        ("lever", "lever_companies"),
        ("ashby", "ashby_boards"),
        ("recruitee", "recruitee_companies"),
        ("smartrecruiters", "smartrecruiters_companies"),
    ]:
        updates[field] = [t for t in targets.get(kind, []) if t]
    # Derived runtime files are snapshots, never a competing editable configuration.
    rss = settings.data_dir / "runtime/rss_feeds.txt"
    watch = settings.data_dir / "runtime/watchlist.txt"
    for path, values in [
        (rss, targets.get("rss", [])),
        (watch, targets.get("direct_discovery", []) + targets.get("watchlist", [])),
    ]:
        if not preview:
            private_write(path, "\n".join(values) + "\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(values) + "\n")
    updates["usajobs_keywords"] = list(terms.role_terms)
    if targets.get("adzuna") and targets["adzuna"][0]:
        updates["adzuna_country"] = targets["adzuna"][0]
    updates.update(
        rss_feeds_file=rss, watchlist_file=watch, watchlist_urls=targets.get("direct_discovery", [])
    )
    if len(profiles) == 1:
        updates.update(
            adzuna_location=profiles[0].location,
            jooble_location=profiles[0].location,
            usajobs_location=profiles[0].location,
            usajobs_remote=profiles[0].remote_only,
        )
    return replace(
        settings,
        search_terms=terms,
        adapters=replace(settings.adapters, **updates),
        enabled_adapters=tuple(dict.fromkeys(enabled)),
        upwork=replace(settings.upwork, enabled="upwork" in enabled),
        imap=replace(settings.imap, alert_senders=[t for t in targets.get("imap", []) if t]),
        discovery=replace(
            settings.discovery, allowed_languages=flatten("allowed_languages") or ["en"]
        ),
    ), rows


def text_block(kind, text):
    return {
        "object": "block",
        "type": kind,
        kind: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _property(spec, ids, *, relations=True):
    if spec.type == "relation":
        return (
            {"relation": {"data_source_id": ids[spec.relation_target], "single_property": {}}}
            if relations
            else None
        )
    if spec.type == "rollup":
        return (
            {
                "rollup": {
                    "relation_property_name": spec.rollup_source[0],
                    "rollup_property_name": spec.rollup_source[1],
                    "function": "checked",
                }
            }
            if relations
            else None
        )
    if spec.type == "formula":
        return {"formula": {"expression": spec.formula}}
    if spec.type in {"select", "multi_select"}:
        return {spec.type: {"options": [{"name": n} for n in spec.options]}}
    return {spec.type: {}}


def provision(notion, root: str) -> dict[str, str]:
    """Provision a fresh hub under an explicitly supplied parent. Restartable."""
    root = page_id(root)
    blocks = list(children(notion, root))
    marker = "Job Search Hub: next actions"
    if blocks and not any(marker in str(b) for b in blocks):
        raise ValueError(
            "Template creation requires an empty page. Create a new private "
            "Job Search Hub page; existing pages are preserved."
        )
    if not blocks:
        notion._request(
            "PATCH",
            f"/blocks/{root}/children",
            json={
                "children": [
                    text_block("heading_2", marker),
                    text_block(
                        "paragraph",
                        (
                            "Set preferences → discover jobs → review matches → resolve "
                            "questions → apply manually → track outcomes."
                        ),
                    ),
                    text_block(
                        "paragraph",
                        "Last refresh: not yet run. Run jobhub run after editing 1 — My search.",
                    ),
                ]
            },
        )
    data_page = next(
        (
            b["id"]
            for b in children(notion, root)
            if b.get("child_page", {}).get("title") == "Data & diagnostics"
        ),
        None,
    )
    if data_page is None:
        data_page = notion._request(
            "POST",
            "/pages",
            json={
                "parent": {"page_id": root},
                "properties": {"title": P.title("Data & diagnostics")},
                "children": [
                    text_block(
                        "paragraph",
                        (
                            "Full databases and advanced settings. Use the numbered pages for "
                            "everyday work."
                        ),
                    )
                ],
            },
        )["id"]
    ids, dbs = discover(notion, root)
    for spec in HUB_SPECS:
        if spec.key in ids:
            continue
        props = {
            p.name: v
            for p in spec.properties
            if (v := _property(p, ids, relations=False)) is not None
        }
        db = notion._request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": data_page},
                "title": [{"type": "text", "text": {"content": spec.title}}],
                "initial_data_source": {"properties": props},
                "is_inline": False,
            },
        )
        dbs[spec.key] = db["id"]
        ids[spec.key] = db["data_sources"][0]["id"]
    for spec in HUB_SPECS:
        props = {p.name: _property(p, ids) for p in spec.properties if p.type == "relation"}
        if props:
            notion.update_data_source(ids[spec.key], properties=props)
    for spec in HUB_SPECS:
        props = {p.name: _property(p, ids) for p in spec.properties if p.type == "rollup"}
        if props:
            notion.update_data_source(ids[spec.key], properties=props)
    current = {b.get("child_page", {}).get("title") for b in children(notion, root)}
    for title, description, keys in STEPS:
        if title in current:
            continue
        content = [text_block("paragraph", description)]
        for key in keys:
            content.append(
                {
                    "object": "block",
                    "type": "link_to_page",
                    "link_to_page": {"type": "database_id", "database_id": dbs[key]},
                }
            )
        notion._request(
            "POST",
            "/pages",
            json={
                "parent": {"page_id": root},
                "properties": {"title": P.title(title)},
                "children": content,
            },
        )
    existing_blocks = list(children(notion, root))
    if not any("Job Search Hub: next actions" in str(b) for b in existing_blocks):
        notion._request(
            "PATCH",
            f"/blocks/{root}/children",
            json={
                "children": [
                    text_block("heading_2", "Job Search Hub: next actions"),
                    text_block(
                        "paragraph",
                        (
                            "Start at 1 — My search, then run jobhub run. Keep the computer "
                            "running for scheduled refreshes. Use Notion decisions to choose "
                            "your next action."
                        ),
                    ),
                    text_block(
                        "paragraph",
                        (
                            "Last refresh: not yet run. Connections and queue counts appear "
                            "here after refresh."
                        ),
                    ),
                ]
            },
        )
    # Safe starter records contain no personal facts or search assumptions.
    for key, props in [
        (
            "search_profiles",
            {
                "Profile": P.title("My search"),
                "Enabled": P.checkbox(True),
                "Languages": P.multi_select(["en"]),
                "Max New Per Run": P.number(50),
            },
        ),
        (
            "sources",
            {
                "Source": P.title("Remotive"),
                "Type": P.rich_text("remotive"),
                "Enabled": P.checkbox(False),
                "Cadence": P.select("Daily"),
            },
        ),
    ]:
        if not next(iter(notion.query_data_source(ids[key])), None):
            notion.create_page(parent={"data_source_id": ids[key]}, properties=props)
    seed_criteria(notion, ids["scoring_criteria"])
    from job_search_hub.notion.views import provision_views

    provision_views(notion, root, ids)
    pages = {
        b.get("child_page", {}).get("title"): b["id"]
        for b in children(notion, root)
        if b.get("type") == "child_page"
    }
    for index, (title, _, _) in enumerate(STEPS):
        pid = pages[title]
        if not any("Continue the workflow" in str(b) for b in children(notion, pid)):
            links = [text_block("heading_3", "Continue the workflow")]
            if index:
                links.append(
                    {
                        "object": "block",
                        "type": "link_to_page",
                        "link_to_page": {"type": "page_id", "page_id": pages[STEPS[index - 1][0]]},
                    }
                )
            if index + 1 < len(STEPS):
                links.append(
                    {
                        "object": "block",
                        "type": "link_to_page",
                        "link_to_page": {"type": "page_id", "page_id": pages[STEPS[index + 1][0]]},
                    }
                )
            notion._request("PATCH", f"/blocks/{pid}/children", json={"children": links})
    return ids


def seed_criteria(notion, ds):
    existing = {P.read_text(row["properties"].get("Key")) for row in notion.query_data_source(ds)}
    for key, title, method in (
        ("role_skill_fit", "Role and skill fit", "LLM"),
        ("qualification_fit", "Qualification fit", "LLM"),
    ):
        if key in existing:
            continue
        notion.create_page(
            parent={"data_source_id": ds},
            properties={
                "Criterion": P.title(title),
                "Key": P.rich_text(key),
                "Category": P.select("Skills"),
                "Active": P.checkbox(True),
                "Weight": P.number(1),
                "Direction": P.select("Positive"),
                "Evaluation Method": P.select(method),
                "Instructions": P.rich_text(
                    "Assess the job against approved profile facts and stated "
                    "interests. Missing evidence is unknown. Do not invent experience "
                    "or qualifications."
                ),
                "Lane Override": P.select("None"),
            },
        )


def update_dashboard(notion, root, summary):
    value = "Job Search Hub: last refresh\n" + summary
    for block in children(notion, page_id(root)):
        if block.get("type") == "paragraph" and (
            "Last refresh:" in str(block) or "Job Search Hub: last refresh" in str(block)
        ):
            notion._request(
                "PATCH",
                f"/blocks/{block['id']}",
                json={"paragraph": text_block("paragraph", value)["paragraph"]},
            )
            return
    notion._request(
        "PATCH",
        f"/blocks/{page_id(root)}/children",
        json={"children": [text_block("paragraph", value)]},
    )
