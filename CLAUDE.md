# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status (read first)

This repo holds the **Python application** line of Memorex. Since 2026-08-26 daily use has moved to
a **markdown incarnation** of the same idea (rules + Obsidian + terminal, living in a separate
workspace); the app has not been used since. `КАК_ПРОДОЛЖИТЬ.md` states this explicitly: the app is
not deleted and remains a test-bed, but documents `00`–`06` describe a superseded line, and
`07_ГДЕ_НУЖЕН_ИИ.md` / `08_РАБОТА_С_ИДЕЯМИ.md` describe the current direction.

Practical consequence: do not treat roadmap items in `00`–`06` or "Ближайшая работа" in
`КАК_ПРОДОЛЖИТЬ.md` as live plans. Fix what is asked; ask before resuming the app roadmap.

Project docs are in Russian; code, identifiers and docstrings are in English; the web UI is Russian.

## Commands

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

All three checks must pass before committing — CI (`.github/workflows/`) runs exactly these.

```bash
uv run pytest tests/test_wiki_first.py::test_name    # single test
uv run memorex app ./my-knowledge                    # current app, http://127.0.0.1:8766
uv run memorex --help                                # legacy compiler CLI
```

Tests run fully offline with fake runners and fake providers. Never add a test that needs network
access or credentials.

## Two stacked systems

The package contains two generations of code. Know which one a task belongs to.

**Current — Wiki-first / Notes** (`wiki_app.py`, `wiki_templates/`, `wiki_static/`,
`wiki_first/`, `workspace_archive.py`). This is the product: local notes, notebooks, FTS search,
attachments, Packet capture, AI proposals, discussions, history, backup/restore.

**Legacy — provenance compiler** (`cli.py` legacy commands, `storage.py`, `compiler.py`,
`extraction.py`, `query.py`, `web.py`, `llm.py`, `ingest.py`, `inbox.py`, `evaluation.py`,
`domain.py`). A retained research prototype (atomic claims, evidence offsets, entities, typed
relations, contradiction review, eval) tagged `provenance-compiler-prototype`. It talks to an
OpenAI-compatible endpoint via `MEMOREX_LLM_*` env vars, not to CLI runners. It is not the current
user path — do not extend it without explicit direction, and do not break it either.

Both share `config.py` (`WorkspaceSettings` from `memorex.toml`) and the numbered SQL migrations
directory, but they use different databases and different `Storage` classes with the same name.

## Wiki-first architecture

- `wiki_app.py` — FastAPI routes, Jinja templates, background task orchestration.
- `wiki_first/service.py` — all business operations (`WikiFirstService`); the only place that
  composes snapshots, prompts, runners and storage.
- `wiki_first/storage.py` — the SQLite boundary (`state.sqlite`) plus the file snapshot tree. All
  SQL lives here; nothing above it writes SQL.
- `wiki_first/runners.py` — subprocess adapters for the `claude` and `codex` CLIs.
- `wiki_first/prompts.py` / `validation.py` / `models.py` — versioned prompts, wiki-tree validation,
  Pydantic response models.

### Data model invariants

Workspace layout is documented in `ARCHITECTURE.md`. The invariants that drive most code:

- **Notes are Markdown files inside an immutable snapshot tree.** A snapshot directory is made
  read-only after it is written; SQLite only indexes identity, notebook membership, FTS, attachments
  and which snapshot is active.
- **Every save is a new snapshot.** `_save_note_snapshot` copies the active tree, writes the file,
  validates, hashes, freezes it, then atomically activates it with an optimistic guard on
  `expected_snapshot_id`. Never mutate an existing snapshot in place.
- **User edits apply immediately; AI writes only a staged proposal** under `jobs/` that a human
  accepts, revises or rejects. This is the core product rule, not a UI detail.
- Original objects and attachments are content-addressed and immutable; ingest is idempotent;
  answers under `answers/` are derived and rebuildable, never evidence.

### Concurrency

`WorkspaceTasks` in `wiki_app.py` runs a single-worker `mutations` executor, a 2-worker `queries`
executor, and one `memorex-packet-queue` thread. The queue thread claims the next Packet from SQLite
only when no mutation is in flight, so writes serialize. Packets survive restart: the queue is
persistent, claims are atomic, and a pending/running discussion turn becomes retryable `failed` on
next start. Long-running work is cancellable through per-task `threading.Event`s.

### Model routing

`memorex.toml` `[wiki.profiles.*]` defines simple / standard / fallback profiles. Routing lives in
`WikiFirstService._ingest_spec` and `_fallback_spec`: small single-item intake → simple profile,
everything else → standard, one retry on failure via fallback. **At most two model calls per user
operation.** Every call is logged to `runner_calls` with profile, runner, model, effort, prompt
version, duration, status and token counts — never secrets. Bump the `*_PROMPT_VERSION` constants in
`prompts.py` when you change a prompt.

Runners shell out to the `claude` / `codex` CLIs with restricted tool sets and sandbox flags
(`_command` in `runners.py`). In tests, inject a fake through
`WikiFirstService(settings, runner_resolver=...)`.

### Migrations

Numbered `.sql` files in `src/memorex/migrations/`. Wiki-first applies
`[0-9][0-9][0-9]_wiki_first_*.sql` in order and records them in `schema_migrations`; the legacy
compiler applies its own set. Migrations are forward-only and must not break existing snapshots of
a real workspace.

## Working conventions

From `AGENTS.md`, the ones that actually change decisions:

- **An architectural idea is not an implementation request.** Identify the observed problem, always
  consider changing nothing, prefer the smallest fix. New subsystems require repeated observed pain
  recorded in the local `USAGE_LOG.md` (git-ignored; `USAGE_LOG.example.md` is the template). Data
  loss and broken promised scenarios always take priority.
- **User language in the UI.** `Packet` → «Добавление / сохранённый материал», `proposal` →
  «Предложение AI / На проверку», `provenance` → «Источники», `attempt` → «Попытка». Never expose
  runner, job, snapshot, queue or database vocabulary outside diagnostics.
- **An existing Note defines its own schema.** When AI edits a note, preserve headings, order,
  table/list shape, tone, links and the source footer; make the smallest requested change.
- Validate every structured model response with Pydantic. Never add an unvalidated JSON fallback.
- Ruff, line length 100, four-space indent, type annotations, small single-purpose modules.
- Never commit `.env`, API keys, `.memorex/`, `USAGE_LOG.md`, `my-knowledge/`, or `Inbound/`.

Pytest is required for every behavioral change. Cover model routing and fallback, unchanged
re-ingest, source immutability, local capture before AI, direct note editing, proposal validation,
queue recovery, readable export and full backup/restore.

Commits: short imperative subject (`Preserve existing note structure`). PRs state the observed
problem, the smallest chosen change, schema/pipeline impact and validation performed.

## Document map

- `README.md` — product overview, user-facing feature list, how to run.
- `ARCHITECTURE.md` — internal layers, storage tree, vocabulary table, model routing.
- `05_IMPLEMENTATION_STATUS.md` — what actually works and what is deliberately not built.
- `07_ГДЕ_НУЖЕН_ИИ.md`, `08_РАБОТА_С_ИДЕЯМИ.md` — current direction (markdown line).
- `00`–`06`, `КАК_ПРОДОЛЖИТЬ.md` below «Точка передачи» — superseded app line, kept for context.
- `experiments/wiki_first/` — historical experiment material, not a roadmap.
