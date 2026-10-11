"""Discover loaded LM Studio models and map them to logical roles.

Roles:
- primary    : extraction, scoring, answer proposals, summarization (general LLM)
- embedding  : semantic dedupe, answer matching, retrieval
- review     : optional second-pass factual review
- triage     : optional fast first-pass classifier

Strategy:
1. If `MODEL_<ROLE>` is set in user TOML or the JOBHUB_* environment, use it verbatim
   after confirming the id is present in `/v1/models`.
2. If the value is blank or `auto`, rank loaded models for the requested role
   and pick the best available fit. The ranker tolerates version differences
   between the doc names and what LM Studio actually has loaded.
3. If a required role still cannot be filled, raise `ModelRoutingError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_search_hub.config import LMStudioSettings
from job_search_hub.logging_setup import get_logger
from job_search_hub.models.client import LMStudioClient

log = get_logger(__name__)


class ModelRoutingError(RuntimeError):
    pass


# Heuristics are used as a fallback signal inside the ranker below. Patterns
# are intentionally tolerant of version drift.
_HEURISTICS: dict[str, list[re.Pattern[str]]] = {
    "primary": [
        re.compile(r"qwen3?\.?5?[-/].*?(7|8|9|14)b", re.I),
        re.compile(r"qwen.*instruct", re.I),
        re.compile(r"llama-?3.*?(8|7)b", re.I),
        re.compile(r"mistral.*?(7|8)b", re.I),
        re.compile(r"gemma-?3n?-?e4b", re.I),
    ],
    "embedding": [
        re.compile(r"qwen3?-?embedding", re.I),
        re.compile(r"text-embedding-qwen", re.I),
        re.compile(r"nomic-embed-text", re.I),
        re.compile(r"bge-(small|base|large)", re.I),
        re.compile(r"embed", re.I),
    ],
    "review": [
        re.compile(r"gemma-?3n?-?e4b", re.I),
        re.compile(r"gemma-?(2|3)", re.I),
        re.compile(r"llama-?3", re.I),
    ],
    "triage": [
        re.compile(r"nemotron.*nano", re.I),
        re.compile(r"phi-?3.*mini", re.I),
        re.compile(r"qwen.*?(1\.5|3)b", re.I),
    ],
}

_AUTO_VALUES = {"auto", "best", "available", "auto-ranked"}


def is_auto_model_value(value: str) -> bool:
    """Return True when an env value asks the router to pick the best loaded model."""
    return value.strip().lower() in _AUTO_VALUES


REQUIRED_ROLES: tuple[str, ...] = ("primary",)
OPTIONAL_ROLES: tuple[str, ...] = ("embedding", "review", "triage")


@dataclass
class RoleMapping:
    role: str
    model_id: str
    source: str  # "override" or "auto-ranked"


class ModelRouter:
    def __init__(self, settings: LMStudioSettings, client: LMStudioClient | None = None) -> None:
        self.settings = settings
        self._client = client or LMStudioClient(settings)
        self._mapping: dict[str, RoleMapping] = {}
        self._available_ids: list[str] = []

    def discover(self) -> dict[str, RoleMapping]:
        """Resolve role -> model id. Logs every decision."""
        try:
            self._available_ids = self._client.list_model_ids()
        except Exception as exc:  # pragma: no cover - depends on local LM Studio
            raise ModelRoutingError(
                f"Failed to query {self.settings.base_url}/models: {exc}"
            ) from exc

        log.info(
            "LM Studio reports %d models loaded: %s", len(self._available_ids), self._available_ids
        )

        for role in REQUIRED_ROLES + OPTIONAL_ROLES:
            override = self.settings.overrides.get(role)
            if override:
                if override not in self._available_ids:
                    if role in REQUIRED_ROLES:
                        raise ModelRoutingError(
                            f"JOBHUB_MODEL_{role.upper()}={override!r} is not loaded in LM Studio. "
                            f"Available: {self._available_ids}"
                        )
                    log.warning(
                        "override MODEL_%s=%r is not loaded; skipping", role.upper(), override
                    )
                    continue
                self._mapping[role] = RoleMapping(role, override, "override")
                log.info("role=%s -> %s (override)", role, override)
                continue

            picked = self._pick(role)
            if picked:
                self._mapping[role] = RoleMapping(role, picked, "auto-ranked")
                log.info("role=%s -> %s (auto-ranked)", role, picked)
            elif role in REQUIRED_ROLES:
                raise ModelRoutingError(
                    f"No model in LM Studio matched role {role!r}. "
                    f"Available: {self._available_ids}. "
                    + (
                        f"Set MODEL_{role.upper()} in config.toml or the namespaced "
                        + "environment to override."
                    )
                )
            else:
                log.info("role=%s -> (none; optional)", role)

        return self._mapping

    def _pick(self, role: str) -> str | None:
        candidates = [
            (self._score_candidate(role, model_id), model_id) for model_id in self._available_ids
        ]
        ranked = [(score, model_id) for score, model_id in candidates if score is not None]
        if not ranked:
            return None
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked[0][1]

    def _score_candidate(self, role: str, model_id: str) -> float | None:
        lowered = model_id.lower()
        if role == "embedding":
            return _score_embedding_model(lowered)
        if role == "triage":
            return _score_triage_model(lowered)
        if role in {"primary", "review"}:
            return _score_general_model(lowered, role=role)
        return None

    def get(self, role: str) -> str:
        mapping = self._mapping.get(role)
        if not mapping:
            raise ModelRoutingError(
                f"role {role!r} not resolved. Call discover() first or set MODEL_{role.upper()}."
            )
        return mapping.model_id

    @property
    def available(self) -> list[str]:
        return list(self._available_ids)


def _parameter_billions(model_id: str) -> float:
    values = [float(match.group(1)) for match in re.finditer(r"(\d+(?:\.\d+)?)\s*b", model_id)]
    return max(values) if values else 0.0


def _matches_any(patterns: list[re.Pattern[str]], model_id: str) -> bool:
    return any(pattern.search(model_id) for pattern in patterns)


def _score_embedding_model(model_id: str) -> float | None:
    if not re.search(r"(embed|embedding|bge)", model_id):
        return None
    score = 20.0
    if "qwen" in model_id:
        score += 45.0
    if "nomic" in model_id:
        score += 35.0
    if "bge-large" in model_id:
        score += 30.0
    elif "bge-base" in model_id:
        score += 24.0
    elif "bge-small" in model_id:
        score += 18.0
    if "text-embedding" in model_id:
        score += 10.0
    score += min(_parameter_billions(model_id), 2.0)
    if _matches_any(_HEURISTICS["embedding"], model_id):
        score += 5.0
    return score


def _score_general_model(model_id: str, *, role: str) -> float | None:
    if re.search(r"(embed|embedding|bge|coder|code)", model_id):
        return None
    known_general = re.search(r"(qwen|gpt|oss|llama|mistral|gemma|phi|nemotron)", model_id)
    if not known_general:
        return None
    params = _parameter_billions(model_id)
    score = params * 2.0
    if "qwen" in model_id:
        score += 100.0
    if "gpt-oss" in model_id or "openai/gpt" in model_id:
        score += 96.0
    if "llama" in model_id:
        score += 82.0
    if "mistral" in model_id:
        score += 78.0
    if "gemma" in model_id:
        score += 68.0
    if "instruct" in model_id:
        score += 8.0
    if role == "review" and "gemma" in model_id:
        score += 8.0
    if _matches_any(_HEURISTICS[role], model_id):
        score += 5.0
    return score


def _score_triage_model(model_id: str) -> float | None:
    if re.search(r"(embed|embedding|bge|coder|code)", model_id):
        return None
    params = _parameter_billions(model_id)
    score = 0.0
    if "nemotron" in model_id or "phi" in model_id:
        score += 80.0
    if "qwen" in model_id:
        score += 65.0
    if "gemma" in model_id:
        score += 55.0
    if 0 < params <= 4:
        score += 40.0 - params
    elif params:
        score -= params
    if _matches_any(_HEURISTICS["triage"], model_id):
        score += 10.0
    return score if score > 0 else None
