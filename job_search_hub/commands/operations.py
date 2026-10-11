"""Tick CLI: a single idempotent entry point compatible with cron and launchd.

Subcommands (per OPUS_BUILD_PROMPT §12):
    run / (default)      ingest + score + manual application sync + local metrics
    ingest               discovery only
    score                score approved/New/Scoring opportunities
    notify               send a notification smoke email
    metrics              log current tick metrics
    weekly-metrics       print a local audit-log rollup
    sync-notion          validate Notion schema (alias of validate)
    answer-bank          inspect/refresh the Answer Bank cache
    posting-check        verify active posting URLs and close unavailable jobs
    lifecycle-sync       apply Notion decisions and application lifecycle moves
    capture              run the FastAPI capture server
    benchmark            benchmark LM Studio models per role
    validate             validate Notion schema; no changes
    models               show discovered LM Studio model routing

Top-level flags:
    --dry-run            default; no writes to Notion or remote services
    --apply             the only way to disable dry-run
"""

from __future__ import annotations

import argparse
import json

from job_search_hub.config import Settings, ensure_directories, load_settings
from job_search_hub.db import open_store
from job_search_hub.logging_setup import get_logger, setup_logging
from job_search_hub.notify import Notifier
from job_search_hub.pipeline import next_steps as ns

log = get_logger(__name__)


# ---------------- helpers ----------------


def _resolve_dry_run(settings: Settings, args: argparse.Namespace) -> bool:
    if args.commit and args.dry_run:
        raise ValueError("Choose either preview or apply, not both.")
    if args.commit:
        return False
    if args.dry_run:
        return True
    return settings.dry_run_default


def _bootstrap(args: argparse.Namespace) -> tuple[Settings, bool]:
    settings = getattr(args, "_settings", None) or load_settings()
    ensure_directories(settings)
    setup_logging(settings.log_dir, level=settings.log_level)
    dry_run = _resolve_dry_run(settings, args)
    log.info("jobhub starting subcommand=%s dry_run=%s", args.subcommand, dry_run)
    return settings, dry_run


def _open_notion(settings: Settings):
    from job_search_hub.notion import NotionClient  # lazy import

    return NotionClient(settings.notion)


def _make_notifier(settings: Settings, *, dry_run: bool) -> Notifier | None:
    smtp = getattr(settings, "smtp", None)
    ledger_path = getattr(settings, "notification_ledger_path", None)
    if smtp is None or ledger_path is None:
        return None
    return Notifier(smtp, ledger_path, dry_run=dry_run)


def cmd_audit_notion(args: argparse.Namespace) -> int:
    settings, _dry = _bootstrap(args)
    from job_search_hub.notion.audit import format_notion_audit, run_notion_audit

    with _open_notion(settings) as notion:
        audit = run_notion_audit(
            settings,
            notion=notion,
            sample_size=args.sample,
            full=args.full,
        )
    print(format_notion_audit(audit, strict_columns=args.strict_columns))
    return 0 if audit.ok(strict_columns=args.strict_columns) else 2


