from __future__ import annotations

from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.profile import load_facts


def test_load_facts_keeps_active_verified_profile_context(tmp_path) -> None:
    pages = [
        {
            "id": "fact-1",
            "properties": {
                "Fact": P.title("Python"),
                "Fact ID": P.rich_text("skill-python"),
                "Fact Type": P.select("Skill"),
                "Parent Fact": P.relation([]),
                "Value": P.rich_text("Python"),
                "Category": P.select("Language"),
                "Emphasis Tags": P.multi_select(["Backend"]),
                "Verified": P.checkbox(True),
                "Approved for Profile": P.checkbox(True),
                "Approved for Applications": P.checkbox(True),
                "Approved for Cover Letters": P.checkbox(False),
                "Metrics Verified": P.checkbox(False),
                "Sensitive": P.checkbox(False),
                "Sort Order": P.number(10),
                "Active": P.checkbox(True),
            },
        },
        {
            "id": "inactive",
            "properties": {
                "Fact": P.title("Inactive"),
                "Active": P.checkbox(False),
            },
        },
    ]

    class Notion:
        def query_data_source(self, data_source_id):
            assert data_source_id == "facts"
            return pages

    settings = load_settings(env_file=tmp_path / "missing.env")
    settings.notion.data_sources["resume_facts"] = "facts"

    facts = load_facts(Notion(), settings)

    assert [fact.page_id for fact in facts] == ["fact-1"]
    assert facts[0].value == "Python"
    assert facts[0].approved_for_profile is True
