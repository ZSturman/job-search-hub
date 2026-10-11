from job_search_hub.config import LMStudioSettings
from job_search_hub.models.routing import ModelRouter, ModelRoutingError


class _StubClient:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def list_model_ids(self) -> list[str]:
        return self.ids


def _settings(**overrides) -> LMStudioSettings:
    return LMStudioSettings(
        base_url="http://localhost:1234/v1",
        api_key="x",
        overrides=overrides,
    )


def test_heuristic_picks_qwen_primary_and_qwen_embedding():
    client = _StubClient(
        [
            "qwen/qwen2.5-7b-instruct",
            "text-embedding-qwen3-embedding-0.6b",
            "google/gemma-3n-e4b",
        ]
    )
    router = ModelRouter(_settings(), client=client)  # type: ignore[arg-type]
    mapping = router.discover()
    assert "primary" in mapping
    assert "embedding" in mapping
    assert "qwen" in mapping["primary"].model_id.lower()
    assert "embedding" in mapping["embedding"].model_id.lower()


def test_auto_ranker_picks_best_loaded_general_model_for_primary():
    client = _StubClient(
        [
            "text-embedding-nomic-embed-text-v1.5",
            "google/gemma-4-e4b",
            "qwen/qwen3.5-9b",
            "qwen2.5-coder-1.5b-instruct-mlx",
            "openai/gpt-oss-20b",
            "qwen/qwen3.6-27b",
        ]
    )
    router = ModelRouter(_settings(), client=client)  # type: ignore[arg-type]
    mapping = router.discover()

    assert mapping["primary"].model_id == "qwen/qwen3.6-27b"
    assert mapping["embedding"].model_id == "text-embedding-nomic-embed-text-v1.5"
    assert mapping["triage"].model_id != "qwen2.5-coder-1.5b-instruct-mlx"


def test_override_wins_when_present():
    client = _StubClient(["my-custom-model", "embed-x"])
    router = ModelRouter(
        _settings(primary="my-custom-model", embedding="embed-x"),
        client=client,  # type: ignore[arg-type]
    )
    mapping = router.discover()
    assert mapping["primary"].source == "override"
    assert mapping["primary"].model_id == "my-custom-model"


def test_override_missing_model_raises_for_required_role():
    client = _StubClient(["something-else"])
    router = ModelRouter(
        _settings(primary="not-loaded"),
        client=client,  # type: ignore[arg-type]
    )
    try:
        router.discover()
    except ModelRoutingError:
        return
    raise AssertionError("expected ModelRoutingError")


def test_no_match_for_required_role_raises():
    client = _StubClient(["totally-unrelated-model"])
    router = ModelRouter(_settings(), client=client)  # type: ignore[arg-type]
    try:
        router.discover()
    except ModelRoutingError:
        return
    raise AssertionError("expected ModelRoutingError")
