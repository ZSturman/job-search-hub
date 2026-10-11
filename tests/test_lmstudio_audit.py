from concurrent.futures import ThreadPoolExecutor

import pytest

from job_search_hub.config import LMStudioSettings
from job_search_hub.models.client import LMStudioClient


class _AuditClient(LMStudioClient):
    def __init__(self, payload, store):
        super().__init__(
            LMStudioSettings(base_url="http://localhost:1234/v1", api_key="x", overrides={}),
            store=store,
            audit_mode="redacted",
        )
        self.payload = payload

    def _post(self, path, body):
        self.last_body = body
        return self.payload


class _SequenceAuditClient(_AuditClient):
    def __init__(self, payloads, store):
        super().__init__(payloads[-1], store)
        self.payloads = iter(payloads)
        self.bodies = []

    def _post(self, path, body):
        self.bodies.append(body)
        return next(self.payloads)


def test_chat_json_records_valid_call(tmp_store):
    client = _AuditClient(
        {"choices": [{"message": {"content": '{"score": 0.8, "reason": "ok"}'}}]},
        tmp_store,
    )
    data = client.chat_json(
        model="m",
        messages=[{"role": "user", "content": "score me@example.com"}],
        json_schema={"type": "object"},
        stage="score",
        role="primary",
        task="fit",
    )
    assert data["score"] == 0.8
    rows = tmp_store.recent_llm_calls(1)
    assert rows[0]["json_valid"] == 1
    assert "[email]" in rows[0]["prompt_preview"]


def test_chat_json_retries_empty_content_then_records_success(tmp_store):
    client = _SequenceAuditClient(
        [
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": '{"score": 0.8}'}}]},
        ],
        tmp_store,
    )
    data = client.chat_json(
        model="m",
        messages=[{"role": "user", "content": "score"}],
        json_schema={"type": "object"},
        stage="score",
        role="primary",
        task="fit",
    )

    assert data == {"score": 0.8}
    rows = tmp_store.recent_llm_calls(2)
    assert [row["json_valid"] for row in rows] == [1, 0]
    assert client.bodies[0]["temperature"] == 0.0
    assert client.bodies[1]["temperature"] == 0.0


def test_chat_json_retries_malformed_json_once(tmp_store):
    client = _SequenceAuditClient(
        [
            {"choices": [{"message": {"content": '{"score": 0.8,}'}}]},
            {"choices": [{"message": {"content": '{"score": 0.8}'}}]},
        ],
        tmp_store,
    )

    assert client.chat_json(
        model="m",
        messages=[{"role": "user", "content": "score"}],
        json_schema={"type": "object"},
        stage="score",
        role="primary",
        task="fit",
    ) == {"score": 0.8}
    rows = tmp_store.recent_llm_calls(2)
    assert rows[1]["error"] == "model request failed"


def test_chat_json_retries_logically_invalid_json_once(tmp_store):
    from job_search_hub.pipeline.structured_output import validate_exact_indexes

    client = _SequenceAuditClient(
        [
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"evaluations":['
                                '{"criterion_index":0,"score":0.8},'
                                '{"criterion_index":0,"score":0.2}'
                                "]}"
                            )
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"evaluations":['
                                '{"criterion_index":0,"score":0.8},'
                                '{"criterion_index":1,"score":0.2}'
                                "]}"
                            )
                        }
                    }
                ]
            },
        ],
        tmp_store,
    )

    data = client.chat_json(
        model="m",
        messages=[{"role": "user", "content": "score"}],
        json_schema={
            "type": "object",
            "required": ["evaluations"],
            "properties": {"evaluations": {"type": "array"}},
        },
        post_validator=lambda payload: validate_exact_indexes(
            payload,
            array_key="evaluations",
            index_key="criterion_index",
            expected_indexes=range(2),
        ),
    )

    assert [row["criterion_index"] for row in data["evaluations"]] == [0, 1]
    rows = tmp_store.recent_llm_calls(2)
    assert rows[1]["json_valid"] == 0
    assert rows[1]["error"] == "model request failed"


def test_chat_json_fails_after_two_invalid_responses(tmp_store):
    client = _SequenceAuditClient(
        [
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        ],
        tmp_store,
    )
    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        client.chat_json(
            model="m",
            messages=[{"role": "user", "content": "score"}],
            json_schema={"type": "object"},
            stage="score",
            role="primary",
            task="fit",
        )
    rows = tmp_store.recent_llm_calls(2)
    assert all(row["json_valid"] == 0 for row in rows)
    assert all(row["fallback_used"] == 1 for row in rows)


def test_chat_json_caches_identical_structured_failures(tmp_store):
    client = _SequenceAuditClient(
        [
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
        ],
        tmp_store,
    )
    kwargs = {
        "model": "m",
        "messages": [{"role": "user", "content": "same input"}],
        "json_schema": {"type": "object"},
        "task": "same-task",
    }

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        client.chat_json(**kwargs)
    with pytest.raises(RuntimeError, match="cached LM Studio failure"):
        client.chat_json(**kwargs)

    assert len(client.bodies) == 2


def test_chat_json_disables_reasoning_for_qwen3(tmp_store):
    client = _AuditClient(
        {"choices": [{"message": {"content": '{"score": 0.8, "reason": "ok"}'}}]},
        tmp_store,
    )
    client.chat_json(
        model="qwen/qwen3.5-9b",
        messages=[{"role": "user", "content": "score"}],
        json_schema={"type": "object"},
    )
    assert client.last_body["reasoning_effort"] == "none"
    assert "chat_template_kwargs" not in client.last_body


def test_chat_json_sends_lmstudio_compatible_nullable_schema(tmp_store):
    client = _AuditClient(
        {"choices": [{"message": {"content": '{"lane_override": "None", "threshold": 0}'}}]},
        tmp_store,
    )
    schema = {
        "type": "object",
        "properties": {
            "lane_override": {
                "type": ["string", "null"],
                "enum": [None, "None", "Skip"],
            },
            "threshold": {"type": ["number", "null"]},
        },
    }

    client.chat_json(
        model="m",
        messages=[{"role": "user", "content": "score"}],
        json_schema=schema,
    )

    outbound = client.last_body["response_format"]["json_schema"]["schema"]
    assert outbound["properties"]["lane_override"]["type"] == "string"
    assert outbound["properties"]["lane_override"]["enum"] == ["None", "Skip"]
    assert outbound["properties"]["threshold"]["type"] == "number"
    assert schema["properties"]["lane_override"]["type"] == ["string", "null"]


def test_last_call_id_is_thread_local(tmp_store):
    client = _AuditClient(
        {"choices": [{"message": {"content": '{"score": 0.8, "reason": "ok"}'}}]},
        tmp_store,
    )
    client.last_call_id = 10

    def worker():
        client.last_call_id = 20
        return client.last_call_id

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(worker).result() == 20
    assert client.last_call_id == 10
