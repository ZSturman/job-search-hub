from __future__ import annotations

from job_search_hub.browser_runtime import BrowserPage
from job_search_hub.config import load_settings
from job_search_hub.ingest.browser_boards import BOARDS, BrowserBoardAdapter


def test_browser_board_normalizes_public_jsonld_and_records_provenance(tmp_path) -> None:
    class Runtime:
        error = ""

        def render(self, *_args, **_kwargs):
            return BrowserPage(
                url="https://wellfound.com/jobs",
                http_status=200,
                html="""
                <script type="application/ld+json">
                  {"@type":"JobPosting","identifier":"wf-1",
                   "title":"Software Engineer","url":"/jobs/wf-1",
                   "description":"Build reliable Python software.",
                   "hiringOrganization":{"name":"Acme"}}
                </script>
                """,
            )

    board = next(board for board in BOARDS if board.key == "wellfound")
    adapter = BrowserBoardAdapter(load_settings(env_file=tmp_path / "missing.env"), board)
    adapter.browser_runtime = Runtime()

    postings = list(adapter.fetch())

    assert len(postings) == 1
    posting = postings[0]
    assert posting.source == "Wellfound"
    assert posting.source_adapter == "wellfound"
    assert posting.company == "Acme"
    assert posting.url == "https://wellfound.com/jobs/wf-1"
    assert posting.extras["strategy"] == "playwright_public_board"
    assert adapter.last_diagnostic["status"] == "ok"


def test_browser_board_records_unavailable_runtime_without_raising(tmp_path) -> None:
    class Runtime:
        error = "Playwright unavailable: Error"

        def render(self, *_args, **_kwargs):
            return None

    board = next(board for board in BOARDS if board.key == "wellfound")
    adapter = BrowserBoardAdapter(load_settings(env_file=tmp_path / "missing.env"), board)
    adapter.browser_runtime = Runtime()

    assert list(adapter.fetch()) == []
    assert adapter.last_diagnostic["status"] == "browser_unavailable"


def test_browser_board_ignores_login_and_listing_navigation_links(tmp_path) -> None:
    class Runtime:
        error = ""

        def render(self, *_args, **_kwargs):
            return BrowserPage(
                url="https://wellfound.com/jobs",
                http_status=200,
                html="""
                <a href="https://account.wellfound.com/login">Log in</a>
                <a href="/jobs">All jobs</a>
                <a href="/jobs/1">Software Engineer</a>
                """,
            )

    board = next(board for board in BOARDS if board.key == "wellfound")
    adapter = BrowserBoardAdapter(load_settings(env_file=tmp_path / "missing.env"), board)
    adapter.browser_runtime = Runtime()

    postings = list(adapter.fetch())
    assert [posting.role_title for posting in postings] == ["Software Engineer"]
    assert adapter.last_diagnostic["status"] == "ok"
