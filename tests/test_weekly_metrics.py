"""Tests for weekly metrics aggregation from the audit log."""

import json
from datetime import UTC, datetime, timedelta

from job_search_hub.pipeline.weekly_metrics import aggregate_week


def _audit(store, *, action, payload, when=None):
    when = when or datetime.now(UTC)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(occurred_at, actor, action, target_kind, "
            "target_id, dry_run, payload_json) VALUES (?,?,?,?,?,?,?)",
            (when.isoformat(), "test", action, "thing", "x", 0, json.dumps(payload)),
        )


def test_aggregate_counts_lanes(tmp_store):
    today = datetime.now(UTC)
    _audit(tmp_store, action="score_opportunity", payload={"lane": "Worth a Look"}, when=today)
    _audit(tmp_store, action="score_opportunity", payload={"lane": "Fast Review"}, when=today)
    _audit(tmp_store, action="score_opportunity", payload={"lane": "High Attention"}, when=today)
    _audit(tmp_store, action="score_opportunity", payload={"lane": "Skip"}, when=today)
    _audit(tmp_store, action="create_opportunity", payload={"excluded": False}, when=today)
    _audit(tmp_store, action="create_opportunity", payload={"excluded": True}, when=today)

    agg = aggregate_week(tmp_store)
    assert agg.opportunities_scored == 4
    assert agg.worth_a_look == 1
    assert agg.fast_review == 1
    assert agg.high_attention == 1
    # one Skip from score + one excluded from create_opportunity
    assert agg.skipped == 2
    assert agg.postings_ingested == 2


def test_old_events_excluded(tmp_store):
    long_ago = datetime.now(UTC) - timedelta(days=60)
    _audit(tmp_store, action="score_opportunity", payload={"lane": "Worth a Look"}, when=long_ago)
    agg = aggregate_week(tmp_store)
    assert agg.opportunities_scored == 0


def test_manual_applications_and_submissions(tmp_store):
    today = datetime.now(UTC)
    _audit(tmp_store, action="prepare_application", payload={}, when=today)
    _audit(tmp_store, action="manual_application", payload={}, when=today)
    _audit(tmp_store, action="status", payload={"status": "Submitted"}, when=today)

    agg = aggregate_week(tmp_store)
    assert agg.manual_applications_created == 1
    assert agg.manual_submissions == 1


def test_sources_checked_comes_from_audit_log(tmp_store):
    today = datetime.now(UTC)
    _audit(tmp_store, action="source_checked", payload={"adapter": "adzuna"}, when=today)
    _audit(tmp_store, action="source_checked", payload={"adapter": "rss"}, when=today)

    agg = aggregate_week(tmp_store)

    assert agg.sources_checked == 2
