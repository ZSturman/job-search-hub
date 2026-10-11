"""Central Notion Select lifecycles and legacy migration helpers."""

from __future__ import annotations

from job_search_hub.notion import properties as P

OPPORTUNITY_STATUSES = (
    "Fetched",
    "Scoring",
    "Review",
    "Verification Needed",
    "Shortlisted",
    "Rejected",
    "Awaiting Answer",
    "Needs Input",
    "Submitted",
    "Interviewing",
    "Offer",
    "Closed",
    "Error",
)
APPLICATION_STATUSES = (
    "Planned",
    "Submitted",
    "Interviewing",
    "Offer",
    "Rejected",
    "Cancelled",
    "Closed",
    "Error",
)

ANSWER_BANK_STATUSES = (
    "Needs Answer",
    "Drafted",
    "Needs Review",
    "Approved",
    "Context Specific",
    "Deprecated",
)

POST_APPLICATION_OPPORTUNITY_STATUSES = {
    "Shortlisted",
    "Submitted",
    "Interviewing",
    "Offer",
    "Closed",
}

_LEGACY_OPPORTUNITY_STATUS = {
    "Ready to Submit": "Shortlisted",
    "Generating": "Review",
    "Awaiting Resume": "Review",
    "Queued": "Review",
    "New": "Fetched",
    "Not started": "Fetched",
    "Scoring": "Scoring",
    "In progress": "Scoring",
    "Worth a Look": "Review",
    "Fast Review": "Review",
    "High Attention": "Review",
    "Skip": "Rejected",
    "Rejected": "Rejected",
    # Old automation auto-approved nearly every accepted posting. Retained
    # rows go back through scoring instead of being silently queued.
    "Approved to Prepare": "Fetched",
    "Preparing": "Review",
    "Waiting for My Answer": "Needs Input",
    "Awaiting Answer": "Awaiting Answer",
    "Needs Resume Review": "Review",
    "Ready for Review": "Shortlisted",
    "Approved to Submit": "Shortlisted",
    "Submitted": "Submitted",
    "Follow Up Due": "Submitted",
    "Interviewing": "Interviewing",
    "Offer": "Offer",
    "Closed": "Closed",
    "Done": "Closed",
    "Error": "Error",
}

_LEGACY_APPLICATION_STATUS = {
    "Not started": "Planned",
    "In progress": "Planned",
    "Needs Review": "Planned",
    "Ready to Submit": "Planned",
    "Generating": "Planned",
    "Awaiting Resume": "Planned",
    "Queued": "Planned",
    "Done": "Submitted",
}

_LEGACY_ANSWER_STATUS = {
    "Needs My Answer": "Needs Answer",
    "Needs My Review": "Needs Review",
    "Not started": "Needs Answer",
    "In progress": "Needs Review",
    "Done": "Approved",
}


def read_status(prop: dict | None) -> str:
    """Read the new Select shape, while tolerating old Status rows."""
    return P.read_select(prop) or P.read_status(prop)


def select_filter(status: str) -> dict:
    return {"property": "Status", "select": {"equals": status}}


def map_opportunity_status(value: str) -> str:
    return (
        value if value in OPPORTUNITY_STATUSES else _LEGACY_OPPORTUNITY_STATUS.get(value, "Fetched")
    )


def map_application_status(value: str) -> str:
    return (
        value if value in APPLICATION_STATUSES else _LEGACY_APPLICATION_STATUS.get(value, "Planned")
    )


def map_answer_status(value: str) -> str:
    return (
        value if value in ANSWER_BANK_STATUSES else _LEGACY_ANSWER_STATUS.get(value, "Needs Answer")
    )
