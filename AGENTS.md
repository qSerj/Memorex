# Repository Guidelines

## Product Direction

Memorex is a Python 3.13 local external memory. The user owns ordinary editable Notes; capture,
attachments, notebooks, history and local search work without AI. AI may organize rough material,
suggest Note changes and discuss selected Notes, but it does not own or silently rewrite memory.
Reliable local capture matters more than elegant analysis.

Describe user work in user language. `Packet` means an addition or saved material, `proposal` means
an AI suggestion, `provenance` means sources, and `attempt` means a processing attempt. Do not expose
`runner`, `job`, `snapshot`, queue internals or database vocabulary in the primary UI unless they
are necessary diagnostics.

An architectural idea is not an implementation request. First identify the observed problem it
solves, always consider changing nothing, and prefer the smallest fix. Do not add a subsystem or
general framework without explicit user direction. Feature development is frozen through
2026-09-15: immediately fix only data loss/corruption, a broken promised scenario, or a strong UX
blocker observed at least three times; record other ideas in the local `USAGE_LOG.md`.

## Project Structure

Application code lives in `src/memorex/`; keep Web/CLI wiring, deterministic ingest, AI adapters,
service logic and SQLite persistence separate. Numbered SQL migrations live under
`src/memorex/migrations/`. Tests are in `tests/`, CI in `.github/workflows/`, and numbered Markdown
files document product intent and status. Internal details belong in `ARCHITECTURE.md`, not at the
top of README.

Runtime data belongs in `.memorex/` and must not be committed. Original source objects are
immutable; AI synthesis and answers are derived, not source evidence.

## Build and Test

```bash
uv sync --locked --all-groups
uv run memorex --help
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Pytest is required for every behavioral change. Use temporary workspaces and fake AI runners so CI
never needs network access or credentials. Cover model routing and fallback, unchanged re-ingest,
source immutability, local capture before AI, direct Note editing, proposal validation, queue
recovery, readable export and full backup/restore. Real endpoint smoke tests are manual and use
non-sensitive data.

## Code Style and Integrity

Use four-space indentation, Python type annotations, small single-purpose modules, and explicit SQL
behind the Storage boundary. Ruff formats and lints. Use `snake_case` functions/modules,
`PascalCase` classes, `UPPER_SNAKE_CASE` constants, and observable test names. Validate every
structured model response with Pydantic; never add an unvalidated JSON fallback.

Treat source objects as immutable, ingest as idempotent, and derived records as rebuildable. Keep
user edits direct; keep AI edits reviewable. An existing Note defines its local schema: preserve its
headings, order, table/list shape, tone, links and source footer, making the smallest requested
change. Never commit `.env`, API keys, `.memorex/`, `USAGE_LOG.md`, or private corpus data. Log model
and prompt versions, profile and effort, but never secrets.

## Commits and Pull Requests

Use short imperative subjects such as `Preserve existing note structure`. Keep commits cohesive and
run all three checks before pushing. Pull requests explain the observed problem, smallest chosen
change, schema/pipeline impact and validation performed.
