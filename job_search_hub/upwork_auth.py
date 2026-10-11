"""Local OAuth token handling for the optional Upwork discovery adapter.

Tokens are intentionally held in the operating system credential store rather than a Notion
property, SQLite, an environment file, or the repository.  The approved
Upwork application's authorization/token endpoints are supplied at runtime
because they belong to the account/application configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from job_search_hub.config import UpworkSettings

KEYCHAIN_SERVICE = "com.jobhub.upwork"
KEYCHAIN_ACCOUNT = "oauth-token"


class UpworkAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class OAuthStart:
    authorization_url: str
    state: str
    code_verifier: str


def load_upwork_token() -> dict | None:
    from job_search_hub.storage import get_secret

    try:
        payload = json.loads(get_secret("UPWORK_TOKEN") or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) and payload.get("access_token") else None


def save_upwork_token(token: dict) -> None:
    from job_search_hub.storage import save_secret

    save_secret("UPWORK_TOKEN", json.dumps(token))


def begin_oauth(settings: UpworkSettings) -> OAuthStart:
    if not settings.client_id:
        raise UpworkAuthError("JOBHUB_UPWORK_CLIENT_ID is not configured")
    if not settings.authorization_url or not settings.token_url:
        raise UpworkAuthError(
            "Set JOBHUB_UPWORK_OAUTH_AUTHORIZATION_URL and JOBHUB_UPWORK_OAUTH_TOKEN_URL "
            "from the approved Upwork app configuration before authorizing."
        )
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in settings.authorization_url else "?"
    return OAuthStart(f"{settings.authorization_url}{separator}{query}", state, verifier)


def complete_oauth(
    settings: UpworkSettings,
    start: OAuthStart,
    *,
    timeout_seconds: int = 300,
) -> dict:
    """Wait for the local redirect, exchange its code, and save the token.

    This intentionally has no browser automation.  The operator opens the
    printed authorization URL while the local callback listener is active.
    """
    redirect = urlparse(settings.redirect_uri)
    if redirect.scheme != "http" or redirect.hostname not in {"127.0.0.1", "localhost"}:
        raise UpworkAuthError("JOBHUB_UPWORK_REDIRECT_URI must be a local http callback URL")
    callback: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required stdlib handler name
            values = parse_qs(urlparse(self.path).query)
            callback["code"] = (values.get("code") or [""])[0]
            callback["state"] = (values.get("state") or [""])[0]
            callback["error"] = (values.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Job Search Hub received the authorization response. You can close this tab."
            )

        def log_message(self, _format, *_args):  # noqa: D401 - silence token-adjacent logs
            return

    server = HTTPServer((redirect.hostname, redirect.port or 80), CallbackHandler)
    server.timeout = max(1, timeout_seconds)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if callback.get("error"):
        raise UpworkAuthError(f"Upwork authorization declined: {callback['error']}")
    if not callback.get("code") or callback.get("state") != start.state:
        raise UpworkAuthError("OAuth callback was missing a valid code/state pair")
    response = httpx.post(
        settings.token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": settings.client_id,
            "code": callback["code"],
            "redirect_uri": settings.redirect_uri,
            "code_verifier": start.code_verifier,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    token = response.json()
    if not isinstance(token, dict) or not token.get("access_token"):
        raise UpworkAuthError("Upwork token response did not contain an access_token")
    save_upwork_token(token)
    return token
