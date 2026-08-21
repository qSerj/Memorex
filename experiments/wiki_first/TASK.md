# TASK — build a Wiki from the complete source corpus

Prompt version: `wiki-first-baseline-v1`

You are building a durable, human-readable knowledge Wiki from messy Russian-language source
materials. This is not factual-triple extraction and not one summary per input file. Your job is
to understand the corpus as a whole and organize the important accumulated meaning.

## Files and permissions

- Read every file under `raw/` before deciding the Wiki structure.
- Treat `raw/` as immutable. Never edit, rename, or delete a source.
- Write only Markdown files under `wiki/` and the final `report.md`.
- Do not use the Internet or outside knowledge to complete missing facts.
- Do not inspect parent directories, application code, other candidate runs, or reference Wikis.
- Some source messages mention photographs that are intentionally absent from this text-only
  baseline. State when the missing attachment prevents a reliable conclusion; never guess its
  contents.

## What the Wiki should capture

Find the natural long-lived topics in the corpus: problems, explanations, decisions, proposals,
arguments, changes of view, unresolved questions, constraints, and relationships between them.
Prefer a small coherent set of substantial pages over many tiny pages. Do not create a page merely
because there is a file, person, product name, or isolated detail.

It is valid and desirable to synthesize a conclusion from several passages. Preserve epistemic
status without turning the prose into a rigid database:

- a direct statement is supported with a source citation;
- a non-obvious combined conclusion starts with `**Синтез:**` and cites all material inputs;
- an uncertain model inference starts with `**Гипотеза:**` and explains the uncertainty;
- a future user-authored note is described as a user statement rather than independent evidence.

Record meaningful disagreement and evolution instead of silently choosing one version. Distinguish
what was actually decided from what was merely discussed or proposed.

## Markdown contract

Create `wiki/README.md` as a concise human landing page: page titles plus one-sentence descriptions.
It is navigation, not a giant summary that future models must always read.

Every other page must:

1. use an ASCII `kebab-case.md` filename and a natural Russian `# Title`;
2. contain readable connected prose, not a dump of extracted records;
3. link related pages with `[[page-slug]]` using the exact target filename without `.md`;
4. cite important statements inline with labels such as `[S1]`;
5. end with `## Источники`, defining every used label.

A source definition points to the relative source file and the narrowest useful locator available:
line range, message timestamp, transcript timestamp, or a combination. Example:

```markdown
- [S1] [meeting.txt](../raw/meeting.txt),
  строки 14–27 — зафиксированная архитектурная позиция.
```

Several pages may cite the same source. A synthesis may cite several sources. Do not invent an
exact quotation requirement when a broader passage is the honest support.

## Work method

1. Read and understand the full corpus.
2. Draft a page map and identify which sources support each page.
3. Write the pages and cross-links.
4. Re-read the Wiki as a whole and merge duplicates, repair weak organization, and preserve useful
   nuance.
5. Verify that every `[[link]]` resolves, every citation label is defined, every referenced source
   exists, and no unsupported fact was introduced.
6. Write `report.md` with: sources read, pages created, major synthesis decisions, uncertain or
   missing context, ignored low-value material, and the result of the self-check.

Do not stop after proposing a structure. Finish the Wiki files.
