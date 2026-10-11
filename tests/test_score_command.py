from __future__ import annotations

from types import SimpleNamespace

from job_search_hub.pipeline.lifecycle_sync import LifecycleSyncStats
from job_search_hub.pipeline.score_stage import ScoreStats


def test_score_command_drains_awaiting_before_normal_scoring(monkeypatch) -> None:
    import job_search_hub.commands.operations as tick
    import job_search_hub.models as models
    import job_search_hub.pipeline.lifecycle_sync as lifecycle_sync
    import job_search_hub.pipeline.score_stage as score_stage

    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    class Router:
        def __init__(self, *args, **kwargs):
            pass

        def discover(self):
            return {}

    class NotionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return None

    def fake_run_score(*args, **kwargs):
        calls.append(kwargs.get("awaiting_only", False))
        stats = ScoreStats(criteria_active=1)
        if kwargs.get("awaiting_only", False):
            stats.awaiting_resolved = 2
        else:
            stats.scored = 3
        return stats

    monkeypatch.setattr(models, "LMStudioClient", Client)
    monkeypatch.setattr(models, "ModelRouter", Router)
    monkeypatch.setattr(score_stage, "run_score", fake_run_score)
    monkeypatch.setattr(
        lifecycle_sync,
        "reconcile_lifecycles",
        lambda *args, **kwargs: LifecycleSyncStats(),
    )
    monkeypatch.setattr(
        tick,
        "_bootstrap",
        lambda args: (
            SimpleNamespace(
                sqlite_path=":memory:",
                lmstudio=SimpleNamespace(),
                llm_audit_mode="",
            ),
            False,
        ),
    )
    monkeypatch.setattr(tick, "open_store", lambda path: object())
    monkeypatch.setattr(tick, "_open_notion", lambda settings: NotionContext())
    monkeypatch.setattr(tick.ns, "print_next_steps", lambda *args, **kwargs: None)

    rc = tick.cmd_score(
        SimpleNamespace(
            subcommand="score",
            awaiting_only=False,
            rescore=False,
            limit=None,
            commit=True,
            dry_run=False,
        )
    )

    assert rc == 0
    assert calls == [True, False]
