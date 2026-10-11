from __future__ import annotations

import hashlib
from types import SimpleNamespace

from job_search_hub.notion import properties as P
from job_search_hub.pipeline.direct_outreach import extract_outreach_signals, run_direct_outreach


def test_explicit_role_contact_is_extracted_but_generic_and_staffing_are_rejected() -> None:
    text = "For this role, email your resume to Maya Jones, Hiring Manager, at maya@acme.example."
    signals = extract_outreach_signals(text, company="Acme", url="https://jobs.acme.example/jobs/1")
    assert len(signals) == 1
    assert signals[0].contact_name == "Maya Jones"
    assert signals[0].email == "maya@acme.example"
    assert (
        extract_outreach_signals(
            "Email support@acme.example for help",
            company="Acme",
            url="https://jobs.acme.example/jobs/1",
        )
        == []
    )
    assert (
        extract_outreach_signals(
            "A staffing agency says email jane@agency.example your resume",
            company="Acme",
            url="https://jobs.acme.example/jobs/1",
        )
        == []
    )


class _Notifier:
    def __init__(self):
        self.calls = []

    def send_once(self, *args):
        self.calls.append(args)
        return True


class _Notion:
    def __init__(self, pages):
        self.pages = pages

    def query_data_source(self, _data_source):
        return self.pages


def test_outreach_digest_uses_personal_notifier_and_honors_daily_dedupe(tmp_store) -> None:
    page = {
        "id": "page-1",
        "url": "https://notion.example/page-1",
        "properties": {
            "Status": P.select("Review"),
            "My Score": P.number(0.91),
            "Role Title": P.rich_text("Platform Engineer"),
            "Company": P.rich_text("Acme"),
            "URL": P.url("https://jobs.acme.example/jobs/1"),
            "Source Adapter": P.select("greenhouse"),
            "Fingerprint": P.rich_text("job-1"),
            "Description": P.rich_text(
                "Email your resume to Maya Jones, Hiring Manager, at maya@acme.example."
            ),
        },
    }
    settings = SimpleNamespace(
        direct_outreach=SimpleNamespace(
            enabled=True,
            minimum_score=0.70,
            max_opportunities_per_pass=8,
            max_digests_per_pass=1,
            max_digests_per_day=1,
            cooldown_days=14,
        ),
        notion=SimpleNamespace(data_sources={"opportunities": "opp"}),
    )
    notifier = _Notifier()
    stats = run_direct_outreach(
        settings, notion=_Notion([page]), store=tmp_store, notifier=notifier, dry_run=False
    )
    assert stats.digest_queued is True
    assert len(notifier.calls) == 1
    assert "maya@acme.example" not in notifier.calls[0][0:2]  # never used as recipient/key
    assert "mailto:maya%40acme.example" in notifier.calls[0][3]
    assert (
        run_direct_outreach(
            settings, notion=_Notion([page]), store=tmp_store, notifier=notifier, dry_run=False
        ).digest_queued
        is False
    )


def test_closed_and_low_score_candidates_are_suppressed(tmp_store) -> None:
    base = {
        "id": "page-2",
        "properties": {
            "Status": P.select("Closed"),
            "My Score": P.number(0.99),
            "Role Title": P.rich_text("Engineer"),
            "Company": P.rich_text("Acme"),
            "URL": P.url("https://jobs.acme.example/jobs/2"),
            "Source Adapter": P.select("greenhouse"),
            "Fingerprint": P.rich_text("job-2"),
            "Description": P.rich_text(
                "Email your resume to Maya Jones, Hiring Manager, maya@acme.example."
            ),
        },
    }
    low = {
        **base,
        "id": "page-3",
        "properties": {
            **base["properties"],
            "Status": P.select("Review"),
            "My Score": P.number(0.20),
        },
    }
    settings = SimpleNamespace(
        direct_outreach=SimpleNamespace(
            enabled=True,
            minimum_score=0.70,
            max_opportunities_per_pass=8,
            max_digests_per_pass=1,
            max_digests_per_day=2,
            cooldown_days=14,
        ),
        notion=SimpleNamespace(data_sources={"opportunities": "opp"}),
    )
    notifier = _Notifier()
    stats = run_direct_outreach(
        settings,
        notion=_Notion([base, low]),
        store=tmp_store,
        notifier=notifier,
        dry_run=False,
    )
    assert stats.qualified == 0
    assert notifier.calls == []


def test_outreach_cooldown_suppresses_unchanged_evidence(tmp_store) -> None:
    instruction = "Email your resume to Maya Jones, Hiring Manager, at maya@acme.example."
    evidence = hashlib.sha256(("maya@acme.example\0" + instruction).encode()).hexdigest()
    tmp_store.record_direct_outreach(
        job_key="job-cooldown",
        contact_key="maya@acme.example",
        evidence_hash=evidence,
        contact_name="Maya Jones",
        contact_role="Hiring Manager",
        contact_email="maya@acme.example",
        instruction=instruction,
        confidence=1.0,
    )
    tmp_store.mark_direct_outreach_notified("job-cooldown", "maya@acme.example")
    page = {
        "id": "page-cooldown",
        "properties": {
            "Status": P.select("Review"),
            "My Score": P.number(0.91),
            "Role Title": P.rich_text("Engineer"),
            "Company": P.rich_text("Acme"),
            "URL": P.url("https://jobs.acme.example/jobs/3"),
            "Source Adapter": P.select("greenhouse"),
            "Fingerprint": P.rich_text("job-cooldown"),
            "Description": P.rich_text(instruction),
        },
    }
    settings = SimpleNamespace(
        direct_outreach=SimpleNamespace(
            enabled=True,
            minimum_score=0.70,
            max_opportunities_per_pass=8,
            max_digests_per_pass=1,
            max_digests_per_day=1,
            cooldown_days=14,
        ),
        notion=SimpleNamespace(data_sources={"opportunities": "opp"}),
    )
    stats = run_direct_outreach(
        settings,
        notion=_Notion([page]),
        store=tmp_store,
        notifier=_Notifier(),
        dry_run=False,
    )
    assert stats.suppressed == 1
    assert stats.digest_queued is False
