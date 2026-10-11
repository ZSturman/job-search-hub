"""Plain-text terminal progress for long-running pipeline stages."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum

from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)


class StagePhase(Enum):
    """Canonical pipeline stages in execution order."""

    INGEST = 1
    SCORE = 2
    RESUME = 3
    WEEKLY = 4


_PHASE_LABELS = {
    StagePhase.INGEST: "Discover and write opportunities",
    StagePhase.SCORE: "Score opportunities with rules and LM Studio",
    StagePhase.RESUME: "Suggest existing resumes and reusable types",
    StagePhase.WEEKLY: "Roll up weekly metrics",
}

_PHASE_NEXT = {
    StagePhase.INGEST: "score the eligible opportunities",
    StagePhase.SCORE: "sync manual application rows and suggest resumes",
    StagePhase.RESUME: "roll up weekly metrics",
    StagePhase.WEEKLY: "pipeline complete",
}


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _bar(percent: float, width: int = 20) -> str:
    filled = min(width, max(0, int(percent / 100.0 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _eta_text(stage: StageProgress) -> str:
    eta = stage.estimated_seconds_remaining
    return _duration(eta) if eta is not None else "calculating"


@dataclass
class StageProgress:
    phase: StagePhase
    started_at: float | None = None
    completed_at: float | None = None
    items_total: int = 0
    items_completed: int = 0
    items_failed: int = 0
    current_item: str = ""
    current_step: str = ""
    next_step: str = ""

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.completed_at or time.monotonic()) - self.started_at

    @property
    def progress_percent(self) -> float:
        if self.items_total <= 0:
            return 0.0
        return min(100.0, self.items_completed / self.items_total * 100.0)

    @property
    def items_remaining(self) -> int:
        return max(0, self.items_total - self.items_completed)

    @property
    def estimated_seconds_remaining(self) -> float | None:
        if self.items_completed <= 0 or self.items_total <= 0:
            return None
        return self.elapsed_seconds / self.items_completed * self.items_remaining

    def reset(self, *, total_items: int, current_step: str, next_step: str) -> None:
        self.started_at = time.monotonic()
        self.completed_at = None
        self.items_total = total_items
        self.items_completed = 0
        self.items_failed = 0
        self.current_item = ""
        self.current_step = current_step
        self.next_step = next_step


@dataclass
class PipelineProgress:
    stages: dict[StagePhase, StageProgress] = field(default_factory=dict)
    active_phases: tuple[StagePhase, ...] = tuple(StagePhase)
    start_time: float | None = None

    def __post_init__(self) -> None:
        for phase in StagePhase:
            self.stages.setdefault(phase, StageProgress(phase=phase))

    def get_stage(self, phase: StagePhase) -> StageProgress:
        return self.stages[phase]

    def configure(self, phases: tuple[StagePhase, ...]) -> None:
        self.active_phases = phases or tuple(StagePhase)
        self.start_time = time.monotonic()

    def pipeline_position(self, phase: StagePhase) -> tuple[int, int]:
        phases = self.active_phases
        try:
            return phases.index(phase) + 1, len(phases)
        except ValueError:
            return phase.value, len(StagePhase)

    def overall_percent(self, phase: StagePhase) -> float:
        position, count = self.pipeline_position(phase)
        if count <= 0:
            return 0.0
        stage = self.get_stage(phase)
        phase_fraction = stage.progress_percent / 100.0
        return ((position - 1) + phase_fraction) / count * 100.0


_global_progress: PipelineProgress | None = None


def get_progress() -> PipelineProgress:
    global _global_progress
    if _global_progress is None:
        _global_progress = PipelineProgress()
        _global_progress.configure(tuple(StagePhase))
    return _global_progress


def reset_progress(*, phases: tuple[StagePhase, ...] | None = None) -> None:
    global _global_progress
    _global_progress = PipelineProgress()
    _global_progress.configure(phases or tuple(StagePhase))


def report_stage_start(
    phase: StagePhase,
    total_items: int = 0,
    *,
    current_step: str = "",
    next_step: str = "",
) -> StageProgress:
    progress = get_progress()
    stage = progress.get_stage(phase)
    stage.reset(
        total_items=total_items,
        current_step=current_step,
        next_step=next_step or _PHASE_NEXT[phase],
    )
    position, count = progress.pipeline_position(phase)
    log.info(
        "PIPELINE %d/%d | %s | %s | now: %s | next: %s",
        position,
        count,
        phase.name,
        _PHASE_LABELS[phase],
        stage.current_step or "starting",
        stage.next_step,
    )
    if total_items:
        report_stage_progress(phase, items_completed=0, force=True)
    return stage


def report_stage_queue(
    phase: StagePhase,
    total_items: int,
    *,
    current_step: str = "",
) -> None:
    stage = get_progress().get_stage(phase)
    stage.items_total = total_items
    if current_step:
        stage.current_step = current_step
    report_stage_progress(phase, items_completed=stage.items_completed, force=True)


def report_stage_progress(
    phase: StagePhase,
    current_item: str = "",
    items_completed: int | None = None,
    increment: bool = False,
    *,
    current_step: str = "",
    next_step: str = "",
    force: bool = False,
) -> None:
    progress = get_progress()
    stage = progress.get_stage(phase)
    if increment:
        stage.items_completed += 1
    elif items_completed is not None:
        stage.items_completed = items_completed
    if current_item:
        stage.current_item = current_item
    if current_step:
        stage.current_step = current_step
    if next_step:
        stage.next_step = next_step

    # Callers choose meaningful checkpoints. "force" is retained for detail
    # updates where the completed count has not changed.
    if not force and stage.items_total > 100 and stage.items_completed % 5:
        return

    percent = stage.progress_percent
    eta_text = _eta_text(stage)
    position, count = progress.pipeline_position(phase)
    overall = progress.overall_percent(phase)
    log.info(
        "%s progress | %s %d/%d done, %d left (%5.1f%%) | "
        "ETA %s | pipeline %d/%d (%5.1f%%) | %s | %s",
        phase.name,
        _bar(percent),
        stage.items_completed,
        stage.items_total,
        stage.items_remaining,
        percent,
        eta_text,
        position,
        count,
        overall,
        stage.current_item or "-",
        stage.current_step or "-",
    )


def report_stage_detail(
    phase: StagePhase,
    *,
    current_item: str,
    current_step: str,
    next_step: str = "",
) -> None:
    stage = get_progress().get_stage(phase)
    stage.current_item = current_item
    stage.current_step = current_step
    if next_step:
        stage.next_step = next_step
    log.info("%s now | %s | %s", phase.name, current_item, current_step)


@contextmanager
def report_stage_heartbeat(
    phase: StagePhase,
    *,
    current_item: str,
    current_step: str,
    next_step: str = "",
    interval_seconds: float = 30.0,
) -> Iterator[None]:
    """Emit periodic progress while one blocking operation is still running."""
    stage = get_progress().get_stage(phase)
    stage.current_item = current_item
    stage.current_step = current_step
    if next_step:
        stage.next_step = next_step

    stop = threading.Event()
    started_at = time.monotonic()

    def _beat() -> None:
        while not stop.wait(interval_seconds):
            elapsed = _duration(time.monotonic() - started_at)
            log.info(
                "%s still working | %s | %s | elapsed %s | ETA %s",
                phase.name,
                stage.current_item or current_item,
                stage.current_step or current_step,
                elapsed,
                _eta_text(stage),
            )

    thread = threading.Thread(
        target=_beat,
        name=f"jobhub-{phase.name.lower()}-progress",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=0.2)


def report_stage_complete(
    phase: StagePhase,
    *,
    items_completed: int | None = None,
    items_failed: int | None = None,
    items_total: int | None = None,
    next_step: str = "",
) -> StageProgress:
    progress = get_progress()
    stage = progress.get_stage(phase)
    if items_completed is not None:
        stage.items_completed = items_completed
    if items_failed is not None:
        stage.items_failed = items_failed
    if items_total is not None:
        stage.items_total = items_total
    stage.completed_at = time.monotonic()
    if next_step:
        stage.next_step = next_step
    log.info(
        "COMPLETE | %s | processed=%d failed=%d elapsed=%s | next: %s",
        phase.name,
        stage.items_completed,
        stage.items_failed,
        _duration(stage.elapsed_seconds),
        stage.next_step or _PHASE_NEXT[phase],
    )
    return stage


def report_item_failed(phase: StagePhase) -> None:
    get_progress().get_stage(phase).items_failed += 1
