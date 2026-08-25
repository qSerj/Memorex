# Implementation Status

Updated: 2026-08-25

## Current milestone: usable local notes with optional AI

Продуктовый центр уже смещён с «Wiki, которую редактирует модель» на локальный заметочник в духе
Evernote. Note является основным объектом. AI-разбор и Discussions — дополнительные пути, а не
условие доступа к памяти.

Текущий пользовательский цикл:

```text
manual Note ───────────────────────────────→ edit / search / attach / history
Packet → local save → background analysis → AI proposal → Review → Notes
selected Notes → Discussion → AI answer ──→ optional new Note
```

Следующий ещё не реализованный продуктовый слой — минимальные Commitments, после него Attention.

## Реализовано

### Заметки без AI

- создание обычной Markdown-заметки;
- ручное редактирование title/body без вызова runner;
- новая immutable snapshot-версия и запись History при каждом сохранении;
- optimistic conflict guard против перезаписи более новой версии;
- автоматическая регистрация прежних тематических Wiki-страниц как Notes;
- новые и неразобранные Notes попадают во «Входящие»;
- страницы, созданные AI, сохраняют source footer и provenance validation.

### Организация, поиск и вложения

- блокноты: создание, переименование, перенос Notes и безопасное удаление с возвратом во
  «Входящие»;
- локальный FTS по заголовкам и тексту без модели, с текстовым fallback;
- произвольные вложения до 10 MiB через content-addressed immutable object store;
- inline preview изображений и скачивание остальных файлов;
- вложения, Notes и их организация входят в full-workspace backup/restore.

### Discussions

- постоянные обсуждения по одной или нескольким явно выбранным Notes;
- видимый и изменяемый список контекста;
- exact snapshot каждой выбранной Note фиксируется для конкретного вопроса;
- вопрос сохраняется до model call;
- pending/running turn после перезапуска становится failed и доступен для retry;
- ответ и ошибка сохраняются в истории;
- изображения из note attachments доступны vision-runner;
- готовый ответ можно открыть как новую редактируемую Note;
- Discussion никогда не изменяет память автоматически.

### Reliable Capture и AI proposals

- Packet из комментария, нескольких TXT/Markdown, PNG/JPEG/WebP и URL;
- немедленное локальное сохранение originals до model call;
- persistent SQLite queue, atomic claim и отсутствие двойного enqueue;
- понятные этапы, elapsed time, Stop, bounded retry и восстановление готового runner-stage;
- несколько proposals могут ждать Review и не блокируют следующие Packets;
- независимый stale proposal rebased на текущую Wiki, конфликтующий возвращается в очередь;
- natural-language revise и ручной Markdown editor proposal;
- Claude запускается без глобальных MCP/plugins/hooks/Serena; Codex доступен как runner/fallback.

### Переносимость и безопасность данных

- полный `.memorex.zip` всего workspace;
- восстановление в новый или существующий workspace;
- автоматическая страховочная копия перед полной заменой;
- snapshots, originals, SQLite, Packets, queue, jobs, discussions и vault переносятся вместе;
- приложение остаётся loopback-only, однопользовательским и без авторизации.

### Сохранённый исследовательский фундамент

Прежний provenance compiler не удалён и отмечен тегом `provenance-compiler-prototype`. В нём
остаются atomic claims, exact evidence offsets, entities, typed relations, contradiction review,
overrides и eval. Эти механизмы можно переиспользовать точечно; они не являются обязательным ядром
обычной Note.

## Проверено

- миграции на копии существующего рабочего workspace сохранили прежние Notes и chats;
- offline suite: 72 tests passed;
- Ruff lint и formatting passed;
- fake runners используются в CI, сеть и credentials для тестов не нужны.

## Следующий evidence-driven шаг

1. Несколько дней пользоваться заметками, вложениями, поиском и Discussions на реальных материалах.
2. Записать неудобства интерфейса и только затем править Note UX небольшими итерациями.
3. Отдельно согласовать минимальную схему Commitments и экран Today.
4. Реализовать ручные Commitments до автоматического AI extraction.
5. После проверки Today решать, нужен ли Attention и какой первый канал доставки.

## Сознательно не реализовано

- Commitments, Today и reminder delivery;
- URL fetching, PDF text/OCR, audio и YouTube ingestion;
- синхронизация, VPS mode, multi-user и мобильное приложение;
- embeddings, vector DB, GraphRAG и обязательная ontology;
- автоматическая активация AI-правок;
- скрытое расширение контекста Discussion до всей базы.

Каноническое направление: [`06_EXTERNAL_MEMORY_DIRECTION.md`](06_EXTERNAL_MEMORY_DIRECTION.md).
