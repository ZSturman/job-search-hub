import logging

from job_search_hub.pipeline.progress import (
    StagePhase,
    report_stage_complete,
    report_stage_progress,
    report_stage_queue,
    report_stage_start,
    reset_progress,
)


def test_progress_reports_counts_eta_and_pipeline_position(caplog):
    caplog.set_level(logging.INFO)
    reset_progress(phases=tuple(StagePhase))

    report_stage_start(
        StagePhase.SCORE,
        current_step="loading scoring queue",
        next_step="sync manual applications",
    )
    report_stage_queue(StagePhase.SCORE, 10, current_step="scoring")
    report_stage_progress(
        StagePhase.SCORE,
        current_item="Acme - Engineer",
        items_completed=4,
        current_step="criterion 3/5: fit (LLM)",
        force=True,
    )
    report_stage_complete(
        StagePhase.SCORE,
        items_completed=10,
        items_total=10,
        next_step="sync manual applications",
    )

    output = caplog.text
    assert "PIPELINE 2/4 | SCORE" in output
    assert "4/10 done, 6 left ( 40.0%)" in output
    assert "pipeline 2/4 ( 35.0%)" in output
    assert "criterion 3/5: fit (LLM)" in output
    assert "COMPLETE | SCORE | processed=10 failed=0" in output


def test_zero_length_queue_is_reported_without_division_error(caplog):
    caplog.set_level(logging.INFO)
    reset_progress(phases=(StagePhase.WEEKLY,))

    report_stage_start(StagePhase.WEEKLY, current_step="loading queue")
    report_stage_queue(StagePhase.WEEKLY, 0, current_step="nothing eligible")
    report_stage_complete(StagePhase.WEEKLY, items_total=0, items_completed=0)

    assert "0/0 done, 0 left" in caplog.text
