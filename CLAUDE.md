# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Memorex is no longer a Python application. Since 2026-08-26 the product is a personal external
memory made of plain markdown files that a strong model reads and edits by written rules — no
server, no database, no code standing between the owner and their notes. This repository holds the
source of the rules and how the project arrived here, not the memory itself: the actual vault lives
outside the repo and is never published. See [`README.md`](README.md) for why the project exists and
[`docs/ИСТОРИЯ.md`](docs/ИСТОРИЯ.md) for how it got here, including where the earlier Python line
went (it survives in the `wiki-first` and `legacy/provenance-compiler` branches — do not resurrect
it here without explicit direction).

Everything in this repo is Markdown, plus the shell script that packages it. There is no build, no
lint, no test suite, and no CI.

## Layout

- [`memory-kit/`](memory-kit/) — the installable skill. This is the only thing under active
  development here.
- [`docs/`](docs/) — current-direction documents (`ГДЕ_НУЖЕН_ИИ.md`, `РАБОТА_С_ИДЕЯМИ.md`) and
  `docs/archive/`, memory pages copied verbatim from the vault that recorded the shift away from
  the Python line. Archive pages carry Obsidian frontmatter and `[[wiki-links]]` that don't resolve
  on GitHub; don't "fix" them, they are evidence, not documentation.

## memory-kit

`memory-kit` is the source of `memory-setup`, a Claude Code skill that bootstraps a markdown memory
vault in an empty directory (or updates the rules in one already deployed) without touching what
the owner has accumulated. Read `memory-kit/README.md` before changing anything here — it explains
the install/update contract in full. The essentials:

- **The kit is the single source of rules.** A vault gets a copy at install time and lives on its
  own after that. A fix made by hand inside a live vault is lost the next time that vault updates,
  unless it is ported back into `memory-kit/references/` here.
- `references/*.md` are copied verbatim into a fresh vault (skills, `.gitignore`, friction log
  template, actions templates). `references/claude-md.md` and `references/memory-identity.md`
  additionally carry `{{placeholders}}` filled in from the owner's answers during setup.
- Bump `VERSION` (semver) for every rule change. If the change requires anything from data already
  in a vault — a new required frontmatter field, a renamed folder, a changed line format — add a
  step file to `migrations/`, named `<from>-to-<to>.md`; format and constraints are in
  `migrations/README.md`. The hard rule there: a migration changes form, never drops content.
- `./memory-kit/build.sh` packages `SKILL.md`, `VERSION`, `references/` and `migrations/` into
  `memory-setup.zip` (git-ignored, rebuilt on demand) for installing as a skill in Claude Code.

### Treating a live vault as a module

The owner runs a live vault at `~/Projects/MemorexClaude`, installed by this kit. Every vault this
kit deploys carries the same self-contained contract: its own root `CLAUDE.md` (from
`references/claude-md.md`) and `.claude/skills/{remember,ingest,review}/` fully specify how to read
and write it — search `memory/` before answering, cite the pages an answer is built from, run new
material through `remember` (one thought) or `ingest` (a messy batch) rather than writing into
`memory/` by hand, commit after every change that touches it. Treat the vault as a module with that
file as its interface, not as a directory to explore freely:

- **Answering a question from it.** When the owner points at the vault to ground something (`go
  check what's currently true there`), read only what its own rules point to — `grep`/`glob` over
  `memory/`, not a manual crawl of the whole tree.
- **Handing it material.** Route it through the vault's own `remember`/`ingest` skill file instead
  of improvising a write — that is its API, and bypassing it defeats the gate the owner relies on.
- **Porting a fix back.** The owner sometimes patches the vault by hand when a rule is in the way of
  real work. When they point at a specific fix, read exactly the files they name and port it into
  `memory-kit/references/` (plus a migration if it affects existing pages).

In every case: don't browse the rest of that vault or any other project on your own. It's personal,
and the owner points at what's relevant rather than asking for a scan.

## Working conventions

- Docs and vault-facing rule text are written in Russian; this file is in English.
- An architectural idea is not an implementation request: identify the observed problem, always
  consider changing nothing first, prefer the smallest fix. This applies doubly to `memory-kit`,
  whose rules were grown from real friction in `~/Projects/MemorexClaude`, not designed up front.
- Commits: short imperative subject line, one coherent change per commit.
