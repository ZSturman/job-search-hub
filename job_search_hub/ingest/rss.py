"""RSS adapter.

Reads a list of RSS/Atom feed URLs (one per line, ``#`` comments allowed)
from the file pointed to by ``RSS_FEEDS_FILE`` (default
``data/rss_feeds.txt``). For each entry, builds a ``RawPosting`` using:

  - ``title``   -> role_title
  - ``link``    -> url + external_job_id (URL is stable enough)
  - ``author``  -> company (falls back to feed title)
  - ``summary`` -> description (HTML stripped)
  - ``published`` / ``updated`` -> posted_at

This adapter does NOT support submission. It is purely a discovery feed.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from time import mktime

from job_search_hub.config import AdapterCredentials
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import (
    error_result,
    result_from_postings,
    skipped_result,
    timed_get,
)
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return html.unescape(_TAG_RE.sub(" ", s)).strip()


def _read_feed_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


class RSSAdapter(Adapter):
    key = "rss"
    label = "RSS"

    def __init__(self, creds: AdapterCredentials) -> None:
        super().__init__()
        self.feeds_file = creds.rss_feeds_file

    def plan_searches(self) -> Iterable[SearchRequest]:
        for feed_url in _read_feed_list(self.feeds_file):
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=feed_url,
                target=feed_url,
                target_kind="feed",
            )

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        try:
            import feedparser
        except ImportError:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason="feedparser not installed",
                )
            ]
        requests = list(self.plan_searches())
        if not requests:
            return [
                skipped_result(
                    source=self.label,
                    source_adapter=self.key,
                    reason=f"no feeds configured ({self.feeds_file} missing or empty)",
                )
            ]
        import httpx

        results: list[SearchResult] = []
        headers = {"User-Agent": "jobhub/0.1 (+rss-adapter)"}
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
            for request in requests:
                try:
                    resp, elapsed_ms = timed_get(client, request)
                    if resp.is_error:
                        results.append(
                            error_result(
                                request,
                                error=resp.text[:500],
                                http_status=resp.status_code,
                                elapsed_ms=elapsed_ms,
                            )
                        )
                        continue
                    parsed = feedparser.parse(resp.content)
                    feed_title = ((parsed.feed or {}).get("title") or request.target).strip()
                    entries = parsed.entries or []
                    postings = [
                        self._to_posting(request.target, feed_title, entry) for entry in entries
                    ]
                    results.append(
                        result_from_postings(
                            request,
                            http_status=resp.status_code,
                            elapsed_ms=elapsed_ms,
                            postings=postings,
                            examples=examples,
                        )
                    )
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    results.append(error_result(request, error=str(exc), http_status=status))
        return results

    def fetch(self) -> Iterable[RawPosting]:
        try:
            import feedparser  # local import; optional dep
        except ImportError:
            log.warning("rss: feedparser not installed; skipping")
            return

        feeds = _read_feed_list(self.feeds_file)
        if not feeds:
            log.info("rss: no feeds configured (%s missing or empty)", self.feeds_file)
            return

        # Fetch with httpx so we get certifi-backed TLS verification, then
        # hand the bytes to feedparser. feedparser.parse() with a URL uses
        # stdlib urllib which often fails SSL on macOS Python builds.
        import httpx

        headers = {"User-Agent": "jobhub/0.1 (+rss-adapter)"}
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as http:
            for feed_url in feeds:
                try:
                    resp = http.get(feed_url)
                    resp.raise_for_status()
                    parsed = feedparser.parse(resp.content)
                except Exception as exc:
                    log.warning("rss: fetch/parse failed for %s: %s", feed_url, type(exc).__name__)
                    continue
                feed_title = ((parsed.feed or {}).get("title") or feed_url).strip()
                entries = parsed.entries or []
                log.debug("rss: %s -> %d entries", feed_title, len(entries))
                for entry in entries:
                    yield self._to_posting(feed_url, feed_title, entry)

    @staticmethod
    def _to_posting(feed_url: str, feed_title: str, entry) -> RawPosting:
        link = (getattr(entry, "link", "") or "").strip()
        title = (getattr(entry, "title", "") or "").strip()
        author = (getattr(entry, "author", "") or "").strip()
        summary = _strip_html(getattr(entry, "summary", "") or "")
        posted_at: datetime | None = None
        for attr in ("published_parsed", "updated_parsed"):
            t = getattr(entry, attr, None)
            if t:
                try:
                    posted_at = datetime.fromtimestamp(mktime(t), tz=UTC)
                except (TypeError, ValueError):
                    posted_at = None
                break
        return RawPosting(
            source="RSS",
            source_adapter="rss",
            external_job_id=link or title,
            company=author or feed_title,
            role_title=title,
            url=link,
            description=summary,
            posted_at=posted_at,
            discovered_at=datetime.now(UTC),
            extras={"feed": feed_url, "feed_title": feed_title},
        )
