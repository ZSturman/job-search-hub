"""Normalize raw postings: fingerprinting, hard-exclusion, lexical dedupe."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_search_hub.ingest.base import RawPosting

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    # Strip common entity suffixes
    for suffix in (
        " inc",
        " incorporated",
        " llc",
        " ltd",
        " limited",
        " corp",
        " corporation",
        " co",
        " gmbh",
        " ag",
        " plc",
        " sa",
    ):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def normalize_role(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"[^a-z0-9 +#./-]+", " ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"

    # Adzuna rotates search-session parameters (`se`) for the same stable ad
    # path. No query parameter on these detail URLs identifies the posting.
    if hostname == "adzuna.com" or hostname.endswith(".adzuna.com"):
        query = ""
    else:
        kept = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not re.match(r"(?i)(utm_|gh_|s_)", key)
        ]
        query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), query, ""))


def fingerprint(posting: RawPosting) -> str:
    """Stable identifier for dedupe across adapters and reruns.

    Combines normalized company+role+url. External job ids alone are
    insufficient because the same posting often appears via multiple
    adapters with different ids.
    """
    # Direct resolution may establish a cross-source identity (an ATS job id
    # or canonical job URL).  Prefer it over the discovery URL so the same
    # listing found through an aggregator and an employer page consolidates.
    identity = str((posting.extras or {}).get("canonical_identity") or "").strip()
    parts = (
        [identity]
        if identity
        else [
            normalize_company(posting.company),
            normalize_role(posting.role_title),
            normalize_url(posting.url),
        ]
    )
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def description_hash(text: str) -> str:
    cleaned = _WHITESPACE_RE.sub(" ", (text or "").strip().lower())
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def excluded_by_keywords(posting: RawPosting, keywords=()) -> tuple[bool, str]:
    """Only user-configured exclusions affect discovery."""
    text = " ".join((posting.role_title, posting.description, posting.company)).casefold()
    hit = next((term for term in keywords if term.strip() and term.casefold() in text), "")
    return bool(hit), f"Excluded keyword: {hit}" if hit else ""


def below_hard_floor(
    posting: RawPosting,
    *,
    annual_floor_usd: int,
    hourly_floor_usd: int,
) -> tuple[bool, str]:
    """Only excludes when comp is *confirmed* below the floor.

    Unknown comp is never an exclusion.
    """
    if posting.compensation_min is None and posting.compensation_max is None:
        return False, ""
    unit = (posting.compensation_unit or "Unknown").lower()
    # Use the *upper* end of the range for the comparison; if the upper end
    # is still below the floor, the role is confirmed sub-floor.
    upper = posting.compensation_max or posting.compensation_min
    if upper is None:
        return False, ""
    if unit == "annual" and upper < annual_floor_usd:
        return True, f"confirmed annual comp upper={upper} < floor={annual_floor_usd}"
    if unit == "hourly" and upper < hourly_floor_usd:
        return True, f"confirmed hourly comp upper={upper} < floor={hourly_floor_usd}"
    return False, ""


def short_description(text: str, *, limit: int = 1800) -> str:
    """Trim a description for storage in a Notion rich_text property."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"
