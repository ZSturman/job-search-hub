from __future__ import annotations

from job_search_hub.config import SMTPSettings
from job_search_hub.notify import Notifier, already_notified


class _SMTP:
    instances: list[_SMTP] = []
    fail = False

    def __init__(self, *_args, **_kwargs):
        self.messages = []
        type(self).instances.append(self)

    def starttls(self):
        return None

    def login(self, *_args):
        return None

    def send_message(self, message):
        if type(self).fail:
            raise RuntimeError("authentication password=do-not-log")
        self.messages.append(message)

    def quit(self):
        return None


def _notifier(tmp_path):
    return Notifier(
        SMTPSettings(
            "smtp.example", 587, "user", "secret", "me@example.com", "me@example.com", True
        ),
        tmp_path / "outbox.sqlite",
    )


def test_outbox_queues_then_delivers_once_with_stable_message_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("job_search_hub.notify.smtplib.SMTP", _SMTP)
    _SMTP.instances.clear()
    notifier = _notifier(tmp_path)
    assert notifier.send_once("digest", "stable", "Subject", "Body") is True
    assert len(notifier.preview()) == 1
    stats = notifier.flush_due()
    assert stats.sent == 1
    assert len(_SMTP.instances) == 1
    assert _SMTP.instances[0].messages[0]["Message-ID"] == "<jobhub-stable@local>"
    assert _SMTP.instances[0].messages[0]["To"] == "me@example.com"
    assert already_notified(notifier.ledger_path, "digest", "stable") is True
    assert notifier.send_once("digest", "stable", "Subject", "Body") is False


def test_failed_outbox_delivery_remains_retryable_and_redacted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("job_search_hub.notify.smtplib.SMTP", _SMTP)
    _SMTP.instances.clear()
    _SMTP.fail = True
    notifier = _notifier(tmp_path)
    notifier.send_once("digest", "retry", "Subject", "Body")
    stats = notifier.flush_due()
    assert stats.retryable == 1
    queued = notifier.preview()[0]
    assert queued.status == "retryable"
    assert "secret" not in queued.last_error
    _SMTP.fail = False
    assert notifier.flush_due(force=True).sent == 1
