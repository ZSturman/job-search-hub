"""Sync curated resume PDFs into Notion Profile Facts."""

from __future__ import annotations

import hashlib
import logging
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from job_search_hub.logging_setup import get_logger
from job_search_hub.models import LMStudioClient, ModelRouter, StructuredOutputError
from job_search_hub.storage import app_paths

RESUME_ROOT = app_paths().data / "resumes"
RESUME_PDF_NAME = "resume.pdf"

log = get_logger(__name__)

_FACT_TYPES = (
    "Identity",
    "Contact",
    "Summary",
    "Skill",
    "Experience Header",
    "Experience Bullet",
    "Project Header",
    "Project Bullet",
    "Education",
    "Certification",
    "Preference",
    "Constraint",
    "Other",
)
_CATEGORIES = (
    "Language",
    "Frontend",
    "Backend",
    "Data",
    "ML",
    "Cloud",
    "Testing",
    "DevOps",
    "Design",
    "3D",
    "Hardware",
    "Education",
    "Certification",
    "Other",
)
_PROFICIENCIES = ("Familiar", "Working", "Strong", "Expert", "Do Not Claim")
_RELATIONSHIPS = (
    "Paid Employment",
    "Contract",
    "Volunteer",
    "Client Project",
    "University Collaboration",
    "Independent Project",
    "Exploratory Project",
    "Not Applicable",
)
_MAX_FACTS_PER_RESUME = 80
_MAX_MODEL_FACTS_PER_RESUME = 12

_FACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "maxItems": _MAX_MODEL_FACTS_PER_RESUME,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "fact_type",
                    "value",
                    "evidence",
                    "organization",
                    "role_label",
                    "relationship_type",
                    "start_date",
                    "end_date",
                    "current",
                    "category",
                    "proficiency",
                    "emphasis_tags",
                    "metrics_verified",
                    "sensitive",
                ],
                "properties": {
                    "fact_type": {"type": "string", "enum": list(_FACT_TYPES)},
                    "value": {"type": "string", "maxLength": 800},
                    "evidence": {"type": "string", "maxLength": 1000},
                    "organization": {"type": "string", "maxLength": 200},
                    "role_label": {"type": "string", "maxLength": 200},
                    "relationship_type": {
                        "type": "string",
                        "enum": list(_RELATIONSHIPS),
                    },
                    "start_date": {"type": ["string", "null"], "maxLength": 30},
                    "end_date": {"type": ["string", "null"], "maxLength": 30},
                    "current": {"type": "boolean"},
                    "category": {"type": "string", "enum": list(_CATEGORIES)},
                    "proficiency": {"type": "string", "enum": list(_PROFICIENCIES)},
                    "emphasis_tags": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "maxLength": 60},
                    },
                    "metrics_verified": {"type": "boolean"},
                    "sensitive": {"type": "boolean"},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class PdfResumeFact:
    fact_id: str
    fact_type: str
    value: str
    evidence: str
    source: str
    resume_title: str
    organization: str = ""
    role_label: str = ""
    relationship_type: str = "Not Applicable"
    start_date: str | None = None
    end_date: str | None = None
    current: bool = False
    category: str = "Other"
    proficiency: str = "Working"
    emphasis_tags: tuple[str, ...] = ()
    metrics_verified: bool = False
    sensitive: bool = False


@dataclass
class ResumeFactsSyncStats:
    pdfs_scanned: int = 0
    unchanged_pdfs: int = 0
    facts_extracted: int = 0
    facts_rejected: int = 0
    model_fallbacks: int = 0
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped_manual: int = 0
    errors: list[str] = field(default_factory=list)


def find_resume_pdfs(resume_root: Path = RESUME_ROOT) -> list[Path]:
    return sorted(resume_root.glob("*/*.pdf"))


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required for resume fact sync") from exc
    pypdf_logger = logging.getLogger("pypdf")
    pypdf_logger.setLevel(logging.ERROR)
    pypdf_logger.addHandler(logging.NullHandler())
    pypdf_logger.propagate = False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _evidence_in_text(evidence: str, text: str) -> bool:
    compact_evidence = _compact_text(evidence)
    if len(compact_evidence) < 3:
        return False
    return compact_evidence in _compact_text(text)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "fact"


