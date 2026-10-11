"""Adapter registry: maps adapter keys to factories.

Every factory takes a ``Settings`` instance so the caller does not have
to know which credentials each adapter needs.
"""

from __future__ import annotations

from collections.abc import Callable

from job_search_hub.config import Settings
from job_search_hub.direct_adapter import DirectDiscoveryAdapter
from job_search_hub.ingest.adzuna import AdzunaAdapter
from job_search_hub.ingest.arbeitnow import ArbeitnowAdapter
from job_search_hub.ingest.ashby import AshbyAdapter
from job_search_hub.ingest.base import Adapter
from job_search_hub.ingest.browser_boards import BOARDS, BrowserBoardAdapter, WorkWithLinearAdapter
from job_search_hub.ingest.greenhouse import GreenhouseAdapter
from job_search_hub.ingest.imap_alerts import IMAPAdapter
from job_search_hub.ingest.jooble import JoobleAdapter
from job_search_hub.ingest.lever import LeverAdapter
from job_search_hub.ingest.organization_sources import (
    OrganizationSourceTarget,
    settings_with_organization_targets,
)
from job_search_hub.ingest.recruitee import RecruiteeAdapter
from job_search_hub.ingest.remotive import RemotiveAdapter
from job_search_hub.ingest.rss import RSSAdapter
from job_search_hub.ingest.smartrecruiters import SmartRecruitersAdapter
from job_search_hub.ingest.upwork import UpworkAdapter
from job_search_hub.ingest.usajobs import USAJobsAdapter
from job_search_hub.ingest.watchlist import WatchlistAdapter

AdapterFactory = Callable[[Settings], Adapter]


REGISTRY: dict[str, AdapterFactory] = {
    "direct_discovery": lambda s: DirectDiscoveryAdapter(s),
    "adzuna": lambda s: AdzunaAdapter(
        s.adapters,
        s.search_terms,
        max_per_query=s.discovery.max_per_query,
        requests_per_minute=s.adapters.adzuna_requests_per_minute,
    ),
    "arbeitnow": lambda s: ArbeitnowAdapter(),
    "ashby": lambda s: AshbyAdapter(s.adapters),
    "greenhouse": lambda s: GreenhouseAdapter(s.adapters),
    "jooble": lambda s: JoobleAdapter(
        s.adapters, s.search_terms, max_per_query=s.discovery.max_per_query
    ),
    "lever": lambda s: LeverAdapter(s.adapters),
    "recruitee": lambda s: RecruiteeAdapter(s.adapters),
    "remotive": lambda s: RemotiveAdapter(s.search_terms, max_per_query=s.discovery.max_per_query),
    "rss": lambda s: RSSAdapter(s.adapters),
    "smartrecruiters": lambda s: SmartRecruitersAdapter(
        s.adapters, s.search_terms, max_per_query=s.discovery.max_per_query
    ),
    "usajobs": lambda s: USAJobsAdapter(s.adapters, max_per_query=s.discovery.max_per_query),
    "upwork": lambda s: UpworkAdapter(s.upwork, s.search_terms),
    "imap": lambda s: IMAPAdapter(s.adapters, s.imap),
    "watchlist": lambda s: WatchlistAdapter(s.adapters),
    "work_with_linear": lambda s: WorkWithLinearAdapter(s),
}

for _board in BOARDS:
    REGISTRY[_board.key] = lambda s, board=_board: BrowserBoardAdapter(s, board)


def build_adapters(
    settings: Settings,
    only: list[str] | None = None,
    *,
    organization_targets: list[OrganizationSourceTarget] | None = None,
) -> list[Adapter]:
    if organization_targets:
        settings = settings_with_organization_targets(settings, organization_targets)
    keys = only or list(REGISTRY.keys())
    adapters = [REGISTRY[k](settings) for k in keys if k in REGISTRY]
    for adapter in adapters:
        if adapter.key == "direct_discovery":
            adapter.organization_targets = [
                target
                for target in (organization_targets or [])
                if target.adapter_key == "direct_discovery"
            ]
    return adapters
