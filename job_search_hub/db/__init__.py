"""Local SQLite store: cache, audit ledger, idempotency, embeddings."""

from job_search_hub.db.store import Store, open_store

__all__ = ["Store", "open_store"]
