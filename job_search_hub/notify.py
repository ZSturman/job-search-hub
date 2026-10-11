"""Durable, deduplicated SMTP notification outbox.

Pipeline stages only enqueue messages.  A command flushes due messages at its
boundary, reusing one SMTP connection for a batch.  The original public
``Notifier`` API remains available so existing scoring/lifecycle code remains
compatible while no longer sending SMTP from inside a stage.
"""

from __future__ import annotations

import hashlib
import smtplib
import sqlite3
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from time import sleep

from job_search_hub.config import SMTPSettings
from job_search_hub.logging_setup import get_logger
from job_search_hub.storage import READ_ONLY

log = get_logger(__name__)


_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS notification_ledger (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS notification_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    recipient TEXT NOT NULL,
    message_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(kind, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_notification_messages_due
ON notification_messages (status, next_attempt_at, lease_until, created_at);
"""


@contextmanager
def _ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_LEDGER_DDL)
        yield conn
        conn.commit()
    finally:
        conn.close()


def stable_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def already_notified(ledger_path: Path, kind: str, key: str) -> bool:
    with _ledger(ledger_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM notification_messages
               WHERE kind=? AND dedupe_key=? AND status='sent'
               UNION ALL
               SELECT 1 FROM notification_ledger WHERE kind=? AND key=?
               LIMIT 1""",
            (kind, key, kind, key),
        ).fetchone()
    return row is not None


def record_notified(ledger_path: Path, kind: str, key: str) -> None:
    """Compatibility helper used by existing callers and tests."""
    with _ledger(ledger_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO notification_ledger (kind, key, sent_at) VALUES (?, ?, ?)",
            (kind, key, datetime.now(UTC).isoformat()),
        )


def clear_notification(ledger_path: Path, kind: str, key: str) -> None:
    with _ledger(ledger_path) as conn:
        conn.execute("DELETE FROM notification_ledger WHERE kind=? AND key=?", (kind, key))
        conn.execute("DELETE FROM notification_messages WHERE kind=? AND dedupe_key=?", (kind, key))


@dataclass(frozen=True)
class QueuedMessage:
    id: int
    kind: str
    key: str
    subject: str
    body: str
    recipient: str
    status: str
    attempts: int
    next_attempt_at: str
    message_id: str
    last_error: str


@dataclass
class DeliveryStats:
    attempted: int = 0
    sent: int = 0
    retryable: int = 0
    skipped: int = 0
    circuit_open: bool = False
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _message_id(key: str) -> str:
    return f"<jobhub-{key}@local>"


def _redact_error(exc: Exception | str) -> str:
    """Keep credential-like details and recipient addresses out of logs/DB summaries."""
    text = str(exc).replace("\n", " ")
    # SMTP exception strings occasionally echo AUTH commands or usernames.
    for token in ("password", "authorization", "auth", "username", "login"):
        if token in text.lower():
            return "SMTP authentication or connection failure"
    return text[:300]


class Notifier:
    """Queue notification messages and flush them safely at command boundaries."""

    def __init__(self, smtp: SMTPSettings, ledger_path: Path, dry_run: bool = False) -> None:
        self.smtp = smtp
        self.ledger_path = ledger_path
        self.dry_run = dry_run or READ_ONLY.get()

    def _build_message(self, message: QueuedMessage) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = self.smtp.sender
        # Never accept an employer-provided recipient: this service is solely
        # for the configured personal notification inbox.
        msg["To"] = self.smtp.recipient
        msg["Message-ID"] = message.message_id
        msg.set_content(message.body)
        return msg

    def _enqueue(self, kind: str, key: str, subject: str, body: str) -> bool:
        if self.dry_run:
            log.info("[dry-run] would queue email kind=%s subject=%r", kind, subject)
            return True
        if not self.smtp.recipient:
            log.warning("SMTP recipient not configured; notification remains unsent kind=%s", kind)
            return False
        # Older runs wrote directly to the ledger.  Respect that ledger during
        # the transition so an upgrade cannot re-notify an already delivered
        # lifecycle or application message.
        if already_notified(self.ledger_path, kind, key):
            log.debug("notification suppressed by delivered ledger kind=%s key=%s", kind, key)
            return False
        now = datetime.now(UTC).isoformat()
        with _ledger(self.ledger_path) as conn:
            existing = conn.execute(
                "SELECT status FROM notification_messages WHERE kind=? AND dedupe_key=?",
                (kind, key),
            ).fetchone()
            if existing is not None:
                log.debug(
                    "notification suppressed kind=%s key=%s status=%s",
                    kind,
                    key,
                    existing["status"],
                )
                return False
            conn.execute(
                """INSERT INTO notification_messages
                   (kind, dedupe_key, subject, body, recipient, message_id, status,
                    attempts, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
                (kind, key, subject, body, self.smtp.recipient, _message_id(key), now, now),
            )
        log.info("queued notification kind=%s subject=%r", kind, subject)
        return True

    def send(self, subject: str, body: str) -> bool:
        """Queue a one-off message with deterministic content-based dedupe."""
        return self._enqueue("message", stable_key("message", subject, body), subject, body)

    def send_once(self, kind: str, key: str, subject: str, body: str) -> bool:
        return self._enqueue(kind, key, subject, body)

    def preview(self, *, limit: int = 100) -> list[QueuedMessage]:
        with _ledger(self.ledger_path) as conn:
            rows = conn.execute(
                """SELECT * FROM notification_messages
                   WHERE status != 'sent' ORDER BY created_at ASC LIMIT ?""",
                (max(0, limit),),
            ).fetchall()
        return [
            QueuedMessage(
                id=int(row["id"]),
                kind=row["kind"],
                key=row["dedupe_key"],
                subject=row["subject"],
                body=row["body"],
                recipient=row["recipient"],
                status=row["status"],
                attempts=int(row["attempts"]),
                next_attempt_at=row["next_attempt_at"] or "",
                message_id=row["message_id"],
                last_error=row["last_error"] or "",
            )
            for row in rows
        ]

    def _due_messages(self, *, force: bool, limit: int) -> list[QueuedMessage]:
        now = datetime.now(UTC)
        with _ledger(self.ledger_path) as conn:
            # Expired sends are ambiguous delivery attempts.  They are retried
            # with the same Message-ID, favoring delivery over a silent loss.
            conn.execute(
                """UPDATE notification_messages
                   SET status='retryable', lease_until=NULL, updated_at=?
                   WHERE status='sending' AND lease_until IS NOT NULL AND lease_until < ?""",
                (now.isoformat(), now.isoformat()),
            )
            clause = "status IN ('pending', 'retryable')"
            params: list[object] = []
            if not force:
                clause += " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                params.append(now.isoformat())
            params.append(max(0, limit))
            rows = conn.execute(
                f"""SELECT * FROM notification_messages WHERE {clause}
                    ORDER BY created_at ASC LIMIT ?""",
                params,
            ).fetchall()
            lease_until = (now + timedelta(minutes=10)).isoformat()
            for row in rows:
                conn.execute(
                    """UPDATE notification_messages SET status='sending', lease_until=?,
                       attempts=attempts+1, updated_at=? WHERE id=?""",
                    (lease_until, now.isoformat(), row["id"]),
                )
        return [
            QueuedMessage(
                id=int(row["id"]),
                kind=row["kind"],
                key=row["dedupe_key"],
                subject=row["subject"],
                body=row["body"],
                recipient=row["recipient"],
                status="sending",
                attempts=int(row["attempts"]) + 1,
                next_attempt_at=row["next_attempt_at"] or "",
                message_id=row["message_id"],
                last_error=row["last_error"] or "",
            )
            for row in rows
        ]

    def _mark_sent(self, message: QueuedMessage) -> None:
        now = datetime.now(UTC).isoformat()
        with _ledger(self.ledger_path) as conn:
            conn.execute(
                """UPDATE notification_messages SET status='sent', sent_at=?, lease_until=NULL,
                   next_attempt_at=NULL, last_error='', updated_at=? WHERE id=?""",
                (now, now, message.id),
            )
            conn.execute(
                """INSERT OR REPLACE INTO notification_ledger (kind, key, sent_at)
                   VALUES (?, ?, ?)""",
                (message.kind, message.key, now),
            )

    def _mark_retryable(self, message: QueuedMessage, error: Exception | str) -> None:
        delay = min(3600, 30 * (2 ** min(message.attempts - 1, 7)))
        now = datetime.now(UTC)
        with _ledger(self.ledger_path) as conn:
            conn.execute(
                """UPDATE notification_messages SET status='retryable', lease_until=NULL,
                   next_attempt_at=?, last_error=?, updated_at=? WHERE id=?""",
                (
                    (now + timedelta(seconds=delay)).isoformat(),
                    _redact_error(error),
                    now.isoformat(),
                    message.id,
                ),
            )

    def flush_due(self, *, force: bool = False, limit: int = 100) -> DeliveryStats:
        stats = DeliveryStats()
        if self.dry_run:
            stats.skipped = len(self.preview(limit=limit))
            log.info("[dry-run] %d queued email(s) would be delivered", stats.skipped)
            return stats
        if not self.smtp.host or not self.smtp.sender or not self.smtp.recipient:
            stats.skipped = len(self.preview(limit=limit))
            log.warning("SMTP not configured; %d queued email(s) remain pending", stats.skipped)
            return stats
        messages = self._due_messages(force=force, limit=limit)
        if not messages:
            return stats

        failures = 0
        server = None
        try:
            server = smtplib.SMTP(self.smtp.host, self.smtp.port, timeout=30)
            if self.smtp.use_tls:
                server.starttls()
            if self.smtp.username:
                server.login(self.smtp.username, self.smtp.password)
            for message in messages:
                if failures >= 3:
                    self._mark_retryable(message, "SMTP circuit breaker open")
                    stats.retryable += 1
                    stats.circuit_open = True
                    continue
                stats.attempted += 1
                try:
                    server.send_message(self._build_message(message))
                    self._mark_sent(message)
                    stats.sent += 1
                    # Gentle batch spacing prevents provider throttling without
                    # imposing a discard limit on valid notifications.
                    if stats.sent < len(messages):
                        sleep(0.2)
                except Exception as exc:
                    failures += 1
                    self._mark_retryable(message, exc)
                    stats.retryable += 1
                    stats.errors.append(_redact_error(exc))
                    with suppress(Exception):
                        server.quit()
                    server = None
                    if failures < 3:
                        try:
                            server = smtplib.SMTP(self.smtp.host, self.smtp.port, timeout=30)
                            if self.smtp.use_tls:
                                server.starttls()
                            if self.smtp.username:
                                server.login(self.smtp.username, self.smtp.password)
                        except Exception as reconnect_exc:
                            failures += 1
                            stats.errors.append(_redact_error(reconnect_exc))
        except Exception as exc:
            summary = _redact_error(exc)
            for message in messages:
                self._mark_retryable(message, summary)
            stats.retryable = len(messages)
            stats.errors.append(summary)
            stats.circuit_open = True
        finally:
            if server is not None:
                with suppress(Exception):
                    server.quit()
        if stats.errors:
            log.warning(
                "email outbox summary attempted=%d sent=%d retryable=%d circuit_open=%s error=%s",
                stats.attempted,
                stats.sent,
                stats.retryable,
                stats.circuit_open,
                stats.errors[0],
            )
        return stats
