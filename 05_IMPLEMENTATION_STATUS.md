# Implementation Status

Updated: 2026-08-20

## Current Milestone

The first milestone is a deliberately thin end-to-end personal research CLI:

```text
init → add → extract → inspect evidence → ask
```

It validates the central project hypothesis: understanding extracted once can be
stored as claims and reused for later answers without rereading the whole corpus.

## Decisions Locked for v1

- Python 3.13, `uv`, Typer, Pydantic, stdlib `sqlite3`; no ORM.
- Explicit SQL behind a small `Storage` boundary and numbered migrations.
- UTF-8 TXT and Markdown only; paragraph-aware, non-overlapping 2,000-character
  segments with normalized-text offsets.
- Source identity is canonical path. A changed checksum creates a revision; an
  unchanged add is idempotent.
- Immutable bytes live in `.memorex/objects/<sha256[:2]>/<sha256>`. The original
  file is left untouched and the object store is never used for discovery.
- Claims are atomic text plus confidence and one exact evidence quote. Entities,
  triples, and ontology remain deferred.
- LLM access uses an OpenAI-compatible `/v1/chat/completions` endpoint with strict
  `json_schema`; there is no permissive JSON fallback.
- `ask` retrieves active claims through FTS5/BM25, validates citation IDs, and does
  not persist the resulting synthesis.

## Implemented

- Git repository initialized on `main`; GitHub `origin` uses
  `git@github.com:qSerj/Memorex.git`.
- Installable `memorex` package and Typer CLI with `init`, `add`, `source list/show`,
  `extract`, `claim list/show`, and `ask` commands.
- SQLite migrations, foreign keys, FTS5, source revisions, segments, jobs, LLM call
  audit records, claims, and exact claim evidence.
- Sharded content-addressed object storage with checksum verification and atomic
  writes.
- Strict Pydantic validation, verbatim evidence anchoring, three-attempt extraction
  and answer validation, active extraction jobs, and `--force` rebuilds.
- Human-readable and `--json` output modes.
- GitHub Actions plus offline unit/integration/CLI tests using a fake LLM provider.

At this checkpoint, `ruff check`, `ruff format --check`, and all 17 tests pass.

## Continue From Another Machine

```bash
git clone git@github.com:qSerj/Memorex.git
cd Memorex
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Then configure `MEMOREX_LLM_BASE_URL`, `MEMOREX_LLM_MODEL`, and optionally
`MEMOREX_LLM_API_KEY`. No credentials or `.memorex` data are committed.

## Next Work

1. Run the documented end-to-end smoke test against the intended real compatible
   endpoint and model; record any compatibility differences around `json_schema`.
2. Add a small non-sensitive fixture source and verify a real extracted claim can
   be traced back to its exact normalized-text span.
3. Exercise Russian and English questions against several sources, then tune prompts
   and FTS token selection only from observed failures.
4. Decide whether the next increment should add PDF parsing, hybrid segment fallback,
   or entities. Do not implement these before the first real evaluation.

## Known v1 Boundaries

There is no inbox watcher, PDF parser, entity resolution, temporal claim model,
embeddings, generated Wiki, saved answer/trail, web UI, or migration from another
backend. Failed extraction jobs stay available for audit but never become active.

