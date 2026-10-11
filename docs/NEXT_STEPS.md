# Continuation and release gates

## Current state

Job Search Hub 0.1.0 is implemented in an independent Git repository with the `jobhub` executable and `job_search_hub` package. The neighboring original project was left intact; preservation checks confirmed 328 original file hashes, the original Git status, and all nine tracked PDFs unchanged. No original operational data, credentials, schedules, or Notion databases were used or modified.

Completed:

- Independent paths, TOML, OS credential storage, namespaced automation environment, package resources, and neutral defaults.
- Task command groups, focused inspection tables, root-based setup, diagnostics, and a shared refresh runner.
- Ten-database builder, five numbered workflow pages, focused linked views, navigation, home queues/failures, and restartable provisioning.
- Retained discovery, verification, deduplication, scoring, qualification evidence, reusable answers, organizations, notifications, capture, and metrics.
- Apply immediately creates Planned tracking independently of documents/answers; old writing, document generation, cover-letter, and submission implementations removed.
- Hash-based local PDF catalog, recommendation cache, resume-type suggestions, staged facts with explicit approval, and metadata-only sync.
- Read-only preview guards, temporary cache snapshots, independent scheduling, run locks, backups, restore, and one-way restartable imports.
- Public README, configuration/data/template/architecture guides, deterministic uv lock, isolated CI, and release-artifact checks.

Validated locally on macOS/Python 3.12:

- **315 isolated tests validated:** 314 passed in the full-suite run with three explicit live checks skipped; one additional benchmark-inspection test passed in the final targeted run. One third-party Starlette TestClient deprecation warning remains; it does not affect the assertions.
- Ruff lint and format checks passed.
- Python 3.11 syntax parsing passed for all Python files. Actual Linux/Python 3.11 execution is delegated to the CI matrix and is not claimed as locally validated.
- Wheel and sdist built. Artifact checks passed: required resources present; jobhub is the only package executable; no PDFs, original namespace/files, private data, or credential-shaped content included.
- The wheel installed through uv into isolated temporary tool directories and ran outside the checkout. Base install excluded Playwright, pypdf, FastAPI, and uvicorn. Help, version, status, source catalog, missing-service guidance, and schedule preview worked without creating user data.

## Next action: a disposable live hub

A live root URL and new integration token have not been supplied. The template exists as a tested builder, not a published duplicate-template link. Complete the following in a disposable Notion workspace before public release:

```bash
# From the Job Search Hub checkout
.venv/bin/jobhub setup --create-template
.venv/bin/jobhub doctor
.venv/bin/jobhub run --preview
.venv/bin/jobhub run
```

Setup should build under a **new blank page** shared with the integration and discover all ten databases. The token prompt is hidden. Enter synthetic role keywords and enable a real public source. If LM Studio is absent, doctor reports AI pending; load a chat model for scoring acceptance.

Check [the complete numbered workflow](NOTION_TEMPLATE.md#disposable-workspace-release-test): Notion and CLI preference equivalence, views, duplicate-template discovery, interruption recovery, unknown/closed/rate-limited posting behavior, invalid model output, fact/answer approval, metadata-only resume suggestions, Apply without a resume, and manual outcome tracking. Do not use a personal operational hub for this exercise.

Optional live schema/audit test requires explicitly supplied `JOBHUB_LIVE_NOTION_ROOT` and `JOBHUB_NOTION_TOKEN` in the test process, plus `JOBHUB_LIVE_NOTION=1`. All other unit tests stay isolated. This read-only schema test is supplementary; it does not replace the numbered user-flow check.

## Other release gates

1. Validate optional browser discovery/capture on the actual machine. Capture authentication is tested locally; Chromium/site behavior remains unverified.
2. Configure an SMTP test account and your notification address; run `jobhub notifications test` and check receipt/outbox retry. No live email has been sent by this implementation.
3. Render schedule preview, then explicitly enable/disable the new schedule in a disposable JOBHUB_HOME. Verify execution after the chosen interval. No launchd/systemd service was registered during implementation.
4. Capture genuine Notion images as specified in `docs/images/README.md`, embed the real home screenshot in README, and publish an empty template duplication link.
5. Run the macOS/Linux Python 3.11/3.12 CI matrix. Windows remains contingent on a separate locking, credential-store, path, installation, and scheduling design/validation.
6. Choose a distribution license and verify the desired PyPI name before publishing. No GitHub remote, package publication, or public template has been created.

Repeat release validation after changes:

```bash
uv sync --locked --extra dev --extra resume --extra capture
uv run --no-sync pytest
uv run --no-sync ruff check job_search_hub tests tools
uv run --no-sync ruff format --check job_search_hub tests tools
uv run --no-sync python -m build
uv run --no-sync python tools/check_release.py dist/*
```

Expected: isolated tests pass, lint/format clean, wheel/sdist build, both artifact checks pass. Live opt-in checks remain skipped unless explicitly configured.

## Known limitations and next engineering work

- View creation uses the separately versioned 2026-03-11 Views API; live rendering must confirm the contract. Ordinary data-source/page API stays pinned to 2025-09-03.
- Aggregate board health currently reports adapter-level failure if any employer target fails. Improve per-target health after live acceptance.
- Some advanced pipeline wrappers still construct their own clients. Further shared-client consolidation and changed-property writes can reduce API traffic.
- Notion reads/root discovery currently re-query schemas each CLI session. Cache discovery safely by root/schema version only after live schema changes are understood.
- Unknown posting/date conditions need human review. Source restrictions and employer-specific protocols may require manual capture or a new adapter.
- Backups are unencrypted. Notion exports preserve database rows/relations, not page body content, views, or edit history.
- Local locking coordinates one computer. Notion has no client uniqueness constraint; multi-computer concurrent writers are outside the default.
- Compensation comparisons do not convert currencies. Language detection increases installation size; replacing it needs equivalent filtering accuracy tests.
- Resume suggestions use only approved facts and available catalog metadata; no resumes or cover letters are generated.

Next Codex step: perform disposable live Notion acceptance using the new hub, fix concrete API/view issues if found, capture real screenshots, and update this single continuation document with the observed results.
