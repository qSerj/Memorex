# Implementation Status

Updated: 2026-08-21

## Current Milestone

Memorex v0.2 implements the first usable decision-knowledge workspace:

```text
inbox → deterministic staging → user metadata/confirmation → LLM compilation
      → claims + exact evidence + entities + relations + summaries
      → decision dossier / reviewed graph / cited answers
```

The database is the machine-readable knowledge core. Generated summaries and the web
view are rebuildable projections, not evidence.

## Implemented

- Isolated workspaces with `memorex.toml`, `inbox/`, private `.memorex/` state, and
  independent model-role profiles.
- Recursive TXT/Markdown discovery, SHA-256 identity, idempotent re-scan, changed-source
  revisions, unambiguous move recognition, and recovery of interrupted inbox jobs.
- Explicit metadata gate before any paid LLM processing.
- Strict decision-aware structured extraction: observation, problem, goal, idea,
  decision, action item; proposed/active/rejected/completed/unknown lifecycle.
- Exact verbatim evidence offsets against normalized immutable source snapshots.
- Entities, typed evidence-backed relations, claims-first source summaries, FTS5
  retrieval, and cited answers.
- Answer guard rejects unsupported content words, retries with validation feedback, and
  falls back to verbatim cited claim text instead of returning embellished synthesis.
- Strong-model proposals for contradiction and superseding. No graph mutation occurs
  until human review; accepted claim links remain auditable.
- Authoritative user overrides with reason and history. Runtime retrieval uses the
  newest override and labels original evidence as pre-correction context.
- Server-rendered local FastAPI UI for dashboard, staging, compilation, dossier,
  evidence inspection, question answering, review, model roles, and eval.
- Isolated model evaluation over identical segments; candidate output never activates
  production claims.
- OpenAI-compatible provider roles (`fast`, `strong`, `answer`), with OpenRouter as the
  documented first configuration.
- Automatic migrations from the v0.1 schema, CI, and offline fake-provider tests.

## Integrity Rules

- Source objects are immutable; reprocessing creates derived jobs, not rewritten evidence.
- Synthesis never becomes evidence automatically.
- Rejected claims do not enter retrieval; accepted superseded claims remain historical
  but are excluded from current answers.
- All model JSON is validated by Pydantic; there is no permissive fallback parser.
- Secrets, workspace state, and private corpora are not committed.

## Deliberately Deferred

- PDF/DOCX/HTML parsers and source-specific locators such as PDF page and bounding box.
- Embeddings, graph community summaries, and a learned query router. FTS5 plus typed
  graph data is sufficient to evaluate the first real corpus before adding these costs.
- Automated entity merge. Ambiguous identity remains a review problem.
- Full temporal interval reasoning beyond stored `valid_from`/`valid_to` fields.
- Saved associative trails and generated Obsidian/Markdown Wiki views.
- GigaChat adapter; the current boundary is OpenAI-compatible endpoints.

## Next Evidence-Driven Work

1. Load the first real entrepreneur corpus and inspect extraction failures and review load.
2. Run model eval across representative Russian conversations before choosing role models.
3. Add PDF support with page-level provenance when the TXT/Markdown workflow is stable.
4. Add hybrid/vector or hierarchical retrieval only if measured questions fail FTS/graph
   retrieval; do not recreate a monolithic context index.
5. Add saved Memex-style trails after repeated question paths appear in real usage.
