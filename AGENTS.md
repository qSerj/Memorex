# Repository Guidelines

## Project Structure & Module Organization

This repository is a design starter pack for **LLM Knowledge Lab**, a local knowledge compiler. Read the numbered documents in order:

- `00_PROJECT_CONTEXT.md`: product goals and core invariants.
- `01_ARCHITECTURE_IDEAS.md`: candidate architecture and retrieval approaches.
- `02_MVP_AND_EVOLUTION.md`: staged MVP and acceptance scenarios.
- `03_CODEX_STARTER_BRIEF.md`: implementation priorities and engineering constraints.
- `04_OPEN_QUESTIONS.md`: decisions that must remain open until discussed.

`README.md` is the entry point. There is no source, test, or asset directory yet. When implementation begins, keep code, tests, migrations, fixtures, and generated Wiki output separate; generated views must never become source evidence.

## Build, Test, and Development Commands

No build system, dependency manifest, or executable code is committed yet. For documentation work, useful checks are:

```bash
rg '^#' *.md        # inspect heading structure
git diff --check    # catch whitespace errors in a Git checkout
```

Document new bootstrap, run, lint, migration, and test commands in `README.md` when introducing them. Prefer a CLI-first workflow and SQLite with FTS5 initially.

## Coding Style & Naming Conventions

Write Markdown with concise headings, short paragraphs, fenced code blocks, and backticks around paths, commands, and schema fields. Preserve the numeric prefix convention for ordered design documents. For future code, use small modules, explicit data structures, deterministic pipeline stages, schema migrations, and the language's standard formatter. Avoid hidden state, provider-specific LLM coupling, and unvalidated structured model output.

## Testing Guidelines

No test framework or coverage threshold is configured. New code should introduce automated tests and fixtures alongside the feature. At minimum, cover idempotent re-ingest, changed-source revisions, exact evidence provenance, superseded claims, and the rule that synthesis is not evidence. Name tests by observable behavior, for example `test_reingest_does_not_duplicate_claims`.

## Commit & Pull Request Guidelines

The repository is initialized but has no commits yet, so no commit convention has been established. Use short, imperative subjects such as `Add source checksum registry`, and keep unrelated changes separate. Pull requests should explain the problem, approach and trade-offs, affected pipeline stages or schema, validation performed, and any open question resolved. Link issues; include CLI output or screenshots when behavior or generated views change.

## Data Integrity & Architecture

Treat raw sources as immutable. Make ingest idempotent, attach every claim to precise evidence, validate all LLM structured output, log model and prompt versions, and keep every derived representation rebuildable. Propose options before encoding unresolved choices from `04_OPEN_QUESTIONS.md`.
