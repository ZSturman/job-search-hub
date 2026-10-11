"""Task-oriented command handlers for Job Search Hub."""

from __future__ import annotations

from datetime import UTC, datetime

from job_search_hub.commands.common import mutating, output, records
from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.storage import (
    app_paths,
    read_json,
)


def _profile(notion, settings, name):
    ds = settings.notion.data_sources["search_profiles"]
    rows = list(
        notion.query_data_source(ds, filter_={"property": "Profile", "title": {"equals": name}})
    )
    if len(rows) > 1:
        raise ValueError("Duplicate profile names; rename them in Notion.")
    return (
        rows[0]
        if rows
        else notion.create_page(
            parent={"data_source_id": ds},
            properties={
                "Profile": P.title(name),
                "Enabled": P.checkbox(True),
                "Languages": P.multi_select(["en"]),
                "Max New Per Run": P.number(50),
            },
        )
    )


def _edit_keywords(notion, settings, name, kind, terms, *, remove=False):
    key = {
        "role": "Role Terms",
        "skill": "Skill Terms",
        "mission": "Mission Terms",
        "exclude": "Excluded Terms",
        "language": "Languages",
    }[kind]
    for term in terms:
        if not term.strip() or len(term) > 100:
            raise ValueError("Keywords must be 1–100 characters.")
    page = _profile(notion, settings, name)
    existing = P.read_multi_select(page["properties"].get(key))
    values = (
        [t for t in existing if t not in terms] if remove else list(dict.fromkeys(existing + terms))
    )
    notion.update_page(page["id"], {key: P.multi_select(values)})
    output({"profile": name, key: values})


def _require_owned(notion, settings, identity, *keys):
    """Reject IDs outside the selected hub, including accidental old-project IDs."""
    from job_search_hub.notion.hub import page_id

    identity = page_id(identity) if identity.startswith(("http://", "https://")) else identity
    page = notion.get_page(identity)
    parent = page.get("parent", {})
    ds = parent.get("data_source_id", "")
    if not any(
        ds.replace("-", "") == settings.notion.data_sources[key].replace("-", "") for key in keys
    ):
        raise ValueError("That record does not belong to the selected Job Search Hub database.")
    return page


