"""Local-model benchmark runner.

Runs a small fixed test battery against the discovered LM Studio roles
and prints latency and checks that each request returns content. The goal is to
help you decide which loaded model to assign to which role; it is not a
research-grade eval.

Prints a summary table on stdout without persisting results.

Run::

    python -m job_search_hub.models.benchmark
    python -m job_search_hub.models.benchmark --tasks chat embed
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from job_search_hub.config import load_settings
from job_search_hub.logging_setup import get_logger, setup_logging
from job_search_hub.models.client import LMStudioClient
from job_search_hub.models.routing import ModelRouter

log = get_logger(__name__)


# ----- task fixtures -----

_CHAT_PROMPTS = [
    (
        "classify",
        "Reply with exactly one word: 'excluded' or 'not_excluded'.\n"
        "Rule: Exclude temporary jobs. Posting: Temporary archive assistant.\n",
    ),
    (
        "classify",
        "Reply with exactly one word: 'excluded' or 'not_excluded'.\n"
        "Rule: Exclude temporary jobs. Posting: Permanent research analyst.\n",
    ),
    (
        "extract",
        "Return ONLY a JSON object with keys 'company' and 'role'. "
        "Posting: Acme Robotics is hiring a Senior Embedded Engineer.",
    ),
]

_EMBED_INPUTS = [
    "Backend engineer with Python and PostgreSQL.",
    "Front-end developer with React and TypeScript.",
    "Archive assistant with records-management experience.",
]


@dataclass
class TaskResult:
    role: str
    model_id: str
    task: str
    label: str
    elapsed_ms: float
    output: str = ""
    ok: bool = True
    error: str = ""


@dataclass
class BenchmarkReport:
    started_at: str = ""
    completed_at: str = ""
    results: list[TaskResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "role        model                                              task        ms   ok"
        ]
        lines.append("-" * 86)
        for r in self.results:
            lines.append(
                f"{r.role:11s} {r.model_id[:50]:50s} {r.task:10s} "
                + f"{r.elapsed_ms:5.0f}  {'Y' if r.ok else 'N'}"
            )
        if self.results:
            chats = [r.elapsed_ms for r in self.results if r.task == "chat"]
            embeds = [r.elapsed_ms for r in self.results if r.task == "embed"]
            if chats:
                lines.append(f"\nchat  median={statistics.median(chats):.0f}ms  n={len(chats)}")
            if embeds:
                lines.append(f"embed median={statistics.median(embeds):.0f}ms  n={len(embeds)}")
        return "\n".join(lines)


def _bench_chat(role: str, model_id: str, client: LMStudioClient) -> list[TaskResult]:
    out: list[TaskResult] = []
    for label, prompt in _CHAT_PROMPTS:
        t0 = time.perf_counter()
        try:
            resp = client.chat(
                model=model_id,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=64,
            )
            elapsed = (time.perf_counter() - t0) * 1000
            text = resp.strip()
            out.append(
                TaskResult(
                    role=role,
                    model_id=model_id,
                    task="chat",
                    label=label,
                    elapsed_ms=elapsed,
                    output=text,
                    ok=bool(text),
                )
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000
            out.append(
                TaskResult(
                    role=role,
                    model_id=model_id,
                    task="chat",
                    label=label,
                    elapsed_ms=elapsed,
                    ok=False,
                    error=str(exc),
                )
            )
    return out


def _bench_embed(role: str, model_id: str, client: LMStudioClient) -> list[TaskResult]:
    out: list[TaskResult] = []
    t0 = time.perf_counter()
    try:
        vectors = client.embed(model=model_id, inputs=_EMBED_INPUTS)
        elapsed = (time.perf_counter() - t0) * 1000
        ok = all(isinstance(v, list) and len(v) > 0 for v in vectors)
        out.append(
            TaskResult(
                role=role,
                model_id=model_id,
                task="embed",
                label="batch",
                elapsed_ms=elapsed,
                output=f"{len(vectors)} vectors of dim {len(vectors[0]) if vectors else 0}",
                ok=ok,
            )
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        out.append(
            TaskResult(
                role=role,
                model_id=model_id,
                task="embed",
                label="batch",
                elapsed_ms=elapsed,
                ok=False,
                error=str(exc),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jobhub-benchmark",
        description="Benchmark loaded LM Studio models per role.",
    )
    parser.add_argument(
        "--tasks",
        nargs="*",
        default=["chat", "embed"],
        choices=["chat", "embed"],
        help="Task suites to run.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    from job_search_hub.storage import READ_ONLY

    if not READ_ONLY.get():
        setup_logging(settings.log_dir, level=settings.log_level)
    report = BenchmarkReport(started_at=datetime.now(UTC).isoformat())

    with LMStudioClient(settings.lmstudio) as client:
        router = ModelRouter(settings.lmstudio, client=client)
        try:
            mapping = router.discover()
        except Exception as exc:
            log.error("benchmark: model routing failed: %s", exc)
            return 2

        for role, m in mapping.items():
            if "chat" in args.tasks and role != "embedding":
                report.results.extend(_bench_chat(role, m.model_id, client))
            if "embed" in args.tasks and role == "embedding":
                report.results.extend(_bench_embed(role, m.model_id, client))

    report.completed_at = datetime.now(UTC).isoformat()
    print(report.summary())
    return 0 if all(r.ok for r in report.results) else 1


if __name__ == "__main__":
    sys.exit(main())
