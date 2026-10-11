"""Optional manual-only Upwork project discovery.

The adapter never posts a proposal, sends a message, or changes an Upwork
resource.  It only reads an explicitly configured, approved projects endpoint
with a local OAuth token held in macOS Keychain.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from job_search_hub.config import SearchTermSettings, UpworkSettings
from job_search_hub.ingest.base import Adapter, RawPosting
from job_search_hub.logging_setup import get_logger
from job_search_hub.upwork_auth import load_upwork_token

log = get_logger(__name__)


class UpworkAdapter(Adapter):
    key = "upwork"
    label = "Upwork (manual only)"

    def __init__(self, settings: UpworkSettings, terms: SearchTermSettings) -> None:
        super().__init__()
        self.settings = settings
        self.terms = terms
        self.skip_reason = ""

    def _configuration_reason(self) -> str:
        if not self.settings.enabled:
            return "Upwork source is disabled; enable it in Notion"
        if not self.settings.projects_api_url:
            return "JOBHUB_UPWORK_PROJECTS_API_URL is not configured"
        if not load_upwork_token():
            return (
                "Upwork OAuth token is absent; run jobhub sources authorize upwork "
                "after API approval"
            )
        return ""

    def fetch(self) -> Iterable[RawPosting]:
        self.skip_reason = self._configuration_reason()
        if self.skip_reason:
            self.last_diagnostic = {"status": "unconfigured", "error": self.skip_reason}
            log.info("upwork: skipped: %s", self.skip_reason)
            return
        token = load_upwork_token()
        assert token is not None  # checked above; avoids accidentally logging it
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        # The exact searchable-project endpoint is assigned to each approved
        # app.  A generic query parameter keeps this adapter harmless until
        # that endpoint is deliberately configured.
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            for query in self.terms.role_terms:
                try:
                    response = client.get(
                        self.settings.projects_api_url,
                        params={"q": query, "limit": 10},
                        headers=headers,
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    self.record_failure(exc)
                    log.warning("upwork: query=%r failed: %s", query, type(exc).__name__)
                    continue
                payload = response.json() or {}
                rows = payload.get("projects") or payload.get("results") or []
                for row in rows:
                    if isinstance(row, dict):
                        yield self._to_posting(row)

    @staticmethod
    def _to_posting(row: dict) -> RawPosting:
        url = str(row.get("url") or row.get("permalink") or "").strip()
        identifier = str(row.get("id") or url).strip()
        client = row.get("client") if isinstance(row.get("client"), dict) else {}
        return RawPosting(
            source="Upwork",
            source_adapter="upwork",
            external_job_id=identifier,
            company=str(client.get("name") or row.get("client_name") or "Upwork client").strip(),
            role_title=str(row.get("title") or "Freelance project").strip(),
            url=url,
            description=str(row.get("description") or row.get("snippet") or ""),
            location=str(row.get("client_location") or row.get("location") or "Remote").strip(),
            remote_status="Remote",
            employment_type=["Freelance", "Contract"],
            compensation_text=str(row.get("budget") or row.get("budget_text") or "").strip(),
            discovered_at=datetime.now(UTC),
            extras={"manual_only": True, "opportunity_type": "Freelance"},
        )
