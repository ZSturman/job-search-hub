"""Active Notion schema, provisioned by the hub template.

Data only. Used by `validate.py` to compare the live data sources against the
expected shape. We do NOT auto-repair the schema; we report and halt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from job_search_hub.pipeline.lifecycle import (
    ANSWER_BANK_STATUSES,
    APPLICATION_STATUSES,
    OPPORTUNITY_STATUSES,
)
from job_search_hub.topics import CANONICAL_TOPICS


@dataclass(frozen=True)
class PropertySpec:
    name: str
    type: str
    options: tuple[str, ...] = ()  # for select / multi_select / status
    relation_target: str | None = None  # database key, e.g. "answer_bank"
    formula: str | None = None  # expected formula expression
    rollup_source: tuple[str, str] | None = None  # (relation_property, target_property)
    required: bool = True


@dataclass(frozen=True)
class DataSourceSpec:
    key: str  # internal key, matches Settings.notion.data_sources
    title: str  # human label
    title_property: str  # the title-type property name
    properties: tuple[PropertySpec, ...] = field(default_factory=tuple)


# Lane / decision option lists used in several places.
LANES = ("Worth a Look", "Fast Review", "High Attention", "Skip")
QUESTION_INTENTS = (
    "Experience",
    "Years of Experience",
    "Proficiency",
    "Credential",
    "Location",
    "Relocation",
    "Work Authorization",
    "Sponsorship",
    "Availability",
    "Compensation",
    "Other",
)
ANSWER_TYPES = (
    "Yes/No",
    "Number",
    "Short Text",
    "Long Text",
    "Select",
    "Multi-select",
    "Sensitive",
)
RELATIONSHIP_TYPES = (
    "Paid Employment",
    "Contract",
    "Volunteer",
    "Client Project",
    "University Collaboration",
    "Independent Project",
    "Exploratory Project",
    "Not Applicable",
)


OPPORTUNITIES_STATUS = OPPORTUNITY_STATUSES


OPPORTUNITIES = DataSourceSpec(
    key="opportunities",
    title="Opportunities",
    title_property="Opportunity",
    properties=(
        PropertySpec("Opportunity", "title"),
        PropertySpec("Company", "rich_text"),
        PropertySpec("Role Title", "rich_text"),
        PropertySpec("URL", "url"),
        PropertySpec("Source", "select"),
        PropertySpec("Source Adapter", "select"),
        PropertySpec("External Job ID", "rich_text"),
        PropertySpec("Fingerprint", "rich_text"),
        PropertySpec("Description", "rich_text"),
        PropertySpec("Discovered At", "date"),
        PropertySpec("Date job was posted", "date"),
        PropertySpec("Updated At", "date"),
        PropertySpec("Posting Checked At", "date"),
        PropertySpec("Location", "rich_text"),
        PropertySpec("Remote Status", "select", options=("Remote", "Hybrid", "On-site", "Unknown")),
        PropertySpec(
            "Schedule", "select", options=("Daytime", "Night Shift", "Variable", "Unknown")
        ),
        PropertySpec("Employment Type", "multi_select"),
        PropertySpec("Compensation Text", "rich_text"),
        PropertySpec("Compensation Min", "number"),
        PropertySpec("Compensation Max", "number"),
        PropertySpec(
            "Compensation Unit", "select", options=("Annual", "Hourly", "Monthly", "Unknown")
        ),
        PropertySpec("Hard Exclusion", "checkbox"),
        PropertySpec("Exclusion Reason", "rich_text"),
        PropertySpec("Model Score", "number"),
        PropertySpec("Confidence", "number"),
        PropertySpec("Model Lane", "select", options=LANES),
        PropertySpec("Model Reason", "rich_text"),
        PropertySpec("Top Matches", "rich_text"),
        PropertySpec("Concerns", "rich_text"),
        PropertySpec("Answer Bank Items", "relation", relation_target="answer_bank"),
        PropertySpec(
            "Missing Answer Count", "rollup", rollup_source=("Answer Bank Items", "Needs Answer")
        ),
        PropertySpec(
            "Qualification Requirements",
            "relation",
            relation_target="qualification_requirements",
        ),
        PropertySpec("Qualification Topics", "multi_select", required=False),
        PropertySpec("Hard Qualification Topics", "multi_select", required=False),
        PropertySpec("Preferred Qualification Topics", "multi_select", required=False),
        PropertySpec(
            "Unresolved Qualification Count",
            "rollup",
            rollup_source=("Qualification Requirements", "Needs Answer"),
        ),
        PropertySpec("Qualification Score", "number"),
        PropertySpec("Base Model Score", "number"),
        PropertySpec("Base Score Numerator", "number"),
        PropertySpec("Base Score Weight", "number"),
        PropertySpec("Base Confidence", "number"),
        PropertySpec("Base Model Reason", "rich_text"),
        PropertySpec("Base Top Matches", "rich_text"),
        PropertySpec("Base Concerns", "rich_text"),
        PropertySpec(
            "Base Lane Override",
            "select",
            options=("None",) + LANES,
        ),
        PropertySpec("Status", "select", options=OPPORTUNITIES_STATUS),
        PropertySpec("My Score", "number"),
        PropertySpec("My Lane", "select", options=LANES),
        PropertySpec(
            "My Decision",
            "select",
            options=("Undecided", "Shortlist", "Apply", "Skip", "Save for Later"),
        ),
        PropertySpec("My Notes", "rich_text"),
        PropertySpec("Resume to use", "rich_text", required=False),
        PropertySpec("Suggested Resume", "rich_text", required=False),
        PropertySpec("Next Action", "rich_text", required=False),
        PropertySpec(
            "Processing State", "select", options=("Pending", "Complete", "Failed"), required=False
        ),
        PropertySpec("Priority", "checkbox"),
        PropertySpec("Organization", "relation", relation_target="organizations"),
        PropertySpec("Application", "relation", relation_target="applications"),
        PropertySpec("Last Error", "rich_text"),
        PropertySpec("Error Notified", "checkbox"),
    ),
)


APPLICATIONS = DataSourceSpec(
    key="applications",
    title="Applications",
    title_property="Application",
    properties=(
        PropertySpec("Application", "title"),
        PropertySpec("Opportunity", "relation", relation_target="opportunities"),
        PropertySpec("Job URL", "url"),
        PropertySpec("Status", "select", options=APPLICATION_STATUSES),
        PropertySpec("Submission Mode", "select", options=("Manual",)),
        PropertySpec("Portal Type", "select"),
        PropertySpec("Answer Bank Items", "relation", relation_target="answer_bank"),
        PropertySpec(
            "Missing Answer Count", "rollup", rollup_source=("Answer Bank Items", "Needs Answer")
        ),
        PropertySpec("Submitted At", "date"),
        PropertySpec("Follow Up At", "date"),
        PropertySpec("Receipt URL", "url"),
        PropertySpec("Confirmation Text", "rich_text"),
        PropertySpec("Audit ID", "rich_text"),
        PropertySpec("My Notes", "rich_text"),
        PropertySpec("Last Error", "rich_text"),
        PropertySpec("Error Notified", "checkbox"),
    ),
)


ORGANIZATIONS = DataSourceSpec(
    key="organizations",
    title="Organizations",
    title_property="Organization",
    properties=(
        PropertySpec("Organization", "title"),
        PropertySpec("Careers URL", "url"),
        PropertySpec("Homepage", "url"),
        PropertySpec("Industry", "multi_select"),
        PropertySpec("Source Types", "multi_select"),
        PropertySpec("Mission Notes", "rich_text"),
        PropertySpec("Ethical Notes", "rich_text"),
        PropertySpec("Ethical Fit", "select", options=("Positive", "Neutral", "Review", "Exclude")),
        PropertySpec("Priority Tier", "select", options=("1", "2", "3", "None")),
        PropertySpec("Check Cadence", "select", options=("Daily", "Weekly", "Monthly", "Manual")),
        PropertySpec("Last Checked", "date"),
        PropertySpec("Next Check", "date"),
        PropertySpec("Remote Friendly", "select", options=("Yes", "No", "Unknown")),
        PropertySpec("Locations", "rich_text"),
        PropertySpec("Notes", "rich_text"),
        PropertySpec("Active", "checkbox"),
    ),
)


RESUME_FACTS = DataSourceSpec(
    key="resume_facts",
    title="Profile Facts",
    title_property="Fact",
    properties=(
        PropertySpec("Fact", "title"),
        PropertySpec("Fact ID", "rich_text"),
        PropertySpec(
            "Fact Type",
            "select",
            options=(
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
            ),
        ),
        PropertySpec("Parent Fact", "relation", relation_target="resume_facts"),
        PropertySpec("Value", "rich_text"),
        PropertySpec("Organization", "rich_text"),
        PropertySpec("Role Label", "rich_text"),
        PropertySpec("Relationship Type", "select", options=RELATIONSHIP_TYPES),
        PropertySpec("Start Date", "date"),
        PropertySpec("End Date", "date"),
        PropertySpec("Current", "checkbox"),
        PropertySpec(
            "Category",
            "select",
            options=(
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
            ),
        ),
        PropertySpec(
            "Proficiency",
            "select",
            options=("Familiar", "Working", "Strong", "Expert", "Do Not Claim"),
        ),
        PropertySpec("Emphasis Tags", "multi_select"),
        PropertySpec("Verified", "checkbox"),
        PropertySpec("Approved for Profile", "checkbox"),
        PropertySpec("Approved for Applications", "checkbox"),
        PropertySpec("Metrics Verified", "checkbox"),
        PropertySpec("Sensitive", "checkbox"),
        PropertySpec("Source", "rich_text"),
        PropertySpec("Sort Order", "number"),
        PropertySpec("Notes", "rich_text"),
        PropertySpec("Active", "checkbox"),
        PropertySpec("Updated At", "last_edited_time"),
    ),
)


ANSWER_BANK = DataSourceSpec(
    key="answer_bank",
    title="Answer Bank",
    title_property="Question",
    properties=(
        PropertySpec("Question", "title"),
        PropertySpec("Normalized Question", "rich_text"),
        PropertySpec(
            "Question Family",
            "select",
            options=(
                "Work Authorization",
                "Sponsorship",
                "Salary",
                "Location",
                "Relocation",
                "Availability",
                "Years of Experience",
                "Skills",
                "Motivation",
                "Background",
                "Demographics",
                "Legal Attestation",
                "Other",
            ),
        ),
        PropertySpec("Answer Type", "select", options=ANSWER_TYPES),
        PropertySpec("Canonical Key", "rich_text"),
        PropertySpec("Topics", "multi_select", options=CANONICAL_TOPICS),
        PropertySpec("Question Intent", "select", options=QUESTION_INTENTS),
        PropertySpec("Normalized Answer", "rich_text"),
        PropertySpec("Superseded By", "relation", relation_target="answer_bank"),
        PropertySpec("My Answer", "rich_text"),
        PropertySpec("Status", "select", options=ANSWER_BANK_STATUSES),
        PropertySpec("Approved for Reuse", "checkbox"),
        PropertySpec("Sensitive", "checkbox"),
        PropertySpec("Context Specific", "checkbox"),
        PropertySpec("Reuse Threshold", "number"),
        PropertySpec("Fact Sources", "relation", relation_target="resume_facts"),
        PropertySpec("Related Opportunities", "relation", relation_target="opportunities"),
        PropertySpec("Related Applications", "relation", relation_target="applications"),
        PropertySpec("First Seen", "date"),
        PropertySpec("Last Used", "date"),
        PropertySpec("Use Count", "number"),
        PropertySpec("Draft Source", "select", options=("Human", "LLM", "Imported")),
        PropertySpec("Notes", "rich_text"),
        PropertySpec("Error Notified", "checkbox"),
        PropertySpec("Needs Answer", "formula", formula='prop("Status") == "Needs Answer"'),
    ),
)


QUALIFICATION_REQUIREMENTS = DataSourceSpec(
    key="qualification_requirements",
    title="Qualification Requirements",
    title_property="Qualification",
    properties=(
        PropertySpec("Qualification", "title"),
        PropertySpec("Opportunity", "relation", relation_target="opportunities"),
        PropertySpec("Answer Bank Item", "relation", relation_target="answer_bank"),
        PropertySpec("Canonical Key", "rich_text"),
        PropertySpec("Topics", "multi_select", options=CANONICAL_TOPICS),
        PropertySpec("Question Intent", "select", options=QUESTION_INTENTS),
        PropertySpec(
            "Requirement Kind",
            "select",
            options=(
                "Skill",
                "Tool",
                "Credential",
                "Years of Experience",
                "Location",
                "Relocation",
                "Work Authorization",
                "Other",
            ),
        ),
        PropertySpec(
            "Importance",
            "select",
            options=(
                "Hard Required",
                "Required",
                "Preferred",
            ),
        ),
        PropertySpec("Answer Type", "select", options=ANSWER_TYPES),
        PropertySpec(
            "Expected Operator",
            "select",
            options=(
                "Is Yes",
                "Minimum",
                "Maximum",
                "Equals",
                "One Of",
                "Contains",
                "Human Review",
            ),
        ),
        PropertySpec("Expected Answer", "rich_text"),
        PropertySpec("Numeric Threshold", "number"),
        PropertySpec("Meets Score", "number"),
        PropertySpec("Partial Score", "number"),
        PropertySpec("Does Not Meet Score", "number"),
        PropertySpec("Score Weight", "number"),
        PropertySpec("Failure Action", "select", options=("Reject", "Score Only")),
        PropertySpec(
            "Resolution",
            "select",
            options=(
                "Awaiting Answer",
                "Meets",
                "Partial",
                "Does Not Meet",
                "Unknown",
                "Error",
            ),
        ),
        PropertySpec("Resolution Reason", "rich_text"),
        PropertySpec("Source Requirement", "rich_text"),
        PropertySpec("Requirement Fingerprint", "rich_text"),
        PropertySpec("Active", "checkbox"),
        PropertySpec("Last Evaluated", "date"),
        PropertySpec(
            "Needs Answer",
            "formula",
            formula='prop("Resolution") == "Awaiting Answer"',
        ),
    ),
)


SCORING_CRITERIA = DataSourceSpec(
    key="scoring_criteria",
    title="Scoring Criteria",
    title_property="Criterion",
    properties=(
        PropertySpec("Criterion", "title"),
        PropertySpec("Key", "rich_text"),
        PropertySpec(
            "Category",
            "select",
            options=(
                "Schedule",
                "Compensation",
                "Ethics",
                "Skills",
                "Transferable Skills",
                "Location",
                "Mission",
                "Learning",
                "Interview Plausibility",
                "Effort",
                "Source Quality",
                "Organization Priority",
                "Other",
            ),
        ),
        PropertySpec("Active", "checkbox"),
        PropertySpec("Weight", "number"),
        PropertySpec(
            "Direction",
            "select",
            options=(
                "Positive",
                "Negative",
                "Hard Exclusion",
                "Human Review Trigger",
                "Lane Override",
            ),
        ),
        PropertySpec(
            "Evaluation Method",
            "select",
            options=(
                "LLM",
                "Rule",
                "Numeric",
                "Keyword",
                "Manual",
                "Hybrid",
            ),
        ),
        PropertySpec("Hard Exclusion", "checkbox"),
        PropertySpec("Lane Override", "select", options=("None",) + LANES),
        PropertySpec("Human Review Trigger", "checkbox"),
        PropertySpec("Instructions", "rich_text"),
        PropertySpec("Minimum Value", "number"),
        PropertySpec("Maximum Value", "number"),
        PropertySpec("Notes", "rich_text"),
        PropertySpec("Sort Order", "number"),
    ),
)


ALL_DATA_SOURCES: tuple[DataSourceSpec, ...] = (
    OPPORTUNITIES,
    APPLICATIONS,
    ORGANIZATIONS,
    RESUME_FACTS,
    ANSWER_BANK,
    QUALIFICATION_REQUIREMENTS,
    SCORING_CRITERIA,
)


def get_spec(key: str) -> DataSourceSpec:
    for ds in ALL_DATA_SOURCES:
        if ds.key == key:
            return ds
    raise KeyError(key)