def _fact_id(*, resume_title: str, fact_type: str, value: str, evidence: str) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                resume_title.strip().lower(),
                fact_type.strip().lower(),
                _compact_text(value),
                _compact_text(evidence),
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"pdf:{_slug(resume_title)}:{_slug(fact_type)}:{digest}"


def _source_for_pdf(path: Path) -> str:
    try:
        rel = path.relative_to(Path.cwd())
    except ValueError:
        rel = path
    return f"pdf:{rel.as_posix()}"


_SKILL_SECTION_CATEGORIES = {
    "languages": "Language",
    "frontend": "Frontend",
    "front-end": "Frontend",
    "apis & backend integration": "Backend",
    "backend / cloud": "Backend",
    "backend": "Backend",
    "cloud / devops": "DevOps",
    "databases / cloud": "Cloud",
    "data / ai": "Data",
    "data / infrastructure": "Data",
    "data engineering": "Data",
    "ai / data / testing": "ML",
    "ai / ml": "ML",
    "testing / tooling": "Testing",
    "testing / tools": "Testing",
    "testing & quality": "Testing",
    "web / app development": "Frontend",
}


def _clean_pdf_line(line: str) -> str:
    return re.sub(r"\s+", " ", line or "").strip()


def _split_skill_values(value: str) -> list[str]:
    items: list[str] = []
    for raw in re.split(r",|;|\s+\|\s+", value):
        item = raw.strip(" .•\t")
        item = re.sub(r"\s+", " ", item)
        if not item or len(item) > 80:
            continue
        if re.search(r"\b(and|with|using|including)\b", item, re.I):
            continue
        items.append(item)
    return items


