from __future__ import annotations

from pathlib import Path

from job_search_hub.config import load_settings
from job_search_hub.models import StructuredOutputError
from job_search_hub.pipeline.resume_facts_sync import (
    _evidence_in_text,
    extract_facts_from_text,
)


class _Router:
    def get(self, role):
        return "model"


class _Store:
    def __init__(self):
        self.rows = []

    def audit(self, **kwargs):
        self.rows.append(kwargs)


def _settings(tmp_path: Path):
    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["resume_facts"] = "facts"
    return settings


def test_evidence_validation_collapses_pdf_whitespace() -> None:
    text = "Built production software with\nPython and TypeScript."
    assert _evidence_in_text("production software with Python", text)
    assert not _evidence_in_text("production software with Go", text)


def test_extract_facts_rejects_model_claims_without_pdf_evidence(tmp_path: Path) -> None:
    class Client:
        def chat_json(self, **kwargs):
            return {
                "facts": [
                    {
                        "fact_type": "Skill",
                        "value": "Python",
                        "evidence": "Python and TypeScript",
                        "organization": "",
                        "role_label": "",
                        "relationship_type": "Not Applicable",
                        "start_date": None,
                        "end_date": None,
                        "current": False,
                        "category": "Language",
                        "proficiency": "Strong",
                        "emphasis_tags": ["Backend"],
                        "metrics_verified": False,
                        "sensitive": False,
                    },
                    {
                        "fact_type": "Skill",
                        "value": "Go",
                        "evidence": "Go and Kubernetes",
                        "organization": "",
                        "role_label": "",
                        "relationship_type": "Not Applicable",
                        "start_date": None,
                        "end_date": None,
                        "current": False,
                        "category": "Backend",
                        "proficiency": "Working",
                        "emphasis_tags": [],
                        "metrics_verified": False,
                        "sensitive": False,
                    },
                ]
            }

    facts, rejected = extract_facts_from_text(
        resume_title="Backend Software Engineer",
        pdf_path=tmp_path / "resume.pdf",
        text="Skills: Python and TypeScript",
        router=_Router(),
        client=Client(),
    )

    assert rejected == 1
    assert [fact.value for fact in facts] == ["Python"]
    assert facts[0].fact_id.startswith("pdf:backend-software-engineer:skill:")


def test_extract_facts_uses_pdf_text_fallback_when_model_json_is_malformed(
    tmp_path: Path,
) -> None:
    class Client:
        def chat_json(self, **kwargs):
            raise StructuredOutputError("malformed JSON after retry")

    text = "\n".join(
        [
            "Frontend: React, TypeScript, Tailwind CSS",
            "Backend / Cloud: Python, FastAPI, AWS, Docker",
            "B.S. Media Communication, Example University | AWS Certified Cloud Practitioner",
        ]
    )

    facts, rejected = extract_facts_from_text(
        resume_title="Front-End Developer",
        pdf_path=tmp_path / "resume.pdf",
        text=text,
        router=_Router(),
        client=Client(),
    )

    assert rejected == 0
    values = {fact.value for fact in facts}
    assert {"React", "TypeScript", "Python", "AWS Certified Cloud Practitioner"} <= values
    assert all(fact.source.startswith("pdf:") for fact in facts)
