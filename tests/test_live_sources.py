"""Opt-in smoke checks for public source contracts, never part of normal pytest."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


def _enabled() -> bool:
    return os.getenv("RUN_LIVE_SOURCE_TESTS", "").strip() == "1"


@pytest.mark.skipif(
    not _enabled(), reason="set RUN_LIVE_SOURCE_TESTS=1 for bounded live source checks"
)
def test_greenhouse_public_board_returns_specific_jobs() -> None:
    from job_search_hub.config import load_settings
    from job_search_hub.ingest.greenhouse import GreenhouseAdapter

    adapter = GreenhouseAdapter(load_settings().adapters)
    adapter.boards = ["ginkgobioworks"]
    jobs = list(adapter.fetch())
    assert jobs and all("/jobs/" in job.url for job in jobs[:3])


@pytest.mark.skipif(
    not _enabled(), reason="set RUN_LIVE_SOURCE_TESTS=1 for bounded live source checks"
)
def test_ashby_public_board_returns_specific_jobs() -> None:
    from job_search_hub.config import load_settings
    from job_search_hub.ingest.ashby import AshbyAdapter

    adapter = AshbyAdapter(load_settings().adapters)
    adapter.boards = ["openai"]
    jobs = list(adapter.fetch())
    assert jobs and all(job.url.startswith("https://jobs.ashbyhq.com/") for job in jobs[:3])
