# Wiki-first laboratory

This directory contains the reproducible protocol and public synthetic fixture for the Wiki-first
experiment. Private sources and their exact candidate Wikis live under `.memorex/wiki-lab/` and
must never be committed.

The initial blind review rated GigaChat weak, Codex good/almost excellent, and Claude
excellent-plus. Codex and Claude outputs are both retained as references: the former is relatively
restrained, while the latter captures more structure and nuance but needs bloat monitoring.
GigaChat is deferred for adapter/prompt research, not rejected as a model family.

## Baseline matrix

Every run receives byte-identical copies of the same primary TXT sources, an empty `wiki/`
directory, and `TASK.md` as its only instruction. The first matrix is:

- Codex `gpt-5.6-sol`, maximum reasoning;
- Claude Code `opus`, maximum effort;
- OpenCode `sber-v2/GigaChat-3-Ultra` through the explicitly versioned `/v2` proxy route.

The agents may read `raw/` and write only `wiki/` and `report.md`. They have no access to the
legacy analysis documents while generating the baseline. Candidate identities are hidden as
A/B/C before human review.

The earlier `/v1` GigaChat result and `/v2` contract failure are retained privately as protocol
diagnostics, not silently overwritten. This keeps transport/tool-loop behavior separate from human
judgment of Wiki quality.

`QUERY_TASK.md` is run later with one fixed answer agent against each candidate. It tests Wiki
navigation separately from Wiki generation. `REVIEW_TEMPLATE.md` records the qualitative result.

## Tracked benchmark

[`benchmark/`](benchmark/README.md) contains only fictional data. Ingest `benchmark/baseline/`
first, freeze the resulting Wiki, then add `benchmark/incremental/`. A successful incremental run
must edit existing topics, preserve earlier provenance/status, and avoid an isolated per-file
summary.

## Experiment gate

The first gate passed for two strong agents, so the repository now contains a small CLI service with
manual proposal activation. Do not build the replacement Web application yet. The next gate is a
real end-to-end corpus run through that service; if it fails, revise understanding or corpus
organization before adding retrieval infrastructure.
