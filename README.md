# Job Search Hub

**Find, evaluate, and track your next role through Notion and the CLI.**

A private job-search assistant that connects source discovery, evidence-based matching, reusable answers, and application tracking to a numbered Notion workspace. Your resumes stay on your computer. You apply through the employer's website yourself.

**Status:** local preview, version 0.1.0. The package and public Notion template have not been published. Live service acceptance is required before release. [Release checklist](docs/NEXT_STEPS.md).

## Your workflow

```text
1 — My search       Choose roles, keywords, exclusions, locations, and sources
2 — Review jobs     Review matches and choose Shortlist, Apply, or Skip
3 — Resolve questions   Review evidence and approve reusable answers
4 — Apply           Choose your existing resume and apply manually
5 — Track progress  Record applications, interviews, offers, and outcomes
```

The home page shows queue counts, refresh time, next actions, and failures. Full databases sit under **Data & diagnostics**. The CLI and Notion edit the same search records.

## Features

- Public ATS boards, feeds, careers pages, supported browser boards, alert mailboxes, and manual capture.
- Posting checks, canonical URLs, deterministic deduplication, rate controls, and source health.
- Scoring explanations and qualification questions grounded in approved profile facts.
- Reusable answers with human approval; exact matching works without embeddings.
- Organization records, application tracking, optional notifications, and local metrics.
- A private PDF library, existing-resume recommendations, and reusable resume-type suggestions.
- Preview, diagnostics, backups, restore, and optional restartable imports.
- Explicit refresh, with optional independent macOS/Linux scheduling.

Choosing **Apply** creates a **Planned** application on the next refresh, or immediately through `jobhub jobs decide`. Missing resumes and unanswered questions remain visible as advisory tasks. Discovery and tracking work without LM Studio; AI tasks remain pending.

Resume drafting, cover-letter workflows, document generation, and automatic application submission are excluded.

## Quick start: current source checkout

Requirements: macOS or Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/), a private Notion page, and an internal Notion integration with access to it. Python 3.12 is recommended; the package supports 3.11+. LM Studio, browser discovery, PDF extraction, capture, and email are optional.

From this repository:

```bash
uv tool install --python 3.12 .
jobhub setup --create-template
jobhub doctor
jobhub run --preview
jobhub run
```

If `jobhub` is outside your shell's PATH, use `uv tool update-shell` and reopen the shell. uv installs the tool in an isolated environment and can supply Python. [uv tool documentation](https://docs.astral.sh/uv/guides/tools/).

For the **first hub**, create a blank private Notion page, grant the integration access, and paste that page's URL when prompted. `--create-template` builds the ten databases, focused views, and numbered pages. It refuses to modify a populated unrelated page and can resume interrupted initialization. [Notion setup](docs/NOTION_TEMPLATE.md).

For a previously built or duplicated hub, run `jobhub setup` without `--create-template`. Only the root URL and integration token are needed; database IDs are discovered beneath the root. Secrets use hidden prompts and the operating system credential store. If no secure store is available, use namespaced environment variables; there is no plaintext fallback.

`doctor` checks schemas and sends a tiny actual chat request to LM Studio. With no model server, it reports AI as unavailable and exits with status 1; the discovery/tracking workflow remains usable. Bare `jobhub` shows help and a recommended next action.

### Installation after publication

These commands become available **after the first PyPI release**:

```bash
uv tool install --python 3.12 notion-job-search-hub
jobhub setup
jobhub doctor
jobhub run
```

pipx alternative, with Python 3.12 already installed:

```bash
pipx install --python python3.12 .                 # current checkout
# pipx install --python python3.12 notion-job-search-hub  # after publication
```

### Optional installation extras

```bash
uv tool install --python 3.12 '.[resume,capture,browser]'
```

For an existing uv tool installation, add `--reinstall`. The `resume` extra enables PDF fact extraction; cataloging PDFs requires no extractor. `capture` enables the authenticated local HTTP endpoint. `browser` enables Playwright; Chromium must also be installed in that tool's Python environment. See [optional services](docs/CONFIGURATION.md). Language detection is a base dependency and accounts for much of the initial download.

## Everyday CLI use

```bash
jobhub search keywords add "platform engineer" --profile technical --kind role
jobhub search keywords add "Python" --profile technical --kind skill
jobhub search keywords add "climate" --profile technical --kind mission
jobhub search keywords add "unwanted term" --profile technical --kind exclude
jobhub sources add --type greenhouse --target example-company
jobhub sources add --type rss --url https://example.org/jobs.xml
jobhub search list
jobhub sources list
jobhub search preview
jobhub run --preview
jobhub run
jobhub jobs list
jobhub jobs list --applications
jobhub jobs review PAGE_ID
jobhub jobs decide OPPORTUNITY_ID Apply
jobhub jobs track APPLICATION_ID Submitted
jobhub status
```

Employer identifiers and URLs above are examples; replace them with real boards/feeds. The default profile is **My search**. A named profile is created when you first edit its keywords. Disable unused profiles in Notion. `search preview` shows redacted request plans; `sources check` makes bounded read-only requests. List commands show tables; add `--json` for complete Notion records. Page IDs are shown in the tables and can also be copied from a Notion URL.

