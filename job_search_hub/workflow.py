"""One shared session and explicit workflow stage dependencies."""

from __future__ import annotations

import fcntl
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_search_hub.config import load_settings
from job_search_hub.db import open_store
from job_search_hub.notion.client import NotionClient
from job_search_hub.notion.hub import HUB_SPECS, bind, snapshot, update_dashboard
from job_search_hub.notion.validate import validate_one
from job_search_hub.storage import READ_ONLY, app_paths, get_setting, write_json


@contextmanager
def session(*, preview=False, preferences=False, remote_read_only=False):
    settings = load_settings()
    with tempfile.TemporaryDirectory(prefix="jobhub-preview-") as scratch:
        if preview:
            root = Path(scratch)
            for old, name in (
                (settings.sqlite_path, "store.sqlite"),
                (settings.notification_ledger_path, "mail.sqlite"),
            ):
                if old.is_file():
                    with (
                        sqlite3.connect(old.as_uri() + "?mode=ro", uri=True) as source,
                        sqlite3.connect(root / name) as target,
                    ):
                        source.backup(target)
            settings = replace(
                settings,
                data_dir=root,
                log_dir=root / "logs",
                sqlite_path=root / "store.sqlite",
                notification_ledger_path=root / "mail.sqlite",
            )
        root = get_setting("NOTION_ROOT", "")
        if not root:
            raise ValueError("Connect a dedicated hub first: jobhub setup")
        token = READ_ONLY.set(preview or READ_ONLY.get())
        try:
            with NotionClient(settings.notion, read_only=remote_read_only) as notion:
                settings = bind(settings, notion, root)
                if preferences:
                    settings, _ = snapshot(settings, notion, preview=preview, due_only=False)
                yield settings, notion
        finally:
            READ_ONLY.reset(token)


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another refresh is running. Wait for it to finish.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def validate_hub(settings, notion):
    reports = [
        validate_one(
            notion,
            s,
            settings.notion.data_sources[s.key],
            relation_targets=settings.notion.data_sources,
            ds_context_cache={},
        )
        for s in HUB_SPECS
    ]
    failed = [
        f"{r.data_source}: " + "; ".join(m.message for m in r.mismatches)
        for r in reports
        if not r.ok
    ]
    if failed:
        raise ValueError("Hub schema mismatch. " + "\n".join(failed))


@contextmanager
def models(settings, store=None):
    from job_search_hub.models import LMStudioClient, ModelRouter

    client = LMStudioClient(
        settings.lmstudio, store=store, audit_mode=settings.llm_audit_mode, timeout=30
    )
    try:
        router = ModelRouter(settings.lmstudio, client=client)
        try:
            router.discover()
        except Exception:
            yield None, None
        else:
            yield client, router
    finally:
        client.close()


