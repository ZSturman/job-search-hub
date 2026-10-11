# Job Search Hub

Find, evaluate, and track jobs through Notion and the CLI. This is an independent public-facing project.

## Preservation and privacy
- Never modify the neighboring Casino Exit project, its data, Git history, Notion databases, or schedules.
- Never copy personal documents, credentials, operational data, or source Git history into this repository.
- Notion owns editable preferences, approved facts, and application state. Local storage owns documents, secrets, and execution data.
- No resume/cover-letter drafting, document generation, or application submission.
- Inspection and preview commands must not write remote state, send email, or modify private documents.

## Repository map
- Python package: `job_search_hub/`; public CLI: `jobhub`.
- Manifest: `pyproject.toml`; tests: `tests/`; docs: `docs/`.
- Packaged public resources: `job_search_hub/resources/`.

## Development
- Python >=3.11; use the project-local virtual environment.
- Derive commands from pyproject.toml; keep README instructions current.
- Run targeted isolated checks before broad testing. Live services must be explicit opt-ins and use disposable data.
- Install: `uv sync --extra dev --extra resume --extra capture`.
- Run: `.venv/bin/jobhub --help`.
- Tests: `.venv/bin/python -m pytest tests/test_hub.py -q`.
- Lint: `.venv/bin/python -m ruff check job_search_hub tests/test_hub.py`.
- Build: `.venv/bin/python -m build`.
- Keep substantive remaining work in `docs/NEXT_STEPS.md`; do not fabricate screenshots.
