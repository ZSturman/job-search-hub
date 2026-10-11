"""Dry-run discovery previews for public job sources."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import httpx

from job_search_hub.config import Settings
from job_search_hub.ingest.base import Adapter, RawPosting, SearchRequest, SearchResult
from job_search_hub.logging_setup import get_logger
from job_search_hub.pipeline.discovery import select_postings
from job_search_hub.pipeline.normalize import fingerprint
from job_search_hub.source_catalog import load_source_catalog

log = get_logger(__name__)

SECRET_KEYS = {
    "authorization_key",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "app_key",
    "appkey",
    "key",
    "password",
    "token",
    "user_agent",
}


def redact_mapping(values: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in (values or {}).items():
        normalized = str(key).lower().replace("-", "_")
        if normalized in SECRET_KEYS or any(part in normalized for part in ("secret", "token")):
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def redacted_request(request: SearchRequest) -> SearchRequest:
    return replace(
        request,
        params=redact_mapping(request.params),
        headers=redact_mapping(request.headers),
    )


def dedupe_postings(postings: Iterable[RawPosting]) -> list[RawPosting]:
    seen: set[str] = set()
    out: list[RawPosting] = []
    for posting in postings:
        key = fingerprint(posting)
        if key in seen:
            continue
        seen.add(key)
        out.append(posting)
    return out


def result_from_postings(
    request: SearchRequest,
    *,
    http_status: int | None,
    elapsed_ms: float | None,
    postings: list[RawPosting],
    examples: int,
    payload: dict[str, Any] | None = None,
) -> SearchResult:
    deduped = dedupe_postings(postings)
    return SearchResult(
        request=redacted_request(request),
        status="ok",
        configured=True,
        http_status=http_status,
        elapsed_ms=elapsed_ms,
        raw_count=len(postings),
        deduped_count=len(deduped),
        accepted_count=len(deduped),
        examples=deduped[:examples],
        postings=deduped,
        payload={**(payload or {}), "_examples_limit": examples},
    )


def skipped_result(
    *,
    source: str,
    source_adapter: str,
    reason: str,
    endpoint: str = "",
    target: str = "",
    target_kind: str = "",
    keyword: str = "",
    pass_name: str = "",
) -> SearchResult:
    return SearchResult(
        request=SearchRequest(
            source=source,
            source_adapter=source_adapter,
            endpoint=endpoint,
            target=target,
            target_kind=target_kind,
            keyword=keyword,
            pass_name=pass_name,
        ),
        status="skipped",
        configured=False,
        skip_reason=reason,
    )


def error_result(
    request: SearchRequest,
    *,
    error: str,
    http_status: int | None = None,
    elapsed_ms: float | None = None,
) -> SearchResult:
    return SearchResult(
        request=redacted_request(request),
        status="error",
        configured=True,
        http_status=http_status,
        elapsed_ms=elapsed_ms,
        error=f"Source request failed (HTTP {http_status}). Run jobhub sources check."
        if http_status
        else "Source request failed. Check connectivity and source configuration.",
    )


def timed_get(
    client: httpx.Client,
    request: SearchRequest,
) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    resp = client.get(request.endpoint, params=request.params, headers=request.headers)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return resp, elapsed_ms


def timed_post_json(
    client: httpx.Client,
    request: SearchRequest,
    *,
    json_payload: dict[str, Any],
) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    resp = client.post(
        request.endpoint,
        params=request.params,
        headers=request.headers,
        json=json_payload,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return resp, elapsed_ms


def adapter_keys_for_sources(settings: Settings, sources: list[str] | None) -> list[str] | None:
    if not sources:
        return None
    from job_search_hub.ingest.registry import REGISTRY

    catalog = load_source_catalog(settings)
    out: list[str] = []
    for source in sources:
        normalized = source.strip().lower()
        if normalized in REGISTRY:
            out.append(normalized)
            continue
        for entry in catalog:
            names = {entry.key.lower(), entry.name.lower(), entry.adapter_key.lower()}
            if normalized in names and entry.adapter_key:
                out.append(entry.adapter_key)
                break
    return out or sources


def build_preview_adapters(settings: Settings, sources: list[str] | None = None) -> list[Adapter]:
    from job_search_hub.ingest.registry import build_adapters

    keys = adapter_keys_for_sources(settings, sources)
    return build_adapters(settings, only=keys)


def run_preview(
    settings: Settings,
    *,
    sources: list[str] | None = None,
    examples: int = 3,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    for adapter in build_preview_adapters(settings, sources=sources):
        try:
            adapter_results = list(adapter.preview(examples=examples))
        except Exception as exc:
            log.exception("dry-run: adapter %s failed", adapter.key)
            adapter_results = [
                error_result(
                    SearchRequest(
                        source=adapter.label,
                        source_adapter=adapter.key,
                        endpoint="",
                        target=adapter.key,
                    ),
                    error=str(exc),
                )
            ]
        ok_results = [result for result in adapter_results if result.status == "ok"]
        if ok_results:
            all_postings = [posting for result in ok_results for posting in result.postings]
            selection = select_postings(all_postings, settings)
            accepted_ids = {fingerprint(posting) for posting in selection.accepted}
            assigned: set[str] = set()
            for result in ok_results:
                accepted = []
                for posting in result.postings:
                    fp = fingerprint(posting)
                    if fp in accepted_ids and fp not in assigned:
                        accepted.append(posting)
                        assigned.add(fp)
                result.accepted_count = len(accepted)
                result.examples = accepted[:examples]
            ok_results[0].rejection_counts = dict(selection.stats.reasons)
        results.extend(adapter_results)
        log_preview_results(adapter_results)
    return results


def planned_searches(
    settings: Settings,
    *,
    sources: list[str] | None = None,
) -> list[SearchRequest | SearchResult]:
    items: list[SearchRequest | SearchResult] = []
    for adapter in build_preview_adapters(settings, sources=sources):
        planned = list(adapter.plan_searches())
        if planned:
            items.extend(redacted_request(p) for p in planned)
        else:
            skipped = list(adapter.preview(examples=0))
            items.extend(
                skipped
                or [
                    skipped_result(
                        source=adapter.label,
                        source_adapter=adapter.key,
                        reason="not configured",
                    )
                ]
            )
    return items


def _request_label(request: SearchRequest) -> str:
    pieces = [f"[{request.source_adapter}]"]
    if request.target_kind and request.target:
        pieces.append(f"{request.target_kind}={request.target}")
    elif request.target:
        pieces.append(f"target={request.target}")
    if request.pass_name:
        pieces.append(f"pass={request.pass_name}")
    if request.keyword:
        pieces.append(f'keyword="{request.keyword}"')
    return " ".join(pieces)


def _format_example(posting: RawPosting) -> str:
    title = posting.role_title or "(untitled)"
    company = posting.company or "(unknown organization)"
    location = posting.location or "Unknown location"
    source = posting.source or posting.source_adapter
    url = posting.url or "-"
    return f"  - {title} | {company} | {location} | {source}\n    {url}"


def format_result(result: SearchResult) -> str:
    request = result.request
    label = _request_label(request)
    if result.status == "skipped":
        return f"{label} skipped: {result.skip_reason or 'not configured'}"
    if result.status == "error":
        http = f" status={result.http_status}" if result.http_status else ""
        elapsed = f" elapsed_ms={result.elapsed_ms:.0f}" if result.elapsed_ms is not None else ""
        return f"{label}{http}{elapsed} error: {result.error or 'unknown error'}"

    params = f" params={request.params}" if request.params else ""
    http = f" status={result.http_status}" if result.http_status else ""
    elapsed = f" elapsed_ms={result.elapsed_ms:.0f}" if result.elapsed_ms is not None else ""
    line = (
        f"{label}{http} jobs={result.raw_count} deduped={result.deduped_count} "
        f"accepted={result.accepted_count}"
        f"{elapsed}{params}"
    )
    if result.rejection_counts:
        rejected = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(result.rejection_counts.items())
            if count
        )
        if rejected:
            line += f" rejected[{rejected}]"
    examples = "\n".join(_format_example(p) for p in result.examples)
    return line if not examples else f"{line}\n{examples}"


def format_preview_results(results: list[SearchResult]) -> str:
    lines = [format_result(r) for r in results]
    by_source: dict[str, dict[str, int]] = {}
    aggregate_seen: list[RawPosting] = []
    for r in results:
        key = r.request.source_adapter
        row = by_source.setdefault(
            key,
            {
                "requests": 0,
                "raw": 0,
                "deduped": 0,
                "accepted": 0,
                "errors": 0,
                "skipped": 0,
                "non_english": 0,
                "excluded_title": 0,
                "irrelevant": 0,
                "duplicate": 0,
                "cap_overflow": 0,
            },
        )
        row["requests"] += 1
        row["raw"] += r.raw_count
        row["deduped"] += r.deduped_count
        row["accepted"] += r.accepted_count
        for reason, count in r.rejection_counts.items():
            row[reason] = row.get(reason, 0) + count
        row["errors"] += 1 if r.status == "error" else 0
        row["skipped"] += 1 if r.status == "skipped" else 0
        aggregate_seen.extend(r.examples)

    aggregate_deduped = dedupe_postings(aggregate_seen)
    lines.append("")
    lines.append("Per-source summary:")
    for source, row in sorted(by_source.items()):
        lines.append(
            f"- {source}: requests={row['requests']} raw={row['raw']} "
            f"deduped={row['deduped']} accepted={row['accepted']} "
            f"non_english={row['non_english']} excluded_title={row['excluded_title']} "
            f"irrelevant={row['irrelevant']} duplicate={row['duplicate']} "
            f"cap_overflow={row['cap_overflow']} skipped={row['skipped']} "
            f"errors={row['errors']}"
        )
    lines.append(
        "Aggregate totals: "
        f"requests={len(results)} raw={sum(r.raw_count for r in results)} "
        f"deduped={sum(r.deduped_count for r in results)} "
        f"accepted={sum(r.accepted_count for r in results)} "
        f"example_deduped={len(aggregate_deduped)} "
        f"skipped={sum(1 for r in results if r.status == 'skipped')} "
        f"errors={sum(1 for r in results if r.status == 'error')}"
    )
    return "\n".join(lines)


def _request_summary_value(request: SearchRequest) -> str:
    if request.keyword:
        prefix = f"{request.pass_name}: " if request.pass_name else ""
        return f"{prefix}{request.keyword}"
    if request.target:
        return request.target
    if request.endpoint:
        return request.endpoint
    return request.source_adapter


def _limited_values(values: list[str], *, limit: int = 8) -> str:
    unique = list(dict.fromkeys(v for v in values if v))
    shown = unique[:limit]
    suffix = f" (+{len(unique) - limit} more)" if len(unique) > limit else ""
    return ", ".join(shown) + suffix


def format_planned_searches(items: list[SearchRequest | SearchResult]) -> str:
    requests = [item for item in items if isinstance(item, SearchRequest)]
    skipped = [item for item in items if isinstance(item, SearchResult)]
    grouped: dict[str, list[SearchRequest]] = {}
    for request in requests:
        grouped.setdefault(request.source_adapter, []).append(request)

    lines: list[str] = [
        "PLANNED JOB SEARCHES",
        "",
        "Summary:",
        f"- Planned requests: {len(requests)}",
        f"- Skipped sources: {len(skipped)}",
    ]
    if grouped:
        lines.append("- Requests by source:")
        for source, source_requests in sorted(grouped.items()):
            lines.append(f"  - {source}: {len(source_requests)}")
    lines.append("")

    if skipped:
        lines.append("Skipped or unconfigured sources:")
        for item in skipped:
            lines.append(f"  - {format_result(item)}")
        lines.append("")

    if not grouped:
        return "\n".join(lines).rstrip()

    lines.append("Search groups:")
    for source, source_requests in sorted(grouped.items()):
        values = [_request_summary_value(request) for request in source_requests]
        lines.append(f"- {source}: {len(source_requests)} request(s) | {_limited_values(values)}")
    return "\n".join(lines).rstrip()


def log_preview_results(results: list[SearchResult]) -> None:
    for result in results:
        request = result.request
        if result.status == "skipped":
            log.info("%s skipped: %s", _request_label(request), result.skip_reason)
            continue
        if result.status == "error":
            log.warning("%s error: %s", _request_label(request), result.error)
            continue
        log.info(
            "%s status=%s jobs=%d deduped=%d elapsed_ms=%s params=%s",
            _request_label(request),
            result.http_status,
            result.raw_count,
            result.deduped_count,
            f"{result.elapsed_ms:.0f}" if result.elapsed_ms is not None else "-",
            request.params,
        )
        for posting in result.examples:
            log.info(
                "%s example: %s | %s | %s | %s",
                _request_label(request),
                posting.role_title,
                posting.company,
                posting.location or "Unknown location",
                posting.url,
            )
