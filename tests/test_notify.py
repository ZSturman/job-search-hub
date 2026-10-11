def test_notifier_dry_run_does_not_send(tmp_path):
    from job_search_hub.config import SMTPSettings
    from job_search_hub.notify import Notifier

    smtp = SMTPSettings(
        host="",
        port=587,
        username="",
        password="",
        sender="",
        recipient="",
        use_tls=True,
    )
    n = Notifier(smtp, tmp_path / "ledger.sqlite", dry_run=True)
    assert n.send("subject", "body") is True


def test_send_once_suppresses_repeats(tmp_path, monkeypatch):
    from job_search_hub.config import SMTPSettings
    from job_search_hub.notify import Notifier, record_notified

    smtp = SMTPSettings(
        host="",
        port=587,
        username="",
        password="",
        sender="me@x",
        recipient="me@x",
        use_tls=True,
    )
    ledger = tmp_path / "ledger.sqlite"
    record_notified(ledger, "error", "k1")

    sent = []
    n = Notifier(smtp, ledger, dry_run=False)
    monkeypatch.setattr(n, "send", lambda subject, body: sent.append(subject) or True)
    # send_once should be suppressed because the kind/key is already in the ledger
    assert n.send_once("error", "k1", "subj", "body") is False
    assert sent == []