`run --preview` uses temporary copies of local caches, makes read-only service requests, and reports proposed changes. It does not write to Notion, deliver notifications, or modify your documents. Newly proposed jobs are not remotely persisted, so their later scoring is deferred to a real run. Conflicting preview/write flags are rejected.

### Resumes and approved facts

```bash
jobhub resumes add ~/Documents/my-resume.pdf --title "Backend engineering" --tag backend
jobhub resumes list
jobhub resumes sync
jobhub resumes suggest --preview
jobhub resumes suggest
jobhub profile extract DOCUMENT_ID
jobhub profile pending
jobhub profile approve FACT_ID
```

Arbitrary PDF filenames are accepted. Content hashes supply stable document IDs and deduplicate copies. Extraction is optional; `profile extract --model DOCUMENT_ID` can use LM Studio. Extracted facts stay local until explicitly approved. Recommendations contain a title, rationale, and relevant jobs, never drafted content or build instructions. PDFs and local paths are never uploaded to Notion. [Data ownership](docs/DATA.md).

### Answers, notifications, and scheduling

```bash
jobhub answers list
jobhub answers match "Are you authorized to work here?"
jobhub answers approve ANSWER_ID --value "Your verified answer"
jobhub notifications list
jobhub schedule enable --hours 6 --preview
jobhub schedule enable --hours 6
jobhub schedule status
jobhub schedule disable
```

Approval is your decision. Notifications send only to your configured notification address; they never contact employers. Scheduling is off until enabled, runs while the computer is available, and uses its own service identity. A local lock prevents overlapping refreshes and CLI writes.

### Maintenance and recovery

```bash
jobhub maintenance validate
jobhub maintenance posting-check
jobhub maintenance dedupe                  # preview
jobhub maintenance dedupe --apply          # explicit write
jobhub maintenance backup ~/jobhub-backup.zip
jobhub maintenance backup ~/jobhub-local.zip --local-only
jobhub maintenance restore ~/jobhub-local.zip --destination ~/jobhub-restored
jobhub maintenance restore ~/jobhub-local.zip --destination ~/jobhub-restored --apply
jobhub maintenance import ~/Documents/selected-resumes       # preview
```

Backups exclude credentials and logs. Local restore requires an empty destination. A Notion snapshot is included in a normal backup but must be separately imported into a different, already configured hub to restore remote records. Imports are one-way, deduplicated, and restartable; they never modify their source or copy credentials. Local storage and backups are **not automatically encrypted**.

## Configuration and architecture

Operational settings live in `config.toml` under standard user directories; they are independent of the checkout. `JOBHUB_HOME` selects an alternative private location. Only `JOBHUB_*` environment variables are read; a working-directory `.env` is not automatically loaded. Keywords, source targets, enabled state, approved facts, and application decisions live in Notion, not parallel local preference files.

LM Studio is the only implemented model provider. A token is needed only if its server authentication is enabled. [LM Studio authentication](https://lmstudio.ai/docs/developer/core/authentication). Embeddings are optional. [Configuration guide](docs/CONFIGURATION.md) · [Architecture](docs/ARCHITECTURE.md).

## Development

```bash
uv sync --locked --extra dev --extra resume --extra capture
uv run --no-sync jobhub --help
uv run --no-sync pytest
uv run --no-sync ruff check job_search_hub tests tools
uv run --no-sync ruff format --check job_search_hub tests tools
uv run --no-sync python -m build
uv run --no-sync python tools/check_release.py dist/*
```

Tests use synthetic data and an isolated Notion fixture. Live checks require explicit opt-in and a disposable workspace; never point them at an active personal hub. CI covers macOS/Linux on Python 3.11/3.12. Current local validation and remaining release gates are recorded in [NEXT_STEPS](docs/NEXT_STEPS.md).

## Troubleshooting and limitations

- **Notion cannot find a database:** grant the integration access to the root and its descendants, then rerun setup. Keep canonical database titles. See [template repair](docs/NOTION_TEMPLATE.md).
- **Credentials cannot be saved:** unlock/configure the OS credential store or export the required `JOBHUB_*` variable in the process running jobhub. `.env` is not a secret store.
- **LM Studio is unavailable:** start its server and load a chat-capable model. `setup --model MODEL_ID` pins a model. Discovery and tracking continue; scores remain pending.
- **Browser source is blocked:** install the optional browser runtime where appropriate. Restricted sites remain manual/alert sources. No bypass or automatic submission is attempted.
- **Another refresh is running:** wait; the lock is released when that process exits. Force-killing a process does not leave a permanent lock.
- **Job availability or posting date is unknown:** review the employer page. Unknown data does not count as a verified posting or invented date.
- **Cross-currency searches:** compensation thresholds compare numeric amounts without currency conversion. Leave thresholds unset when comparing different currencies.

Windows, live service behavior, the public duplicate-template link, representative screenshots, and PyPI publication remain release gates. Hosted models, interview preparation, follow-ups, and search-effectiveness reporting are future extensions. Resume and cover-letter generation remain excluded.

## Contributing and release

Keep private state out of fixtures and release artifacts. Use isolated tests and task-based CLI commands. A distribution license must be chosen by the owner before public release; no license grant is asserted here. [Real screenshot capture instructions](docs/images/README.md).
