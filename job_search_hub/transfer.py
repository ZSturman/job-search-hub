"""Private backups and restartable, one-way imports. Credentials are excluded."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from job_search_hub.notion import properties as P
from job_search_hub.notion.hub import HUB_SPECS
from job_search_hub.storage import SECRET_KEYS, app_paths, private_write, read_config


def export_notion(notion, settings, root):
    return {
        "format": "jobhub-export-v1",
        "root": root,
        "sources": {
            spec.key: list(notion.query_data_source(settings.notion.data_sources[spec.key]))
            for spec in HUB_SPECS
        },
    }


def backup(output: Path, *, notion_export=None):
    import tomli_w

    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("Choose a new backup filename; existing backups are preserved.")
    paths = app_paths()
    config = read_config()
    config["settings"] = {
        k: v
        for k, v in config.get("settings", {}).items()
        if k.removeprefix("JOBHUB_") not in SECRET_KEYS
    }
    with tempfile.TemporaryDirectory(prefix="jobhub-backup-") as temp:
        stage = Path(temp)
        items = [("config/config.toml", tomli_w.dumps(config).encode())]
        if paths.data.exists():
            for file in paths.data.rglob("*"):
                if not file.is_file() or file.is_symlink():
                    continue
                rel = file.relative_to(paths.data)
                if any(p.startswith(".") or p in {"backups", "logs", "runtime"} for p in rel.parts):
                    continue
                if (
                    file == output
                    or file.suffix in {".zip", ".wal", ".shm"}
                    or file.name.endswith(("-wal", "-shm"))
                ):
                    continue
                if file.suffix in {".sqlite", ".db"}:
                    snap = stage / hashlib.sha256(str(rel).encode()).hexdigest()
                    with (
                        sqlite3.connect(file.as_uri() + "?mode=ro", uri=True) as src,
                        sqlite3.connect(snap) as dst,
                    ):
                        src.backup(dst)
                    data = snap.read_bytes()
                else:
                    data = file.read_bytes()
                items.append(("data/" + rel.as_posix(), data))
        if notion_export is not None:
            items.append(("notion.json", json.dumps(notion_export).encode()))
        target = stage / "backup.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json", json.dumps({"format": "jobhub-backup-v1", "credentials": False})
            )
            for name, data in items:
                archive.writestr(name, data)
        private_write(output, target.read_bytes())
    return {"backup": str(output), "files": len(items), "credentials_included": False}


def restore(archive: Path, destination: Path):
    """Restore local data into an empty independent JOBHUB_HOME, never overwrite."""
    import tomllib

    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Restore destination must be empty.")
    entries = []
    with zipfile.ZipFile(archive) as z:
        if json.loads(z.read("manifest.json")).get("format") != "jobhub-backup-v1":
            raise ValueError("Not a Job Search Hub backup.")
        if sum(i.file_size for i in z.infolist()) > 2_000_000_000:
            raise ValueError("Backup exceeds 2 GB unpacked limit.")
        for entry in z.infolist():
            p = PurePosixPath(entry.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in entry.filename:
                raise ValueError("Unsafe backup path.")
            if p.parts[0] not in {"config", "data", "manifest.json", "notion.json"}:
                raise ValueError("Unknown backup entry.")
            if entry.is_dir():
                continue
            body = z.read(entry)
            if entry.filename == "config/config.toml":
                settings = tomllib.loads(body.decode()).get("settings", {})
                if any(k.removeprefix("JOBHUB_") in SECRET_KEYS for k in settings):
                    raise ValueError("Backup contains credentials.")
            entries.append((p, body))
    # Validate the whole archive before creating anything; stage and atomically promote.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".jobhub-restore-") as temp:
        stage = Path(temp) / "home"
        stage.mkdir(mode=0o700)
        for p, body in entries:
            target = stage / str(p)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(body)
            target.chmod(0o600)
        if destination.exists():
            destination.rmdir()
        os.replace(stage, destination)
    return {
        "restored_to": str(destination),
        "next": (
            f"Set JOBHUB_HOME={destination}; configure credentials "
            + "separately. Notion data remains in the connected hub."
        ),
    }


def import_documents(source: Path, *, apply=False):
    from job_search_hub.library import add_resume, catalog

    source = source.expanduser().resolve()
    if source == app_paths().data or app_paths().data in source.parents:
        raise ValueError("Choose a separate import source.")
    folder = source / "profile/resumes" if (source / "profile/resumes").is_dir() else source
    files = sorted(p for p in folder.rglob("*.pdf") if p.is_file() and not p.is_symlink())
    known = {r["id"] for r in catalog()}
    result = []
    for p in files:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        state = "already imported" if digest in known else "would import"
        if apply and digest not in known:
            add_resume(p, p.parent.name if p.parent != folder else p.stem)
            known.add(digest)
            state = "imported"
        result.append({"file": str(p), "status": state})
    return result


def import_notion(payload: dict, notion, settings, root, *, apply=False):
    from job_search_hub.pipeline.lifecycle import map_application_status, map_opportunity_status

    if payload.get("format") != "jobhub-export-v1":
        raise ValueError("Use a Job Search Hub JSON export.")
    from job_search_hub.notion.hub import page_id

    if page_id(payload["root"]) == page_id(root):
        raise ValueError("Import source and destination hubs must differ.")
    identity = hashlib.sha256(payload["root"].encode()).hexdigest()[:16]
    mapping = {}
    pending = []
    for spec in HUB_SPECS:
        remote = list(notion.query_data_source(settings.notion.data_sources[spec.key]))
        existing = {P.read_text(p["properties"].get("Import ID")): p["id"] for p in remote}
        for row in payload.get("sources", {}).get(spec.key, []):
            token = f"{identity}:{row['id']}"
            key = (spec.key, row["id"])
            props = {}
            for prop in spec.properties:
                legacy = {"Approved for Profile": "Approved for Resume"}
                value = row.get("properties", {}).get(prop.name)
                if value is None and prop.name in legacy:
                    value = row.get("properties", {}).get(legacy[prop.name])
                if value is None or prop.type in {
                    "relation",
                    "rollup",
                    "formula",
                    "last_edited_time",
                    "created_time",
                }:
                    continue
                block = value.get(prop.type)
                if prop.type in {"rich_text", "title"}:
                    block = [
                        {
                            "type": "text",
                            "text": {
                                "content": P.read_title(value)
                                if prop.type == "title"
                                else P.read_text(value)
                            },
                        }
                    ]
                if prop.type == "select":
                    name = P.read_select(value)
                    if prop.name == "Status":
                        if spec.key == "opportunities":
                            name = map_opportunity_status(name)
                        elif spec.key == "applications":
                            name = map_application_status(name)
                    if prop.name == "Submission Mode":
                        name = "Manual"
                    if prop.name == "My Decision" and name == "Review":
                        name = "Shortlist"
                    block = {"name": name} if name else None
                if prop.type == "multi_select":
                    block = [{"name": n} for n in P.read_multi_select(value)]
                props[prop.name] = {prop.type: block}
            props["Import ID"] = P.rich_text(token)
            if apply:
                new = existing.get(token)
                if new is None:
                    new = notion.create_page(
                        parent={"data_source_id": settings.notion.data_sources[spec.key]},
                        properties=props,
                    )["id"]
                mapping[key] = new
            pending.append((spec, row, props, token))
    if apply:
        for spec, row, _props, _token in pending:
            links = {
                p.name: P.relation(
                    [
                        mapping[(p.relation_target, i)]
                        for i in P.read_relation_ids(row.get("properties", {}).get(p.name))
                        if (p.relation_target, i) in mapping
                    ]
                )
                for p in spec.properties
                if p.type == "relation"
            }
            if links:
                notion.update_page(mapping[(spec.key, row["id"])], links)
    return {"rows": len(pending), "applied": apply, "source_unchanged": True}
