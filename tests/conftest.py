from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path: Path, request):
    """Force a clean environment for tests that load Settings.

    Tests that need real config should set their own env vars after this
    fixture runs.
    """
    import os

    from job_search_hub import storage

    live_notion = ".live." in request.module.__name__ and os.getenv("JOBHUB_LIVE_NOTION") == "1"
    keep = (
        {"JOBHUB_LIVE_NOTION", "JOBHUB_LIVE_NOTION_ROOT", "JOBHUB_NOTION_TOKEN"}
        if live_notion
        else set()
    )
    if live_notion and not (
        os.getenv("JOBHUB_LIVE_NOTION_ROOT") and os.getenv("JOBHUB_NOTION_TOKEN")
    ):
        pytest.fail("Live tests require an explicit disposable root and JOBHUB_NOTION_TOKEN.")
    for key in list(os.environ):
        if key.startswith("JOBHUB_") and key not in keep:
            monkeypatch.delenv(key, raising=False)
    if live_notion:
        monkeypatch.setenv("JOBHUB_NOTION_ROOT", os.environ["JOBHUB_LIVE_NOTION_ROOT"])
    monkeypatch.setenv("JOBHUB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JOBHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBHUB_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("JOBHUB_SQLITE_PATH", str(tmp_path / "data/job_search_hub.sqlite"))
    monkeypatch.setenv("JOBHUB_NOTIFICATION_LEDGER_PATH", str(tmp_path / "data/ledger.sqlite"))

    class TestCredentialStore:
        def get_password(self, *_):
            return None

        def set_password(self, *_):
            raise RuntimeError("test keychain is locked")

    monkeypatch.setattr(storage, "_credential_backend", lambda: TestCredentialStore())
    if request.module.__name__.split(".")[-1] in {
        "test_discovery_filters",
        "test_discovery_preview",
    }:
        monkeypatch.setenv(
            "JOBHUB_JOB_SEARCH_ROLE_TERMS",
            (
                "React Developer|Frontend Engineer|Software Engineer|Software "
                "Developer|Engineer|Developer|IT Specialist|Data Analyst"
            ),
        )
        monkeypatch.setenv("JOBHUB_JOB_SEARCH_SKILL_TERMS", "React|Python|TypeScript|SQL|AWS")
        monkeypatch.setenv(
            "JOBHUB_JOB_SEARCH_MEDIA_TERMS", "Video Editor|Media Producer|Content Creator"
        )
    storage.load_configuration()
    token = storage.READ_ONLY.set(False)
    yield
    storage.READ_ONLY.reset(token)
    storage._configuration = {}


@pytest.fixture
def tmp_store(tmp_path: Path):
    from job_search_hub.db import open_store

    return open_store(tmp_path / "store.sqlite")
