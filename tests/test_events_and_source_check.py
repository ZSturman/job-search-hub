from job_search_hub.events import emit_event, read_events
from job_search_hub.pipeline.source_check import (
    SourceCheckResult,
    _next_allowed,
    format_source_check,
)


def test_event_jsonl_round_trip(tmp_path):
    emit_event(
        tmp_path,
        event="source_check",
        stage="source-check",
        message="RSS ok",
        payload={"count": 2},
    )
    events = read_events(tmp_path, limit=10)
    assert len(events) == 1
    assert events[0]["payload"]["count"] == 2


def test_source_check_format_includes_error():
    text = format_source_check(
        [
            SourceCheckResult(
                source_key="rss",
                name="RSS",
                adapter_key="rss",
                target="https://example.com/feed",
                status="error",
                configured=True,
                error="rate limited",
                http_status=429,
            )
        ]
    )
    assert "rate limited" in text
    assert "429" in text


def test_next_allowed_empty_without_retry_after():
    assert _next_allowed(None) == ""
    assert "T" in _next_allowed(1.0)
