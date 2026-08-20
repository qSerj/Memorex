# Repository Guidelines

## Project Structure & Module Organization

Memorex is a Python 3.13 CLI and local knowledge compiler. Application code lives in
`src/memorex/`; keep CLI wiring, deterministic ingest, LLM adapters, query logic, and
SQLite persistence in separate modules. Numbered SQL migrations live under
`src/memorex/migrations/`. Tests are in `tests/`, CI is in `.github/workflows/`, and
the numbered Markdown files document product intent and implementation status.

Runtime data belongs in `.memorex/` and must not be committed. Generated knowledge
and answers are derived views, never source evidence.

## Build, Test, and Development Commands

```bash
uv sync --locked --all-groups  # install exact runtime and dev dependencies
uv run memorex --help          # inspect the CLI
uv run ruff check .            # lint Python
uv run ruff format --check .   # verify formatting
uv run pytest                  # run offline tests
```

Use `uv run memorex init` before local CLI experiments. Document new commands in
`README.md` when introducing them.

## Coding Style & Naming Conventions

Use four-space indentation, Python type annotations, small single-purpose modules,
and explicit SQL behind the `Storage` boundary. Ruff is the formatter and linter.
Name functions and modules with `snake_case`, classes with `PascalCase`, constants
with `UPPER_SNAKE_CASE`, and tests by observable behavior, for example
`test_reingest_is_idempotent`. Validate every structured model response with
Pydantic; never add an unvalidated JSON fallback.

## Testing Guidelines

Pytest is required for every behavioral change. Use temporary workspaces and fake
LLM providers so CI never needs network access or credentials. Cover unchanged
re-ingest, changed-source revisions, object deduplication, exact provenance offsets,
failed validation, active-job selection, claims-first retrieval, and citation
validation. A real endpoint smoke test is manual and must use non-sensitive data.

## Commit & Pull Request Guidelines

Use short imperative subjects such as `Add versioned source ingest`. Keep commits
cohesive and run all three checks above before pushing. Pull requests should explain
the problem, chosen trade-offs, schema or pipeline impact, validation performed, and
any decision taken from `04_OPEN_QUESTIONS.md`. Link issues and include CLI output
when behavior changes.

## Data Integrity & Configuration

Treat source objects as immutable, ingest as idempotent, synthesis as non-evidence,
and derived records as rebuildable. Never commit `.env`, API keys, `.memorex/`, or
private corpus data. Log model and prompt versions, but never secrets.
