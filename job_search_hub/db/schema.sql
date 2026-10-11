-- Local SQLite cache, audit ledger, idempotency, and embeddings store.
-- Notion remains the editable source of truth.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Cached opportunity rows for dedupe + idempotency. Notion is canonical.
CREATE TABLE IF NOT EXISTS opportunities_cache (
    fingerprint TEXT PRIMARY KEY,
    notion_page_id TEXT,
    source TEXT NOT NULL,
    source_adapter TEXT NOT NULL,
    external_job_id TEXT,
    company TEXT,
    role_title TEXT,
    url TEXT,
    description_hash TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_pushed_to_notion TEXT,
    notion_version_token TEXT
);

CREATE INDEX IF NOT EXISTS idx_opps_external ON opportunities_cache (source_adapter, external_job_id);
CREATE INDEX IF NOT EXISTS idx_opps_company ON opportunities_cache (company, role_title);

-- Embeddings for dedupe + retrieval. Stored as raw bytes (float32 little-endian).
CREATE TABLE IF NOT EXISTS embeddings (
    namespace TEXT NOT NULL,   -- e.g. opportunity, answer_question, resume_fact
    key TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    text_preview TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace, key, model)
);

-- Cached Answer Bank rows for fast similarity lookup.
CREATE TABLE IF NOT EXISTS answer_cache (
    notion_page_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    normalized_question TEXT NOT NULL,
    question_family TEXT,
    answer_type TEXT,
    answer TEXT,
    status TEXT,
    approved_for_reuse INTEGER NOT NULL DEFAULT 0,
    sensitive INTEGER NOT NULL DEFAULT 0,
    context_specific INTEGER NOT NULL DEFAULT 0,
    reuse_threshold REAL,
    last_synced TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answer_family ON answer_cache (question_family, answer_type);

-- Cached Scoring Criteria rows. Read each tick.
CREATE TABLE IF NOT EXISTS scoring_criteria_cache (
    notion_page_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    machine_key TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    category TEXT,
    weight REAL,
    direction TEXT,
    evaluation_method TEXT,
    hard_exclusion INTEGER NOT NULL DEFAULT 0,
    lane_override TEXT,
    human_review_trigger INTEGER NOT NULL DEFAULT 0,
    instructions TEXT,
    minimum_value REAL,
    maximum_value REAL,
    sort_order INTEGER,
    last_synced TEXT NOT NULL
);

-- Cached Profile Facts. Filtered to active + verified + approved at use sites.
CREATE TABLE IF NOT EXISTS resume_facts_cache (
    notion_page_id TEXT PRIMARY KEY,
    fact TEXT NOT NULL,
    fact_id TEXT,
    fact_type TEXT,
    parent_fact_id TEXT,
    organization TEXT,
    role_label TEXT,
    relationship_type TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    approved_for_profile INTEGER NOT NULL DEFAULT 0,
    approved_for_applications INTEGER NOT NULL DEFAULT 0,
    metrics_verified INTEGER NOT NULL DEFAULT 0,
    sensitive INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    properties_json TEXT NOT NULL,  -- full property snapshot
    last_synced TEXT NOT NULL
);

-- Audit ledger: every action that changed state.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,        -- module name
    action TEXT NOT NULL,       -- e.g. ingest, score, lifecycle_sync, notify
    target_kind TEXT,           -- e.g. opportunity, application, answer
    target_id TEXT,             -- notion page id or fingerprint
    dry_run INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log (target_kind, target_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action, occurred_at);

-- Per-source health and rate-limit history. One row per source target check.
CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    source_key TEXT NOT NULL,
    adapter_key TEXT,
    target TEXT,
    status TEXT NOT NULL,
    configured INTEGER NOT NULL DEFAULT 0,
    postings_seen INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    rate_limit_remaining TEXT,
    retry_after_seconds REAL,
    next_allowed_at TEXT,
    error TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs (source_key, occurred_at);
CREATE INDEX IF NOT EXISTS idx_source_runs_status ON source_runs (status, occurred_at);

-- Retry ledger for posted-date backfill. One row per Opportunity page.
CREATE TABLE IF NOT EXISTS posting_date_attempts (
    opportunity_page_id TEXT PRIMARY KEY,
    source_adapter TEXT,
    url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL,
    last_status TEXT,
    last_reason TEXT,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_posting_date_attempts_status
ON posting_date_attempts (last_status, last_attempt_at);

-- Local posting-verification cadence.  Notion retains the operator-visible
-- state; this table prevents an ambiguous 403 from causing a write and a
-- browser launch on every scheduled run.
CREATE TABLE IF NOT EXISTS posting_verification_state (
    opportunity_page_id TEXT PRIMARY KEY,
    host TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    checked_at TEXT NOT NULL,
    next_check_at TEXT,
    unknown_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_posting_verification_due
ON posting_verification_state (next_check_at, checked_at);

CREATE TABLE IF NOT EXISTS posting_verification_hosts (
    host TEXT PRIMARY KEY,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    next_allowed_at TEXT,
    last_reason TEXT,
    updated_at TEXT NOT NULL
);

-- Local LLM audit trail. Prompt/response fields are redacted by default.
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    stage TEXT,
    role TEXT,
    task TEXT,
    model TEXT NOT NULL,
    prompt_hash TEXT,
    prompt_preview TEXT,
    response_preview TEXT,
    json_valid INTEGER,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_stage ON llm_calls (stage, role, occurred_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls (model, occurred_at);

-- Suppresses repeated, identical degraded structured-output attempts while a
-- local model is unhealthy.  Only a prompt hash and concise redacted reason
-- are retained; a changed input retries immediately.
CREATE TABLE IF NOT EXISTS llm_failure_cache (
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL,
    failed_at TEXT NOT NULL,
    next_retry_at TEXT NOT NULL,
    PRIMARY KEY (task, model, prompt_hash)
);

CREATE INDEX IF NOT EXISTS idx_llm_failure_cache_due
ON llm_failure_cache (next_retry_at);

-- Per-criterion scoring explanation rows.
CREATE TABLE IF NOT EXISTS score_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    opportunity_page_id TEXT NOT NULL,
    criterion_key TEXT NOT NULL,
    criterion_name TEXT,
    category TEXT,
    weight REAL NOT NULL,
    direction TEXT,
    evaluation_method TEXT,
    score REAL NOT NULL,
    weighted_effect REAL NOT NULL,
    reason TEXT,
    triggered_hard_exclusion INTEGER NOT NULL DEFAULT 0,
    triggered_human_review INTEGER NOT NULL DEFAULT 0,
    lane_override TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    llm_call_id INTEGER,
    dry_run INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_score_components_opp ON score_components (opportunity_page_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_score_components_key ON score_components (criterion_key, occurred_at);

-- Idempotency keys: prevents re-running the same operation in a single window.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (operation, key)
);

-- Reusable employer-career discovery state.  This is intentionally local:
-- Notion continues to own the review/application workflow.
CREATE TABLE IF NOT EXISTS source_target_state (
    target_url TEXT PRIMARY KEY,
    source_key TEXT,
    strategy TEXT,
    status TEXT NOT NULL,
    evidence_json TEXT,
    last_discovered_at TEXT,
    last_success_at TEXT,
    next_retry_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_target_state_due
ON source_target_state (next_retry_at, updated_at);

-- Keeps discovery provenance separate from the canonical Notion URL so a
-- direct posting can supersede an aggregator without losing audit context.
CREATE TABLE IF NOT EXISTS posting_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT,
    discovery_url TEXT NOT NULL,
    canonical_url TEXT,
    source_adapter TEXT,
    source_quality TEXT,
    resolution_method TEXT,
    verification_status TEXT,
    redirect_chain_json TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(source_adapter, discovery_url)
);

CREATE INDEX IF NOT EXISTS idx_posting_provenance_identity
ON posting_provenance (identity_key, last_seen);

-- Explicit, employer-provided direct-outreach opportunities.  Public contact
-- details are retained only for audit/digest generation and never logged.
CREATE TABLE IF NOT EXISTS direct_outreach_opportunities (
    job_key TEXT NOT NULL,
    contact_key TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    contact_name TEXT,
    contact_role TEXT,
    contact_email TEXT,
    instruction TEXT,
    confidence REAL NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_notified_at TEXT,
    PRIMARY KEY (job_key, contact_key)
);

CREATE TABLE IF NOT EXISTS direct_outreach_digests (
    digest_day TEXT PRIMARY KEY,
    sent_at TEXT,
    message_key TEXT,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
