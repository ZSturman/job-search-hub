"""Reject private state, old executables, and credentials in wheels and sdists."""

from __future__ import annotations

import configparser
import re
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_DIRS = {".git", ".venv", "__pycache__", "data", "logs", "profile", "resumes"}
PRIVATE_SUFFIXES = {".pdf", ".docx", ".sqlite", ".db", ".pem", ".key", ".p12", ".pfx"}
SECRET = re.compile(rb"(?:ntn_[A-Za-z0-9]{24,}|secret_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{32,})")


def members(path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            yield from ((name, archive.read(name)) for name in archive.namelist())
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for entry in archive:
                if entry.isfile():
                    name = PurePosixPath(entry.name)
                    yield "/".join(name.parts[1:]), archive.extractfile(entry).read()
    else:
        raise ValueError(f"Unsupported archive: {path.name}")


def check(path):
    files = dict(members(path))
    for name, body in files.items():
        item = PurePosixPath(name)
        if (
            set(item.parts) & PRIVATE_DIRS
            or item.suffix.lower() in PRIVATE_SUFFIXES
            or item.name.startswith(".env")
            and item.name != ".env.example"
            or "casino_exit" in name.lower()
            or item.name.startswith("credentials.")
            or "sqlite-" in item.name
            or item.suffix == ".log"
        ):
            raise ValueError(f"Private or retired file in artifact: {name}")
        if SECRET.search(body):
            raise ValueError(f"Credential-shaped content in artifact: {name}")
    required = {"job_search_hub/db/schema.sql", "job_search_hub/resources/source_catalog.yaml"}
    if not required.issubset(files):
        raise ValueError("Required package resources are missing.")
    if path.suffix == ".whl":
        entries = next(body for name, body in files.items() if name.endswith("entry_points.txt"))
        parser = configparser.ConfigParser()
        parser.read_string(entries.decode())
        if dict(parser["console_scripts"]) != {"jobhub": "job_search_hub.cli:main"}:
            raise ValueError("The wheel must expose jobhub only.")
    print(f"PASS {path.name}: {len(files)} files; private-state/credential/resource checks passed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tools/check_release.py dist/*.whl dist/*.tar.gz")
    for argument in sys.argv[1:]:
        check(Path(argument))
