# Repository Guidelines

## Product Direction

Memorex is a personal external memory made of plain markdown files, governed by written rules that
a strong model reads and applies — not a Python application. The owner's earlier local-first
Evernote-style app is retired (it survives in the `wiki-first` and `legacy/provenance-compiler`
branches); daily use moved to a markdown vault plus a Claude Code skill on 2026-08-26 and the switch
was confirmed on 2026-09-03. `README.md` explains why the project exists; `docs/ИСТОРИЯ.md` has
the full account of how it got here.

This repository is not the memory itself. It holds `memory-kit`, the installable skill that
bootstraps and updates markdown vaults, and `docs/`, which records the current direction and
archives the pages that documented the shift. The actual vault the owner uses day to day lives
outside this repo (`~/Projects/MemorexClaude`) and is never published here.

## Project Structure

- `memory-kit/` — the only thing under active development. `SKILL.md` is the installer logic,
  `references/` are the files it copies verbatim into a fresh vault, `migrations/` are versioned
  steps for updating a vault whose data has to change shape, `build.sh` packages all of it into
  `memory-setup.zip`. Read `memory-kit/README.md` before touching any of this.
- `docs/` — `ГДЕ_НУЖЕН_ИИ.md` and `РАБОТА_С_ИДЕЯМИ.md` describe the current product direction;
  `docs/archive/` holds memory pages copied as-is from the vault, kept as evidence of how the
  decision matured, not as maintained documentation. Leave their Obsidian frontmatter and
  `[[wiki-links]]` alone.

## Build and Test

There is no application to build or test. The only executable step is packaging the skill:

```bash
cd memory-kit && ./build.sh
```

This produces `memory-setup.zip`, installed as a skill through the Claude Code interface. There is
no CI in this repository.

## Editorial Integrity

`memory-kit` is the single source of the vault rules; a deployed vault gets a copy at install time
and lives on its own afterward. A change here does not apply to existing vaults until each is
explicitly told to update. Consequences for editing it:

- Every rule change bumps `VERSION` (semver).
- A change that a vault's existing data would need to catch up to — a new required field, a
  renamed folder, a changed record format — gets a migration file in `migrations/`, following the
  format in `migrations/README.md`. A migration changes the form of what's stored, never its
  content; it is written as instructions for a model, not a script, because most of what it does
  (rereading a page, deriving tags from its content) a script cannot do.
- `references/*.md` are copied into new vaults verbatim. Do not soften or trim them "for a fresh
  vault" — they were grown from real friction, not designed in the abstract, and a new vault should
  start from the same point as an established one.
## Interacting with a Live Vault

Every vault this kit deploys carries the same contract: its own root `CLAUDE.md` plus
`.claude/skills/{remember,ingest,review}/` fully specify how to read and write it. Treat a live
vault (e.g. `~/Projects/MemorexClaude`) as a module with that file as its interface, not as a
directory to explore freely:

- To answer a question from it, read only what its own rules point to (search `memory/`, don't
  crawl the tree).
- To hand it material, route it through the vault's own `remember`/`ingest` skill rather than
  writing into `memory/` directly.
- To port a fix the owner made there by hand back into this kit, read exactly the files they name
  and update `memory-kit/references/` (plus a migration if it affects existing pages).

Don't browse the rest of that vault, or any other project, on your own.

## Commits and Pull Requests

Short imperative subject line, one coherent change per commit. State the observed problem behind a
rule change, not just the wording change itself.