def remote_command(args):
    from job_search_hub.library import (
        add_resume,
        approve_fact,
        catalog,
        stage_facts,
        suggest,
        sync_library,
    )
    from job_search_hub.workflow import models, session

    preview = not mutating(args)
    if args.command == "sources" and args.action == "authorize":
        from job_search_hub.commands.operations import cmd_upwork_auth

        args.commit, args.dry_run = True, False
        args.subcommand, args.print_url = "sources authorize", False
        return cmd_upwork_auth(args)
    if args.command == "resumes" and args.action == "list":
        output(catalog())
        return 0
    if args.command == "resumes" and args.action == "add":
        output(add_resume(args.file, args.title, args.tag))
        return 0
    if args.command == "profile" and args.action == "pending":
        output(read_json(app_paths().data / "profile/pending.json", []))
        return 0
    if args.command == "profile" and args.action == "extract":
        if args.model:
            with models(load_settings()) as (client, router):
                output(stage_facts(args.document_id, client=client, router=router))
        else:
            output(stage_facts(args.document_id))
        print("Facts stayed local. Review evidence, then use jobhub profile approve FACT_ID.")
        return 0
    if args.command == "sources" and args.action == "catalog":
        from job_search_hub.source_catalog import load_source_catalog

        output(
            [
                {"type": r.adapter_key, "name": r.name, "status": r.status}
                for r in load_source_catalog(load_settings())
            ]
        )
        return 0
    with session(
        preview=preview,
        preferences=args.command == "sources"
        and args.action == "check"
        or args.command == "search"
        and args.action == "preview"
        or args.command == "jobs"
        and args.action == "add",
    ) as (settings, notion):
        if args.command == "search":
            if args.action == "list":
                records(
                    list(notion.query_data_source(settings.notion.data_sources["search_profiles"])),
                    json_output=args.json,
                )
            elif args.action == "preview":
                from job_search_hub.ingest.preview import planned_searches

                output(
                    planned_searches(settings, sources=list(settings.enabled_adapters))
                    if settings.enabled_adapters
                    else []
                )
            elif args.action == "keywords":
                _edit_keywords(
                    notion,
                    settings,
                    args.profile,
                    args.kind,
                    [args.term],
                    remove=args.operation == "remove",
                )
            else:
                for value in (args.max_new, args.minimum_annual, args.minimum_hourly):
                    if value is not None and value < 0:
                        raise ValueError("Profile limits must be non-negative.")
                page = _profile(notion, settings, args.name)
                props = {"Enabled": P.checkbox(not args.disable)}
                for arg, key in [
                    ("location", "Location"),
                    ("max_new", "Max New Per Run"),
                    ("minimum_annual", "Minimum Annual Pay"),
                    ("minimum_hourly", "Minimum Hourly Pay"),
                ]:
                    value = getattr(args, arg)
                    if value is not None:
                        if isinstance(value, (int, float)) and value < 0:
                            raise ValueError("Profile limits must be non-negative.")
                        props[key] = P.rich_text(value) if arg == "location" else P.number(value)
                if args.remote_only:
                    props["Remote Only"] = P.checkbox(True)
                notion.update_page(page["id"], props)
                print("Search profile updated in Notion.")
        elif args.command == "sources":
            ds = settings.notion.data_sources["sources"]
            if args.action == "list":
                records(list(notion.query_data_source(ds)), json_output=args.json)
            elif args.action == "check":
                from job_search_hub.ingest.preview import run_preview

                output(
                    [
                        {
                            "source": r.request.source_adapter,
                            "status": r.status,
                            "error": r.error,
                            "reason": r.skip_reason,
                        }
                        for r in run_preview(
                            settings, sources=list(settings.enabled_adapters), examples=1
                        )
                    ]
                    if settings.enabled_adapters
                    else []
                )
            elif args.action == "add":
                from job_search_hub.ingest.registry import REGISTRY

                target = args.url or args.target
                kind = args.type
                if kind in {"rss", "careers", "watchlist", "manual"} and not target.startswith(
                    ("https://", "http://")
                ):
                    raise ValueError("Supply an http(s) URL for this source.")
                if (
                    kind in {"greenhouse", "lever", "ashby", "recruitee", "smartrecruiters"}
                    and not target
                ):
                    raise ValueError("Supply the employer board identifier with --target.")
                if kind == "adzuna" and (len(target) != 2 or not target.isalpha()):
                    raise ValueError("Supply the Adzuna country code with --target, such as gb.")
                supported = kind in REGISTRY or kind == "careers"
                if not supported:
                    if not target.startswith(("http://", "https://")):
                        raise ValueError("Supply --url for an unsupported manual source.")
                    kind = "manual"
                props = {
                    "Source": P.title(args.name or args.type),
                    "Type": P.rich_text(kind),
                    "Target": P.rich_text(target),
                    "Enabled": P.checkbox(supported),
                    "Cadence": P.select(args.cadence if supported else "Manual"),
                    "Status": P.rich_text(
                        "Configured; run jobhub sources check"
                        if supported
                        else "Manual capture / alerts; no automated adapter"
                    ),
                }
                rows = list(
                    notion.query_data_source(
                        ds,
                        filter_={
                            "and": [
                                {"property": "Type", "rich_text": {"equals": kind}},
                                {"property": "Target", "rich_text": {"equals": target}},
                            ]
                        },
                    )
                )
                if len(rows) > 1:
                    raise ValueError("Duplicate source targets; merge them on 1 — My search.")
                if rows:
                    from job_search_hub.library import update_changed

                    update_changed(notion, rows[0], props)
                    output(rows[0])
                else:
                    output(notion.create_page(parent={"data_source_id": ds}, properties=props))
            else:
                _require_owned(notion, settings, args.id, "sources")
                notion.update_page(args.id, {"Enabled": P.checkbox(args.action == "enable")})
                print("Source updated.")
        elif args.command == "jobs":
            if args.action == "list":
                key = "applications" if args.applications else "opportunities"
                records(
                    list(notion.query_data_source(settings.notion.data_sources[key])),
                    json_output=args.json,
                )
            elif args.action == "add":
                from job_search_hub.db import open_store
                from job_search_hub.ingest.base import RawPosting
                from job_search_hub.pipeline.ingest_stage import IngestStats, _ingest_posting

                raw = RawPosting(
                    source="Manual",
                    source_adapter="manual",
                    external_job_id=args.url,
                    url=args.url,
                    role_title=args.title,
                    company=args.company,
                    description=args.description,
                )
                stats = IngestStats()
                _ingest_posting(
                    raw,
                    notion=notion,
                    settings=settings,
                    store=open_store(settings.sqlite_path),
                    stats=stats,
                    dry_run=False,
                )
                output(stats)
            elif args.action == "decide":
                from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles

                _require_owned(notion, settings, args.id, "opportunities")
                notion.update_page(args.id, {"My Decision": P.select(args.decision)})
                output(reconcile_lifecycles(settings, notion=notion, dry_run=False))
            elif args.action == "track":
                _require_owned(notion, settings, args.id, "applications")
                props = {"Status": P.select(args.status)}
                if args.status == "Submitted":
                    props["Submitted At"] = P.date_property(datetime.now(UTC))
                notion.update_page(args.id, props)
                from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles

                output(reconcile_lifecycles(settings, notion=notion, dry_run=False))
            elif args.action == "review":
                page = _require_owned(notion, settings, args.id, "applications", "opportunities")
                props = page["properties"]
                output(
                    {
                        "title": P.read_title(props.get("Application"))
                        or P.read_title(props.get("Opportunity")),
                        "apply_url": P.read_url(props.get("Job URL"))
                        or P.read_url(props.get("URL")),
                        "next_action": P.read_text(props.get("Next Action")),
                        "status": P.read_select(props.get("Status")),
                    }
                )
            else:
                from job_search_hub.db import open_store
                from job_search_hub.pipeline.explain import explain_opportunity

                print(
                    explain_opportunity(
                        notion=notion,
                        settings=settings,
                        store=open_store(settings.sqlite_path),
                        query=args.query,
                    )
                )
        elif args.command == "resumes":
            if args.action == "sync":
                sync_library(notion, settings)
                print("Library metadata synchronized; PDFs stayed local.")
            else:
                with models(settings) as (client, router):
                    output(suggest(notion, settings, client=client, router=router, preview=preview))
        elif args.command == "profile":
            if args.action == "approve":
                approve_fact(notion, settings, args.fact_id)
                print("Reviewed fact approved in Notion.")
            else:
                records(
                    list(notion.query_data_source(settings.notion.data_sources["resume_facts"])),
                    json_output=args.json,
                )
        elif args.command == "answers":
            ds = settings.notion.data_sources["answer_bank"]
            if args.action == "list":
                records(list(notion.query_data_source(ds)), json_output=args.json)
            elif args.action == "approve":
                _require_owned(notion, settings, args.id, "answer_bank")
                notion.update_page(
                    args.id,
                    {
                        "My Answer": P.rich_text(args.value),
                        "Status": P.select("Approved"),
                        "Approved for Reuse": P.checkbox(True),
                    },
                )
                print("Answer approved.")
            else:
                from job_search_hub.db import open_store
                from job_search_hub.pipeline.answer_bank_cli import propose_yes_no_answers
                from job_search_hub.pipeline.answers import (
                    load_answer_bank,
                    refresh_answer_embeddings,
                )

                with models(settings) as (client, router):
                    if client is None and args.action != "match":
                        raise ValueError("This command needs LM Studio. Run jobhub doctor.")
                    store = open_store(settings.sqlite_path)
                    if args.action == "propose":
                        output(
                            propose_yes_no_answers(
                                notion=notion,
                                settings=settings,
                                store=store,
                                router=router,
                                client=client,
                                dry_run=preview,
                                limit=args.limit,
                                approve=False,
                            )
                        )
                    elif args.action == "refresh":
                        output(
                            refresh_answer_embeddings(
                                answers=load_answer_bank(notion, settings),
                                router=router,
                                client=client,
                                store=store,
                            )
                        )
                    else:
                        from job_search_hub.pipeline.answer_bank_cli import reuse_test

                        output(
                            reuse_test(
                                rows=load_answer_bank(notion, settings),
                                store=open_store(settings.sqlite_path),
                                router=router,
                                client=client,
                                question=args.query,
                                answer_type=args.answer_type,
                            )
                        )
    return 0
