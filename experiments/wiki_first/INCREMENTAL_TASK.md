# TASK — incrementally update an existing Wiki

Prompt version: `wiki-first-incremental-v1`

An existing Wiki is present in `wiki/`. A single new source is present in `incoming/`. Read the
existing Wiki first, then read every file in `incoming/`. Update the accumulated knowledge rather
than rebuilding the Wiki from scratch or writing an isolated summary of the new file.

## Boundaries

- Work only in the current directory.
- Treat `raw/` and `incoming/` as immutable evidence.
- Write only Markdown under `wiki/` and `incremental-report.md`.
- Do not inspect parent directories, other candidates, or the Internet.
- The incoming fixture explicitly says that it is synthetic. Preserve that status visibly: its
  statements are valid only inside this experiment and must never be presented as real-world
  evidence.

## Required behavior

1. Identify which existing pages the new material changes, confirms, narrows, or leaves untouched.
2. Edit those pages in place. Create a new thematic page only if the material introduces a durable
   topic that genuinely does not belong on an existing page.
3. Preserve the earlier state and its provenance when a position evolved. Do not silently replace
   “what was believed” with “what the follow-up says”. Describe the sequence and current status.
4. Distinguish decisions, proposals, corrections, unresolved questions, synthesis, and hypotheses.
5. Add narrow source locators pointing to `../incoming/...` for all important new statements.
6. Repair landing-page descriptions and Wiki links when page meaning or navigation changed.
7. Do not change unrelated wording merely to impose a new writing style.

## Report and self-check

Write `incremental-report.md` with:

- pages changed, created, and intentionally left untouched;
- earlier conclusions that were confirmed, corrected, or superseded;
- new decisions and open questions;
- whether the update produced any isolated per-source summary;
- verification that every Wiki link resolves, every citation label is defined, and every new
  source reference exists.

Finish the edits and report; do not stop at a proposed patch plan.