def refresh(*, preview=False, limit=None):
    from job_search_hub.browser_runtime import BrowserRuntime
    from job_search_hub.library import suggest, sync_library
    from job_search_hub.logging_setup import setup_logging
    from job_search_hub.notify import Notifier
    from job_search_hub.notion import properties as P
    from job_search_hub.pipeline.ingest_stage import run_ingest
    from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles
    from job_search_hub.pipeline.organizations import sync_opportunity_organizations
    from job_search_hub.pipeline.posting_verification import verify_tick_preflight
    from job_search_hub.pipeline.score_stage import run_score, send_answer_digest
    from job_search_hub.pipeline.weekly_metrics import aggregate_week

    result = {
        "preview": preview,
        "discovered": 0,
        "scored": 0,
        "planned_applications": 0,
        "recommendations": 0,
        "errors": [],
        "pending": [],
    }
    with (
        session(preview=preview) as (settings, notion),
        run_lock(settings.data_dir / ".refresh.lock"),
    ):
        validate_hub(settings, notion)
        settings, source_rows = snapshot(settings, notion, preview=preview)
        setup_logging(settings.log_dir, level=settings.log_level)
        store = open_store(settings.sqlite_path)
        notifier = Notifier(settings.smtp, settings.notification_ledger_path, dry_run=preview)
        attempted = set()
        with BrowserRuntime() as browser:
            if settings.enabled_adapters and settings.search_terms.role_terms:
                attempted.update(settings.enabled_adapters)
                stats = run_ingest(
                    settings,
                    notion=notion,
                    store=store,
                    dry_run=preview,
                    only_adapters=list(settings.enabled_adapters),
                    browser_runtime=browser,
                )
                result["discovered"] = stats.new_opportunities
                result["errors"].extend(stats.errors)
            else:
                result["pending"].append("Enable a source and add role keywords on 1 — My search.")
            from job_search_hub.pipeline.posting_date_backfill import run_posting_date_backfill

            try:
                dates = run_posting_date_backfill(
                    settings,
                    notion=notion,
                    store=store,
                    dry_run=preview,
                    limit=limit or 10,
                    max_attempts=3,
                )
                result["errors"].extend(dates.errors)
            except Exception as exc:
                result["errors"].append(
                    f"Posting-date check failed ({type(exc).__name__}); missing "
                    + "dates require review."
                )
            verified = False
            try:
                verification = verify_tick_preflight(
                    settings, notion=notion, store=store, dry_run=preview, browser_runtime=browser
                )
                result["errors"].extend(verification.errors)
                verified = not verification.errors
            except Exception as exc:
                result["errors"].append(
                    f"Verification failed ({type(exc).__name__}); AI advancement skipped."
                )
            with models(settings, store) as (client, router):
                if verified and client is not None:
                    # Each scoring queue verifies its candidates; failures block advancement.
                    for awaiting in (True, False):
                        stats = run_score(
                            settings,
                            notion=notion,
                            store=store,
                            router=router,
                            client=client,
                            awaiting_only=awaiting,
                            limit=limit,
                            dry_run=preview,
                            notifier=notifier,
                        )
                        result["scored"] += stats.scored
                        result["errors"].extend(stats.errors)
                        send_answer_digest(notifier, stats, dry_run=preview)
                elif client is None:
                    result["pending"].append(
                        "LM Studio unavailable: discovery/tracking remain active; AI tasks pending."
                    )
                if not preview:
                    sync_library(notion, settings)
                if verified:
                    try:
                        groups = suggest(
                            notion,
                            settings,
                            client=client,
                            router=router,
                            store=store,
                            preview=preview,
                        )
                        result["recommendations"] = len(groups)
                    except Exception as exc:
                        result["errors"].append(
                            f"Resume suggestions pending ({type(exc).__name__}); "
                            + "selections preserved."
                        )
            # Tracking is a record of intent, never an application submission.
            lifecycle = reconcile_lifecycles(
                settings, notion=notion, store=store, notifier=notifier, dry_run=preview
            )
            result["planned_applications"] = lifecycle.manual_applications_created
            result["errors"].extend(lifecycle.errors)
        sync_opportunity_organizations(
            settings, notion=notion, dry_run=preview, create_missing="trusted"
        )
        if not preview:
            now = datetime.now(UTC)
            for row in source_rows:
                p = row["properties"]
                kind = P.read_text(p.get("Type"))
                cadence = P.read_select(p.get("Cadence"))
                if kind == "careers":
                    kind = "direct_discovery"
                if kind not in attempted:
                    continue
                health = store.latest_source_run(
                    source_key=kind, target=P.read_text(p.get("Target"))
                ) or store.latest_source_run(source_key=kind, target="ingest")
                if health is None:
                    continue
                failed = health["status"] not in {
                    "ok",
                    "healthy",
                    "available",
                    "new",
                    "changed",
                    "unchanged",
                }
                delta = (
                    timedelta(hours=1)
                    if failed
                    else timedelta(days={"Daily": 1, "Weekly": 7, "Monthly": 30}.get(cadence, 1))
                )
                notion.update_page(
                    row["id"],
                    {
                        "Last Checked": P.date_property(now),
                        "Next Check": P.date_property(now + delta),
                        "Status": P.rich_text(health["status"]),
                        "Last Error": P.rich_text(
                            "Source failed; run jobhub sources check" if failed else ""
                        ),
                    },
                )
            if settings.direct_outreach.enabled:
                from job_search_hub.pipeline.direct_outreach import run_direct_outreach

                run_direct_outreach(
                    settings, notion=notion, store=store, notifier=notifier, dry_run=False
                )
            if now.weekday() == 0:
                aggregate_week(store)
            result["last_refresh"] = now.isoformat()
            jobs = list(notion.query_data_source(settings.notion.data_sources["opportunities"]))
            counts = {}
            for job in jobs:
                status = P.read_select(job["properties"].get("Status")) or "Unknown"
                counts[status] = counts.get(status, 0) + 1
            result["queues"] = counts
            actions = "; ".join(result["pending"]) or "Review your numbered workflow pages."
            failures = "; ".join(result["errors"]) or "None"
            update_dashboard(
                notion,
                get_setting("NOTION_ROOT"),
                (
                    f"Last refresh: {now.isoformat()}\nQueues: {counts}"
                    + f"\nDiscovered: {result['discovered']} • Scored: "
                    + f"{result['scored']}\nNext actions: "
                    + f"{actions}\nFailures: {failures}"
                ),
            )
            write_json(app_paths().data / "last_run.json", result)
            if settings.smtp.host and settings.smtp.recipient:
                notifier.send_once(
                    kind="refresh_digest",
                    key=f"refresh:{now.date()}",
                    subject="Job Search Hub: daily update",
                    body=(
                        f"New jobs: {result['discovered']}\nScored: {result['scored']}"
                        + f"\nPlanned applications: {result['planned_applications']}"
                        + f"\nOpen your Notion hub: {get_setting('NOTION_ROOT')}"
                    ),
                )
                notifier.flush_due()
    return result
