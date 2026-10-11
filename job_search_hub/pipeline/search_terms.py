"""Role, skill, and mission term matching for discovered postings."""

from __future__ import annotations

from job_search_hub.config import Settings
from job_search_hub.ingest.base import RawPosting


def _haystack(posting: RawPosting) -> str:
    return " ".join(
        str(part or "")
        for part in (
            posting.role_title,
            posting.company,
            posting.location,
            posting.description,
            " ".join(str(x) for x in posting.employment_type),
        )
    ).lower()


def match_terms(posting: RawPosting, settings: Settings) -> dict[str, list[str]]:
    text = _haystack(posting)
    return {
        "roles": [term for term in settings.search_terms.role_terms if term.lower() in text],
        "skills": [term for term in settings.search_terms.skill_terms if term.lower() in text],
        "mission": [term for term in settings.search_terms.mission_terms if term.lower() in text],
    }


def format_matches(matches: dict[str, list[str]], *, limit: int = 12) -> str:
    parts: list[str] = []
    for label in ("roles", "skills", "mission"):
        values = matches.get(label) or []
        if values:
            parts.append(f"{label}: {', '.join(values[:limit])}")
    return " | ".join(parts)


def annotate_posting(posting: RawPosting, settings: Settings) -> RawPosting:
    matches = match_terms(posting, settings)
    posting.extras = dict(posting.extras or {})
    posting.extras["matched_terms"] = matches
    posting.extras["top_matches"] = format_matches(matches)
    return posting
