"""Optional authenticated local capture endpoint; imports have no side effects."""

import hmac
import json
from datetime import UTC, datetime

from job_search_hub.config import load_settings
from job_search_hub.ingest.base import RawPosting
from job_search_hub.storage import private_write


def create_app(settings=None, *, session_factory=None):
    """Construct the server only when explicitly requested by the user."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
        from pydantic import BaseModel, Field, HttpUrl
    except ImportError as exc:
        raise ValueError(
            "Capture is optional. Install notion-job-search-hub[capture] to enable it."
        ) from exc
    from job_search_hub.workflow import run_lock, session

    settings = settings or load_settings()
    session_factory = session_factory or session
    app = FastAPI(title="Job Search Hub Capture", docs_url=None, redoc_url=None)

    class CapturePayload(BaseModel):
        company: str = Field(default="", max_length=300)
        role_title: str = Field(min_length=1, max_length=300)
        url: HttpUrl
        description: str = Field(default="", max_length=100_000)
        location: str = Field(default="", max_length=1000)
        posted_at: datetime | None = None

    def authenticate(authorization: str | None = Header(default=None)):
        if not settings.capture.auth_token:
            raise HTTPException(503, "Configure JOBHUB_CAPTURE_AUTH_TOKEN first.")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Missing bearer token.")
        supplied = authorization.split(None, 1)[1].strip()
        if not hmac.compare_digest(supplied, settings.capture.auth_token):
            raise HTTPException(403, "Invalid bearer token.")

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "job-search-hub-capture"}

    @app.post("/capture")
    def capture(payload: CapturePayload, _: None = Depends(authenticate)):
        from job_search_hub.db import open_store
        from job_search_hub.pipeline.ingest_stage import IngestStats, _ingest_posting

        with run_lock(settings.data_dir / ".refresh.lock"):
            record = payload.model_dump(mode="json")
            record["captured_at"] = datetime.now(UTC).isoformat()
            capture_log = settings.data_dir / "capture.jsonl"
            previous = capture_log.read_text() if capture_log.exists() else ""
            private_write(capture_log, previous + json.dumps(record) + "\n")
            try:
                with session_factory(preferences=True) as (runtime, notion):
                    posting = RawPosting(
                        source="Manual Capture",
                        source_adapter="manual",
                        external_job_id=str(payload.url),
                        company=payload.company,
                        role_title=payload.role_title,
                        url=str(payload.url),
                        description=payload.description,
                        location=payload.location,
                        posted_at=payload.posted_at,
                    )
                    stats = IngestStats()
                    _ingest_posting(
                        posting,
                        settings=runtime,
                        notion=notion,
                        store=open_store(runtime.sqlite_path),
                        stats=stats,
                        dry_run=False,
                    )
                    return {
                        "saved": True,
                        "processed": True,
                        "new": stats.new_opportunities,
                        "duplicates": stats.duplicates,
                        "excluded": stats.hard_excluded,
                    }
            except Exception as exc:
                return {
                    "saved": True,
                    "processed": False,
                    "error": type(exc).__name__,
                    "next": "Capture is saved locally. Run jobhub doctor, then recapture to retry.",
                }

    return app
