"""Task-oriented command handlers for Job Search Hub."""

from __future__ import annotations

import json

from job_search_hub.commands.common import output
from job_search_hub.storage import (
    get_setting,
)


def maintenance(args):
    from job_search_hub.storage import private_write
    from job_search_hub.transfer import (
        backup,
        export_notion,
        import_documents,
        import_notion,
        restore,
    )
    from job_search_hub.workflow import session

    apply = getattr(args, "apply", False)
    if args.action == "restore":
        output(
            restore(args.file, args.destination)
            if apply
            else {
                "would_restore": str(args.file),
                "destination": str(args.destination),
                "next": "Add --apply to restore into an empty directory.",
            }
        )
        return 0
    if args.action == "import" and args.source.is_dir():
        output(import_documents(args.source, apply=apply))
        return 0
    if args.action == "backup":
        if args.local_only:
            output(backup(args.file))
            return 0
        if not get_setting("NOTION_ROOT", ""):
            raise ValueError(
                "Connect a dedicated hub first: jobhub setup "
                "(or use --local-only to back up local data without Notion)"
            )
    if args.action == "capture":
        from job_search_hub.capture.__main__ import main as capture_main

        return capture_main()
    if args.action == "benchmark":
        from job_search_hub.models.benchmark import main as benchmark_main

        return benchmark_main(["--tasks", *args.tasks])
    with session(
        preview=not apply and args.action not in {"backup", "export"},
        remote_read_only=args.action in {"backup", "export"},
    ) as (
        settings,
        notion,
    ):
        if args.action in {"backup", "export"}:
            payload = export_notion(notion, settings, get_setting("NOTION_ROOT"))
            if args.action == "backup":
                output(backup(args.file, notion_export=payload))
            else:
                private_write(args.file, json.dumps(payload, indent=2))
                print("Private Notion snapshot exported; credentials excluded.")
            return 0
        if args.action == "import":
            output(
                import_notion(
                    json.loads(args.source.read_text()),
                    notion,
                    settings,
                    get_setting("NOTION_ROOT"),
                    apply=apply,
                )
            )
            return 0
        if args.action == "validate":
            from job_search_hub.workflow import validate_hub

            validate_hub(settings, notion)
            print("All hub schemas valid.")
            return 0
        from job_search_hub.commands import operations

        functions = {
            "audit": "cmd_audit_notion",
            "dedupe": "cmd_dedupe",
            "cleanup": "cmd_cleanup_opportunities",
            "posting-check": "cmd_posting_check",
            "posting-dates": "cmd_posting_date_backfill",
            "organizations": "cmd_org_sync",
            "qualifications": "cmd_migrate_qualifications",
            "repair-urls": "cmd_migrate_canonical_urls",
            "verification": "cmd_migrate_verification_needed",
            "weekly-metrics": "cmd_weekly_metrics",
            "model-log": "cmd_llm_log",
            "watch": "cmd_watch",
        }
        args._settings = settings
        args.commit = apply
        args.dry_run = not apply
        args.subcommand = args.action
        for key, value in {
            "sample": 25,
            "strict_columns": False,
            "max_age_hours": 48,
            "browser_fallback": "auto",
            "max_attempts": 3,
            "create_missing": "trusted",
            "examples": 3,
            "migrate_statuses": False,
            "tail": 50,
        }.items():
            setattr(args, key, value)
        return getattr(operations, functions[args.action])(args)
