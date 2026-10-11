"""SQLite operational store.

Notion is the editable source of truth. SQLite holds the local cache, audit
ledger, idempotency keys, and embeddings.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


@dataclass
class Store:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self) -> None:
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(ddl)
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
                )

    # ----- audit -----
    def audit(
        self,
        *,
        actor: str,
        action: str,
        target_kind: str | None = None,
        target_id: str | None = None,
        dry_run: bool = False,
        payload: dict | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO audit_log
                   (occurred_at, actor, action, target_kind, target_id, dry_run, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    actor,
                    action,
                    target_kind,
                    target_id,
                    1 if dry_run else 0,
                    json.dumps(payload) if payload is not None else None,
                ),
            )

    # ----- source health -----
    def record_source_run(
        self,
        *,
        source_key: str,
        adapter_key: str | None = None,
        target: str | None = None,
        status: str,
        configured: bool,
        postings_seen: int = 0,
        http_status: int | None = None,
        rate_limit_remaining: str | None = None,
        retry_after_seconds: float | None = None,
        next_allowed_at: str | None = None,
        error: str | None = None,
        notified: bool = False,
        payload: dict | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO source_runs
                   (occurred_at, source_key, adapter_key, target, status, configured,
                    postings_seen, http_status, rate_limit_remaining,
                    retry_after_seconds, next_allowed_at, error, notified, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    source_key,
                    adapter_key,
                    target,
                    status,
                    1 if configured else 0,
                    postings_seen,
                    http_status,
                    rate_limit_remaining,
                    retry_after_seconds,
                    next_allowed_at,
                    error,
                    1 if notified else 0,
                    json.dumps(payload) if payload is not None else None,
                ),
            )
            return int(cur.lastrowid)

    def recent_source_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM source_runs ORDER BY occurred_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def latest_source_run(
        self,
        *,
        source_key: str,
        target: str | None = None,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            if target is None:
                return conn.execute(
                    """SELECT * FROM source_runs
                       WHERE source_key=?
                       ORDER BY occurred_at DESC LIMIT 1""",
                    (source_key,),
                ).fetchone()
            return conn.execute(
                """SELECT * FROM source_runs
                   WHERE source_key=? AND target=?
                   ORDER BY occurred_at DESC LIMIT 1""",
                (source_key, target),
            ).fetchone()

    # ----- direct-source discovery / provenance -----
    def source_target_state(self, target_url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM source_target_state WHERE target_url=?", (target_url,)
            ).fetchone()

    def recent_source_target_states(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM source_target_state ORDER BY updated_at DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()

    def upsert_source_target_state(
        self,
        *,
        target_url: str,
        source_key: str,
        strategy: str,
        status: str,
        evidence: dict | None = None,
        last_success_at: str | None = None,
        next_retry_at: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO source_target_state
                   (target_url, source_key, strategy, status, evidence_json,
                    last_discovered_at, last_success_at, next_retry_at, last_error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(target_url) DO UPDATE SET
                     source_key=excluded.source_key,
                     strategy=excluded.strategy,
                     status=excluded.status,
                     evidence_json=excluded.evidence_json,
                     last_discovered_at=excluded.last_discovered_at,
                     last_success_at=COALESCE(
                         excluded.last_success_at, source_target_state.last_success_at),
                     next_retry_at=excluded.next_retry_at,
                     last_error=excluded.last_error,
                     updated_at=excluded.updated_at""",
                (
                    target_url,
                    source_key,
                    strategy,
                    status,
                    json.dumps(evidence or {}),
                    now,
                    last_success_at,
                    next_retry_at,
                    (last_error or "")[:1900],
                    now,
                ),
            )

    def record_posting_provenance(
        self,
        *,
        identity_key: str,
        discovery_url: str,
        canonical_url: str,
        source_adapter: str,
        source_quality: str,
        resolution_method: str,
        verification_status: str,
        redirect_chain: list[str] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO posting_provenance
                   (identity_key, discovery_url, canonical_url, source_adapter,
                    source_quality, resolution_method, verification_status,
                    redirect_chain_json, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_adapter, discovery_url) DO UPDATE SET
                     identity_key=excluded.identity_key,
                     canonical_url=excluded.canonical_url,
                     source_quality=excluded.source_quality,
                     resolution_method=excluded.resolution_method,
                     verification_status=excluded.verification_status,
                     redirect_chain_json=excluded.redirect_chain_json,
                     last_seen=excluded.last_seen""",
                (
                    identity_key,
                    discovery_url,
                    canonical_url,
                    source_adapter,
                    source_quality,
                    resolution_method,
                    verification_status,
                    json.dumps(redirect_chain or []),
                    now,
                    now,
                ),
            )

    def provenance_for_url(self, url: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM posting_provenance
                   WHERE canonical_url=? OR discovery_url=?
                   ORDER BY last_seen DESC LIMIT 1""",
                (url, url),
            ).fetchone()

    # ----- direct outreach -----
    def direct_outreach_opportunity(self, job_key: str, contact_key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM direct_outreach_opportunities
                   WHERE job_key=? AND contact_key=?""",
                (job_key, contact_key),
            ).fetchone()

    def record_direct_outreach(
        self,
        *,
        job_key: str,
        contact_key: str,
        evidence_hash: str,
        contact_name: str,
        contact_role: str,
        contact_email: str,
        instruction: str,
        confidence: float,
    ) -> sqlite3.Row | None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            previous = conn.execute(
                """SELECT * FROM direct_outreach_opportunities
                   WHERE job_key=? AND contact_key=?""",
                (job_key, contact_key),
            ).fetchone()
            conn.execute(
                """INSERT INTO direct_outreach_opportunities
                   (job_key, contact_key, evidence_hash, contact_name, contact_role,
                    contact_email, instruction, confidence, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_key, contact_key) DO UPDATE SET
                     evidence_hash=excluded.evidence_hash,
                     contact_name=excluded.contact_name,
                     contact_role=excluded.contact_role,
                     contact_email=excluded.contact_email,
                     instruction=excluded.instruction,
                     confidence=excluded.confidence,
                     last_seen=excluded.last_seen""",
                (
                    job_key,
                    contact_key,
                    evidence_hash,
                    contact_name,
                    contact_role,
                    contact_email,
                    instruction,
                    confidence,
                    now,
                    now,
                ),
            )
            return previous

    def mark_direct_outreach_notified(self, job_key: str, contact_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE direct_outreach_opportunities SET last_notified_at=?
                   WHERE job_key=? AND contact_key=?""",
                (datetime.now(UTC).isoformat(), job_key, contact_key),
            )

    def direct_outreach_digest_count(self, digest_day: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS count FROM direct_outreach_digests
                   WHERE digest_day=? AND status IN ('queued', 'sent')""",
                (digest_day,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def record_direct_outreach_digest(
        self,
        *,
        digest_day: str,
        status: str,
        message_key: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO direct_outreach_digests
                   (digest_day, sent_at, message_key, status, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(digest_day) DO UPDATE SET
                     sent_at=excluded.sent_at,
                     message_key=excluded.message_key,
                     status=excluded.status,
                     updated_at=excluded.updated_at""",
                (digest_day, now if status == "sent" else None, message_key, status, now),
            )

    # ----- posted-date backfill attempts -----
    def posting_date_attempt(self, opportunity_page_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM posting_date_attempts WHERE opportunity_page_id=?",
                (opportunity_page_id,),
            ).fetchone()

    # ----- posting verification cadence -----
    def posting_verification_state(self, opportunity_page_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM posting_verification_state WHERE opportunity_page_id=?",
                (opportunity_page_id,),
            ).fetchone()

    def posting_verification_due(
        self,
        opportunity_page_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        state = self.posting_verification_state(opportunity_page_id)
        if state is None or not state["next_check_at"]:
            return True
        try:
            due_at = datetime.fromisoformat(str(state["next_check_at"]))
        except ValueError:
            return True
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        return due_at <= (now or datetime.now(UTC))

    def host_verification_due(self, host: str, *, now: datetime | None = None) -> bool:
        if not host:
            return True
        with self.connect() as conn:
            state = conn.execute(
                "SELECT next_allowed_at FROM posting_verification_hosts WHERE host=?", (host,)
            ).fetchone()
        if state is None or not state["next_allowed_at"]:
            return True
        try:
            due_at = datetime.fromisoformat(str(state["next_allowed_at"]))
        except ValueError:
            return True
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        return due_at <= (now or datetime.now(UTC))

    def record_posting_verification(
        self,
        *,
        opportunity_page_id: str,
        host: str,
        status: str,
        reason: str,
        checked_at: datetime,
    ) -> None:
        """Persist a conservative retry schedule for one posting and host."""
        is_unknown = status == "unknown"
        now = datetime.now(UTC)
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT unknown_count FROM posting_verification_state WHERE opportunity_page_id=?",
                (opportunity_page_id,),
            ).fetchone()
            unknown_count = (
                (int(previous["unknown_count"]) if previous else 0) + 1 if is_unknown else 0
            )
            # 1h, 6h, 24h, then a maximum of seven days.  Definitive and
            # available results are still recorded, but they do not impose a
            # retry delay.
            delay_hours = min(
                24 * 7,
                1 if unknown_count <= 1 else 6 * (4 ** (unknown_count - 2)),
            )
            next_check = (now + timedelta(hours=delay_hours)).isoformat() if is_unknown else None
            conn.execute(
                """INSERT INTO posting_verification_state
                   (opportunity_page_id, host, status, reason, checked_at,
                    next_check_at, unknown_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(opportunity_page_id) DO UPDATE SET
                     host=excluded.host, status=excluded.status, reason=excluded.reason,
                     checked_at=excluded.checked_at, next_check_at=excluded.next_check_at,
                     unknown_count=excluded.unknown_count""",
                (
                    opportunity_page_id,
                    host,
                    status,
                    reason[:1200],
                    checked_at.astimezone(UTC).isoformat(),
                    next_check,
                    unknown_count,
                ),
            )
            if host:
                host_previous = conn.execute(
                    "SELECT unknown_count FROM posting_verification_hosts WHERE host=?", (host,)
                ).fetchone()
                host_unknowns = (
                    (int(host_previous["unknown_count"]) if host_previous else 0) + 1
                    if is_unknown
                    else 0
                )
                host_delay = min(
                    24 * 7,
                    1 if host_unknowns <= 1 else 6 * (4 ** (host_unknowns - 2)),
                )
                host_next = (now + timedelta(hours=host_delay)).isoformat() if is_unknown else None
                conn.execute(
                    """INSERT INTO posting_verification_hosts
                       (host, unknown_count, next_allowed_at, last_reason, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(host) DO UPDATE SET
                         unknown_count=excluded.unknown_count,
                         next_allowed_at=excluded.next_allowed_at,
                         last_reason=excluded.last_reason, updated_at=excluded.updated_at""",
                    (host, host_unknowns, host_next, reason[:1200], now.isoformat()),
                )

    # ----- resume PDF sync state -----
    def record_posting_date_attempt(
        self,
        *,
        opportunity_page_id: str,
        source_adapter: str,
        url: str,
        status: str,
        reason: str,
        resolved: bool = False,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        resolved_at = now if resolved else None
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO posting_date_attempts
                   (opportunity_page_id, source_adapter, url, attempts,
                    last_attempt_at, last_status, last_reason, resolved_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                   ON CONFLICT (opportunity_page_id) DO UPDATE SET
                     source_adapter=excluded.source_adapter,
                     url=excluded.url,
                     attempts=posting_date_attempts.attempts + 1,
                     last_attempt_at=excluded.last_attempt_at,
                     last_status=excluded.last_status,
                     last_reason=excluded.last_reason,
                     resolved_at=COALESCE(excluded.resolved_at, posting_date_attempts.resolved_at)
                """,
                (
                    opportunity_page_id,
                    source_adapter,
                    url,
                    now,
                    status,
                    reason[:1900],
                    resolved_at,
                ),
            )
            row = conn.execute(
                "SELECT attempts FROM posting_date_attempts WHERE opportunity_page_id=?",
                (opportunity_page_id,),
            ).fetchone()
            return int(row["attempts"] if row is not None else 0)

    # ----- LLM observability -----
    def record_llm_call(
        self,
        *,
        stage: str | None,
        role: str | None,
        task: str | None,
        model: str,
        prompt_hash: str | None,
        prompt_preview: str | None,
        response_preview: str | None,
        json_valid: bool | None,
        fallback_used: bool = False,
        latency_ms: float | None = None,
        error: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO llm_calls
                   (occurred_at, stage, role, task, model, prompt_hash,
                    prompt_preview, response_preview, json_valid, fallback_used,
                    latency_ms, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    stage,
                    role,
                    task,
                    model,
                    prompt_hash,
                    prompt_preview,
                    response_preview,
                    None if json_valid is None else (1 if json_valid else 0),
                    1 if fallback_used else 0,
                    latency_ms,
                    error,
                ),
            )
            return int(cur.lastrowid)

    def recent_llm_calls(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM llm_calls ORDER BY occurred_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def cached_llm_failure(self, *, task: str, model: str, prompt_hash: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT last_error, next_retry_at FROM llm_failure_cache
                   WHERE task=? AND model=? AND prompt_hash=?""",
                (task, model, prompt_hash),
            ).fetchone()
        if row is None:
            return None
        try:
            retry_at = datetime.fromisoformat(str(row["next_retry_at"]))
        except ValueError:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return str(row["last_error"]) if retry_at > datetime.now(UTC) else None

    def record_llm_failure(
        self,
        *,
        task: str,
        model: str,
        prompt_hash: str,
        error: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.connect() as conn:
            row = conn.execute(
                """SELECT failure_count FROM llm_failure_cache
                   WHERE task=? AND model=? AND prompt_hash=?""",
                (task, model, prompt_hash),
            ).fetchone()
            count = (int(row["failure_count"]) if row else 0) + 1
            delay_minutes = min(24 * 60, 15 * (4 ** min(count - 1, 4)))
            conn.execute(
                """INSERT INTO llm_failure_cache
                   (task, model, prompt_hash, failure_count, last_error, failed_at, next_retry_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(task, model, prompt_hash) DO UPDATE SET
                     failure_count=excluded.failure_count, last_error=excluded.last_error,
                     failed_at=excluded.failed_at, next_retry_at=excluded.next_retry_at""",
                (
                    task,
                    model,
                    prompt_hash,
                    count,
                    error[:500],
                    now.isoformat(),
                    (now + timedelta(minutes=delay_minutes)).isoformat(),
                ),
            )

    def clear_llm_failure(self, *, task: str, model: str, prompt_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM llm_failure_cache WHERE task=? AND model=? AND prompt_hash=?",
                (task, model, prompt_hash),
            )

    # ----- score components -----
    def record_score_component(
        self,
        *,
        opportunity_page_id: str,
        criterion_key: str,
        criterion_name: str,
        category: str,
        weight: float,
        direction: str,
        evaluation_method: str,
        score: float,
        weighted_effect: float,
        reason: str,
        triggered_hard_exclusion: bool,
        triggered_human_review: bool,
        lane_override: str,
        fallback_used: bool,
        error: str,
        llm_call_id: int | None,
        dry_run: bool,
    ) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO score_components
                   (occurred_at, opportunity_page_id, criterion_key, criterion_name,
                    category, weight, direction, evaluation_method, score,
                    weighted_effect, reason, triggered_hard_exclusion,
                    triggered_human_review, lane_override, fallback_used, error,
                    llm_call_id, dry_run)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    opportunity_page_id,
                    criterion_key,
                    criterion_name,
                    category,
                    weight,
                    direction,
                    evaluation_method,
                    score,
                    weighted_effect,
                    reason,
                    1 if triggered_hard_exclusion else 0,
                    1 if triggered_human_review else 0,
                    lane_override,
                    1 if fallback_used else 0,
                    error,
                    llm_call_id,
                    1 if dry_run else 0,
                ),
            )
            return int(cur.lastrowid)

    def latest_score_components(self, opportunity_page_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT sc.*
                   FROM score_components sc
                   JOIN (
                     SELECT criterion_key, MAX(id) AS id
                     FROM score_components
                     WHERE opportunity_page_id=?
                     GROUP BY criterion_key
                   ) latest ON latest.id = sc.id
                   ORDER BY ABS(sc.weight) DESC, sc.criterion_key""",
                (opportunity_page_id,),
            ).fetchall()

    # ----- idempotency -----
    def claim_idempotency(self, operation: str, key: str, ttl_seconds: int = 3600) -> bool:
        """Return True if the caller now owns the key. False if already held."""
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT expires_at FROM idempotency_keys WHERE operation=? AND key=?",
                (operation, key),
            ).fetchone()
            if existing and existing["expires_at"] > now.isoformat():
                return False
            conn.execute(
                """INSERT INTO idempotency_keys (operation, key, expires_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT (operation, key) DO UPDATE SET expires_at=excluded.expires_at""",
                (operation, key, expires),
            )
            return True

    # ----- opportunities cache -----
    def upsert_opportunity_cache(
        self,
        *,
        fingerprint: str,
        source: str,
        source_adapter: str,
        external_job_id: str | None,
        company: str | None,
        role_title: str | None,
        url: str | None,
        description_hash: str | None,
        notion_page_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO opportunities_cache
                   (fingerprint, notion_page_id, source, source_adapter, external_job_id,
                    company, role_title, url, description_hash, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (fingerprint) DO UPDATE SET
                     last_seen = excluded.last_seen,
                     notion_page_id = COALESCE(
                         excluded.notion_page_id, opportunities_cache.notion_page_id),
                     description_hash = excluded.description_hash""",
                (
                    fingerprint,
                    notion_page_id,
                    source,
                    source_adapter,
                    external_job_id,
                    company,
                    role_title,
                    url,
                    description_hash,
                    now,
                    now,
                ),
            )

    def find_opportunity_by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM opportunities_cache WHERE fingerprint=?", (fingerprint,)
            ).fetchone()

    def remove_opportunity_cache_pages(self, page_ids: list[str]) -> None:
        page_ids = [page_id for page_id in page_ids if page_id]
        if not page_ids:
            return
        placeholders = ",".join("?" for _ in page_ids)
        with self.connect() as conn:
            conn.execute(
                f"DELETE FROM opportunities_cache WHERE notion_page_id IN ({placeholders})",
                page_ids,
            )

    def remove_stale_opportunity_fingerprints(
        self,
        *,
        notion_page_id: str,
        fingerprint: str,
    ) -> None:
        if not notion_page_id:
            return
        with self.connect() as conn:
            conn.execute(
                """DELETE FROM opportunities_cache
                   WHERE notion_page_id=? AND fingerprint<>?""",
                (notion_page_id, fingerprint),
            )

    def prune_opportunity_cache(self, valid_fingerprints: set[str]) -> None:
        with self.connect() as conn:
            conn.execute(
                """CREATE TEMP TABLE valid_opportunity_fingerprints (
                       fingerprint TEXT PRIMARY KEY
                   )"""
            )
            conn.executemany(
                "INSERT INTO valid_opportunity_fingerprints (fingerprint) VALUES (?)",
                ((fingerprint,) for fingerprint in valid_fingerprints),
            )
            conn.execute(
                """DELETE FROM opportunities_cache
                   WHERE fingerprint NOT IN (
                       SELECT fingerprint FROM valid_opportunity_fingerprints
                   )"""
            )

    # ----- embeddings -----
    def store_embedding(
        self,
        *,
        namespace: str,
        key: str,
        model: str,
        vector: list[float],
        text_preview: str | None = None,
    ) -> None:
        blob = struct.pack(f"<{len(vector)}f", *vector)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO embeddings
                   (namespace, key, model, dim, vector, text_preview, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (namespace, key, model) DO UPDATE SET
                     vector=excluded.vector,
                     dim=excluded.dim,
                     text_preview=excluded.text_preview,
                     created_at=excluded.created_at""",
                (
                    namespace,
                    key,
                    model,
                    len(vector),
                    blob,
                    text_preview,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_embeddings(self, namespace: str, model: str) -> list[tuple[str, list[float]]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, dim, vector FROM embeddings WHERE namespace=? AND model=?",
                (namespace, model),
            ).fetchall()
        result = []
        for r in rows:
            vec = list(struct.unpack(f"<{r['dim']}f", r["vector"]))
            result.append((r["key"], vec))
        return result


def open_store(path: Path) -> Store:
    return Store(path=path)
