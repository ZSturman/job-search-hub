import json
from dataclasses import asdict, is_dataclass


def output(value):
    if is_dataclass(value):
        value = asdict(value)
    print(json.dumps(value, indent=2, default=str, ensure_ascii=False))


def mutating(args):
    if args.command == "run":
        return not args.preview
    if args.command == "setup":
        return True
    if args.command in {"status", "doctor"}:
        return False
    if args.command == "schedule":
        return args.action != "status" and not getattr(args, "preview", False)
    if args.command == "maintenance":
        return args.action in {"backup", "export", "capture"} or getattr(args, "apply", False)
    if args.command == "jobs":
        return args.action in {"add", "decide", "track"}
    if args.command == "search":
        return args.action in {"profile", "keywords"}
    if args.command == "sources":
        return args.action in {"add", "enable", "disable", "authorize"}
    if args.command == "resumes":
        return args.action in {"add", "sync"} or args.action == "suggest" and not args.preview
    if args.command == "profile":
        return args.action in {"extract", "approve"}
    if args.command == "answers":
        return args.action in {"approve", "refresh"} or args.action == "propose" and args.apply
    return args.command == "notifications" and args.action in {"test", "retry"}


def records(rows, *, json_output=False):
    """Show the small set of fields needed for a decision; retain JSON on request."""
    if json_output:
        return output(rows)
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    from job_search_hub.notion import properties as P

    if not rows:
        print("Nothing here yet. Follow the next step in your Notion hub.")
        return
    first = rows[0]["properties"]
    title = next((key for key, prop in first.items() if "title" in prop), "Title")
    columns = {
        "Opportunity": ["Status", "My Decision", "Model Score"],
        "Application": ["Status", "Next Action"],
        "Profile": ["Enabled", "Role Terms", "Location"],
        "Source": ["Enabled", "Type", "Target", "Status"],
        "Fact": ["Value", "Verified", "Approved for Profile"],
        "Question": ["Status", "My Answer", "Approved for Reuse"],
    }.get(title, ["Status"])
    table = Table("ID", title, *columns, show_lines=True)
    for row in rows:
        props = row["properties"]
        values = []
        for key in [title, *columns]:
            prop = props.get(key, {})
            if "checkbox" in prop:
                value = "Yes" if P.read_checkbox(prop) else "No"
            elif "number" in prop:
                value = str(P.read_number(prop)) if P.read_number(prop) is not None else "—"
            elif "multi_select" in prop:
                value = ", ".join(P.read_multi_select(prop))
            else:
                value = (
                    P.read_title(prop)
                    or P.read_text(prop)
                    or P.read_select(prop)
                    or P.read_status(prop)
                    or P.read_url(prop)
                    or "—"
                )
            values.append(Text(value))
        table.add_row(Text(row["id"]), *values)
    Console().print(table)
