from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from job_search_hub.config import NotionSettings
from job_search_hub.notion.client import (
    NotionClient,
    NotionRateLimitError,
    NotionRequestCoordinator,
    NotionTransientError,
)


@dataclass
class _FakeResponse:
    status_code: int
    payload: dict
    headers: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return str(self.payload)

    def json(self) -> dict:
        return self.payload


class _FakeHTTP:
    def __init__(self, responses: list[tuple[str, str, _FakeResponse]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {path}")
        exp_method, exp_path, resp = self.responses.pop(0)
        assert method == exp_method
        assert path == exp_path
        return resp

    def close(self) -> None:
        return None


def _client(fake_http: _FakeHTTP) -> NotionClient:
    client = NotionClient(NotionSettings(token="token", api_version="2025-09-03", data_sources={}))
    client._client = fake_http
    return client


def _retrying_client(fake_http: _FakeHTTP, sleeps: list[float]) -> NotionClient:
    coordinator = NotionRequestCoordinator(sleep=sleeps.append, jitter=False)
    client = NotionClient(
        NotionSettings(token="token", api_version="2025-09-03", data_sources={}),
        coordinator=coordinator,
    )
    client._client = fake_http
    return client


def test_get_data_source_accepts_data_source_id_directly() -> None:
    fake = _FakeHTTP(
        [
            (
                "GET",
                "/data_sources/ds_1",
                _FakeResponse(200, {"object": "data_source", "id": "ds_1", "properties": {}}),
            ),
            (
                "GET",
                "/data_sources/ds_1",
                _FakeResponse(200, {"object": "data_source", "id": "ds_1", "properties": {}}),
            ),
        ]
    )
    client = _client(fake)

    row = client.get_data_source("ds_1")

    assert row["id"] == "ds_1"


def test_get_data_source_resolves_database_id_to_data_source_id() -> None:
    fake = _FakeHTTP(
        [
            (
                "GET",
                "/data_sources/db_1",
                _FakeResponse(404, {"code": "object_not_found"}),
            ),
            (
                "GET",
                "/databases/db_1",
                _FakeResponse(
                    200, {"id": "db_1", "data_sources": [{"id": "ds_real", "name": "Default"}]}
                ),
            ),
            (
                "GET",
                "/data_sources/ds_real",
                _FakeResponse(200, {"object": "data_source", "id": "ds_real", "properties": {}}),
            ),
        ]
    )
    client = _client(fake)

    row = client.get_data_source("db_1")

    assert row["id"] == "ds_real"


def test_create_page_resolves_parent_data_source_id() -> None:
    fake = _FakeHTTP(
        [
            (
                "GET",
                "/data_sources/db_1",
                _FakeResponse(404, {"code": "object_not_found"}),
            ),
            (
                "GET",
                "/databases/db_1",
                _FakeResponse(
                    200, {"id": "db_1", "data_sources": [{"id": "ds_real", "name": "Default"}]}
                ),
            ),
            (
                "POST",
                "/pages",
                _FakeResponse(200, {"id": "page_1"}),
            ),
        ]
    )
    client = _client(fake)

    page = client.create_page(
        parent={"data_source_id": "db_1"},
        properties={"Name": {"title": [{"text": {"content": "x"}}]}},
    )

    assert page["id"] == "page_1"
    _, _, kwargs = fake.calls[-1]
    assert kwargs["json"]["parent"]["data_source_id"] == "ds_real"


def test_update_data_source_properties() -> None:
    fake = _FakeHTTP(
        [
            (
                "GET",
                "/data_sources/ds_1",
                _FakeResponse(200, {"object": "data_source", "id": "ds_1"}),
            ),
            (
                "PATCH",
                "/data_sources/ds_1",
                _FakeResponse(200, {"object": "data_source", "id": "ds_1"}),
            ),
        ]
    )
    client = _client(fake)

    client.update_data_source(
        "ds_1",
        properties={"Job URL": {"url": {}}},
    )

    _, _, kwargs = fake.calls[-1]
    assert kwargs["json"] == {"properties": {"Job URL": {"url": {}}}}


def test_archive_page_moves_page_to_trash() -> None:
    fake = _FakeHTTP(
        [
            (
                "PATCH",
                "/pages/page_1",
                _FakeResponse(200, {"id": "page_1", "in_trash": True}),
            ),
        ]
    )
    client = _client(fake)

    client.archive_page("page_1")

    _, _, kwargs = fake.calls[-1]
    assert kwargs["json"] == {"in_trash": True}


def test_archive_page_is_idempotent_when_notion_says_already_archived() -> None:
    fake = _FakeHTTP(
        [
            (
                "PATCH",
                "/pages/page_1",
                _FakeResponse(
                    400,
                    {
                        "object": "error",
                        "status": 400,
                        "code": "validation_error",
                        "message": (
                            "Can't edit block that is archived. You must unarchive "
                            "the block before editing."
                        ),
                    },
                ),
            ),
        ]
    )
    client = _client(fake)

    result = client.archive_page("page_1")

    assert result == {"id": "page_1", "archived": True, "in_trash": True}
    assert len(fake.calls) == 1


def test_request_retries_429_using_retry_after() -> None:
    sleeps: list[float] = []
    fake = _FakeHTTP(
        [
            ("POST", "/pages", _FakeResponse(429, {"error": "slow down"}, {"Retry-After": "7"})),
            ("POST", "/pages", _FakeResponse(200, {"id": "page_1"})),
        ]
    )
    client = _retrying_client(fake, sleeps)

    page = client.create_page(parent={"page_id": "root"}, properties={})

    assert page["id"] == "page_1"
    assert sleeps == [7.0]
    assert client.stats.rate_limited == 1
    assert client.stats.retries == 1


def test_request_retries_529_then_succeeds() -> None:
    sleeps: list[float] = []
    fake = _FakeHTTP(
        [
            ("PATCH", "/pages/page_1", _FakeResponse(529, {"error": "overloaded"})),
            ("PATCH", "/pages/page_1", _FakeResponse(200, {"id": "page_1"})),
        ]
    )
    client = _retrying_client(fake, sleeps)

    client.update_page("page_1", {"Status": {"select": {"name": "Fetched"}}})

    assert sleeps == [1.0]
    assert client.stats.overloaded_529 == 1
    assert client.stats.retries == 1


def test_request_classifies_final_5xx_failure() -> None:
    sleeps: list[float] = []
    fake = _FakeHTTP(
        [
            ("GET", "/pages/page_1", _FakeResponse(500, {"error": "bad"})),
            ("GET", "/pages/page_1", _FakeResponse(500, {"error": "bad"})),
            ("GET", "/pages/page_1", _FakeResponse(500, {"error": "bad"})),
            ("GET", "/pages/page_1", _FakeResponse(500, {"error": "bad"})),
            ("GET", "/pages/page_1", _FakeResponse(500, {"error": "bad"})),
        ]
    )
    client = _retrying_client(fake, sleeps)

    with pytest.raises(NotionTransientError):
        client.get_page("page_1")

    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert client.stats.server_errors == 5
    assert client.stats.failed_requests == 1


def test_request_raises_rate_limit_after_final_retry() -> None:
    sleeps: list[float] = []
    fake = _FakeHTTP(
        [
            ("GET", "/pages/page_1", _FakeResponse(429, {"error": "limited"})),
            ("GET", "/pages/page_1", _FakeResponse(429, {"error": "limited"})),
            ("GET", "/pages/page_1", _FakeResponse(429, {"error": "limited"})),
            ("GET", "/pages/page_1", _FakeResponse(429, {"error": "limited"})),
            ("GET", "/pages/page_1", _FakeResponse(429, {"error": "limited"})),
        ]
    )
    client = _retrying_client(fake, sleeps)

    with pytest.raises(NotionRateLimitError):
        client.get_page("page_1")

    assert client.stats.rate_limited == 5
    assert client.stats.failed_requests == 1
