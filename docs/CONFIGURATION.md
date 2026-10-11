# Configuration and integrations

## Ownership

Notion's Search Profiles and Sources are the editable configuration. Use **1 — My search** or the equivalent `jobhub search ...` / `jobhub sources ...` commands. CLI changes write those records; refresh derives a transient local snapshot. Scoring Criteria controls weights, directions, methods, and instructions.

`config.toml` contains connections and execution settings. Inspect its location with `jobhub doctor`. macOS defaults to `~/Library/Application Support/job-search-hub/`; Linux uses the platform's XDG config/data directories. `JOBHUB_HOME=/absolute/private/location` creates `config/` and `data/` there.

Example nonsecret TOML:

```toml
[settings]
LMSTUDIO_BASE_URL = "http://localhost:1234/v1"
MODEL_PRIMARY = "your-loaded-chat-model"
SCORE_WORKERS = 2
SMTP_HOST = "smtp.example.org"
SMTP_PORT = 587
SMTP_USERNAME = "your-account"
SMTP_FROM = "you@example.org"
SMTP_TO = "you@example.org"
```

Do not put tokens or passwords in this file. `jobhub setup --credential NAME` stores a hidden value under credential service **job-search-hub**. Supported names are listed in command help. Secure OS backends are required; plaintext keyring plugins are rejected. Automation may inject `JOBHUB_NAME` into its environment instead.

The token is entered interactively during setup. Noninteractive setup reads `JOBHUB_NOTION_TOKEN`. Existing non-prefixed variables and old project credentials are ignored. `.env.example` documents names; it is not automatically loaded.

## LM Studio

1. Install LM Studio and start its local server.
2. Load a model that supports chat, not only embeddings.
3. Run `jobhub setup --lmstudio-url http://localhost:1234/v1 --model MODEL_ID`.
4. If server authentication is enabled, run `jobhub setup --credential LMSTUDIO_API_KEY`.
5. Run `jobhub doctor`; it sends a small chat request to validate actual capability.

You can set `--embedding-model MODEL_ID` separately. Failed or absent embeddings fall back to exact answer matching and deterministic deduplication. Invalid structured responses are retried once and remain pending without publishing degraded scores. A model-client protocol provides an extension boundary; hosted providers are not implemented.

Default model audit records contain metadata and hashes, with no prompt/response excerpts. `LLM_AUDIT_MODE = "full"` explicitly enables content logging for troubleshooting; this may contain private data. `jobhub maintenance model-log` inspects local metadata.

## Sources

`jobhub sources catalog` lists supported protocols. Add targets in Notion or through the CLI:

| Type | Target | Credentials/capability |
|---|---|---|
| greenhouse / lever / ashby / recruitee / smartrecruiters | Employer's board identifier | Public ATS API |
| rss | Feed URL | Read-only feed discovery |
| careers | Public careers URL | Bounded direct discovery; may require browser extra |
| watchlist | Public page URL | Change/reachability checks and manual capture |
| remotive / arbeitnow | Empty | Public API; local keyword filtering |
| adzuna | Country code, e.g. `gb` | ADZUNA_APP_ID in TOML; ADZUNA_APP_KEY in credential store |
| jooble | Empty | JOOBLE_API_KEY in credential store |
| usajobs | Empty | USAJOBS_USER_AGENT in TOML; USAJOBS_AUTH_KEY in credential store |
| imap | Exact alert sender address | IMAP connection settings; read-only mailbox parsing |
| upwork | Empty | Approved app endpoint and OAuth access; opt-in authorization |
| unsupported type / manual | Website URL | Manual capture or an alert route; no generic scraper promised |

Country, location, and source access vary. Supported browser boards do not imply permission to scrape restricted content. Health, Last Checked, and Next Check are system-maintained. Daily/weekly/monthly cadence controls intake; failures retry after an hour. Disabled/manual sources are skipped. `sources check` makes read-only diagnostics without changing health or cadence.

For Upwork, configure `UPWORK_CLIENT_ID`, `UPWORK_OAUTH_AUTHORIZATION_URL`, `UPWORK_OAUTH_TOKEN_URL`, and `UPWORK_PROJECTS_API_URL` from your approved app. `jobhub sources authorize upwork` prints a consent URL and waits for its separate local callback on port 8788. Token storage uses the OS credential store. It does not submit applications.

## Optional dependencies

Install extras from this checkout with `uv tool install --reinstall --python 3.12 '.[resume,capture,browser]'`; after publication substitute `notion-job-search-hub[resume,capture,browser]`.

For Playwright Chromium, identify the uv tool directory with `uv tool dir`, then run its Python:

```bash
"$(uv tool dir)/notion-job-search-hub/bin/python" -m playwright install chromium
```

This is a manual browser download, not part of ordinary startup. Python import errors and unavailable Chromium should report browser-specific guidance. Other sources keep working.

Capture:

```bash
jobhub setup --credential CAPTURE_AUTH_TOKEN
jobhub maintenance capture
```

The endpoint listens on `127.0.0.1:8766`, requires a bearer token, and accepts POST `/capture` with `url`, `role_title`, optional `company`, `description`, `location`, and `posted_at`. It saves a private local capture record, then explicitly ingests into the selected hub. If Notion fails, the response reports that capture is saved but unprocessed. After repair, recapture to retry; deduplication prevents a second job row. No server or HTTP documentation interface starts on import.

## Notifications and schedules

Setup asks for SMTP settings only if email is enabled. Use `jobhub notifications test` to send an explicit test to your own configured address; ordinary inspection/setup does not send mail. Notification retries use a local outbox and dedupe ledger.

For mailbox discovery configure IMAP_HOST, IMAP_PORT, IMAP_USERNAME, and IMAP_MAILBOX in TOML; store IMAP_PASSWORD securely and add exact sender addresses as enabled IMAP sources in Notion. Parsing uses a read-only connection and BODY.PEEK.

`jobhub schedule enable --preview` renders configuration without registration. Actual enable creates **com.jobsearchhub.refresh** on macOS or **job-search-hub.service/timer** under Linux user systemd. It does not change other jobs. Credentials injected only in an interactive shell are not inherited by launchd/systemd; use the OS store or arrange service environment injection yourself. The scheduler saves only JOBHUB_HOME, not passwords. Uninstalling/replacing a tool environment may require re-enabling its schedule.
