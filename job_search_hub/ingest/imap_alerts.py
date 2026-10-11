"""IMAP adapter: extract job postings from job-alert emails.

Reads UNREAD messages from the configured mailbox whose ``From`` address
matches one of ``IMAP_ALERT_SENDERS``. For each message, tries to extract
job links + titles using a small set of heuristics (LinkedIn / Indeed /
Glassdoor / Hacker News "Who's hiring" / generic anchor text).

Submission is never supported.

Authentication: stdlib ``imaplib`` with ``IMAP_HOST/PORT/USERNAME/PASSWORD``.
For Gmail use an app password — never your real password.

The adapter opens the mailbox read-only and uses ``BODY.PEEK[]`` so dry runs
and normal discovery do not mark messages Seen or otherwise mutate mailbox
state.
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from email.message import Message
from urllib.parse import urlparse

from job_search_hub.config import AdapterCredentials, IMAPSettings
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.ingest.preview import error_result, result_from_postings, skipped_result
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)

_HREF_RE = re.compile(r'<a [^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _company_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2]
    return host


class IMAPAdapter(Adapter):
    key = "imap"
    label = "Email Alerts"

    def __init__(
        self, creds: AdapterCredentials, imap: IMAPSettings, *, timeout: float = 30.0
    ) -> None:
        super().__init__()
        self.creds = creds
        self.imap = imap
        self.timeout = timeout

    def _exact_senders(self) -> list[str]:
        return [
            sender
            for sender in self.imap.alert_senders
            if "@" in sender and "*" not in sender and not sender.startswith("@")
        ]

    def plan_searches(self) -> Iterable[SearchRequest]:
        for sender in self._exact_senders():
            yield SearchRequest(
                source=self.label,
                source_adapter=self.key,
                endpoint=f"imaps://{self.imap.host}/{self.imap.mailbox}",
                target=sender,
                target_kind="sender",
                params={"criteria": "UNSEEN FROM exact sender", "readonly": True},
            )

    def _skip_reason(self) -> str:
        if not (self.imap.host and self.imap.username and self.imap.password):
            return "IMAP credentials not configured; skipping"
        if not self.imap.alert_senders:
            return "no exact alert senders configured (IMAP_ALERT_SENDERS empty)"
        if not self._exact_senders():
            return "IMAP_ALERT_SENDERS must contain exact sender email addresses"
        return ""

    def preview(self, *, examples: int = 3) -> Iterable[SearchResult]:
        reason = self._skip_reason()
        if reason:
            return [skipped_result(source=self.label, source_adapter=self.key, reason=reason)]
        requests = list(self.plan_searches())
        results: list[SearchResult] = []
        try:
            conn = imaplib.IMAP4_SSL(self.imap.host, self.imap.port, timeout=self.timeout)
        except Exception as exc:
            return [
                error_result(
                    SearchRequest(source=self.label, source_adapter=self.key, endpoint="imaps"),
                    error=f"connect failed: {exc}",
                )
            ]
        try:
            conn.login(self.imap.username, self.imap.password)
            conn.select(self.imap.mailbox, readonly=True)
            for request in requests:
                started = time.perf_counter()
                try:
                    postings = list(self._fetch_from(conn, request.target))
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    results.append(
                        result_from_postings(
                            request,
                            http_status=None,
                            elapsed_ms=elapsed_ms,
                            postings=postings,
                            examples=examples,
                        )
                    )
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    results.append(error_result(request, error=str(exc), elapsed_ms=elapsed_ms))
        except Exception as exc:
            return [
                error_result(
                    SearchRequest(source=self.label, source_adapter=self.key, endpoint="imaps"),
                    error=f"error during read-only preview: {exc}",
                )
            ]
        finally:
            with contextlib.suppress(Exception):
                conn.logout()
        return results

    def fetch(self) -> Iterable[RawPosting]:
        reason = self._skip_reason()
        if reason:
            self.last_diagnostic = {"status": "unconfigured", "error": reason}
            log.info("imap: %s", reason)
            return

        try:
            conn = imaplib.IMAP4_SSL(self.imap.host, self.imap.port, timeout=self.timeout)
        except Exception as exc:
            self.record_failure(exc)
            log.warning("imap: connect failed: %s", type(exc).__name__)
            return
        try:
            conn.login(self.imap.username, self.imap.password)
            conn.select(self.imap.mailbox, readonly=True)
            for sender in self._exact_senders():
                yield from self._fetch_from(conn, sender)
        except Exception as exc:
            self.record_failure(exc)
            log.warning("imap: error during fetch: %s", type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    def _fetch_from(self, conn, sender: str) -> Iterable[RawPosting]:
        typ, data = conn.search(None, "UNSEEN", "FROM", f'"{sender}"')
        if typ != "OK":
            log.warning("imap: search failed for sender=%s", sender)
            return
        msg_ids = (data[0] or b"").split()
        log.info("imap: sender=%s unseen=%d", sender, len(msg_ids))
        for mid in msg_ids:
            typ, payload = conn.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not payload:
                continue
            raw = payload[0][1] if isinstance(payload[0], tuple) else b""
            msg = email.message_from_bytes(raw)
            yield from self._extract_postings(sender, msg)

    @staticmethod
    def _html_body(msg: Message) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        return part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    except Exception:
                        continue
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        )
                    except Exception:
                        continue
            return ""
        try:
            return (msg.get_payload(decode=True) or b"").decode(
                msg.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            return ""

    @staticmethod
    def _extract_postings(sender: str, msg: Message) -> Iterable[RawPosting]:
        body = IMAPAdapter._html_body(msg)
        subject = (msg.get("Subject") or "").strip()
        date_hdr = msg.get("Date") or ""
        try:
            posted_at = email.utils.parsedate_to_datetime(date_hdr)
        except (TypeError, ValueError):
            posted_at = datetime.now(UTC)

        seen_urls: set[str] = set()
        for href, anchor in _HREF_RE.findall(body):
            url = href.strip()
            if not url.startswith("http"):
                continue
            # Filter out unsubscribe / mail tracking pixels / legal links
            low = url.lower()
            if any(x in low for x in ("unsubscribe", "tracking", "/optout", "preferences")):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            title = _strip(anchor)
            if not title or len(title) < 4:
                continue
            company = _company_from_url(url)
            yield RawPosting(
                source="Email",
                source_adapter="imap",
                external_job_id=url,
                company=company,
                role_title=title,
                url=url,
                description=subject,
                posted_at=posted_at,
                discovered_at=datetime.now(UTC),
                extras={"sender": sender, "subject": subject},
            )
