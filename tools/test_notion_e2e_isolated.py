"""Complete isolated Notion end-to-end verification with realistic dummy data.

Creates, populates, updates, and verifies an independent Notion hub test workspace
with realistic job listings, companies, applications, recruiter notes, interview
schedules, candidate resume facts, and reusable answers.

Can run with the in-memory HubNotion fixture or against a live disposable Notion root
page when --live is specified.

Outputs a full snapshot to docs/test_notion_workspace_snapshot.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import UUID

from job_search_hub.config import NotionSettings, load_settings
from job_search_hub.notion import properties as P
from job_search_hub.notion.hub import bind, provision
from job_search_hub.pipeline.lifecycle_sync import reconcile_lifecycles
from job_search_hub.pipeline.organizations import sync_opportunity_organizations
from job_search_hub.transfer import export_notion
from job_search_hub.workflow import validate_hub
from tests.hub_fake import HubNotion


def run_isolated_notion_test(
    live_token: str | None = None,
    live_root: str | None = None,
    snapshot_path: Path | str | None = None,
) -> dict:
    if live_token and live_root:
        from job_search_hub.notion.client import NotionClient

        notion = NotionClient(NotionSettings(live_token, "2025-09-03", {}))
        root = live_root
        print(f"Connecting to live Notion root: {root}")
    else:
        root = str(UUID(int=42))
        notion = HubNotion()
        notion.blocks[root] = []
        print(f"Using isolated HubNotion fixture (root: {root})")

    # Step 1: Provision all 10 databases, views, and numbered navigation pages
    print("Provisioning 10 databases, views, and numbered navigation pages...")
    provision(notion, root)
    base_settings = load_settings()
    settings = bind(base_settings, notion, root)

    # Validate template schemas
    validate_hub(settings, notion)
    print("All 10 database schemas successfully validated.")

    ds = settings.notion.data_sources

    # Step 2: Populate Organizations (Companies, recruiters, and notes)
    print("Populating Organizations (Companies & Recruiters)...")
    notion.create_page(
        parent={"data_source_id": ds["organizations"]},
        properties={
            "Organization": P.title("Acme Cloud Infrastructure"),
            "Careers URL": P.url("https://acmecloud.example/careers"),
            "Homepage": P.url("https://acmecloud.example"),
            "Industry": P.multi_select(["Cloud & Infrastructure", "Developer Tools"]),
            "Remote Friendly": P.select("Yes"),
            "Priority Tier": P.select("1"),
            "Ethical Fit": P.select("Positive"),
            "Locations": P.rich_text("San Francisco, CA / Remote (US)"),
            "Notes": P.rich_text(
                "Series D infrastructure provider. "
                "Recruiter contact: Sarah Jenkins (sarah.j@acmecloud.example). "
                "Strong engineering blog, excellent Kubernetes tooling culture."
            ),
            "Active": P.checkbox(True),
        },
    )

    notion.create_page(
        parent={"data_source_id": ds["organizations"]},
        properties={
            "Organization": P.title("GreenPulse AI"),
            "Careers URL": P.url("https://greenpulse.example/jobs"),
            "Homepage": P.url("https://greenpulse.example"),
            "Industry": P.multi_select(["Climate Tech", "AI & Machine Learning"]),
            "Remote Friendly": P.select("Yes"),
            "Priority Tier": P.select("1"),
            "Ethical Fit": P.select("Positive"),
            "Locations": P.rich_text("Seattle, WA / Remote (North America)"),
            "Notes": P.rich_text(
                "Climate intelligence platform optimizing grid power. "
                "Recruiter: Marcus Vance. "
                "Engineering team is 35 people, fast interview turnaround."
            ),
            "Active": P.checkbox(True),
        },
    )

    notion.create_page(
        parent={"data_source_id": ds["organizations"]},
        properties={
            "Organization": P.title("VectorDB Labs"),
            "Careers URL": P.url("https://vectordb.example/careers"),
            "Homepage": P.url("https://vectordb.example"),
            "Industry": P.multi_select(["Database", "Data Infrastructure"]),
            "Remote Friendly": P.select("Yes"),
            "Priority Tier": P.select("2"),
            "Ethical Fit": P.select("Neutral"),
            "Locations": P.rich_text("New York, NY / Remote (US)"),
            "Notes": P.rich_text(
                "Open-source vector search engine. High-performance C++ and Python codebase."
            ),
            "Active": P.checkbox(True),
        },
    )

    # Step 3: Populate Opportunities (Job Listings)
    print("Populating Opportunities (Job Listings)...")
    job_platform = notion.create_page(
        parent={"data_source_id": ds["opportunities"]},
        properties={
            "Opportunity": P.title("Acme Cloud Infrastructure • Staff Platform Engineer"),
            "Role Title": P.rich_text("Staff Platform Engineer"),
            "Company": P.rich_text("Acme Cloud Infrastructure"),
            "URL": P.url("https://acmecloud.example/careers/platform-engineer-401"),
            "Description": P.rich_text(
                "Lead our Kubernetes infrastructure team. Architect multi-region mesh, "
                "build internal developer platforms, and ensure 99.99% uptime for core APIs. "
                "Requirements: 7+ years backend/platform experience, deep Kubernetes internals."
            ),
            "Location": P.rich_text("Remote (US)"),
            "Remote Status": P.select("Remote"),
            "Employment Type": P.multi_select(["Full-time"]),
            "Compensation Text": P.rich_text("$195,000 - $225,000 + equity"),
            "Compensation Min": P.number(195000),
            "Compensation Max": P.number(225000),
            "Compensation Unit": P.select("Annual"),
            "Source": P.select("greenhouse"),
            "My Decision": P.select("Apply"),
            "Status": P.select("Review"),
        },
    )

    job_sre = notion.create_page(
        parent={"data_source_id": ds["opportunities"]},
        properties={
            "Opportunity": P.title("Acme Cloud Infrastructure • Senior SRE (Distributed Systems)"),
            "Role Title": P.rich_text("Senior SRE (Distributed Systems)"),
            "Company": P.rich_text("Acme Cloud Infrastructure"),
            "URL": P.url("https://acmecloud.example/careers/sre-distributed-systems"),
            "Description": P.rich_text(
                "Drive reliability and performance across petabyte-scale storage pipelines. "
                "Build automated chaos experiments and incident response automation."
            ),
            "Location": P.rich_text("Remote (US)"),
            "Remote Status": P.select("Remote"),
            "Employment Type": P.multi_select(["Full-time"]),
            "Compensation Text": P.rich_text("$180,000 - $210,000"),
            "Compensation Min": P.number(180000),
            "Compensation Max": P.number(210000),
            "Compensation Unit": P.select("Annual"),
            "Source": P.select("greenhouse"),
            "My Decision": P.select("Shortlist"),
            "Status": P.select("Review"),
        },
    )

    job_climate = notion.create_page(
        parent={"data_source_id": ds["opportunities"]},
        properties={
            "Opportunity": P.title("GreenPulse AI • Principal Backend Architect"),
            "Role Title": P.rich_text("Principal Backend Architect"),
            "Company": P.rich_text("GreenPulse AI"),
            "URL": P.url("https://greenpulse.example/jobs/principal-backend-architect"),
            "Description": P.rich_text(
                "Design real-time telemetry ingestion pipelines processing millions of sensors. "
                "Scale event streams with Kafka, Python, and Rust services deployed on AWS."
            ),
            "Location": P.rich_text("Remote (US/Canada)"),
            "Remote Status": P.select("Remote"),
            "Employment Type": P.multi_select(["Full-time"]),
            "Compensation Text": P.rich_text("$200,000 - $240,000"),
            "Compensation Min": P.number(200000),
            "Compensation Max": P.number(240000),
            "Compensation Unit": P.select("Annual"),
            "Source": P.select("lever"),
            "My Decision": P.select("Apply"),
            "Status": P.select("Review"),
        },
    )

    job_vectordb = notion.create_page(
        parent={"data_source_id": ds["opportunities"]},
        properties={
            "Opportunity": P.title("VectorDB Labs • Senior Systems Engineer"),
            "Role Title": P.rich_text("Senior Systems Engineer"),
            "Company": P.rich_text("VectorDB Labs"),
            "URL": P.url("https://vectordb.example/careers/systems-engineer"),
            "Description": P.rich_text(
                "Optimize SIMD vector index search latency. "
                "Write high-throughput storage engines and gRPC interfaces."
            ),
            "Location": P.rich_text("Remote (US)"),
            "Remote Status": P.select("Remote"),
            "Employment Type": P.multi_select(["Full-time"]),
            "Compensation Text": P.rich_text("$185,000 - $220,000"),
            "Compensation Min": P.number(185000),
            "Compensation Max": P.number(220000),
            "Compensation Unit": P.select("Annual"),
            "Source": P.select("ashby"),
            "My Decision": P.select("Apply"),
            "Status": P.select("Review"),
        },
    )

    # Step 4: Populate Resume Facts (Approved Candidate Profile)
    print("Populating Candidate Profile Facts...")
    notion.create_page(
        parent={"data_source_id": ds["resume_facts"]},
        properties={
            "Fact": P.title("Python (8+ years backend/distributed systems)"),
            "Fact ID": P.rich_text("pdf:backend-sre:skill:python-8yr"),
            "Fact Type": P.select("Skill"),
            "Category": P.select("Backend"),
            "Value": P.rich_text("Python (asyncio, FastAPI, PyTorch, PySpark, packaging)"),
            "Proficiency": P.select("Expert"),
            "Verified": P.checkbox(True),
            "Approved for Profile": P.checkbox(True),
            "Approved for Applications": P.checkbox(True),
            "Active": P.checkbox(True),
        },
    )

    fact_k8s = notion.create_page(
        parent={"data_source_id": ds["resume_facts"]},
        properties={
            "Fact": P.title("Kubernetes & Cloud Infrastructure"),
            "Fact ID": P.rich_text("pdf:backend-sre:skill:k8s-cloud"),
            "Fact Type": P.select("Skill"),
            "Category": P.select("DevOps"),
            "Value": P.rich_text("Kubernetes, Terraform, AWS (EKS, RDS, S3), Docker, Helm"),
            "Proficiency": P.select("Expert"),
            "Verified": P.checkbox(True),
            "Approved for Profile": P.checkbox(True),
            "Approved for Applications": P.checkbox(True),
            "Active": P.checkbox(True),
        },
    )

    fact_auth = notion.create_page(
        parent={"data_source_id": ds["resume_facts"]},
        properties={
            "Fact": P.title("US Work Authorization (Citizen)"),
            "Fact ID": P.rich_text("profile:legal:work-auth-us"),
            "Fact Type": P.select("Preference"),
            "Category": P.select("Other"),
            "Value": P.rich_text("Authorized to work in the US; no sponsorship needed"),
            "Proficiency": P.select("Expert"),
            "Verified": P.checkbox(True),
            "Approved for Profile": P.checkbox(True),
            "Approved for Applications": P.checkbox(True),
            "Active": P.checkbox(True),
        },
    )

    # Step 5: Populate Answer Bank (Reusable Question & Answers)
    print("Populating Reusable Answer Bank...")
    notion.create_page(
        parent={"data_source_id": ds["answer_bank"]},
        properties={
            "Question": P.title("Are you legally authorized to work in the United States?"),
            "Question Family": P.select("Work Authorization"),
            "Answer Type": P.select("Yes/No"),
            "Approved Answer": P.rich_text("Yes"),
            "Approved for Reuse": P.checkbox(True),
            "Supporting Facts": P.relation([fact_auth["id"]]),
            "Evidence Summary": P.rich_text("US Citizen with unrestricted work authorization."),
            "Active": P.checkbox(True),
        },
    )

    notion.create_page(
        parent={"data_source_id": ds["answer_bank"]},
        properties={
            "Question": P.title("Will you now or in the future require visa sponsorship?"),
            "Question Family": P.select("Sponsorship"),
            "Answer Type": P.select("Yes/No"),
            "Approved Answer": P.rich_text("No"),
            "Approved for Reuse": P.checkbox(True),
            "Supporting Facts": P.relation([fact_auth["id"]]),
            "Evidence Summary": P.rich_text("No visa sponsorship required now or in the future."),
            "Active": P.checkbox(True),
        },
    )

    notion.create_page(
        parent={"data_source_id": ds["answer_bank"]},
        properties={
            "Question": P.title("Describe your experience managing production Kubernetes:"),
            "Question Family": P.select("Skills"),
            "Answer Type": P.select("Long Text"),
            "Approved Answer": P.rich_text(
                "Managed multi-tenant EKS and bare-metal Kubernetes clusters for 2,500+ pods. "
                "Implemented ArgoCD GitOps pipelines and Prometheus/Grafana observability."
            ),
            "Approved for Reuse": P.checkbox(True),
            "Supporting Facts": P.relation([fact_k8s["id"]]),
            "Active": P.checkbox(True),
        },
    )

    # Step 6: Populate Resume Library
    print("Populating Resume Library...")
    notion.create_page(
        parent={"data_source_id": ds["resume_library"]},
        properties={
            "Resume": P.title("Staff Platform & SRE Resume (2026)"),
            "Document ID": P.rich_text(
                "0160a788b607f270ff965e45cc328059d4a49939378d121ff42055ac45a6c83c"
            ),
            "Tags": P.multi_select(["platform", "kubernetes", "sre", "python"]),
            "Availability": P.select("Available"),
            "Recommendation": P.rich_text("Best fit for Staff Platform and SRE roles."),
            "Jobs": P.relation([job_platform["id"], job_sre["id"]]),
        },
    )

    # Step 7: Populate Search Profiles and Sources
    print("Populating Search Profiles & Sources...")
    notion.create_page(
        parent={"data_source_id": ds["search_profiles"]},
        properties={
            "Profile": P.title("Platform & Distributed Systems"),
            "Enabled": P.checkbox(True),
            "Role Terms": P.multi_select(["Platform Engineer", "Staff SRE", "Backend Architect"]),
            "Skill Terms": P.multi_select(["Kubernetes", "Python", "Go", "Distributed Systems"]),
            "Mission Terms": P.multi_select(["Developer Tools", "Cloud", "Climate Tech"]),
            "Excluded Terms": P.multi_select(["PHP", "WordPress", "Junior"]),
            "Location": P.rich_text("Remote US"),
            "Remote Only": P.checkbox(True),
            "Minimum Annual Pay": P.number(170000),
        },
    )

    notion.create_page(
        parent={"data_source_id": ds["sources"]},
        properties={
            "Source": P.title("Acme Cloud Careers"),
            "Type": P.rich_text("greenhouse"),
            "Target": P.rich_text("acmecloud"),
            "Enabled": P.checkbox(True),
            "Cadence": P.select("Daily"),
            "Status": P.rich_text("Active"),
        },
    )

    # Step 8: Reconcile Lifecycles (Trigger core application creation for jobs marked 'Apply')
    print("Running Lifecycle Reconciliation (reconcile_lifecycles)...")
    summary = reconcile_lifecycles(settings, notion=notion, dry_run=False)
    assert not summary.errors, f"Reconciliation encountered errors: {summary.errors}"
    print(f"Lifecycle reconciliation created applications: {summary.manual_applications_created}")

    # Step 9: Verify and update Applications to different lifecycle stages
    print("Updating applications through interview and offer lifecycle states...")
    created_apps = list(notion.query_data_source(ds["applications"]))
    assert len(created_apps) == 3, f"Expected 3 applications created, got {len(created_apps)}"

    # Identify applications by linked job
    job_to_app = {}
    for app in created_apps:
        rel_jobs = P.read_relation_ids(app["properties"]["Opportunity"])
        if rel_jobs:
            job_to_app[rel_jobs[0]] = app

    # Advance Acme Platform Engineer to "Submitted"
    app_acme = job_to_app[job_platform["id"]]
    notion.update_page(
        app_acme["id"],
        {
            "Status": P.select("Submitted"),
            "Submitted At": P.date_property("2026-10-15T12:00:00Z"),
            "Confirmation Text": P.rich_text("Application received! Confirmation #ACM-2026-9481."),
            "Receipt URL": P.url("https://acmecloud.example/confirmation/9481"),
            "My Notes": P.rich_text("Submitted with Staff Platform Resume. Referral from team."),
        },
    )

    # Advance GreenPulse AI to "Interviewing"
    app_greenpulse = job_to_app[job_climate["id"]]
    notion.update_page(
        app_greenpulse["id"],
        {
            "Status": P.select("Interviewing"),
            "Submitted At": P.date_property("2026-10-10T12:00:00Z"),
            "Follow Up At": P.date_property("2026-10-18T17:00:00Z"),
            "Confirmation Text": P.rich_text("Selected for Interview Round 1."),
            "My Notes": P.rich_text(
                "Technical architecture screen scheduled with VP of Engineering for Oct 18. "
                "Topic: High-throughput telemetry ingestion & Kafka partitioning strategy."
            ),
        },
    )

    # Advance VectorDB Labs to "Offer"
    app_vectordb = job_to_app[job_vectordb["id"]]
    notion.update_page(
        app_vectordb["id"],
        {
            "Status": P.select("Offer"),
            "Submitted At": P.date_property("2026-10-01T10:00:00Z"),
            "My Notes": P.rich_text(
                "Offer received: $195,000 base + 0.15% equity (4yr vest). "
                "Decision deadline: end of month."
            ),
        },
    )

    # Step 10: Sync Opportunity Organizations
    print("Running Organization sync (sync_opportunity_organizations)...")
    sync_opportunity_organizations(settings, notion=notion, dry_run=False)

    # Step 11: Export the full isolated Notion snapshot
    snapshot_data = export_notion(notion, settings, root)
    if snapshot_path:
        out_p = Path(snapshot_path)
        out_p.write_text(json.dumps(snapshot_data, indent=2, default=str), encoding="utf-8")
        print(f"Snapshot written to {out_p}")

    # Summary verification counts
    counts = {key: len(records) for key, records in snapshot_data["sources"].items()}
    return {
        "status": "success",
        "root": root,
        "database_counts": counts,
        "snapshot_path": str(snapshot_path) if snapshot_path else None,
        "applications": {
            "Submitted": "Acme Cloud Infrastructure • Staff Platform Engineer",
            "Interviewing": "GreenPulse AI • Principal Backend Architect",
            "Offer": "VectorDB Labs • Senior Systems Engineer",
        },
    }


def main_cli():
    parser = argparse.ArgumentParser(description="Test Notion with isolated realistic dummy data.")
    parser.add_argument(
        "--live", action="store_true", help="Run against a real disposable Notion workspace"
    )
    parser.add_argument("--token", help="Notion integration token (required with --live)")
    parser.add_argument("--root", help="Notion root page URL or ID (required with --live)")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "docs"
        / "test_notion_workspace_snapshot.json",
        help="Path to export the Notion snapshot",
    )
    args = parser.parse_args()

    result = run_isolated_notion_test(
        live_token=args.token if args.live else None,
        live_root=args.root if args.live else None,
        snapshot_path=args.snapshot,
    )
    print("\n=== ISOLATED NOTION VERIFICATION RESULTS ===")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main_cli()
