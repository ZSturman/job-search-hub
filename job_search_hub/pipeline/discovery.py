"""Deterministic pre-Notion posting qualification, ranking, and caps."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from job_search_hub.config import Settings
from job_search_hub.ingest.base import RawPosting
from job_search_hub.pipeline.normalize import fingerprint

_SPACE_RE = re.compile(r"\s+")
_SENTENCE_RE = re.compile(r"(?:[.!?]\s+|\n+)")
_TARGET_MARKET_CONTEXT_RE = re.compile(
    r"\b(?:based|located|remote|reside|resident|residency|eligible|eligibility|"
    r"authorized|authorised|work authorization|work authorisation|applicants?|"
    r"candidates?|employees?|hiring|position|role|location)\b",
    re.I,
)
_US_MARKET_RE = re.compile(
    r"\b(?:united states|u\.?s\.?a?\.?|us[- ](?:only|based)|america(?:n)?)\b",
    re.I,
)

# Common job-market country names and aliases. This is intentionally focused
# on employment targeting rather than attempting to be a geopolitical parser.
_NON_US_COUNTRY_ALIASES = (
    "afghanistan",
    "albania",
    "algeria",
    "argentina",
    "armenia",
    "australia",
    "austria",
    "bangladesh",
    "belarus",
    "belgium",
    "bolivia",
    "brazil",
    "bulgaria",
    "cambodia",
    "cameroon",
    "canada",
    "chile",
    "china",
    "colombia",
    "costa rica",
    "croatia",
    "cyprus",
    "czech republic",
    "czechia",
    "denmark",
    "dominican republic",
    "ecuador",
    "egypt",
    "estonia",
    "ethiopia",
    "finland",
    "france",
    "germany",
    "ghana",
    "greece",
    "guatemala",
    "hong kong",
    "hungary",
    "iceland",
    "india",
    "indonesia",
    "ireland",
    "israel",
    "italy",
    "jamaica",
    "japan",
    "jordan",
    "kenya",
    "latvia",
    "lebanon",
    "lithuania",
    "luxembourg",
    "malaysia",
    "malta",
    "mexico",
    "moldova",
    "morocco",
    "netherlands",
    "new zealand",
    "nigeria",
    "north macedonia",
    "norway",
    "pakistan",
    "panama",
    "peru",
    "philippines",
    "poland",
    "portugal",
    "qatar",
    "romania",
    "saudi arabia",
    "serbia",
    "singapore",
    "slovakia",
    "slovenia",
    "south africa",
    "south korea",
    "spain",
    "sri lanka",
    "sweden",
    "switzerland",
    "taiwan",
    "thailand",
    "tunisia",
    "turkey",
    "ukraine",
    "united arab emirates",
    "united kingdom",
    "uruguay",
    "venezuela",
    "vietnam",
)
_NON_US_REGION_RE = re.compile(
    r"\b(?:emea|apac|latam|europe(?:an)?|asia(?:n)?|africa(?:n)?|"
    r"middle east|uk[- ]only|eu[- ]only)\b",
    re.I,
)

_EXECUTIVE_TITLE_PATTERNS = (
    re.compile(r"\bpresident\b", re.I),
    re.compile(r"\bvice[\s-]+president\b", re.I),
    re.compile(r"\bvp\b", re.I),
    re.compile(
        r"\b(?:chief executive|chief operating|chief financial|chief technology|"
        r"chief information|chief marketing|chief revenue|chief product|"
        r"chief people|chief security) officer\b",
        re.I,
    ),
    re.compile(r"\b(?:ceo|coo|cfo|cto|cio|cmo|cro|cpo|ciso)\b", re.I),
)

_SALES_TITLE_PATTERNS = (
    re.compile(r"\bsales\b", re.I),
    re.compile(r"\baccount (?:executive|manager|director|representative)\b", re.I),
    re.compile(r"\bbusiness development\b", re.I),
    re.compile(r"\b(?:sdr|bdr)\b", re.I),
    re.compile(r"\bpre[\s-]?sales\b", re.I),
)

_GENERAL_TECH_TITLE_PATTERNS = (
    re.compile(r"\b(?:software|frontend|front end|full stack|mobile|ios|swift)\b", re.I),
    re.compile(r"\b(?:engineer|developer|sdet|qa|test automation)\b", re.I),
    re.compile(r"\b(?:developer tools|internal tools|integration|applications)\b", re.I),
    re.compile(r"\b(?:ai evaluation|quality engineering|research tools)\b", re.I),
)

_PLAUSIBLE_FIT_TITLE_PATTERNS = (
    re.compile(
        r"\b(?:software|front[\s-]?end|full[\s-]?stack|web(?: applications?)?|"
        r"mobile|ios|swift|react native|python|typescript|product)\s+"
        r"(?:[\w+#.-]+\s+){0,3}(?:engineer|developer)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:engineer|developer)\b.*\b(?:software|front[\s-]?end|"
        r"full[\s-]?stack|web applications?|mobile|ios|swift|react native|"
        r"python|typescript|product)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:sdet|qa(?: automation)? engineer|test automation engineer|"
        r"software (?:development )?engineer in test|software test engineer|"
        r"automation qa engineer|quality engineer)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:devops(?: and tools)?|ml[\s-]?ops|site reliability|sre|platform|"
        r"cloud|infrastructure|"
        r"developer tools?|internal tools?|api integration|workflow automation|"
        r"build|release|systems?|tools development)\s+"
        r"(?:engineer|developer|lead)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:ai|ml|machine learning|agent|computer vision|data|analytics|bi|"
        r"data visualization|scientific software|scientific computing|simulation|"
        r"application security|product security|security)\s+"
        r"(?:[\w/+-]+\s+){0,3}(?:engineer|developer|evaluator)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:engineer|developer)\b.*\b(?:ai|ml|machine learning|agents?|"
        r"computer vision|data visualization|scientific computing|security|"
        r"automation|developer tools?|internal tools?)\b",
        re.I,
    ),
    re.compile(r"\bmember of technical staff\b", re.I),
    re.compile(r"\bresearch engineer\b", re.I),
    re.compile(
        r"\b(?:data|business intelligence|bi|quality assurance|qa|software test)"
        r"\s+(?:analyst|specialist|developer)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:solutions?|customer|support|implementation|integration)"
        r"\s+engineer\b",
        re.I,
    ),
    re.compile(
        r"\bdeveloper relations\b.*\b(?:ai|api|data|developer|platform|tools?)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:video|film|post[\s-]?production|digital content)\s+"
        r"(?:editor|producer|coordinator|specialist)\b",
        re.I,
    ),
    re.compile(r"\b(?:motion graphics?|motion) (?:designer|artist)\b", re.I),
    re.compile(
        r"\b(?:technical artist|3d (?:artist|generalist|animator)|blender artist|"
        r"cad (?:designer|technician)|digital fabrication (?:technician|specialist)|"
        r"additive manufacturing (?:technician|engineer)|3d printing technician)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:founding|graduate)\s+engineer\b.*\b(?:ai|software|product|coding)\b",
        re.I,
    ),
)

SECONDARY_SIGNALS = (
    "n8n",
    "notion api",
    "webhook",
    "email automation",
    "oauth",
    "access control",
    "rbac",
    "role-based access",
    "secrets management",
    "application security",
    "observability",
    "performance optimization",
    "performance tuning",
)


@dataclass(frozen=True)
class PostingDecision:
    accepted: bool
    reason: str = ""
    language: str = ""
    language_confidence: float = 0.0
    language_text_letters: int = 0
    mixed_language: bool = False
    title_matches: tuple[str, ...] = ()
    core_matches: tuple[str, ...] = ()
    secondary_matches: tuple[str, ...] = ()
    profile: str = "My search"

    @property
    def rank(self) -> tuple[int, int, int]:
        return (
            1 if self.title_matches else 0,
            len(self.core_matches),
            len(self.secondary_matches),
        )


@dataclass
class SelectionStats:
    non_english: int = 0
    excluded_title: int = 0
    irrelevant: int = 0
    duplicates: int = 0
    cap_overflow: int = 0
    company_quota: int = 0
    profile_quota: int = 0
    cross_source_duplicates: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if reason == "non_english":
            self.non_english += 1
        elif reason == "excluded_title":
            self.excluded_title += 1
        elif reason == "irrelevant":
            self.irrelevant += 1
        elif reason == "duplicate":
            self.duplicates += 1
        elif reason == "cap_overflow":
            self.cap_overflow += 1
        elif reason == "company_quota":
            self.company_quota += 1
        elif reason == "profile_quota":
            self.profile_quota += 1
        elif reason == "cross_source_duplicate":
            self.cross_source_duplicates += 1


@dataclass
class SelectionResult:
    accepted: list[RawPosting]
    stats: SelectionStats
    decisions: dict[str, PostingDecision]


@dataclass
class GlobalIntakeState:
    """Run-scoped diversity guard shared by every source adapter.

    The identity deliberately ignores an aggregator URL.  The same employer
    and normalized role returned by Jooble and a first-party board should be
    considered a single new discovery, with the better-ranked row retained.
    """

    company_counts: dict[str, int] = field(default_factory=dict)
    identity_sources: dict[str, set[str]] = field(default_factory=dict)


def _normalized_identity_part(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _company_key(posting: RawPosting) -> str:
    return _normalized_identity_part(posting.company) or "unknown-company"


def _cross_source_identity(posting: RawPosting) -> str:
    return f"{_company_key(posting)}:{_normalized_identity_part(posting.role_title)}"


def _profile_for(posting: RawPosting, settings: Settings) -> str:
    title = (posting.role_title or "").casefold()
    for profile in settings.search_terms.profiles:
        if any(term.casefold() in title for term in profile.role_terms):
            return profile.name
    return settings.search_terms.profiles[0].name if settings.search_terms.profiles else "My search"


def global_admission_reason(
    posting: RawPosting,
    settings: Settings,
    state: GlobalIntakeState,
) -> str:
    """Return a diversity rejection reason without reserving intake capacity."""
    identity = _cross_source_identity(posting)
    company = _company_key(posting)
    prior_sources = state.identity_sources.get(identity, set())
    if prior_sources and posting.source_adapter not in prior_sources:
        return "cross_source_duplicate"
    if state.company_counts.get(company, 0) >= settings.discovery.max_new_per_company:
        return "company_quota"
    return ""


def admit_global_posting(
    posting: RawPosting,
    settings: Settings,
    state: GlobalIntakeState,
) -> str:
    """Commit one *new* canonical posting to the run-scoped diversity state.

    Call this only after canonical resolution and durable deduplication.  In
    particular, an unresolved aggregator redirect must never exhaust capacity
    needed by a later first-party board.
    """
    reason = global_admission_reason(posting, settings, state)
    if reason:
        return reason
    identity = _cross_source_identity(posting)
    company = _company_key(posting)
    state.identity_sources.setdefault(identity, set()).add(posting.source_adapter)
    state.company_counts[company] = state.company_counts.get(company, 0) + 1
    return ""


@dataclass(frozen=True)
class TargetMarketSignal:
    level: str = "none"  # none / mixed / foreign
    countries: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreflightResult:
    accepted: bool
    reason: str = ""
    language: str = ""
    language_confidence: float = 0.0
    mixed_language: bool = False
    remoteok: bool = False
    target_market: TargetMarketSignal = TargetMarketSignal()


def _posting_text(posting: RawPosting) -> str:
    return _SPACE_RE.sub(
        " ",
        " ".join(
            part
            for part in (
                posting.role_title,
                posting.company,
                posting.location,
                posting.description,
            )
            if part
        ),
    ).strip()


def _language_text(posting: RawPosting) -> str:
    return _SPACE_RE.sub(
        " ",
        " ".join(part for part in (posting.role_title, posting.description) if part),
    ).strip()


def excluded_title_reason(title: str) -> str:
    title = title or ""
    for pattern in _EXECUTIVE_TITLE_PATTERNS:
        if pattern.search(title):
            return "executive title"
    for pattern in _SALES_TITLE_PATTERNS:
        if pattern.search(title):
            return "sales title"
    return ""


def plausible_fit_title(title: str) -> bool:
    """Return whether a title is plausibly aligned enough to retain for review."""
    if excluded_title_reason(title):
        return False
    return any(pattern.search(title or "") for pattern in _PLAUSIBLE_FIT_TITLE_PATTERNS)


def is_remoteok_url(url: str) -> bool:
    hostname = (urlsplit(url or "").hostname or "").lower()
    return hostname == "remoteok.com" or hostname.endswith(".remoteok.com")


def generic_listing_url_reason(url: str) -> str:
    """Return a rejection reason for URLs that are not individual postings."""
    parsed = urlsplit((url or "").strip())
    hostname = (parsed.hostname or "").lower()
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/").lower()
    if hostname in {"remoteok.com", "www.remoteok.com"} and path in {
        "",
        "/remote-jobs",
        "/remote-dev-jobs",
    }:
        return "generic RemoteOK job-list URL"
    if path in {"", "/", "/jobs", "/careers", "/search", "/remote-jobs"}:
        return "generic job-list URL"
    return ""


def target_market_signal(title: str, description: str, location: str = "") -> TargetMarketSignal:
    """Detect explicit non-US employment targeting, ignoring incidental mentions."""
    title_location = " ".join(part for part in (title, location) if part)
    text = " ".join(part for part in (title, location, description) if part)
    lower = text.lower()
    countries = tuple(
        country
        for country in _NON_US_COUNTRY_ALIASES
        if re.search(rf"\b{re.escape(country)}\b", lower)
    )
    region_hit = _NON_US_REGION_RE.search(text)
    if not countries and not region_hit:
        return TargetMarketSignal()

    explicit_title_or_location = any(
        re.search(rf"\b{re.escape(country)}\b", title_location, re.I) for country in countries
    ) or bool(_NON_US_REGION_RE.search(title_location))
    contextual = False
    for match in re.finditer(
        "|".join(rf"\b{re.escape(country)}\b" for country in countries)
        if countries
        else _NON_US_REGION_RE.pattern,
        text,
        re.I,
    ):
        window = text[max(0, match.start() - 80) : match.end() + 80]
        if _TARGET_MARKET_CONTEXT_RE.search(window):
            contextual = True
            break
    if not explicit_title_or_location and not contextual:
        return TargetMarketSignal()

    has_us = bool(_US_MARKET_RE.search(text))
    names = countries or ((region_hit.group(0).lower(),) if region_hit else ())
    return TargetMarketSignal("mixed" if has_us else "foreign", names)


def score_preflight(posting: RawPosting, settings: Settings) -> PreflightResult:
    """Cheap objective gate used immediately before model scoring."""
    url_reason = generic_listing_url_reason(posting.url)
    remoteok = is_remoteok_url(posting.url)
    target_market = TargetMarketSignal()
    if url_reason:
        return PreflightResult(
            False,
            url_reason,
            remoteok=remoteok,
            target_market=target_market,
        )

    language_text = _language_text(posting)
    language, confidence, mixed = detect_language(
        language_text,
        minimum_confidence=settings.discovery.language_min_confidence,
    )
    if mixed or (
        language
        and confidence >= settings.discovery.language_min_confidence
        and language not in settings.discovery.allowed_languages
    ):
        detail = "mixed-language posting" if mixed else f"non-English posting ({language})"
        return PreflightResult(
            False,
            detail,
            language=language,
            language_confidence=confidence,
            mixed_language=mixed,
            remoteok=remoteok,
            target_market=target_market,
        )

    terms = getattr(getattr(settings, "search_terms", None), "role_terms", ())
    if terms and not any(term.casefold() in posting.role_title.casefold() for term in terms):
        return PreflightResult(
            False,
            "role title does not match the configured search preferences",
            language=language,
            language_confidence=confidence,
            mixed_language=mixed,
            remoteok=remoteok,
            target_market=target_market,
        )
    return PreflightResult(
        True,
        language=language,
        language_confidence=confidence,
        mixed_language=mixed,
        remoteok=remoteok,
        target_market=target_market,
    )


def _language_detector():
    try:
        from lingua import LanguageDetectorBuilder
    except ImportError as exc:  # pragma: no cover - dependency failure
        raise RuntimeError(
            "lingua-language-detector is required for job language filtering"
        ) from exc
    return LanguageDetectorBuilder.from_all_languages().build()


_DETECTOR = None


def detect_language(text: str, *, minimum_confidence: float) -> tuple[str, float, bool]:
    """Return ISO language, confidence, and whether the text is mixed."""
    letters = sum(character.isalpha() for character in (text or ""))
    if letters < 40:
        return "", 0.0, False
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = _language_detector()
    values = _DETECTOR.compute_language_confidence_values((text or "")[:6000])
    if not values:
        return "", 0.0, False
    top = values[0]
    iso = top.language.iso_code_639_1.name.lower()

    mixed = False
    for segment in _SENTENCE_RE.split((text or "")[:6000]):
        if sum(character.isalpha() for character in segment) < 40:
            continue
        segment_values = _DETECTOR.compute_language_confidence_values(segment)
        if not segment_values:
            continue
        segment_top = segment_values[0]
        segment_iso = segment_top.language.iso_code_639_1.name.lower()
        if segment_iso != iso and segment_top.value >= minimum_confidence:
            mixed = True
            break
    return iso, float(top.value), mixed


def evaluate_posting(posting: RawPosting, settings: Settings) -> PostingDecision:
    if any(
        term.casefold() in _posting_text(posting).casefold() for term in settings.excluded_terms
    ):
        return PostingDecision(False, "excluded_preference")
    if generic_listing_url_reason(posting.url):
        return PostingDecision(False, "invalid_url")
    profile_name = _profile_for(posting, settings)
    profile = next((p for p in settings.search_terms.profiles if p.name == profile_name), None)
    if profile:
        if any(t.casefold() in _posting_text(posting).casefold() for t in profile.excluded_terms):
            return PostingDecision(False, "excluded_preference")
        if (
            profile.location
            and profile.location.casefold() not in (posting.location or "").casefold()
            and posting.remote_status != "Remote"
        ):
            return PostingDecision(False, "location_preference")
        if profile.remote_only and posting.remote_status != "Remote":
            return PostingDecision(False, "remote_preference")
        floor = (
            profile.min_annual
            if posting.compensation_unit == "Annual"
            else profile.min_hourly
            if posting.compensation_unit == "Hourly"
            else 0
        )
        if floor and posting.compensation_max is not None and posting.compensation_max < floor:
            return PostingDecision(False, "compensation_preference")
    title_reason = ""  # Exclusions belong to the user search profile.
    if title_reason:
        return PostingDecision(False, "excluded_title")

    text = _posting_text(posting)
    language_text = _language_text(posting)
    language_letters = sum(character.isalpha() for character in language_text)
    language, confidence, mixed = detect_language(
        language_text,
        minimum_confidence=settings.discovery.language_min_confidence,
    )
    if (
        mixed
        or language
        not in (
            profile.allowed_languages
            if profile and profile.allowed_languages
            else settings.discovery.allowed_languages
        )
        or confidence < settings.discovery.language_min_confidence
    ):
        return PostingDecision(
            False,
            "non_english",
            language=language,
            language_confidence=confidence,
            language_text_letters=language_letters,
            mixed_language=mixed,
        )

    title_lower = (posting.role_title or "").lower()
    text_lower = text.lower()
    profile = _profile_for(posting, settings)
    title_matches = tuple(
        term for term in settings.search_terms.role_terms if term.lower() in title_lower
    )
    core_matches = tuple(
        term for term in settings.search_terms.skill_terms if term.lower() in text_lower
    )
    secondary_matches = tuple(term for term in SECONDARY_SIGNALS if term in text_lower)
    if not title_matches:
        return PostingDecision(
            False,
            "irrelevant",
            language=language,
            language_confidence=confidence,
            language_text_letters=language_letters,
            mixed_language=mixed,
            core_matches=core_matches,
            secondary_matches=secondary_matches,
            profile=profile,
        )
    return PostingDecision(
        True,
        language=language,
        language_confidence=confidence,
        language_text_letters=language_letters,
        mixed_language=mixed,
        title_matches=title_matches,
        core_matches=core_matches,
        secondary_matches=secondary_matches,
        profile=profile,
    )


def _bucket(posting: RawPosting) -> str:
    extras = posting.extras or {}
    return str(
        extras.get("search_bucket")
        or extras.get("board")
        or extras.get("feed")
        or extras.get("company_identifier")
        or extras.get("company")
        or "default"
    )


def _rank_key(
    posting: RawPosting,
    decision: PostingDecision,
) -> tuple[int, int, int, float, str]:
    posted = posting.posted_at or datetime(1970, 1, 1, tzinfo=UTC)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return (
        decision.rank[0],
        decision.rank[1],
        decision.rank[2],
        posted.timestamp(),
        fingerprint(posting),
    )


def select_postings(
    postings: list[RawPosting],
    settings: Settings,
    *,
    max_per_bucket: int | None = None,
    max_per_source: int | None = None,
    global_state: GlobalIntakeState | None = None,
) -> SelectionResult:
    max_per_bucket = max_per_bucket or settings.discovery.max_per_query
    max_per_source = max_per_source or settings.discovery.max_per_source
    stats = SelectionStats()
    decisions: dict[str, PostingDecision] = {}
    qualified_by_bucket: dict[str, list[RawPosting]] = {}
    seen: set[str] = set()

    for posting in postings:
        fp = fingerprint(posting)
        if fp in seen:
            stats.reject("duplicate")
            continue
        seen.add(fp)
        decision = evaluate_posting(posting, settings)
        decisions[fp] = decision
        if not decision.accepted:
            stats.reject(decision.reason)
            continue
        qualified_by_bucket.setdefault(_bucket(posting), []).append(posting)

    bucket_limited: list[RawPosting] = []
    for bucket_postings in qualified_by_bucket.values():
        ranked = sorted(
            bucket_postings,
            key=lambda posting: _rank_key(posting, decisions[fingerprint(posting)]),
            reverse=True,
        )
        bucket_limited.extend(ranked[:max_per_bucket])
        for _ in ranked[max_per_bucket:]:
            stats.reject("cap_overflow")

    ranked_source = sorted(
        bucket_limited,
        key=lambda posting: _rank_key(posting, decisions[fingerprint(posting)]),
        reverse=True,
    )
    for _ in ranked_source[max_per_source:]:
        stats.reject("cap_overflow")
    accepted = ranked_source[:max_per_source]
    if global_state is not None:
        globally_accepted: list[RawPosting] = []
        for posting in accepted:
            reason = admit_global_posting(posting, settings, global_state)
            if reason:
                stats.reject(reason)
                continue
            globally_accepted.append(posting)
        accepted = globally_accepted
    return SelectionResult(
        accepted=accepted,
        stats=stats,
        decisions=decisions,
    )
