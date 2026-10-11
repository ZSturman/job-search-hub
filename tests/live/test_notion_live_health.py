from __future__ import annotations

import pytest

from job_search_hub.notion.audit import run_notion_audit
from job_search_hub.storage import get_setting

pytestmark = pytest.mark.skipif(
    get_setting("JOBHUB_LIVE_NOTION") != "1",
    reason="set JOBHUB_LIVE_NOTION=1 to run live Notion checks",
)


def test_live_notion_schema_and_operational_audit() -> None:
    from job_search_hub.workflow import session, validate_hub

    with session(preview=True) as (settings, notion):
        validate_hub(settings, notion)
        audit = run_notion_audit(settings, notion=notion, sample_size=5)

    assert audit.ok()
