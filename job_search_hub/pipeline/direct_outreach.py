"""Identify explicit, role-specific direct-outreach instructions.

This is deliberately an alerting feature, not an application sender.  It
only uses hiring-context text that came with the verified job and sends its
digest through the local notification outbox, whose recipient is fixed to the
user's configured personal inbox.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

from job_search_hub.config import DirectOutreachSettings, Settings
from job_search_hub.db import Store
from job_search_hub.notify import Notifier, stable_key
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.canonical_resolution import source_quality
from job_search_hub.pipeline.lifecycle import read_status
from job_search_hub.pipeline.normalize import normalize_company, normalize_role

_EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w-])", re.I)
_MAILTO_RE = re.compile(r"mailto:([^?\s\"'>]+)", re.I)
_INSTRUCTION_RE = re.compile(
    r"(?is)(?:email|send|reach out|contact|introduce yourself|apply by emailing|"
    r"applications? (?:through|via) .*?not monitored)"
    r".{0,240}?(?:resume|cv|portfolio|work samples|github|application|@|mailto:)",
)
_NAMED_CONTACT_RE = re.compile(
    r"(?i)(?:email|contact|reach out to|send)"
    r"(?:\s+(?:your )?(?:resume|cv|portfolio|work samples))?(?:\s+to)?\s+"
    r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3})"
    r"(?:\s*[,–—-]\s*|\s+\(?)(hiring manager|recruiter|founder|"
    r"head of [A-Za-z ]+|talent [A-Za-z ]+)",
)
_GENERIC_LOCAL = {
    "support",
    "help",
    "info",
    "hello",
    "contact",
    "careers",
    "jobs",
    "recruiting",
    "talent",
    "hr",
}
_STAFFING_RE = re.compile(
    r"\b(staffing (agency|firm)|recruiting agency|third[- ]party recruiter|c2c|multiple client)\b",
    re.I,
)
_TERMINAL_STATUSES = {"closed", "rejected", "submitted", "interviewing", "offer", "withdrawn"}
_TRUSTED_ADAPTERS = {
    "greenhouse",
    "lever",
    "ashby",
    "recruitee",
    "smartrecruiters",
    "direct_discovery",
}


@dataclass(frozen=True)
class OutreachSignal:
    email: str
    instruction: str
    confidence: float
    contact_name: str = ""
    contact_role: str = ""


@dataclass(frozen=True)
class OutreachCandidate:
    job_key: str
    page_id: str
    title: str
    company: str
    score: float
    lane: str
    url: str
    original_url: str
    page_url: str
    source_quality: str
    signal: OutreachSignal
    ranking: float


@dataclass
class OutreachStats:
    scanned: int = 0
    qualified: int = 0
    queued: int = 0
    suppressed: int = 0
    digest_queued: bool = False


def _redacted(value: str) -> str:
    if "@" not in value:
        return value
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _instruction_for_email(text: str, email: str) -> str:
    for match in _INSTRUCTION_RE.finditer(text):
        snippet = re.sub(r"\s+", " ", match.group(0)).strip()
        if email.lower() in snippet.lower() or "mailto:" in snippet.lower():
            return snippet[:500]
    # A short surrounding window still has to contain a direct action phrase.
    index = text.lower().find(email.lower())
    if index >= 0:
        window = re.sub(r"\s+", " ", text[max(0, index - 180) : index + 220]).strip()
        if re.search(r"\b(email|send|contact|reach out|apply)\b", window, re.I):
            return window[:500]
    return ""


def extract_outreach_signals(description: str, *, company: str, url: str) -> list[OutreachSignal]:
    """Require an explicit hiring instruction; an incidental email is ignored."""
    text = description or ""
    if _STAFFING_RE.search(text):
        return []
    named: dict[str, tuple[str, str]] = {}
    for match in _NAMED_CONTACT_RE.finditer(text):
        named[match.group(1).casefold()] = (match.group(1), match.group(2).strip())
    emails = {match.group(1).lower() for match in _EMAIL_RE.finditer(text)}
    emails.update(match.group(1).lower() for match in _MAILTO_RE.finditer(text))
    company_host = (urlsplit(url).hostname or "").lower()
    company_root = ".".join(company_host.split(".")[-2:]) if company_host else ""
    signals: list[OutreachSignal] = []
    for email in sorted(emails):
        local, _, domain = email.partition("@")
        instruction = _instruction_for_email(text, email)
        if not instruction or local.casefold() in _GENERIC_LOCAL:
            continue
        name = role = ""
        # Associate an explicitly named contact if their name is nearby.
        for key, pair in named.items():
            if (
                key in instruction.casefold()
                or key
                in text[
                    max(0, text.lower().find(email) - 180) : text.lower().find(email) + 60
                ].casefold()
            ):
                name, role = pair
                break
        domain_match = bool(company_root and domain.endswith(company_root))
        if not domain_match and not name:
            # A public external mailbox is only useful when the job explicitly
            # identifies the hiring person or role.
            continue
        confidence = (
            0.70 + (0.15 if domain_match else 0) + (0.10 if name else 0) + (0.05 if role else 0)
        )
        signals.append(
            OutreachSignal(
                email=email,
                instruction=instruction,
                confidence=min(1.0, confidence),
                contact_name=name,
                contact_role=role,
            )
        )
    return signals


def _score(props: dict) -> float:
    value = P.read_number(props.get("My Score"))
    if value is None:
        value = P.read_number(props.get("Model Score"))
    score = float(value or 0)
    return score / 100.0 if score > 1 else score


def _cooldown_active(previous, evidence_hash: str, cooldown_days: int) -> bool:
    if (
        previous is None
        or previous["evidence_hash"] != evidence_hash
        or not previous["last_notified_at"]
    ):
        return False
    try:
        notified = datetime.fromisoformat(previous["last_notified_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return notified + timedelta(days=cooldown_days) > datetime.now(UTC)


def _render_digest(
    candidates: list[OutreachCandidate], overflow: list[OutreachCandidate]
) -> tuple[str, str]:
    lines = ["Verified roles with an explicit, role-specific direct-contact instruction:", ""]
    for candidate in candidates:
        signal = candidate.signal
        mailto = f"mailto:{quote(signal.email)}?subject={quote('Regarding ' + candidate.title)}"
        label = (
            f"{candidate.title} — {candidate.company} "
            f"({candidate.score:.0%}, {candidate.lane or 'unlabeled'})"
        )
        contact = (
            f"{signal.contact_name or 'Hiring contact'}"
            f"{(' — ' + signal.contact_role) if signal.contact_role else ''} "
            f"<{signal.email}>"
        )
        lines.extend(
            [
                label,
                f"Job: {candidate.url}",
                f"Contact: {contact}",
                f"Action: {mailto}",
                f"Instruction: {signal.instruction}",
                f"Verified: {datetime.now(UTC).date().isoformat()}",
                *([f"Record: {candidate.page_url}"] if candidate.page_url else []),
                *(
                    [f"Discovered via: {candidate.original_url}"]
                    if candidate.original_url and candidate.original_url != candidate.url
                    else []
                ),
                "",
            ]
        )
    if overflow:
        lines.extend(
            ["Additional qualified opportunities (not expanded to keep this digest concise):"]
        )
        lines.extend(f"- {item.title} — {item.company}: {item.url}" for item in overflow)
    return "Job Search Hub: direct outreach opportunities", "\n".join(lines)


def run_direct_outreach(
    settings: Settings,
    *,
    notion,
    store: Store,
    notifier: Notifier | None,
    dry_run: bool,
) -> OutreachStats:
    stats = OutreachStats()
    config: DirectOutreachSettings = settings.direct_outreach
    if not config.enabled:
        return stats
    day = datetime.now(UTC).date().isoformat()
    if store.direct_outreach_digest_count(day) >= config.max_digests_per_day:
        return stats
    ds_id = settings.notion.data_sources.get("opportunities", "")
    if not ds_id:
        return stats
    candidates: list[OutreachCandidate] = []
    for page in notion.query_data_source(ds_id):
        props = page.get("properties") or {}
        status = read_status(props.get("Status")).casefold()
        if status in _TERMINAL_STATUSES:
            continue
        score = _score(props)
        if score < config.minimum_score:
            continue
        stats.scanned += 1
        url = P.read_url(props.get("URL"))
        title = P.read_text(props.get("Role Title"))
        company = P.read_text(props.get("Company"))
        adapter = P.read_select(props.get("Source Adapter")).casefold()
        provenance = store.provenance_for_url(url) if url else None
        verified = (
            bool(provenance and provenance["verification_status"] == "verified")
            or adapter in _TRUSTED_ADAPTERS
        )
        if not verified or not title or not company or not url:
            continue
        description = P.read_text(props.get("Description"))
        if _STAFFING_RE.search(description):
            continue
        quality = provenance["source_quality"] if provenance else source_quality(adapter, url)
        if quality == "aggregator":
            continue
        original = provenance["discovery_url"] if provenance else ""
        for signal in extract_outreach_signals(description, company=company, url=url):
            job_key = str(
                P.read_text(props.get("Fingerprint"))
                or page.get("id")
                or f"{normalize_company(company)}:{normalize_role(title)}"
            )
            contact_key = signal.email.casefold()
            evidence_hash = hashlib.sha256(
                (signal.email + "\0" + signal.instruction).encode()
            ).hexdigest()
            if dry_run:
                previous = store.direct_outreach_opportunity(job_key, contact_key)
            else:
                previous = store.record_direct_outreach(
                    job_key=job_key,
                    contact_key=contact_key,
                    evidence_hash=evidence_hash,
                    contact_name=signal.contact_name,
                    contact_role=signal.contact_role,
                    contact_email=signal.email,
                    instruction=signal.instruction,
                    confidence=signal.confidence,
                )
            if _cooldown_active(previous, evidence_hash, config.cooldown_days):
                stats.suppressed += 1
                continue
            ranking = (
                score
                + signal.confidence * 0.25
                + (0.1 if signal.contact_name else 0)
                + (0.1 if quality in {"direct_employer", "official_ats"} else 0)
            )
            candidates.append(
                OutreachCandidate(
                    job_key,
                    page.get("id", ""),
                    title,
                    company,
                    score,
                    P.read_select(props.get("My Lane")) or P.read_select(props.get("Model Lane")),
                    url,
                    original,
                    page.get("url", ""),
                    quality,
                    signal,
                    ranking,
                )
            )
    candidates.sort(
        key=lambda item: (-item.ranking, item.company.casefold(), item.title.casefold())
    )
    stats.qualified = len(candidates)
    if not candidates or notifier is None or config.max_digests_per_pass <= 0:
        return stats
    selected = candidates[: config.max_opportunities_per_pass]
    overflow = candidates[config.max_opportunities_per_pass :]
    subject, body = _render_digest(selected, overflow)
    digest_key = stable_key(
        "direct_outreach",
        day,
        *[f"{item.page_id}:{item.signal.email}:{item.signal.instruction}" for item in candidates],
    )
    if notifier.send_once("direct_outreach_digest", digest_key, subject, body):
        stats.digest_queued = True
        stats.queued = len(candidates)
        if not dry_run:
            store.record_direct_outreach_digest(
                digest_day=day, status="queued", message_key=digest_key
            )
            for candidate in candidates:
                store.mark_direct_outreach_notified(
                    candidate.job_key, candidate.signal.email.casefold()
                )
    return stats
