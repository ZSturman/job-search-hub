"""Configuration loaded from user TOML, namespaced environment, and the credential store.

Settings are loaded once at process start. Missing required values fail loudly
when the affected feature is exercised, not at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from job_search_hub.storage import app_paths, get_setting, load_configuration

REPO_ROOT = Path(__file__).resolve().parent


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _auto_model(value: str | None) -> bool:
    return (value or "").strip().lower() in {"auto", "best", "available", "auto-ranked"}


def _split_list(value: str | None, *, separators: str = ",|;") -> list[str]:
    """Split env list values that may use comma, pipe, or semicolon."""
    if not value:
        return []
    import re

    pattern = "[" + re.escape(separators) + "]"
    return [item.strip() for item in re.split(pattern, value) if item.strip()]


def _path(value: str | None, default: str) -> Path:
    raw = value or default
    if raw == "data":
        return app_paths().data
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = app_paths().data / raw.removeprefix("data/")
    return p


@dataclass(frozen=True)
class NotionSettings:
    token: str
    api_version: str
    data_sources: dict[str, str]


@dataclass(frozen=True)
class LMStudioSettings:
    base_url: str
    api_key: str
    overrides: dict[str, str]  # role -> model id


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str
    use_tls: bool


@dataclass(frozen=True)
class IMAPSettings:
    host: str
    port: int
    username: str
    password: str
    mailbox: str
    alert_senders: list[str]


@dataclass(frozen=True)
class SearchProfile:
    """A named intake lane with a deliberately bounded share of new rows."""

    name: str
    role_terms: list[str]
    max_new_per_run: int
    skill_terms: list[str] = field(default_factory=list)
    mission_terms: list[str] = field(default_factory=list)
    excluded_terms: list[str] = field(default_factory=list)
    location: str = ""
    allowed_languages: list[str] = field(default_factory=lambda: ["en"])
    remote_only: bool = False
    min_annual: float = 0
    min_hourly: float = 0


@dataclass(frozen=True)
class SearchTermSettings:
    role_terms: list[str]
    skill_terms: list[str]
    mission_terms: list[str]
    profiles: tuple[SearchProfile, ...] = ()


@dataclass(frozen=True)
class DiscoverySettings:
    allowed_languages: list[str]
    language_min_confidence: float
    max_per_query: int
    max_per_source: int
    max_new_per_run: int
    max_new_per_company: int = 2


@dataclass(frozen=True)
class DirectDiscoverySettings:
    """Safety and pacing controls for employer-career source discovery."""

    enabled: bool
    timeout_seconds: float
    max_redirects: int
    max_response_bytes: int
    max_targets_per_run: int
    retry_hours: int
    rediscovery_days: int
    browser_enabled: bool


@dataclass(frozen=True)
class DirectOutreachSettings:
    """Conservative notification controls for explicit employer outreach."""

    enabled: bool
    minimum_score: float
    max_opportunities_per_pass: int
    max_digests_per_pass: int
    max_digests_per_day: int
    cooldown_days: int


@dataclass(frozen=True)
class UpworkSettings:
    """Optional, manual-only project discovery via an approved Upwork app."""

    enabled: bool
    client_id: str
    redirect_uri: str
    authorization_url: str
    token_url: str
    projects_api_url: str


@dataclass(frozen=True)
class AdapterCredentials:
    usajobs_user_agent: str
    usajobs_auth_key: str
    usajobs_keywords: list[str]
    usajobs_job_category_codes: list[str]
    usajobs_location: str
    usajobs_radius_miles: int
    usajobs_remote: bool
    usajobs_who_may_apply: str
    usajobs_date_posted_days: int
    adzuna_app_id: str
    adzuna_app_key: str
    adzuna_country: str
    adzuna_location: str
    adzuna_results_per_page: int
    adzuna_requests_per_minute: int
    jooble_api_key: str
    jooble_location: str
    jooble_radius_km: str
    greenhouse_boards: list[str]
    lever_companies: list[str]
    ashby_boards: list[str]
    recruitee_companies: list[str]
    smartrecruiters_companies: list[str]
    rss_feeds_file: Path
    watchlist_file: Path
    watchlist_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CaptureSettings:
    bind_host: str
    bind_port: int
    auth_token: str


@dataclass(frozen=True)
class Settings:
    timezone: str
    data_dir: Path
    log_dir: Path
    sqlite_path: Path
    notification_ledger_path: Path
    source_catalog_file: Path
    dry_run_default: bool
    hard_floor_annual_usd: int
    hard_floor_hourly_usd: int
    llm_audit_mode: str
    log_level: str
    score_workers: int
    notion: NotionSettings
    lmstudio: LMStudioSettings
    smtp: SMTPSettings
    imap: IMAPSettings
    adapters: AdapterCredentials
    search_terms: SearchTermSettings
    discovery: DiscoverySettings
    direct_discovery: DirectDiscoverySettings
    direct_outreach: DirectOutreachSettings
    upwork: UpworkSettings
    capture: CaptureSettings

    enabled_adapters: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = ()

    notion_db_keys: tuple[str, ...] = field(
        default=(
            "opportunities",
            "applications",
            "organizations",
            "resume_facts",
            "answer_bank",
            "qualification_requirements",
            "scoring_criteria",
        )
    )


_NOTION_ENV_KEYS = {
    "opportunities": "JOBHUB_NOTION_DS_OPPORTUNITIES",
    "applications": "JOBHUB_NOTION_DS_APPLICATIONS",
    "organizations": "JOBHUB_NOTION_DS_ORGANIZATIONS",
    "resume_facts": "JOBHUB_NOTION_DS_RESUME_FACTS",
    "answer_bank": "JOBHUB_NOTION_DS_ANSWER_BANK",
    "qualification_requirements": "JOBHUB_NOTION_DS_QUALIFICATION_REQUIREMENTS",
    "scoring_criteria": "JOBHUB_NOTION_DS_SCORING_CRITERIA",
}


DEFAULT_ROLE_TERMS = ()
DEFAULT_MEDIA_ROLE_TERMS = ()
DEFAULT_3D_HARDWARE_ROLE_TERMS = ()
DEFAULT_SKILL_TERMS = ()
DEFAULT_MISSION_TERMS = ()


def load_settings(env_file: Path | None = None) -> Settings:
    """Load independent operational settings; Notion overrides search preferences.

    Missing values are stored as empty strings; callers that need a value must
    validate it and raise a clear error if absent.
    """
    load_configuration(env_file=env_file)

    notion = NotionSettings(
        token=get_setting("JOBHUB_NOTION_TOKEN", ""),
        api_version=get_setting("JOBHUB_NOTION_API_VERSION", "2025-09-03"),
        data_sources={k: get_setting(env, "") for k, env in _NOTION_ENV_KEYS.items()},
    )

    lmstudio = LMStudioSettings(
        base_url=get_setting("JOBHUB_LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        api_key=get_setting("JOBHUB_LMSTUDIO_API_KEY", ""),
        overrides={
            role: raw
            for role in ("primary", "embedding", "review", "triage")
            if (raw := get_setting(f"JOBHUB_MODEL_{role.upper()}", "").strip())
            and not _auto_model(raw)
        },
    )

    smtp = SMTPSettings(
        host=get_setting("JOBHUB_SMTP_HOST", ""),
        port=_int(get_setting("JOBHUB_SMTP_PORT"), 587),
        username=get_setting("JOBHUB_SMTP_USERNAME", ""),
        password=get_setting("JOBHUB_SMTP_PASSWORD", ""),
        sender=get_setting("JOBHUB_SMTP_FROM", ""),
        recipient=get_setting("JOBHUB_SMTP_TO", ""),
        use_tls=_bool(get_setting("JOBHUB_SMTP_USE_TLS"), True),
    )

    imap = IMAPSettings(
        host=get_setting("JOBHUB_IMAP_HOST", ""),
        port=_int(get_setting("JOBHUB_IMAP_PORT"), 993),
        username=get_setting("JOBHUB_IMAP_USERNAME", ""),
        password=get_setting("JOBHUB_IMAP_PASSWORD", ""),
        mailbox=get_setting("JOBHUB_IMAP_MAILBOX", "INBOX"),
        alert_senders=_csv(get_setting("JOBHUB_IMAP_ALERT_SENDERS")),
    )

    roles = _split_list(get_setting("JOBHUB_JOB_SEARCH_ROLE_TERMS"))
    search_terms = SearchTermSettings(
        role_terms=roles,
        skill_terms=_split_list(get_setting("JOBHUB_JOB_SEARCH_SKILL_TERMS")),
        mission_terms=_split_list(get_setting("JOBHUB_JOB_SEARCH_MISSION_TERMS")),
        profiles=(SearchProfile("My search", roles, 50),),
    )
    discovery = DiscoverySettings(
        allowed_languages=[
            item.lower()
            for item in _split_list(
                get_setting("JOBHUB_JOB_ALLOWED_LANGUAGES", "en"), separators=",|;"
            )
        ],
        language_min_confidence=float(get_setting("JOBHUB_JOB_LANGUAGE_MIN_CONFIDENCE", "0.70")),
        max_per_query=_int(get_setting("JOBHUB_JOB_SEARCH_MAX_PER_QUERY"), 10),
        max_per_source=_int(get_setting("JOBHUB_JOB_SEARCH_MAX_PER_SOURCE"), 50),
        max_new_per_run=_int(get_setting("JOBHUB_JOB_SEARCH_MAX_NEW_PER_RUN"), 250),
        max_new_per_company=max(1, _int(get_setting("JOBHUB_JOB_SEARCH_MAX_NEW_PER_COMPANY"), 2)),
    )
    direct_discovery = DirectDiscoverySettings(
        enabled=_bool(get_setting("JOBHUB_DIRECT_DISCOVERY_ENABLED"), True),
        timeout_seconds=float(get_setting("JOBHUB_DIRECT_DISCOVERY_TIMEOUT_SECONDS", "15")),
        max_redirects=_int(get_setting("JOBHUB_DIRECT_DISCOVERY_MAX_REDIRECTS"), 8),
        max_response_bytes=_int(
            get_setting("JOBHUB_DIRECT_DISCOVERY_MAX_RESPONSE_BYTES"), 2_000_000
        ),
        max_targets_per_run=_int(get_setting("JOBHUB_DIRECT_DISCOVERY_MAX_TARGETS_PER_RUN"), 50),
        retry_hours=_int(get_setting("JOBHUB_DIRECT_DISCOVERY_RETRY_HOURS"), 12),
        rediscovery_days=_int(get_setting("JOBHUB_DIRECT_DISCOVERY_REDISCOVERY_DAYS"), 14),
        browser_enabled=_bool(get_setting("JOBHUB_DIRECT_DISCOVERY_BROWSER_ENABLED"), True),
    )
    direct_outreach = DirectOutreachSettings(
        enabled=_bool(get_setting("JOBHUB_DIRECT_OUTREACH_ENABLED"), False),
        minimum_score=float(get_setting("JOBHUB_DIRECT_OUTREACH_MINIMUM_SCORE", "0.70")),
        max_opportunities_per_pass=_int(get_setting("JOBHUB_DIRECT_OUTREACH_MAX_PER_PASS"), 8),
        max_digests_per_pass=_int(get_setting("JOBHUB_DIRECT_OUTREACH_MAX_DIGESTS_PER_PASS"), 1),
        max_digests_per_day=_int(get_setting("JOBHUB_DIRECT_OUTREACH_MAX_DIGESTS_PER_DAY"), 1),
        cooldown_days=_int(get_setting("JOBHUB_DIRECT_OUTREACH_COOLDOWN_DAYS"), 14),
    )
    upwork = UpworkSettings(
        enabled=_bool(get_setting("JOBHUB_UPWORK_ENABLED"), False),
        client_id=get_setting("JOBHUB_UPWORK_CLIENT_ID", ""),
        redirect_uri=get_setting("JOBHUB_UPWORK_REDIRECT_URI", "http://127.0.0.1:8788/callback"),
        authorization_url=get_setting("JOBHUB_UPWORK_OAUTH_AUTHORIZATION_URL", ""),
        token_url=get_setting("JOBHUB_UPWORK_OAUTH_TOKEN_URL", ""),
        projects_api_url=get_setting("JOBHUB_UPWORK_PROJECTS_API_URL", ""),
    )

    adapters = AdapterCredentials(
        usajobs_user_agent=get_setting("JOBHUB_USAJOBS_USER_AGENT", ""),
        usajobs_auth_key=get_setting("JOBHUB_USAJOBS_AUTH_KEY", ""),
        usajobs_keywords=_split_list(
            get_setting("JOBHUB_USAJOBS_KEYWORDS") or get_setting("JOBHUB_USAJOBS_KEYWORD"),
            separators="|,",
        ),
        usajobs_job_category_codes=_split_list(
            get_setting("JOBHUB_USAJOBS_JOB_CATEGORY_CODES"), separators=";,|"
        ),
        usajobs_location=get_setting("JOBHUB_USAJOBS_LOCATION", ""),
        usajobs_radius_miles=_int(get_setting("JOBHUB_USAJOBS_RADIUS_MILES"), 50),
        usajobs_remote=_bool(get_setting("JOBHUB_USAJOBS_REMOTE"), False),
        usajobs_who_may_apply=get_setting("JOBHUB_USAJOBS_WHO_MAY_APPLY", ""),
        usajobs_date_posted_days=_int(get_setting("JOBHUB_USAJOBS_DATE_POSTED_DAYS"), 0),
        adzuna_app_id=get_setting("JOBHUB_ADZUNA_APP_ID", ""),
        adzuna_app_key=get_setting("JOBHUB_ADZUNA_APP_KEY", ""),
        adzuna_country=get_setting("JOBHUB_ADZUNA_COUNTRY", "us"),
        adzuna_location=get_setting(
            "JOBHUB_ADZUNA_LOCATION", get_setting("JOBHUB_JOB_SEARCH_LOCATION", "")
        ),
        adzuna_results_per_page=_int(get_setting("JOBHUB_ADZUNA_RESULTS_PER_PAGE"), 25),
        adzuna_requests_per_minute=max(
            1, _int(get_setting("JOBHUB_ADZUNA_REQUESTS_PER_MINUTE"), 25)
        ),
        jooble_api_key=get_setting("JOBHUB_JOOBLE_API_KEY", ""),
        jooble_location=get_setting(
            "JOBHUB_JOOBLE_LOCATION", get_setting("JOBHUB_JOB_SEARCH_LOCATION", "")
        ),
        jooble_radius_km=get_setting("JOBHUB_JOOBLE_RADIUS_KM", "40"),
        greenhouse_boards=_csv(get_setting("JOBHUB_GREENHOUSE_BOARDS")),
        lever_companies=_csv(get_setting("JOBHUB_LEVER_COMPANIES")),
        ashby_boards=_csv(get_setting("JOBHUB_ASHBY_BOARDS")),
        recruitee_companies=_csv(get_setting("JOBHUB_RECRUITEE_COMPANIES")),
        smartrecruiters_companies=_csv(get_setting("JOBHUB_SMARTRECRUITERS_COMPANIES")),
        rss_feeds_file=_path(get_setting("JOBHUB_RSS_FEEDS_FILE"), "data/rss_feeds.txt"),
        watchlist_file=_path(get_setting("JOBHUB_WATCHLIST_FILE"), "data/watchlist.txt"),
        watchlist_urls=[],
    )

    capture = CaptureSettings(
        bind_host=get_setting("JOBHUB_CAPTURE_BIND_HOST", "127.0.0.1"),
        bind_port=_int(get_setting("JOBHUB_CAPTURE_BIND_PORT"), 8766),
        auth_token=get_setting("JOBHUB_CAPTURE_AUTH_TOKEN", ""),
    )

    return Settings(
        timezone=get_setting("JOBHUB_APP_TIMEZONE", "UTC"),
        data_dir=_path(get_setting("JOBHUB_DATA_DIR"), "data"),
        log_dir=_path(get_setting("JOBHUB_LOG_DIR"), "logs"),
        sqlite_path=_path(get_setting("JOBHUB_SQLITE_PATH"), "data/job_search_hub.sqlite"),
        notification_ledger_path=_path(
            get_setting("JOBHUB_NOTIFICATION_LEDGER_PATH"), "data/notification_ledger.sqlite"
        ),
        source_catalog_file=_path(
            get_setting(
                "JOBHUB_SOURCE_CATALOG_FILE", str(REPO_ROOT / "resources/source_catalog.yaml")
            ),
            "source_catalog.yaml",
        ),
        dry_run_default=_bool(get_setting("JOBHUB_DRY_RUN_DEFAULT"), True),
        hard_floor_annual_usd=_int(get_setting("JOBHUB_HARD_FLOOR_COMP_USD_ANNUAL"), 0),
        hard_floor_hourly_usd=_int(get_setting("JOBHUB_HARD_FLOOR_COMP_USD_HOURLY"), 0),
        llm_audit_mode=get_setting("JOBHUB_LLM_AUDIT_MODE", "off"),
        log_level=get_setting("JOBHUB_LOG_LEVEL", "INFO"),
        score_workers=min(8, max(1, _int(get_setting("JOBHUB_SCORE_WORKERS"), 2))),
        notion=notion,
        lmstudio=lmstudio,
        smtp=smtp,
        imap=imap,
        adapters=adapters,
        search_terms=search_terms,
        discovery=discovery,
        direct_discovery=direct_discovery,
        direct_outreach=direct_outreach,
        upwork=upwork,
        capture=capture,
    )


def ensure_directories(settings: Settings) -> None:
    """Create operational directories if they don't exist."""
    for directory in (
        settings.data_dir,
        settings.log_dir,
        settings.sqlite_path.parent,
        settings.notification_ledger_path.parent,
        settings.source_catalog_file.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
