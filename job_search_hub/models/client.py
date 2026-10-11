"""Minimal OpenAI-compatible client for LM Studio.

We only depend on httpx (no openai SDK) to keep the surface small and
explicit. Supports listing models, chat completions (with optional JSON
response_format), and embeddings.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from jsonschema import ValidationError, validate
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from job_search_hub.config import LMStudioSettings
from job_search_hub.db import Store
from job_search_hub.logging_setup import get_logger

log = get_logger(__name__)


_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(httpx.TransportError),
)


class StructuredOutputError(RuntimeError):
    pass


def _lmstudio_json_schema(schema: Any) -> Any:
    """Return a JSON Schema variant accepted by LM Studio's grammar parser."""
    if isinstance(schema, list):
        return [_lmstudio_json_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    converted = {key: _lmstudio_json_schema(value) for key, value in schema.items()}
    type_value = converted.get("type")
    if isinstance(type_value, list):
        non_null_types = [item for item in type_value if isinstance(item, str) and item != "null"]
        if non_null_types:
            converted["type"] = non_null_types[0]
        else:
            converted.pop("type", None)
        enum_value = converted.get("enum")
        if isinstance(enum_value, list):
            converted["enum"] = [item for item in enum_value if item is not None]
    return converted


class LMStudioClient:
    def __init__(
        self,
        settings: LMStudioSettings,
        *,
        timeout: float = 120.0,
        store: Store | None = None,
        audit_mode: str = "off",
    ) -> None:
        self.settings = settings
        self.store = store
        self.audit_mode = audit_mode
        self._call_state = threading.local()
        self._client = httpx.Client(
            base_url=settings.base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}),
            },
        )

    @property
    def last_call_id(self) -> int | None:
        return getattr(self._call_state, "last_call_id", None)

    @last_call_id.setter
    def last_call_id(self, value: int | None) -> None:
        self._call_state.last_call_id = value

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LMStudioClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @_RETRY
    def _get(self, path: str) -> dict[str, Any]:
        resp = self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    @_RETRY
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.post(path, json=body)
        if resp.status_code >= 400:
            detail = f": {resp.text[:500]}" if self.audit_mode == "full" else ""
            raise RuntimeError(f"LM Studio {path} -> HTTP {resp.status_code}{detail}")
        return resp.json()

    def _preview_messages(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if self.audit_mode == "off":
            return h, ""
        if self.audit_mode != "full":
            raw = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", raw)
            raw = re.sub(
                r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", "[phone]", raw
            )
        return h, raw[:1200]

    def _preview_response(self, text: str) -> str:
        if self.audit_mode == "off":
            return ""
        if self.audit_mode != "full":
            text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
            text = re.sub(
                r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b", "[phone]", text
            )
        return text[:1200]

    def _record_call(
        self,
        *,
        stage: str,
        role: str,
        task: str,
        model: str,
        messages: list[dict[str, str]],
        response_text: str,
        json_valid: bool | None,
        fallback_used: bool,
        latency_ms: float | None,
        error: str,
    ) -> None:
        self.last_call_id = None
        if self.store is None:
            return
        prompt_hash, prompt_preview = self._preview_messages(messages)
        self.last_call_id = self.store.record_llm_call(
            stage=stage,
            role=role,
            task=task,
            model=model,
            prompt_hash=prompt_hash,
            prompt_preview=prompt_preview,
            response_preview=self._preview_response(response_text),
            json_valid=json_valid,
            fallback_used=fallback_used,
            latency_ms=latency_ms,
            error=error if self.audit_mode == "full" else ("model request failed" if error else ""),
        )

    def list_model_ids(self) -> list[str]:
        data = self._get("/models")
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        stage: str = "",
        role: str = "",
        task: str = "",
    ) -> str:
        schema_payload = (
            json_schema.get("schema")
            if (
                isinstance(json_schema, dict)
                and "schema" in json_schema
                and "type" not in json_schema
            )
            else json_schema
        )
        schema_payload = _lmstudio_json_schema(schema_payload)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured", "schema": schema_payload, "strict": True},
            }
        started = time.perf_counter()
        text = ""
        error = ""
        try:
            data = self._post("/chat/completions", body)
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError(f"LM Studio returned no choices: {data}")
            text = choices[0].get("message", {}).get("content", "") or ""
            return text
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self._record_call(
                stage=stage,
                role=role,
                task=task,
                model=model,
                messages=messages,
                response_text=text,
                json_valid=None,
                fallback_used=False,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                error=error
                if self.audit_mode == "full"
                else ("model request failed" if error else ""),
            )

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_schema: dict,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        stage: str = "",
        role: str = "",
        task: str = "",
        post_validator: Callable[[Any], None] | None = None,
    ) -> Any:
        prompt_hash, _prompt_preview = self._preview_messages(messages)
        if self.store is not None and task:
            cached_error = self.store.cached_llm_failure(
                task=task,
                model=model,
                prompt_hash=prompt_hash,
            )
            if cached_error:
                raise StructuredOutputError(
                    f"cached LM Studio failure for task={task}; retry deferred: {cached_error}"
                )
        original_schema = (
            json_schema.get("schema")
            if (
                isinstance(json_schema, dict)
                and "schema" in json_schema
                and "type" not in json_schema
            )
            else json_schema
        )
        schema_payload = _lmstudio_json_schema(original_schema)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "structured", "schema": schema_payload, "strict": True},
            },
        }
        # LM Studio's OpenAI-compatible endpoint honors reasoning_effort for
        # Qwen reasoning models. Without this, the model can put the entire
        # schema-constrained response in reasoning_content and leave content empty.
        if re.search(
            r"(?:^|[/_-])(?:qwen3(?:\.5)?|nemotron)(?:[/_.-]|$)",
            model,
            re.I,
        ):
            body["reasoning_effort"] = "none"
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        attempts = 2
        for attempt in range(1, attempts + 1):
            attempt_body = dict(body)
            if attempt > 1:
                attempt_body["temperature"] = 0.0
            started = time.perf_counter()
            text = ""
            error = ""
            valid = False
            try:
                data = self._post("/chat/completions", attempt_body)
                choices = data.get("choices") or []
                if not choices:
                    raise StructuredOutputError("LM Studio returned no chat choices")
                choice = choices[0]
                text = choice.get("message", {}).get("content", "") or ""
                if not text.strip():
                    finish_reason = choice.get("finish_reason") or "unknown"
                    usage = data.get("usage") or {}
                    raise StructuredOutputError(
                        "LM Studio returned empty content "
                        f"(finish_reason={finish_reason}, usage={usage})"
                    )
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise StructuredOutputError(
                        "LM Studio returned malformed JSON "
                        f"(line={exc.lineno}, column={exc.colno}, char={exc.pos})"
                        + (f": {text[:500]}" if self.audit_mode == "full" else "")
                    ) from (exc if self.audit_mode == "full" else None)
                try:
                    validate(instance=parsed, schema=original_schema)
                    if post_validator is not None:
                        post_validator(parsed)
                except ValidationError as exc:
                    path = ".".join(str(part) for part in exc.absolute_path)
                    location = f" at {path}" if path else ""
                    raise StructuredOutputError(
                        f"LM Studio returned schema-invalid JSON{location}"
                        + (f": {exc.message}" if self.audit_mode == "full" else "")
                    ) from (exc if self.audit_mode == "full" else None)
                except StructuredOutputError:
                    raise
                except Exception as exc:
                    raise StructuredOutputError(
                        "LM Studio returned logically invalid JSON"
                        + (f": {exc}" if self.audit_mode == "full" else "")
                    ) from (exc if self.audit_mode == "full" else None)
                valid = True
                if self.store is not None and task:
                    self.store.clear_llm_failure(
                        task=task,
                        model=model,
                        prompt_hash=prompt_hash,
                    )
                return parsed
            except StructuredOutputError as exc:
                error = str(exc)
                if attempt < attempts:
                    log.warning(
                        "structured LM Studio response failed for task=%s "
                        "(attempt %d/%d); retrying at temperature 0: %s",
                        task or "(unspecified)",
                        attempt,
                        attempts,
                        str(exc)[:500],
                    )
                    continue
                if self.store is not None and task:
                    self.store.record_llm_failure(
                        task=task,
                        model=model,
                        prompt_hash=prompt_hash,
                        error=error
                        if self.audit_mode == "full"
                        else ("model request failed" if error else ""),
                    )
                raise StructuredOutputError(
                    f"{exc} (structured response failed after {attempts} attempts)"
                ) from (exc if self.audit_mode == "full" else None)
            except Exception as exc:
                error = str(exc)
                if self.store is not None and task:
                    self.store.record_llm_failure(
                        task=task,
                        model=model,
                        prompt_hash=prompt_hash,
                        error=error
                        if self.audit_mode == "full"
                        else ("model request failed" if error else ""),
                    )
                raise
            finally:
                self._record_call(
                    stage=stage,
                    role=role,
                    task=task,
                    model=model,
                    messages=messages,
                    response_text=text,
                    json_valid=valid,
                    fallback_used=not valid,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    error=error
                    if self.audit_mode == "full"
                    else ("model request failed" if error else ""),
                )
        raise AssertionError("structured response retry loop exited unexpectedly")

    def embed(
        self,
        *,
        model: str,
        inputs: list[str],
        stage: str = "",
        role: str = "embedding",
        task: str = "",
    ) -> list[list[float]]:
        body = {"model": model, "input": inputs}
        started = time.perf_counter()
        error = ""
        count = 0
        try:
            data = self._post("/embeddings", body)
            items = data.get("data") or []
            items.sort(key=lambda x: x.get("index", 0))
            vectors = [item.get("embedding", []) for item in items]
            count = len(vectors)
            return vectors
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            messages = [{"role": "user", "content": "\n".join(inputs[:3])}]
            self._record_call(
                stage=stage,
                role=role,
                task=task,
                model=model,
                messages=messages,
                response_text=f"{count} embedding vector(s)",
                json_valid=None,
                fallback_used=False,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                error=error
                if self.audit_mode == "full"
                else ("model request failed" if error else ""),
            )
