# Baseline run log

Date: 2026-08-21

The generated Wikis and private source copies are stored under `.memorex/wiki-lab/` and are
intentionally excluded from Git. This log records only reproducible protocol facts and structural
checks; it is not the human quality verdict.

## Corpus integrity

- 11 primary TXT files were copied into each candidate workspace.
- Every candidate copy was byte-identical to its source after generation.
- Legacy analyses and absent image attachments were not exposed to the agents.
- Each agent started with an empty `wiki/` directory and the same `TASK.md`.

## Generation runs

| Run | Runtime and model | Result | Deterministic contract check |
|---|---|---|---|
| Codex baseline | Codex, `gpt-5.6-sol`, maximum reasoning | 6 Wiki files, 34,852 bytes | pass: 14 resolved Wiki links, 27 source labels |
| Claude baseline | Claude Code, `opus`, maximum effort | 9 Wiki files, 93,820 bytes | pass: 61 resolved Wiki links, 57 source labels |
| GigaChat diagnostic | OpenCode, `GigaChat-3-Ultra`, `/v1` | Required a resumed agent turn after reading during smoke; full Wiki completed | fail: links appeared only on the landing page; 5 thematic pages had no Wiki links |
| GigaChat baseline | OpenCode, `GigaChat-3-Ultra`, `/v2` | Smoke and full generation both completed in one uninterrupted tool loop; 7 Wiki files, 7,096 bytes | fail: no Wiki links; landing-page citation labels were undefined |

The `/v2` run is retained as the GigaChat candidate because it is the explicitly requested and
versioned protocol. The `/v1` result remains available as a diagnostic rather than being discarded.
The difference demonstrates that tool-loop reliability and Wiki quality must be evaluated
separately.

An additional Codex Responses API smoke test against `/v2` was attempted because the proxy's
official guide recommends that client path. It stopped at proxy authentication before reaching
the model. No project or global credential was changed, and the temporary OpenCode configuration
used for the `/v2` run was securely removed afterwards.

## Remaining experiment gates

1. Copy the three baseline Wikis into anonymous A/B/C review workspaces.
2. Run `QUERY_TASK.md` with one fixed answer agent against each candidate.
3. Perform the qualitative review before revealing model identities.
4. Add the same real follow-up note to each candidate and inspect whether existing pages evolve
   coherently instead of producing an isolated summary.

The first two gates are complete. The fixed query agent answered all six questions from the Codex
and Claude Wikis without opening raw sources. Against the GigaChat `/v2` Wiki it needed raw-source
fallback for five of six questions, chiefly to recover chronology, cross-topic context, and
epistemic status omitted by the Wiki. The incremental-ingest test remains the final baseline gate.

## Blind human review

The user reviewed the three candidates in Obsidian before the mapping was revealed:

- candidate A: weak;
- candidate B: good, close to excellent;
- candidate C: excellent-plus;
- B and C may be near parity, with a slight preference for C.

Mapping after review: A was GigaChat `/v2`, B was Codex, and C was Claude. The qualitative judgment
therefore agrees with the independent query probe: Codex and Claude both passed the Wiki-first
baseline, with Claude preferred for this corpus; the GigaChat setup needs further protocol/model
investigation rather than being discarded from future experiments.

## Synthetic incremental-ingest probe

A deliberately diagnostic, fully fictional follow-up was added in an isolated private fixture. It
confirmed one earlier direction while narrowing several capability claims, rejected a proposed
implementation path, defined a measurable pilot, corrected an unsupported causal explanation,
assigned roles, and deferred a secondary automation idea. The fixture states explicitly that its
events never happened and must not be treated as real evidence.

| Run | Update behavior | Structural result |
|---|---|---|
| Codex | Integrated the follow-up into five existing pages, preserved the earlier state, and marked the synthetic layer on every affected page | 83 insertions, 12 deletions; deterministic contract check passed |
| Claude | Updated eight existing pages and created `pilot-i-etapnost.md` as a durable cross-topic pilot page; preserved history and synthetic status | 745 insertions, 53 deletions; deterministic contract check passed, but growth is potentially excessive |
| GigaChat `/v2` | Made four small edits, omitted several affected topics, and presented the fixture as a real specialist confirmation despite the explicit warning | 14 insertions, 7 deletions; contract and epistemic-status checks failed |

This probe demonstrates accumulation for the Codex and Claude paths: both changed existing topics
instead of producing a per-source summary. It also reveals the next concrete design problem. Codex
is substantially more restrained; Claude produces a richer cross-topic synthesis but may require a
page-growth or consolidation rule before repeated ingest.
