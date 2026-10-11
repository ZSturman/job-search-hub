"""Short, task-oriented follow-up guidance for advanced operations."""

from dataclasses import dataclass


@dataclass
class Step:
    text: str
    command: str = ""

    def render(self):
        return f"  • {self.text}" + (f"\n    {self.command}" if self.command else "")


def print_next_steps(title, steps):
    for step in steps:
        print(step.render())


def steps_for_ingest(**stats):
    if stats.get("errors"):
        return [Step("Check source configuration and availability.", "jobhub sources check")]
    return [Step("Review 2 — Review jobs after refreshing.", "jobhub run")]


def steps_for_score(**stats):
    steps = [Step("Review matches on 2 — Review jobs; choose Shortlist, Apply, or Skip.")]
    if stats.get("awaiting_answer"):
        steps.append(
            Step("Approve reusable answers on 3 — Resolve questions.", "jobhub answers list")
        )
    if stats.get("errors") or stats.get("degraded_evaluations"):
        steps.append(Step("Check the model integration before retrying.", "jobhub doctor"))
    if stats.get("dry_run"):
        steps.append(Step("Preview completed; refresh when ready.", "jobhub run"))
    return steps
