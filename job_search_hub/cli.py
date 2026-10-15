"""Task-oriented command handlers for Job Search Hub."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from job_search_hub.commands.common import mutating, output
from job_search_hub.config import load_settings
from job_search_hub.storage import (
    READ_ONLY,
    SECRET_KEYS,
    app_paths,
    read_json,
)


def parser():
    p = argparse.ArgumentParser(
        prog="jobhub",
        description="Find, evaluate, and track your next role through Notion and the CLI.",
    )
    p.add_argument("--version", action="version", version="Job Search Hub 0.1.0")
    sub = p.add_subparsers(dest="command")
    setup = sub.add_parser(
        "setup", help="Connect a new Notion hub and configure optional integrations"
    )
    setup.add_argument("--root-url")
    setup.add_argument(
        "--create-template",
        action="store_true",
        help="Create a clean template under the supplied blank page",
    )
    setup.add_argument(
        "--credential",
        choices=sorted(SECRET_KEYS),
        help="Store one credential using a hidden prompt",
    )
    setup.add_argument("--lmstudio-url")
    setup.add_argument("--model")
    setup.add_argument("--embedding-model")
    setup.add_argument("--role", action="append", default=[])
    setup.add_argument("--non-interactive", action="store_true")
    run = sub.add_parser("run", help="Refresh the hub: discover, evaluate, and track")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--limit", type=int)
    sub.add_parser("status", help="Show the last local run and next action")
    doctor = sub.add_parser(
        "doctor", help="Check configuration, Notion, and actual model capability"
    )
    doctor.add_argument(
        "--sources", action="store_true", help="Also make bounded live source checks"
    )
    jobs = sub.add_parser("jobs", help="Review, capture, and track jobs").add_subparsers(
        dest="action", required=True
    )
    listing = jobs.add_parser("list")
    listing.add_argument("--applications", action="store_true", help="Show tracking rows")
    listing.add_argument("--json", action="store_true", help="Show complete Notion JSON")
    decide = jobs.add_parser("decide")
    decide.add_argument("id")
    decide.add_argument("decision", choices=["Shortlist", "Apply", "Skip", "Save for Later"])
    track = jobs.add_parser("track")
    track.add_argument("id")
    track.add_argument(
        "status",
        choices=[
            "Planned",
            "Submitted",
            "Interviewing",
            "Offer",
            "Rejected",
            "Cancelled",
            "Closed",
        ],
    )
    explain = jobs.add_parser("explain")
    explain.add_argument("query")
    capture = jobs.add_parser("add")
    capture.add_argument("url")
    capture.add_argument("--title", required=True)
    capture.add_argument("--company", default="")
    capture.add_argument("--description", default="")
    review = jobs.add_parser("review")
    review.add_argument("id")
    search = sub.add_parser(
        "search", help="Edit the same search profiles used in Notion"
    ).add_subparsers(dest="action", required=True)
    search.add_parser("list").add_argument("--json", action="store_true")
    search.add_parser("preview")
    prof = search.add_parser("profile")
    prof.add_argument("name")
    prof.add_argument("--location")
    prof.add_argument("--remote-only", action="store_true")
    prof.add_argument("--max-new", type=int)
    prof.add_argument("--minimum-annual", type=float)
    prof.add_argument("--minimum-hourly", type=float)
    prof.add_argument("--disable", action="store_true")
    kw = search.add_parser("keywords").add_subparsers(dest="operation", required=True)
    for operation in ("add", "remove"):
        a = kw.add_parser(operation)
        a.add_argument("term")
        a.add_argument("--profile", default="My search")
        a.add_argument(
            "--kind", choices=["role", "skill", "mission", "exclude", "language"], default="role"
        )
    sources = sub.add_parser(
        "sources", help="Configure supported feeds, boards, and manual sources"
    ).add_subparsers(dest="action", required=True)
    sources.add_parser("list").add_argument("--json", action="store_true")
    sources.add_parser("catalog")
    sources.add_parser("check")
    auth = sources.add_parser("authorize")
    auth.add_argument("provider", choices=["upwork"])
    auth.add_argument("--timeout-seconds", type=float, default=120)
    add = sources.add_parser("add")
    add.add_argument("--type", required=True)
    add.add_argument("--target", default="")
    add.add_argument("--url", default="")
    add.add_argument("--name")
    add.add_argument("--cadence", choices=["Daily", "Weekly", "Monthly", "Manual"], default="Daily")
    for name in ("enable", "disable"):
        a = sources.add_parser(name)
        a.add_argument("id")
    resumes = sub.add_parser(
        "resumes", help="Catalog your PDFs and suggest reusable resume types"
    ).add_subparsers(dest="action", required=True)
    resumes.add_parser("list").add_argument("--json", action="store_true", help="Output raw JSON")
    a = resumes.add_parser("add")
    a.add_argument("file", type=Path)
    a.add_argument("--title", required=True)
    a.add_argument("--tag", action="append", default=[])
    resumes.add_parser("sync")
    a = resumes.add_parser("suggest")
    a.add_argument("--preview", action="store_true")
    profile = sub.add_parser(
        "profile", help="Review facts locally before approving them for Notion"
    ).add_subparsers(dest="action", required=True)
    profile.add_parser("list").add_argument("--json", action="store_true")
    profile.add_parser("pending")
    a = profile.add_parser("extract")
    a.add_argument("document_id")
    a.add_argument("--model", action="store_true")
    a = profile.add_parser("approve")
    a.add_argument("fact_id")
    answers = sub.add_parser(
        "answers", help="Review reusable answers and qualification proposals"
    ).add_subparsers(dest="action", required=True)
    answers.add_parser("list").add_argument("--json", action="store_true")
    a = answers.add_parser("approve")
    a.add_argument("id")
    a.add_argument("--value", required=True)
    a = answers.add_parser("propose")
    a.add_argument(
        "--apply", action="store_true", help="Save proposals as Needs Review; never auto-approve"
    )
    a.add_argument("--limit", type=int, default=20)
    a = answers.add_parser("match")
    a.add_argument("query")
    a.add_argument("--answer-type", default="Short Text")
    answers.add_parser("refresh")
    notifications = sub.add_parser(
        "notifications", help="Preview, test, or retry your own notification outbox"
    ).add_subparsers(dest="action", required=True)
    notifications.add_parser("list")
    notifications.add_parser("test")
    notifications.add_parser("retry")
    schedule = sub.add_parser(
        "schedule", help="Opt in to an independent local refresh schedule"
    ).add_subparsers(dest="action", required=True)
    a = schedule.add_parser("enable")
    a.add_argument("--hours", type=int, default=6)
    a.add_argument("--preview", action="store_true")
    schedule.add_parser("disable")
    schedule.add_parser("status")
    maintenance = sub.add_parser(
        "maintenance", help="Advanced audits, migrations, backups, and optional imports"
    ).add_subparsers(dest="action", required=True)
    for name in (
        "validate",
        "audit",
        "dedupe",
        "cleanup",
        "posting-check",
        "posting-dates",
        "organizations",
        "qualifications",
        "repair-urls",
        "verification",
        "weekly-metrics",
        "model-log",
        "watch",
    ):
        a = maintenance.add_parser(name)
        g = a.add_mutually_exclusive_group()
        g.add_argument("--apply", action="store_true")
        g.add_argument("--preview", action="store_true")
        a.add_argument("--limit", type=int, default=25)
        a.add_argument("--force", action="store_true")
        a.add_argument("--full", action="store_true")
        a.add_argument("--include-irrelevant", action="store_true")
        a.add_argument("--verbose", action="store_true")
    a = maintenance.add_parser("backup")
    a.add_argument("file", type=Path)
    a.add_argument("--local-only", action="store_true")
    a = maintenance.add_parser("export")
    a.add_argument("file", type=Path)
    a = maintenance.add_parser("restore")
    a.add_argument("file", type=Path)
    a.add_argument("--destination", type=Path, required=True)
    a.add_argument("--apply", action="store_true")
    a = maintenance.add_parser("import")
    a.add_argument("source", type=Path)
    a.add_argument("--apply", action="store_true")
    a = maintenance.add_parser("benchmark")
    a.add_argument("--tasks", nargs="+", choices=["chat", "embed"], default=["chat", "embed"])
    maintenance.add_parser(
        "capture", help="Start the optional authenticated local capture endpoint"
    )
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        print("\nStart with jobhub setup, then jobhub doctor and jobhub run.")
        return 0
    if getattr(args, "limit", None) is not None and args.limit < 1:
        p.error("--limit must be positive")
    token = READ_ONLY.set(not mutating(args))
    try:
        if args.command == "setup":
            from job_search_hub.commands.setup import setup

            return setup(args)
        if args.command == "doctor":
            from job_search_hub.commands.setup import doctor

            return doctor(args)
        if args.command == "run":
            from job_search_hub.workflow import refresh

            result = refresh(preview=args.preview, limit=args.limit)
            output(result)
            return 1 if result["errors"] else 0
        if args.command == "status":
            output(
                read_json(
                    app_paths().data / "last_run.json",
                    {"status": "Not yet run", "next": "jobhub setup, then jobhub run"},
                )
            )
            return 0
        if args.command == "maintenance":
            from job_search_hub.commands.maintenance import maintenance

            if mutating(args) and args.action != "capture":
                from job_search_hub.workflow import run_lock

                with run_lock(app_paths().data / ".refresh.lock"):
                    return maintenance(args)
            return maintenance(args)
        if args.command == "schedule":
            from job_search_hub.scheduling import disable, enable
            from job_search_hub.storage import write_json

            if args.action == "status":
                output(read_json(app_paths().config / "schedule.json", {"enabled": False}))
                return 0
            result = (
                enable(args.hours, preview=args.preview) if args.action == "enable" else disable()
            )
            if not getattr(args, "preview", False):
                write_json(app_paths().config / "schedule.json", result)
            output(result)
            return 0
        if args.command == "notifications":
            from job_search_hub.notify import Notifier

            settings = load_settings()
            notifier = Notifier(
                settings.smtp, settings.notification_ledger_path, dry_run=READ_ONLY.get()
            )
            if args.action == "list":
                path = settings.notification_ledger_path
                if not path.exists():
                    output([])
                else:
                    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                        output(
                            db.execute(
                                "SELECT kind,subject,status FROM notification_messages ORDER BY "
                                "created_at DESC LIMIT 25"
                            ).fetchall()
                        )
            elif args.action == "test":
                if not settings.smtp.host or not settings.smtp.recipient:
                    raise ValueError("Configure JOBHUB_SMTP_* settings first.")
                notifier.send_once(
                    kind="smoke",
                    key=f"smoke:{datetime.now(UTC).isoformat()}",
                    subject="Job Search Hub: connection test",
                    body="Your personal notification integration is working.",
                )
                output(notifier.flush_due())
            else:
                output(notifier.flush_due(force=True))
            return 0
        from job_search_hub.commands.hub import remote_command

        if mutating(args):
            from job_search_hub.workflow import run_lock

            with run_lock(app_paths().data / ".refresh.lock"):
                return remote_command(args)
        return remote_command(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Keep provider error bodies out of normal output; they can contain private text.
        if isinstance(exc, ValueError):
            print(f"Job Search Hub: {exc}")
        else:
            print(
                f"Job Search Hub: {type(exc).__name__}. Run jobhub doctor; "
                + "existing data was preserved."
            )
        return 2
    finally:
        READ_ONLY.reset(token)
