"""Task-oriented command handlers for Job Search Hub."""

from __future__ import annotations

import getpass
import importlib.util
from urllib.parse import urlsplit

from job_search_hub.commands.common import output
from job_search_hub.commands.hub import _edit_keywords
from job_search_hub.config import load_settings
from job_search_hub.notion import properties as P
from job_search_hub.storage import (
    app_paths,
    get_secret,
    read_config,
    save_config,
    save_secret,
)


def setup(args):
    from job_search_hub.notion.client import NotionClient
    from job_search_hub.notion.hub import bind, page_id, provision
    from job_search_hub.workflow import validate_hub

    config = read_config()
    values = config.setdefault("settings", {})
    if args.credential:
        secret = getpass.getpass(f"{args.credential} (hidden): ")
        if not secret:
            raise ValueError("Credential is empty; nothing was saved.")
        save_secret(args.credential, secret)
        print("Credential saved in the operating system credential store.")
        return 0
    if args.lmstudio_url:
        parsed = urlsplit(args.lmstudio_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LM Studio URL must be an http(s) server URL ending in /v1.")
        values["LMSTUDIO_BASE_URL"] = args.lmstudio_url
    if args.model:
        values["MODEL_PRIMARY"] = args.model
    if args.embedding_model:
        values["MODEL_EMBEDDING"] = args.embedding_model
    root = args.root_url or values.get("NOTION_ROOT", "")
    if not root and not args.non_interactive:
        print(
            "Duplicate a Job Search Hub template into your private workspace. "
            "For the first template, create a blank page, grant your "
            "integration access, and use setup --create-template --root-url "
            "URL. See docs/NOTION_TEMPLATE.md."
        )
        root = input("Dedicated hub root page URL: ").strip()
    if not root:
        if args.lmstudio_url or args.model or args.embedding_model:
            save_config(config)
            print("Model settings saved. Connect Notion with jobhub setup --root-url URL.")
            return 0
        raise ValueError(
            "Supply --root-url for your separate hub; use --create-template to"
            " initialize a blank page."
        )
    root = page_id(root)
    if not get_secret("NOTION_TOKEN"):
        if args.non_interactive:
            raise ValueError(
                "Set JOBHUB_NOTION_TOKEN or run jobhub setup --credential NOTION_TOKEN."
            )
        save_secret("NOTION_TOKEN", getpass.getpass("Notion integration token (hidden): "))
    save_config(config)
    settings = load_settings()
    with NotionClient(settings.notion) as notion:
        if args.create_template:
            provision(notion, root)
        settings = bind(settings, notion, root)
        validate_hub(settings, notion)
        values["NOTION_ROOT"] = root
        for key, ds in settings.notion.data_sources.items():
            values["NOTION_DS_" + key.upper()] = ds
        save_config(config)
        roles = args.role
        if not roles and not args.non_interactive and not values.get("ONBOARDING_COMPLETE"):
            entry = input(
                "Role keywords, separated by commas (or leave blank to edit in Notion): "
            ).strip()
            roles = [term.strip() for term in entry.split(",") if term.strip()]
        if roles:
            _edit_keywords(notion, settings, "My search", "role", roles, remove=False)
        if not args.non_interactive and not values.get("ONBOARDING_COMPLETE"):
            if input("Enable the public Remotive source? [y/N]: ").strip().lower() in {"y", "yes"}:
                ds = settings.notion.data_sources["sources"]
                rows = list(
                    notion.query_data_source(
                        ds, filter_={"property": "Type", "rich_text": {"equals": "remotive"}}
                    )
                )
                if rows:
                    notion.update_page(rows[0]["id"], {"Enabled": P.checkbox(True)})
            if input("Configure optional LM Studio now? [y/N]: ").strip().lower() in {"y", "yes"}:
                values["LMSTUDIO_BASE_URL"] = (
                    input("Server URL [http://localhost:1234/v1]: ").strip()
                    or "http://localhost:1234/v1"
                )
                model = input("Loaded chat model ID (blank for auto selection): ").strip()
                if model:
                    values["MODEL_PRIMARY"] = model
                if input("Is LM Studio server authentication enabled? [y/N]: ").strip().lower() in {
                    "y",
                    "yes",
                }:
                    save_secret("LMSTUDIO_API_KEY", getpass.getpass("LM Studio token (hidden): "))
            if input("Enable optional email notifications now? [y/N]: ").strip().lower() in {
                "y",
                "yes",
            }:
                values["SMTP_HOST"] = input("SMTP server: ").strip()
                values["SMTP_PORT"] = int(input("SMTP port [587]: ").strip() or "587")
                values["SMTP_USERNAME"] = input(
                    "SMTP username (blank for no authentication): "
                ).strip()
                values["SMTP_FROM"] = input("Sender address: ").strip()
                values["SMTP_TO"] = input("Your notification address: ").strip()
                if values["SMTP_USERNAME"]:
                    save_secret("SMTP_PASSWORD", getpass.getpass("SMTP password (hidden): "))
            values["ONBOARDING_COMPLETE"] = True
            save_config(config)
    print(
        "Connected Job Search Hub: https:"
        + f"//www.notion.so/{root.replace('-', '')}\n1. Add role "
        + "keywords and enable sources on 1 — My search.\n2. Configure "
        + "LM Studio if desired: jobhub setup --model MODEL_ID\n3. Run "
        + "jobhub doctor, then jobhub run --preview and jobhub "
        + "run.\nScheduling is off until jobhub schedule enable."
    )
    return 0


def doctor(args):
    from job_search_hub.workflow import models, session, validate_hub

    settings = load_settings()
    errors = []
    output(
        {
            "configuration": str(app_paths().config_file),
            "documents": str(app_paths().data / "resumes"),
            "browser_extra": importlib.util.find_spec("playwright") is not None,
            "pdf_extra": importlib.util.find_spec("pypdf") is not None,
            "secrets": "Credential store or JOBHUB_* environment; never config.toml",
        }
    )
    try:
        with session(preview=True, preferences=args.sources) as (s, n):
            validate_hub(s, n)
            print("Notion: dedicated hub reachable; schemas valid.")
            if args.sources:
                from job_search_hub.ingest.preview import run_preview

                output(
                    [
                        {
                            "source": r.request.source_adapter,
                            "status": r.status,
                            "error": r.error,
                            "reason": r.skip_reason,
                        }
                        for r in run_preview(s, sources=list(s.enabled_adapters), examples=1)
                    ]
                    if s.enabled_adapters
                    else []
                )
    except Exception as exc:
        errors.append("Notion: " + str(exc))
    with models(settings) as (client, router):
        if client is None:
            errors.append(
                "LM Studio unavailable. Start its local server and load a chat "
                "model; discovery/tracking still work."
            )
        else:
            try:
                response = client.chat(
                    model=router.get("primary"),
                    messages=[{"role": "user", "content": "Reply OK."}],
                    max_tokens=8,
                )
                if not response.strip():
                    raise ValueError("Chat returned empty content.")
                print("LM Studio: actual chat request succeeded.")
                if "embedding" in router.discover():
                    try:
                        client.embed(
                            model=router.get("embedding"), inputs=["Job Search Hub connection test"]
                        )
                        print("Embeddings: actual request succeeded.")
                    except Exception:
                        print("Embeddings unavailable; exact matching remains available.")
                else:
                    print("Embeddings not configured; exact matching remains available.")
            except Exception:
                errors.append(
                    "Model is listed but cannot answer a chat request. Load it in LM "
                    "Studio or choose another model."
                )
    for error in errors:
        print(error)
    return 1 if errors else 0
