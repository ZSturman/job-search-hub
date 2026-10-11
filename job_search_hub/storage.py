"""Independent user storage, configuration, and credential access."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import platformdirs
import tomli_w

READ_ONLY: ContextVar[bool] = ContextVar("jobhub_read_only", default=False)
_configuration: dict = {}
SECRET_KEYS = frozenset(
    {
        "NOTION_TOKEN",
        "LMSTUDIO_API_KEY",
        "SMTP_PASSWORD",
        "IMAP_PASSWORD",
        "USAJOBS_AUTH_KEY",
        "ADZUNA_APP_KEY",
        "JOOBLE_API_KEY",
        "CAPTURE_AUTH_TOKEN",
        "UPWORK_TOKEN",
    }
)


@dataclass(frozen=True)
class AppPaths:
    config: Path
    data: Path

    @property
    def config_file(self) -> Path:
        return self.config / "config.toml"


def app_paths() -> AppPaths:
    override = os.environ.get("JOBHUB_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        return AppPaths(root / "config", root / "data")
    return AppPaths(
        Path(platformdirs.user_config_dir("job-search-hub")),
        Path(platformdirs.user_data_dir("job-search-hub")),
    )


def read_config() -> dict:
    path = app_paths().config_file
    return tomllib.loads(path.read_text()) if path.exists() else {"settings": {}}


def load_configuration(*, env_file: Path | None = None) -> None:
    global _configuration
    _configuration = dict(read_config().get("settings", {}))
    if env_file is not None and env_file.exists():
        from dotenv import dotenv_values

        _configuration.update(
            {
                k.removeprefix("JOBHUB_"): v
                for k, v in dotenv_values(env_file).items()
                if k.startswith("JOBHUB_") and k.removeprefix("JOBHUB_") not in SECRET_KEYS
            }
        )


def get_secret(name: str) -> str:
    name = name.removeprefix("JOBHUB_")
    if value := os.environ.get(f"JOBHUB_{name}"):
        return value
    try:
        return _credential_backend().get_password("job-search-hub", name) or ""
    except Exception:
        return ""


def _credential_backend():
    """Accept OS stores only, including safe members of a chained backend."""
    import keyring

    backend = keyring.get_keyring()
    candidates = getattr(backend, "backends", (backend,))
    trusted = {
        "keyring.backends.macOS",
        "keyring.backends.Windows",
        "keyring.backends.SecretService",
        "keyring.backends.libsecret",
    }
    for candidate in candidates:
        if type(candidate).__module__ in trusted:
            return candidate
    raise ValueError("An operating system credential store is unavailable.")


def save_secret(name: str, value: str) -> None:
    if READ_ONLY.get():
        raise ValueError("Preview cannot change credentials.")
    name = name.removeprefix("JOBHUB_")
    if name not in SECRET_KEYS:
        raise ValueError("Unknown credential name.")
    if not value:
        raise ValueError("Credential is empty; nothing was saved.")
    try:
        _credential_backend().set_password("job-search-hub", name, value)
    except Exception as exc:
        raise ValueError(
            f"Credential store unavailable. Set JOBHUB_{name} in your environment; "
            "no plaintext secret was saved."
        ) from exc


def get_setting(name: str, default=None):
    name = name.removeprefix("JOBHUB_")
    if name in SECRET_KEYS:
        return get_secret(name) or default
    value = os.environ.get(f"JOBHUB_{name}", _configuration.get(name, default))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if value is not None else None


def private_write(path: Path, content: str | bytes) -> None:
    if READ_ONLY.get():
        raise ValueError("Preview cannot write user data.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".jobhub-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def save_config(config: dict) -> None:
    settings = config.get("settings", {})
    if any(k.removeprefix("JOBHUB_") in SECRET_KEYS for k in settings):
        raise ValueError("Credentials belong in the credential store or environment.")
    private_write(app_paths().config_file, tomli_w.dumps(config))
    load_configuration()


def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path: Path, value) -> None:
    private_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