def _dedupe_facts(facts: list[PdfResumeFact]) -> list[PdfResumeFact]:
    seen: set[tuple[str, str]] = set()
    deduped: list[PdfResumeFact] = []
    for fact in facts:
        key = (
            fact.fact_type.strip().lower(),
            _compact_text(fact.value),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped[:_MAX_FACTS_PER_RESUME]


def _deterministic_facts_from_text(
    *,
    resume_title: str,
    pdf_path: Path,
    text: str,
) -> list[PdfResumeFact]:
    """Extract high-confidence facts from section lines copied from the PDF."""

    source = _source_for_pdf(pdf_path)
    facts: list[PdfResumeFact] = []
    for raw_line in text.splitlines():
        line = _clean_pdf_line(raw_line)
        if not line or ":" not in line:
            continue
        label, value = line.split(":", 1)
        label_key = _compact_text(label)
        category = _SKILL_SECTION_CATEGORIES.get(label_key)
        if not category:
            continue
        evidence = line
        if not _evidence_in_text(evidence, text):
            continue
        for skill in _split_skill_values(value):
            facts.append(
                PdfResumeFact(
                    fact_id=_fact_id(
                        resume_title=resume_title,
                        fact_type="Skill",
                        value=skill,
                        evidence=evidence,
                    ),
                    fact_type="Skill",
                    value=skill,
                    evidence=evidence,
                    source=source,
                    resume_title=resume_title,
                    role_label=resume_title,
                    relationship_type="Not Applicable",
                    current=False,
                    category=category,
                    proficiency="Strong",
                    emphasis_tags=(category,),
                    metrics_verified=False,
                    sensitive=False,
                )
            )

    for raw_line in text.splitlines():
        line = _clean_pdf_line(raw_line)
        if not line:
            continue
        if "B.S." in line or "Certified" in line:
            for part in re.split(r"\s+\|\s+", line):
                value = part.strip()
                if not value:
                    continue
                fact_type = "Certification" if "Certified" in value else "Education"
                facts.append(
                    PdfResumeFact(
                        fact_id=_fact_id(
                            resume_title=resume_title,
                            fact_type=fact_type,
                            value=value,
                            evidence=line,
                        ),
                        fact_type=fact_type,
                        value=value,
                        evidence=line,
                        source=source,
                        resume_title=resume_title,
                        role_label=resume_title,
                        relationship_type="Not Applicable",
                        current=False,
                        category=("Certification" if fact_type == "Certification" else "Education"),
                        proficiency="Strong",
                        emphasis_tags=(),
                        metrics_verified=False,
                        sensitive=False,
                    )
                )
    return _dedupe_facts(facts)


def _extract_facts_from_text_with_status(
    *,
    resume_title: str,
    pdf_path: Path,
    text: str,
    router: ModelRouter,
    client: LMStudioClient,
    use_model: bool = True,
) -> tuple[list[PdfResumeFact], int, bool]:
    system = (
        "Extract reusable candidate facts from a resume PDF text. Return only facts "
        "that are explicitly supported by the provided text. Every fact must include "
        "an evidence string copied exactly from the resume text. Do not invent "
        "metrics, dates, employers, titles, credentials, or tools. Return at most "
        f"{_MAX_MODEL_FACTS_PER_RESUME} high-signal facts. Prefer concise facts that "
        "are not already obvious from simple section heading lists."
    )
    fallback_facts = _deterministic_facts_from_text(
        resume_title=resume_title,
        pdf_path=pdf_path,
        text=text,
    )
    if not use_model:
        return fallback_facts, 0, False
    try:
        payload = client.chat_json(
            model=router.get("primary"),
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        f"Resume title: {resume_title}\n"
                        f"PDF path: {pdf_path}\n\n"
                        f"Resume text:\n{text[:9000]}"
                    ),
                },
            ],
            json_schema=_FACT_SCHEMA,
            max_tokens=4096,
            stage="resume-facts",
            role="primary",
            task="extract_resume_pdf_facts",
        )
    except StructuredOutputError as exc:
        log.warning(
            "resume facts model extraction failed for %s; using PDF text fallback: %s",
            pdf_path,
            str(exc)[:500],
        )
        return fallback_facts, 0, True
    rows = payload.get("facts") or []
    facts: list[PdfResumeFact] = list(fallback_facts)
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        value = str(row.get("value") or "").strip()
        evidence = str(row.get("evidence") or "").strip()
        if not value or not _evidence_in_text(evidence, text):
            rejected += 1
            continue
        fact_type = str(row.get("fact_type") or "Other")
        facts.append(
            PdfResumeFact(
                fact_id=_fact_id(
                    resume_title=resume_title,
                    fact_type=fact_type,
                    value=value,
                    evidence=evidence,
                ),
                fact_type=fact_type,
                value=value,
                evidence=evidence,
                source=_source_for_pdf(pdf_path),
                resume_title=resume_title,
                organization=str(row.get("organization") or "").strip(),
                role_label=str(row.get("role_label") or "").strip(),
                relationship_type=str(row.get("relationship_type") or "Not Applicable"),
                start_date=row.get("start_date") or None,
                end_date=row.get("end_date") or None,
                current=bool(row.get("current")),
                category=str(row.get("category") or "Other"),
                proficiency=str(row.get("proficiency") or "Working"),
                emphasis_tags=tuple(
                    str(v).strip() for v in row.get("emphasis_tags") or [] if str(v).strip()
                ),
                metrics_verified=bool(row.get("metrics_verified")),
                sensitive=bool(row.get("sensitive")),
            )
        )
    return _dedupe_facts(facts), rejected, False


def extract_facts_from_text(
    *,
    resume_title: str,
    pdf_path: Path,
    text: str,
    router: ModelRouter,
    client: LMStudioClient,
) -> tuple[list[PdfResumeFact], int]:
    facts, rejected, _model_failed = _extract_facts_from_text_with_status(
        resume_title=resume_title,
        pdf_path=pdf_path,
        text=text,
        router=router,
        client=client,
        use_model=True,
    )
    return facts, rejected
