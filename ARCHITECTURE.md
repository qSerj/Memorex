# Technical Architecture

Этот документ описывает внутреннее устройство. Его термины обычно не должны появляться в основном
пользовательском интерфейсе.

## Слои

- `src/memorex/wiki_app.py` и `wiki_templates/` — локальный Web UI;
- `wiki_first/service.py` — Notes, capture, review, discussions и query;
- `wiki_first/storage.py` — SQLite persistence boundary и файловые snapshots;
- `wiki_first/runners.py` — изолированные Codex/Claude CLI adapters;
- `workspace_archive.py` — полная копия, восстановление и читаемый экспорт;
- остальные ingest/synthesis/query modules — сохранённый provenance compiler.

## Хранение

```text
workspace/
├── memorex.toml
├── inbox/                       # legacy filesystem intake
└── .memorex/
    ├── memorex.db               # provenance compiler prototype
    └── wiki-first/
        ├── state.sqlite         # notes index, queue, jobs, calls, discussions
        ├── objects/             # immutable originals and attachments
        ├── jobs/                # staged AI proposal revisions
        ├── snapshots/           # immutable versions: wiki + source views
        └── answers/             # derived answers, never evidence
```

Notes are canonical Markdown inside the active immutable snapshot. SQLite indexes identity,
notebook membership, full text, attachments and activation. User edits atomically activate a new
snapshot. AI writes only to a staged proposal. Original objects are immutable, ingest is
idempotent, and derived answers can be rebuilt.

## Internal-to-user vocabulary

| Internal | User-facing |
| --- | --- |
| Packet | Добавление / сохранённый материал |
| proposal | Предложение AI / На проверку |
| provenance | Источники |
| attempt | Попытка / история обработки |
| runner, job, snapshot | Hidden except diagnostic details |

## Model routing

A simple intake has at most one material besides its user comment, at most 8,000 text characters
(or one image), at
most 1,000 characters of comment/instruction, and at most one selected thematic Note besides
README. Everything else is standard.

Simple uses Luna/medium; a semantic contract failure retries once with Terra/medium. A technical
Codex failure retries once with Claude Sonnet/medium. Standard ingest, revise, ask and discussions
use Terra/medium and one Claude fallback. Explicit legacy `--runner` overrides remain for manual
experiments. Every call records profile, runner, model, effort, prompt version, duration, status and
token counts, but never secrets.

Existing `[wiki]` runner settings remain readable and power explicit legacy overrides. Full
backup/restore retains all runtime state. Readable export is one-way by design and contains current
notes, sources and attachments; it is not an import format.
