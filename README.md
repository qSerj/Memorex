# Memorex

Memorex развивается как локальная персональная внешняя память: обычный заметочник работает без
AI, а агент-библиотекарь помогает разбирать неопрятные материалы и обсуждать сохранённое. Заметки
можно создавать, редактировать, искать, раскладывать по блокнотам и снабжать вложениями даже при
полностью недоступной модели.

Packet — альтернативный быстрый вход для неразобранного материала: одно добавление объединяет
пользовательскую мысль, несколько файлов, изображений и ссылок. AI может предложить, как встроить
его в Notes, но пользователь всегда может создать и буквально отредактировать Note сам. Будущие
Commitments и Attention образуют отдельные слои намерений и возвращения внимания. Каноническое
направление и границы итераций описаны в
[`06_EXTERNAL_MEMORY_DIRECTION.md`](06_EXTERNAL_MEMORY_DIRECTION.md).

Основные пути сознательно разделены:

```text
manual Note ───────────────────────────────→ edit / search / attach / history
Packet → immutable originals → AI proposal → Review → Notes
selected Notes → AI Discussion ───────────→ optional new Note
```

Существующий provenance-oriented compiler с claims, exact evidence, graph review, eval и Web UI не
удалён. Он сохранён как отдельный прототип и по-прежнему доступен через старые команды. Текущий
заметочный путь не зависит от его онтологии или таблиц.

## Быстрый старт

Для обычных Notes требуются Python 3.13 и `uv`; AI-функции дополнительно требуют авторизованный
Claude Code или Codex CLI. По умолчанию semantic ingest
выполняет Claude Opus с максимальным effort, query — Codex `gpt-5.6-sol` с максимальным reasoning.
Если основной ingest-runner технически завершается ошибкой или выдаёт невалидную Wiki, Memorex один
раз пробует второй сильный runner. GigaChat пока не исключён из будущих исследований, но не входит в
runtime fallback первой версии. Внутренние вызовы Claude запускаются в safe mode: глобальные MCP,
плагины, hooks и Serena не загружаются и не расходуют время на задачи Memorex.

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

Для обычной локальной работы предварительный `workspace init` не нужен: Web-приложение само
предложит создать или выбрать workspace и запомнит последний выбранный путь.

```bash
uv run memorex app ./my-knowledge
# следующие запуски могут быть просто: uv run memorex app
```

Приложение доступно только на `127.0.0.1` и использует локальные Jinja/CSS/JS. Основной интерфейс
содержит редактируемые Markdown-заметки, блокноты, локальный полнотекстовый поиск, вложения,
обсуждения с явно выбранным контекстом, AI-черновики, History и страницу «Перенос». Ручное
сохранение сразу создаёт immutable snapshot; Review требуется только для предложений модели.
После изменения Memorex атомарно обновляет read-only Obsidian vault в `WORKSPACE/vault/`.

Страница «Перенос» скачивает весь workspace одним `.memorex.zip`: конфигурацию, Inbox, обе SQLite
базы, Packets, очередь, originals, snapshots, jobs, Discussions и vault. Этот архив можно восстановить
в новую папку на другом компьютере или целиком поверх существующего Memorex workspace. Перед
заменой существующая база автоматически архивируется в соседнюю `.memorex-backups/`; слияния двух
баз первая версия не делает. Backup/restore недоступен, пока выполняется анализ или query.

Экран «Добавить» сохраняет Packet — один необязательный комментарий, несколько TXT/Markdown,
PNG/JPEG/WebP и несколько URL. Для каждой картинки можно оставить анализ включённым, выключить
его и сохранить изображение только как связанный артефакт либо дать модели отдельную инструкцию
вроде «распознай таблицу». Общий комментарий служит общим контекстом для Packet. Локальное
сохранение подтверждается сразу и не ждёт модель; после этого страницу можно закрыть.
Анализируемые элементы одного Packet обрабатываются совместно постоянной фоновой очередью и
создают один proposal. Runner работает последовательно, но готовый proposal не останавливает
следующие Packets: несколько результатов могут независимо ждать Review. Очередь переживает
перезапуск приложения, делает ограниченные повторы технических сбоев и хранит историю попыток
внутри карточки Packet.
Пока идёт анализ, карточка каждые две секунды обновляет фактический этап, runner и прошедшее время;
если модель уже закончила, а локальная сборка результата сорвалась, сохранённый результат можно
восстановить без повторного обращения к модели.
URL в текущем срезе только запоминаются и явно ожидают будущий importer — Memorex пока не скачивает
их содержимое. Для картинок не запускается отдельный OCR-конвейер: модель инспектирует оригинал,
а распознанный текст и описание остаются производным знанием с прямой ссылкой на изображение.

Перед ingest не нужно заполнять title, author, date, type или authority. Принимаются UTF-8 `.txt`
и `.md`, а также `.png`, `.jpg`, `.jpeg` и `.webp` размером до 10 MiB; модель читает каждый текст
целиком и получает выбранные изображения как визуальные входы. Команда
`wiki tell` тем же reviewable путём передаёт Wiki произвольную мысль, поправку или предпочтение:

```bash
uv run memorex --workspace ./my-knowledge wiki tell \
  "Считай стоимость важнее скорости, пока я явно не изменю этот приоритет"
```

Ни ingest, ни tell не меняют активную базу автоматически. Они создают proposal. `apply` проверяет
Markdown-контракт и неизменность базового snapshot, затем атомарно активирует всю версию. Доступны
`wiki reject`, `wiki history`, `wiki rollback`, `wiki validate` и `wiki status`.
Если за время ожидания Review активная Wiki изменилась, независимый proposal переносится на неё
детерминированно. При изменении одной и той же тематической страницы Packet автоматически
возвращается в очередь вместо тихого перезаписывания более свежего знания.

`wiki status` печатает абсолютный путь активной read-only Wiki. Эту папку удобно открыть в Obsidian
как диагностический vault. Редактировать сам vault руками не следует: Web-интерфейс предоставляет
обычный Markdown-редактор с предпросмотром, сохраняющий правку как новую версию без вызова AI.

Новая заметка попадает во «Входящие» или выбранный блокнот. К ней можно приложить файлы размером
до 10 MiB: изображения показываются прямо в интерфейсе, остальные вложения сохраняются неизменно
и скачиваются. Экран «Обсуждения» позволяет закрепить одну или несколько заметок; модель получает
только этот видимый контекст. Вопрос и состояние попытки сохраняются до запуска модели, поэтому
оборванный ответ можно повторить. Любой готовый ответ открывается как предварительно заполненная
новая заметка, но никогда не переписывает память автоматически.

Каноническое содержание Notes остаётся Markdown, а Web UI строит из него безопасное
HTML-представление.
Изображение-источник можно указать ссылкой `[скан](../sources/name.png)` или встроить в страницу как
`![описание](../sources/name.png)`. В отличие от текста, image citation не использует номера строк.

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
- прямое ручное редактирование заметок с immutable snapshots и отдельный review AI-изменений;
- блокноты, локальный поиск и immutable-вложения без зависимости от модели;
- постоянные обсуждения с явным контекстом и повтором оборванного ответа;
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
