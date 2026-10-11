from __future__ import annotations

from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.pipeline.cleanup import cleanup_opportunities
from job_search_hub.pipeline.lifecycle import map_opportunity_status

SPANISH = (
    "Desarrolla aplicaciones con React y Python junto a un equipo de ingeniería. "
    "Escribe pruebas automatizadas, revisa código y mejora la seguridad y el "
    "rendimiento del producto para nuestros clientes."
)
THIN_ENGLISH = "Senior Software Engineer"


def _page(page_id: str, *, priority: bool = False, relation: bool = False) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Company": P.rich_text("Acme"),
            "Role Title": P.rich_text("React Developer"),
            "URL": P.url(f"https://example.com/jobs/{page_id}"),
            "Description": P.rich_text(SPANISH),
            "Status": {"status": {"name": "New"}},
            "My Decision": P.select("Undecided"),
            "Priority": P.checkbox(priority),
            "Application": P.relation(["app-1"] if relation else []),
            "Model Score": P.number(None),
        },
    }


def test_cleanup_dry_run_and_commit_protect_touched_rows(tmp_path) -> None:
    class Notion:
        def __init__(self):
            self.archived = []
            self.updated = []

        def query_data_source(self, data_source_id, **kwargs):
            return [
                _page("archive-me"),
                _page("priority", priority=True),
                _page("application", relation=True),
            ]

        def archive_page(self, page_id):
            self.archived.append(page_id)

        def update_page(self, page_id, properties):
            self.updated.append((page_id, properties))

    settings = load_settings(env_file=tmp_path / "missing.env")
    notion = Notion()
    dry = cleanup_opportunities(settings, notion=notion)
    assert dry.would_archive == 1
    assert dry.protected == 2
    assert notion.archived == []
    assert notion.updated == []

    committed = cleanup_opportunities(settings, notion=notion, commit=True)
    assert committed.archived == 1
    assert notion.archived == ["archive-me"]
    assert notion.updated == []

    migrated = cleanup_opportunities(
        settings,
        notion=notion,
        commit=True,
        migrate_statuses=True,
    )
    assert migrated.statuses_migrated == 2
    assert {page_id for page_id, _ in notion.updated} == {"priority", "application"}
    assert all(props["Status"] == P.select("Fetched") for _, props in notion.updated)


def test_legacy_approved_rows_do_not_map_to_queue() -> None:
    assert map_opportunity_status("Approved to Prepare") == "Fetched"


def test_cleanup_retains_plausible_technical_title_with_sparse_description(
    tmp_path,
) -> None:
    page = _page("technical")
    page["properties"]["Role Title"] = {"rich_text": [{"plain_text": "Senior Software Engineer"}]}
    page["properties"]["Description"] = {"rich_text": [{"plain_text": THIN_ENGLISH}]}

    class Notion:
        def __init__(self):
            self.archived = []

        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def archive_page(self, page_id):
            self.archived.append(page_id)

        def update_page(self, page_id, properties):
            return None

    settings = load_settings(env_file=tmp_path / "missing.env")
    notion = Notion()
    stats = cleanup_opportunities(settings, notion=notion, commit=True)

    assert stats.archived == 0
    assert stats.retained_for_review == 1
    assert stats.retention_reasons == {"insufficient language evidence": 1}
    assert notion.archived == []


def test_cleanup_requires_opt_in_before_archiving_irrelevant_rows(tmp_path) -> None:
    page = _page("irrelevant")
    page["properties"]["Company"] = {"rich_text": [{"plain_text": "Acme"}]}
    page["properties"]["Role Title"] = {"rich_text": [{"plain_text": "Office Assistant"}]}
    page["properties"]["Description"] = {
        "rich_text": [
            {
                "plain_text": (
                    "Coordinate office schedules, prepare meeting notes, organize "
                    "supplies, and support administrative projects for the team."
                )
            }
        ]
    }

    class Notion:
        def __init__(self):
            self.archived = []

        def query_data_source(self, data_source_id, **kwargs):
            return [page]

        def archive_page(self, page_id):
            self.archived.append(page_id)

        def update_page(self, page_id, properties):
            return None

    settings = load_settings(env_file=tmp_path / "missing.env")
    notion = Notion()
    safe = cleanup_opportunities(settings, notion=notion, commit=True)
    assert safe.archived == 0
    assert safe.retention_reasons == {"relevance cleanup requires explicit opt-in": 1}

    opted_in = cleanup_opportunities(
        settings,
        notion=notion,
        commit=True,
        include_irrelevant=True,
    )
    assert opted_in.archived == 1
    assert notion.archived == ["irrelevant"]


def test_cleanup_snapshots_and_deduplicates_before_writes(tmp_path) -> None:
    duplicate = _page("duplicate")

    class Notion:
        def __init__(self):
            self.archived = []
            self.query_finished = False

        def query_data_source(self, data_source_id, **kwargs):
            yield duplicate
            yield duplicate
            yield {"id": "trashed", "archived": True, "properties": {}}
            self.query_finished = True

        def archive_page(self, page_id):
            assert self.query_finished
            self.archived.append(page_id)

        def update_page(self, page_id, properties):
            assert self.query_finished

    settings = load_settings(env_file=tmp_path / "missing.env")
    notion = Notion()
    stats = cleanup_opportunities(settings, notion=notion, commit=True)

    assert stats.already_trashed == 1
    assert stats.archived == 1
    assert notion.archived == ["duplicate"]