def cmd_posting_check(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.browser_runtime import BrowserRuntime
    from job_search_hub.pipeline.posting_verification import verify_active_opportunities

    store = open_store(settings.sqlite_path)
    with BrowserRuntime() as browser_runtime, _open_notion(settings) as notion:
        stats = verify_active_opportunities(
            settings,
            notion=notion,
            store=store,
            dry_run=dry_run,
            max_age_hours=args.max_age_hours,
            force=args.force,
            limit=args.limit,
            browser_fallback=args.browser_fallback,
            browser_runtime=browser_runtime,
        )
    opportunity_updates = stats.dry_run_updates if dry_run else stats.notion_updates
    application_updates = (
        stats.dry_run_applications_cancelled if dry_run else stats.applications_cancelled
    )
    print(
        "Posting check: "
        f"candidates={stats.candidates} checked={stats.checked} "
        f"available={stats.available} closed={stats.closed} "
        f"stale_posted_closed={stats.stale_posted_closed} unknown={stats.unknown} "
        f"{'would update' if dry_run else 'updated'} opportunities={opportunity_updates} "
        f"{'would cancel' if dry_run else 'cancelled'} applications={application_updates} "
        f"skipped_recent={stats.skipped_recent} "
        f"skipped_missing_url={stats.skipped_missing_url} "
        f"errors={len(stats.errors)}"
    )
    for error in stats.errors[:10]:
        print(f"  - {error}")
    if dry_run:
        print("Re-run with --apply to write Closed statuses and cancel stale packets.")
    return 0 if not stats.errors else 1


def cmd_posting_date_backfill(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.pipeline.posting_date_backfill import run_posting_date_backfill

    store = open_store(settings.sqlite_path)
    with _open_notion(settings) as notion:
        stats = run_posting_date_backfill(
            settings,
            notion=notion,
            store=store,
            dry_run=dry_run,
            limit=args.limit,
            force=args.force,
            max_attempts=args.max_attempts,
        )
    opportunity_updates = stats.dry_run_updates if dry_run else stats.updated
    application_updates = (
        stats.dry_run_applications_cancelled if dry_run else stats.applications_cancelled
    )
    print(
        "Posting-date backfill: "
        f"candidates={stats.candidates} "
        f"{'would update' if dry_run else 'updated'} opportunities={opportunity_updates} "
        f"exact={stats.exact_dates} estimated={stats.estimated_dates} "
        f"closed={stats.closed} unknown={stats.unknown} "
        f"skipped_existing={stats.skipped_existing} "
        f"skipped_attempt_limit={stats.skipped_attempt_limit} "
        f"{'would cancel' if dry_run else 'cancelled'} applications={application_updates} "
        f"errors={len(stats.errors)}"
    )
    for error in stats.errors[:10]:
        print(f"  - {error}")
    if dry_run:
        print(
            "Re-run with --apply to write confirmed dates and review missing dates, "
            "and record attempts."
        )
    return 0 if not stats.errors else 1


def cmd_watch(args: argparse.Namespace) -> int:
    settings, _dry = _bootstrap(args)
    from job_search_hub.events import read_events

    events = read_events(settings.log_dir, limit=args.tail)
    if not events:
        print("(no events)")
        return 0
    for event in events:
        print(
            f"{event.get('occurred_at', '')} "
            f"{event.get('stage', ''):<14s} "
            f"{event.get('event', ''):<18s} "
            f"{event.get('message', '')}"
        )
    return 0


def cmd_llm_log(args: argparse.Namespace) -> int:
    settings, _dry = _bootstrap(args)
    store = open_store(settings.sqlite_path)
    rows = store.recent_llm_calls(limit=args.limit)
    if not rows:
        print("(no LLM calls recorded)")
        return 0
    for r in rows:
        valid = "-" if r["json_valid"] is None else ("yes" if r["json_valid"] else "no")
        fallback = " fallback" if r["fallback_used"] else ""
        print(
            f"{r['occurred_at']} [{r['stage'] or '-'}:{r['role'] or '-'}] "
            f"{r['model']} task={r['task'] or '-'} json={valid}{fallback} "
            f"latency={r['latency_ms'] or 0:.0f}ms"
        )
        if r["error"]:
            print(f"  error: {r['error']}")
        if args.verbose:
            print(f"  prompt: {(r['prompt_preview'] or '')[:500]}")
            print(f"  response: {(r['response_preview'] or '')[:500]}")
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.pipeline.ingest_stage import reconcile_notion_opportunities

    store = open_store(settings.sqlite_path)
    with _open_notion(settings) as notion:
        stats = reconcile_notion_opportunities(
            settings,
            notion=notion,
            store=store,
            dry_run=dry_run,
        )
    action = "would trash" if dry_run else "trashed"
    print(
        f"Scanned {stats.pages_scanned} Notion opportunities; "
        f"{action} {stats.duplicate_pages} duplicate pages; "
        f"updated {stats.fingerprints_updated} fingerprints."
    )
    if dry_run:
        print("Re-run with --apply to apply the cleanup.")
    return 0


def cmd_migrate_canonical_urls(args: argparse.Namespace) -> int:
    """Repair legacy supplemental URLs in small, dry-run-first batches."""
    settings, dry_run = _bootstrap(args)
    from job_search_hub.ingest.base import RawPosting
    from job_search_hub.notion import properties as P
    from job_search_hub.pipeline.canonical_resolution import canonical_identity, resolve_posting
    from job_search_hub.pipeline.lifecycle import read_status
    from job_search_hub.pipeline.normalize import fingerprint

    terminal = {"Closed", "Submitted", "Interviewing", "Offer", "Rejected", "Withdrawn"}
    supplemental = {"adzuna", "jooble", "remotive", "arbeitnow", "rss", "imap"}
    store = open_store(settings.sqlite_path)
    scanned = resolved = updated = skipped = 0
    with _open_notion(settings) as notion:
        for page in notion.query_data_source(settings.notion.data_sources["opportunities"]):
            if scanned >= args.limit:
                break
            props = page.get("properties") or {}
            adapter = P.read_select(props.get("Source Adapter")).casefold()
            if adapter not in supplemental or read_status(props.get("Status")) in terminal:
                continue
            url = P.read_url(props.get("URL"))
            if not url:
                skipped += 1
                continue
            scanned += 1
            posting = RawPosting(
                P.read_select(props.get("Source")),
                adapter,
                P.read_text(props.get("External Job ID")),
                P.read_text(props.get("Company")),
                P.read_text(props.get("Role Title")),
                url,
            )
            result = resolve_posting(
                posting,
                timeout=settings.direct_discovery.timeout_seconds,
                max_redirects=settings.direct_discovery.max_redirects,
                max_response_bytes=settings.direct_discovery.max_response_bytes,
            )
            if not result.accepted:
                skipped += 1
                continue
            resolved += 1
            posting.url = result.canonical_url
            posting.extras["canonical_identity"] = canonical_identity(posting, posting.url)
            if not dry_run:
                store.record_posting_provenance(
                    identity_key=posting.extras["canonical_identity"],
                    discovery_url=result.discovery_url,
                    canonical_url=result.canonical_url,
                    source_adapter=adapter,
                    source_quality=result.source_quality,
                    resolution_method=result.resolution_method,
                    verification_status="verified",
                    redirect_chain=result.redirect_chain,
                )
            if url != result.canonical_url:
                updated += 1
                if not dry_run:
                    notion.update_page(
                        page.get("id", ""),
                        {
                            "URL": P.url(result.canonical_url),
                            "Fingerprint": P.rich_text(fingerprint(posting)),
                        },
                    )
    print(
        f"Canonical URL migration: scanned={scanned} resolved={resolved} "
        f"{'would update' if dry_run else 'updated'}={updated} skipped={skipped}"
    )
    if dry_run:
        print("Re-run with --apply after reviewing the batch; terminal history is untouched.")
    return 0


def cmd_cleanup_opportunities(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.pipeline.cleanup import cleanup_opportunities, format_cleanup

    with _open_notion(settings) as notion:
        stats = cleanup_opportunities(
            settings,
            notion=notion,
            commit=not dry_run,
            include_irrelevant=args.include_irrelevant,
            migrate_statuses=args.migrate_statuses,
            example_limit=args.examples,
        )
    print(format_cleanup(stats, commit=not dry_run))
    return 0


def cmd_org_sync(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.pipeline.organizations import sync_opportunity_organizations

    with _open_notion(settings) as notion:
        stats = sync_opportunity_organizations(
            settings,
            notion=notion,
            dry_run=dry_run,
            limit=args.limit,
            create_missing=args.create_missing,
        )
    print(
        "Org sync: "
        f"scanned={stats.scanned} "
        f"{'would link' if dry_run else 'linked'}={stats.linked} "
        f"{'would_create' if dry_run else 'created'}="
        f"{stats.would_create if dry_run else stats.created} "
        f"{'would_link_existing' if dry_run else 'link_existing'}="
        f"{stats.would_link_existing if dry_run else stats.link_existing} "
        f"skipped_existing={stats.skipped_existing} "
        f"skipped_missing_company={stats.skipped_missing_company} "
        f"skipped_noisy={stats.skipped_noisy} "
        f"skipped_link_only_missing={stats.skipped_link_only_missing} "
        f"errors={len(stats.errors)}"
    )
    for key in (
        "would_create" if dry_run else "created",
        "would_link_existing" if dry_run else "link_existing",
        "skipped_noisy",
        "skipped_link_only_missing",
    ):
        examples = stats.examples.get(key, [])
        if examples:
            print(f"{key} examples:")
            for example in examples:
                print(f"  - {example}")
    for error in stats.errors[:10]:
        print(f"  - {error}")
    return 0 if not stats.errors else 1


def cmd_score(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.models import LMStudioClient, ModelRouter
    from job_search_hub.pipeline.progress import StagePhase, reset_progress
    from job_search_hub.pipeline.score_stage import run_score, send_answer_digest

    reset_progress(phases=(StagePhase.SCORE,))

    store = open_store(settings.sqlite_path)
    router: ModelRouter | None = None
    client: LMStudioClient | None = None
    if not getattr(args, "awaiting_only", False):
        try:
            client = LMStudioClient(
                settings.lmstudio,
                store=store,
                audit_mode=settings.llm_audit_mode,
            )
            router = ModelRouter(settings.lmstudio, client=client)
            router.discover()
        except Exception as exc:
            log.warning(
                "LM Studio unavailable; LLM-method criteria will fall back to neutral. %s",
                exc,
            )

    try:
        with _open_notion(settings) as notion:
            from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles

            notifier = _make_notifier(settings, dry_run=dry_run)
            if not getattr(args, "awaiting_only", False):
                reconcile_lifecycles(
                    settings,
                    notion=notion,
                    store=store,
                    notifier=notifier,
                    dry_run=dry_run,
                )
            if getattr(args, "awaiting_only", False):
                stats = run_score(
                    settings,
                    notion=notion,
                    store=store,
                    awaiting_only=True,
                    limit=getattr(args, "limit", None),
                    dry_run=dry_run,
                )
            else:
                stats = run_score(
                    settings,
                    notion=notion,
                    store=store,
                    awaiting_only=True,
                    limit=getattr(args, "limit", None),
                    dry_run=dry_run,
                )
                normal_stats = run_score(
                    settings,
                    notion=notion,
                    store=store,
                    router=router,
                    client=client,
                    rescore=getattr(args, "rescore", False),
                    limit=getattr(args, "limit", None),
                    notifier=notifier,
                    dry_run=dry_run,
                )
                stats.absorb(normal_stats)
            lifecycle_stats = reconcile_lifecycles(
                settings,
                notion=notion,
                store=store,
                notifier=notifier,
                dry_run=dry_run,
            )
            if lifecycle_stats.errors:
                stats.errors.extend(lifecycle_stats.errors)
            send_answer_digest(notifier, stats, dry_run=dry_run)
            if getattr(getattr(settings, "direct_outreach", None), "enabled", False):
                try:
                    from job_search_hub.pipeline.direct_outreach import run_direct_outreach

                    outreach = run_direct_outreach(
                        settings,
                        notion=notion,
                        store=store,
                        notifier=notifier,
                        dry_run=dry_run,
                    )
                    log.info(
                        "direct outreach: scanned=%d qualified=%d queued=%d suppressed=%d",
                        outreach.scanned,
                        outreach.qualified,
                        outreach.queued,
                        outreach.suppressed,
                    )
                except Exception as exc:
                    log.warning("direct outreach pass failed without stopping score: %s", exc)
    finally:
        if client is not None:
            client.close()

    log.info("score stats: %s", stats)
    ns.print_next_steps(
        "score",
        ns.steps_for_score(
            scored=stats.scored,
            by_lane=stats.by_lane,
            excluded=stats.excluded,
            criteria_active=stats.criteria_active,
            errors=stats.errors,
            dry_run=dry_run,
            degraded_evaluations=stats.degraded_evaluations,
            awaiting_answer=stats.awaiting_answer,
            awaiting_resolved=stats.awaiting_resolved,
            awaiting_requeued=stats.awaiting_requeued,
            posting_review=stats.posting_review,
            questions_created=stats.questions_created,
            questions_reused=stats.questions_reused,
        ),
    )

    return 0 if not stats.errors else 1


def cmd_migrate_qualifications(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.models import LMStudioClient, ModelRouter
    from job_search_hub.pipeline.qualification_migration import (
        backfill_awaiting_opportunities,
        defer_backfill_candidates,
        find_awaiting_backfill_candidates,
        migrate_answer_bank,
    )

    store = open_store(settings.sqlite_path)
    with _open_notion(settings) as notion:
        client = LMStudioClient(
            settings.lmstudio,
            store=store,
            audit_mode=settings.llm_audit_mode,
        )
        router = ModelRouter(settings.lmstudio, client=client)
        try:
            model_available = False
            try:
                router.discover()
                model_available = True
            except Exception as exc:
                log.warning("qualification migration model assist unavailable: %s", exc)
            stats = migrate_answer_bank(
                settings,
                notion=notion,
                store=store,
                dry_run=dry_run,
                router=router if model_available else None,
                client=client if model_available else None,
            )
            candidates = find_awaiting_backfill_candidates(
                settings,
                notion=notion,
                store=store,
                stats=stats,
                dry_run=dry_run,
            )
            if candidates:
                if not model_available:
                    defer_backfill_candidates(
                        candidates,
                        reason="LM Studio unavailable",
                        store=store,
                        stats=stats,
                        dry_run=dry_run,
                    )
                else:
                    backfill_awaiting_opportunities(
                        settings,
                        notion=notion,
                        store=store,
                        router=router,
                        client=client,
                        stats=stats,
                        dry_run=dry_run,
                        pages=candidates,
                    )
        finally:
            client.close()
    print(
        "Qualification migration: "
        f"answers={stats.answers_scanned} "
        f"canonicalized={stats.answers_canonicalized} "
        f"duplicate_groups={stats.duplicate_groups} "
        f"model_duplicate_groups={stats.model_duplicate_groups} "
        f"deprecated={stats.duplicates_deprecated} "
        f"conflicts={stats.conflicts} "
        f"opportunities_relinked={stats.opportunities_relinked} "
        f"applications_relinked={stats.applications_relinked} "
        f"requirements_relinked={stats.requirements_relinked} "
        f"awaiting_candidates={stats.awaiting_candidates} "
        f"awaiting_backfilled={stats.awaiting_backfilled} "
        f"awaiting_deferred={stats.awaiting_deferred} "
        f"errors={len(stats.errors)}"
    )
    for deferred in stats.deferred[:10]:
        print(f"  deferred: {deferred}")
    for error in stats.errors[:10]:
        print(f"  - {error}")
    return 0 if not stats.errors else 1


def cmd_upwork_auth(args: argparse.Namespace) -> int:
    """Run the one-time local OAuth consent flow for manual Upwork discovery."""
    settings, _dry_run = _bootstrap(args)
    from job_search_hub.upwork_auth import UpworkAuthError, begin_oauth, complete_oauth

    try:
        start = begin_oauth(settings.upwork)
        print("Open this URL in a browser to authorize the approved Upwork app:\n")
        print(start.authorization_url)
        if args.print_url:
            return 0
        print("\nWaiting for the local OAuth callback…")
        complete_oauth(settings.upwork, start, timeout_seconds=args.timeout_seconds)
    except UpworkAuthError as exc:
        print(f"Upwork authorization not completed: {exc}")
        return 1
    except Exception as exc:
        print(f"Upwork authorization failed: {exc}")
        return 1
    print("Upwork token saved in the operating system credential store. Enable its Notion source.")
    return 0


def cmd_migrate_verification_needed(args: argparse.Namespace) -> int:
    settings, dry_run = _bootstrap(args)
    from job_search_hub.pipeline.posting_verification import migrate_inconclusive_review_rows

    with _open_notion(settings) as notion:
        stats = migrate_inconclusive_review_rows(
            settings,
            notion,
            dry_run=dry_run,
            limit=args.limit,
        )
    verb = "would move" if dry_run else "moved"
    print(
        f"Verification Needed migration: candidates={stats.candidates} "
        f"{verb}={stats.dry_run_updates if dry_run else stats.notion_updates}"
    )
    return 0


def cmd_weekly_metrics(args: argparse.Namespace) -> int:
    settings, _dry_run = _bootstrap(args)
    from job_search_hub.pipeline.weekly_metrics import aggregate_week

    store = open_store(settings.sqlite_path)
    agg = aggregate_week(store)
    print(json.dumps(agg.__dict__, default=str, sort_keys=True))
    return 0
