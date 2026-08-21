# Memorex

Memorex проверяет Wiki-first гипотезу: может ли сильная LLM превращать реальные неопрятные
TXT/Markdown-материалы в постепенно растущую связанную Wiki, которую полезно читать и по которой
можно задавать вопросы.

Новый путь сознательно короткий:

```text
checksum + immutable raw → целостные документы → сильный LLM-администратор
                         → reviewable Markdown proposal → immutable Wiki snapshot
                         → LLM-навигация и ответ
```

Существующий provenance-oriented compiler с claims, exact evidence, graph review, eval и Web UI не
удалён. Он сохранён как отдельный прототип и по-прежнему доступен через старые команды. Wiki-first
не зависит от его онтологии или таблиц.

## Быстрый старт Wiki-first

Требуются Python 3.13, `uv` и авторизованный Claude Code или Codex CLI. По умолчанию semantic ingest
выполняет Claude Opus с максимальным effort, query — Codex `gpt-5.6-sol` с максимальным reasoning.
Если основной ingest-runner технически завершается ошибкой или выдаёт невалидную Wiki, Memorex один
раз пробует второй сильный runner. GigaChat пока не исключён из будущих исследований, но не входит в
runtime fallback первой версии.

```bash
uv sync --locked --all-groups
uv run memorex workspace init ./my-knowledge --name "Моя база"

cp meeting.txt ./my-knowledge/inbox/
cp notes.md ./my-knowledge/inbox/

uv run memorex --workspace ./my-knowledge wiki ingest
uv run memorex --workspace ./my-knowledge wiki review JOB_ID --diff
uv run memorex --workspace ./my-knowledge wiki revise JOB_ID \
  "Слишком много мелких страниц; объедини повторяющиеся темы"
uv run memorex --workspace ./my-knowledge wiki apply JOB_ID
uv run memorex --workspace ./my-knowledge wiki ask \
  "Почему мы изменили подход к лаборатории моделей?"
```

Перед ingest не нужно заполнять title, author, date, type или authority. В первой версии принимаются
UTF-8 `.txt` и `.md`; модель читает каждый файл целиком и сама извлекает полезный контекст. Команда
`wiki tell` тем же reviewable путём передаёт Wiki произвольную мысль, поправку или предпочтение:

```bash
uv run memorex --workspace ./my-knowledge wiki tell \
  "Считай стоимость важнее скорости, пока я явно не изменю этот приоритет"
```

Ни ingest, ни tell не меняют активную базу автоматически. Они создают proposal. `apply` проверяет
Markdown-контракт и неизменность базового snapshot, затем атомарно активирует всю версию. Доступны
`wiki reject`, `wiki history`, `wiki rollback`, `wiki validate` и `wiki status`.

`wiki status` печатает абсолютный путь активной read-only Wiki. Эту папку удобно открыть в Obsidian
как диагностический vault. Редактировать её руками не следует: Memorex проверяет hash дерева и
остановит ingest/query после внешнего изменения. Поправки нужно передавать через `wiki tell` или
`wiki revise`.

## Workspace и конфигурация

```text
my-knowledge/
├── memorex.toml
├── inbox/                         # пользователь кладёт TXT/Markdown сюда
└── .memorex/
    ├── memorex.db                 # сохранённый provenance-compiler
    └── wiki-first/
        ├── state.sqlite           # checksum, jobs, snapshots, activations, calls
        ├── objects/               # immutable raw и normalized objects
        ├── jobs/                  # proposal revisions
        ├── snapshots/             # immutable Wiki versions + source views
        └── answers/               # производные ответы, не evidence
```

Настройки runner находятся в `memorex.toml` и не содержат секретов:

```toml
[wiki]
ingest_runner = "claude"
query_runner = "codex"
claude_model = "opus"
claude_effort = "max"
codex_model = "gpt-5.6-sol"
codex_reasoning_effort = "max"
```

Для разового сравнения можно передать `--runner claude` или `--runner codex` в `wiki ingest`,
`wiki revise` и `wiki ask`.

## Что проверяет первая версия

- deterministic discovery по path/checksum и идемпотентный re-ingest;
- неизменяемые raw/normalized objects;
- понимание целого документа вместо обязательного `2000 chars → claims`;
- обновление тематических страниц, cross-links, несколько источников у synthesis;
- ручной review/revise/apply без прямого редактирования внутренней Wiki;
- immutable snapshots, stale-proposal guard, integrity check и rollback;
- лог runner/model/version/prompt/duration/errors без секретов;
- query через Wiki, с возможностью открыть raw sources, но без превращения ответа в знание.

Синтетический публичный benchmark и протокол лежат в
[`experiments/wiki_first`](experiments/wiki_first/README.md). Точные Wiki по приватному корпусу
сохранены отдельно и не коммитятся; tracked recovery metadata находится в
[`PRIVATE_REFERENCES.md`](experiments/wiki_first/PRIVATE_REFERENCES.md).

## Сохранённый provenance compiler

Старый вертикальный путь остаётся доступен для исследований factual QA:

```bash
export MEMOREX_LLM_BASE_URL=https://openrouter.ai/api/v1
export MEMOREX_LLM_API_KEY=ваш_ключ

uv run memorex --workspace ./my-knowledge workspace models \
  --fast openai/gpt-4.1-mini \
  --strong openai/gpt-4.1 \
  --answer openai/gpt-4.1-mini

uv run memorex --workspace ./my-knowledge inbox scan
uv run memorex serve ./my-knowledge
```

Он использует metadata gate, атомарные claims, exact evidence, typed relations, proposals,
overrides, FTS5, eval и Web UI. Эти механизмы не считаются удалёнными или плохими; они временно не
являются центром продуктового эксперимента. Состояние до поворота дополнительно отмечено Git-тегом
`provenance-compiler-prototype`.

## Разработка

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Тесты полностью офлайн и используют fake runners/providers. Реальные agent CLI можно запускать
только на несекретном корпусе либо в приватном ignored workspace.

Текущий срез архитектуры описан в [`05_IMPLEMENTATION_STATUS.md`](05_IMPLEMENTATION_STATUS.md).
