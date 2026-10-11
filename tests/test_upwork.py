from __future__ import annotations

from dataclasses import replace

from job_search_hub.config import load_settings
from job_search_hub.ingest.upwork import UpworkAdapter
from job_search_hub.upwork_auth import UpworkAuthError, begin_oauth


def test_upwork_adapter_skips_cleanly_without_oauth(monkeypatch, tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    configured = replace(
        settings.upwork,
        enabled=True,
        projects_api_url="https://approved.example.test/projects",
    )
    monkeypatch.setattr("job_search_hub.ingest.upwork.load_upwork_token", lambda: None)

    adapter = UpworkAdapter(configured, settings.search_terms)

    assert list(adapter.fetch()) == []
    assert "OAuth token is absent" in adapter.skip_reason


def test_upwork_oauth_requires_approved_app_endpoints(tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    configured = replace(settings.upwork, client_id="approved-client")

    try:
        begin_oauth(configured)
    except UpworkAuthError as exc:
        assert "JOBHUB_UPWORK_OAUTH_AUTHORIZATION_URL" in str(exc)
    else:  # pragma: no cover - auth must never silently start without an app configuration
        raise AssertionError("missing Upwork endpoints were accepted")


def test_upwork_oauth_start_uses_local_pkce_callback(tmp_path) -> None:
    settings = load_settings(env_file=tmp_path / "missing.env")
    configured = replace(
        settings.upwork,
        client_id="approved-client",
        authorization_url="https://approved.example.test/authorize",
        token_url="https://approved.example.test/token",
    )

    start = begin_oauth(configured)

    assert "code_challenge=" in start.authorization_url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8788%2Fcallback" in start.authorization_url
    assert start.state
    assert start.code_verifier
