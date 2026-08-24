# Implementation Status

Updated: 2026-08-24

## Current Milestone: Reliable Capture on Wiki-first

Главный эксперимент теперь проверяет не строгость атомарного factual QA, а качество накопленной
Wiki:

```text
Packet → local immutable save → persistent queue → whole-document strong-agent understanding
       → staged Markdown Wiki proposal → human review/revise/apply
       → immutable active snapshot → read-only agent navigation/query
```

Первый milestone считается успешным продуктово только после проверки на реальных материалах:
«модель действительно поняла происходившее и разумно встроила новое содержание». Тесты и
валидаторы подтверждают механику, но не заменяют эту оценку.

Продуктовый ориентир — персональная внешняя память, принимающая один Packet из мысли, нескольких
файлов и ссылок. Capture, Memory/Wiki, будущие Commitments и Attention остаются разными слоями.
Пользователь не обязан поддерживать дисциплину разбора вручную. Полная формулировка и порядок
небольших итераций находятся в
[`06_EXTERNAL_MEMORY_DIRECTION.md`](06_EXTERNAL_MEMORY_DIRECTION.md).

## Implemented in Wiki-first

- Отдельный `.memorex/wiki-first/state.sqlite`; новый путь не зависит от claims schema.
- Рекурсивное обнаружение UTF-8 TXT/Markdown, SHA-256, source revisions, immutable raw и normalized
  objects, отсутствие обязательной metadata-формы.
- Целые документы передаются semantic administrator; 2k-сегменты старого pipeline не являются
  границей понимания.
- Claude Code и Codex CLI adapters с настраиваемыми сильными моделями. Default: Claude для ingest,
  Codex для query; одна автоматическая попытка второго runner при технической или contract-ошибке.
- Один незавершённый proposal за раз. Ingest/tell создают staging Wiki и отчёт, но не активируют их.
- Deterministic validator для имён страниц, H1, Wiki-links, citation labels, source paths/line
  bounds и growth warnings.
- Natural-language `review → revise → apply/reject`; apply всей версии атомарен и запрещён при
  изменившемся base snapshot.
- Immutable Wiki snapshots, activation history, rollback, tree-hash integrity guard и абсолютный
  read-only путь для просмотра в Obsidian.
- `wiki tell` для пользовательской мысли/поправки/приоритета через тот же управляемый ingest.
- `wiki ask` запускает read-only навигацию по копии активной Wiki/sources; answer логируется как
  derived result и не становится knowledge.
- Web Packet intake атомарно сохраняет комментарий, несколько TXT/Markdown и URL как одно
  добавление и сразу подтверждает локальную запись, не ожидая runner.
- Постоянная SQLite-очередь отделяет Capture от анализа: один Packet имеет один текущий статус,
  переживает перезапуск, захватывается worker атомарно и не дублируется быстрыми повторами.
- Технический сбой runner автоматически повторяется через 5 и 30 секунд, затем Packet остаётся
  сохранённым с ручной командой повтора. Contract/local failures не зацикливаются.
- История model jobs сгруппирована внутри Packet; UI показывает понятный статус и причину, а во
  время работы раз в две секунды обновляет фактический этап, runner и прошедшее время. Процент не
  выдумывается, поскольку agent CLI не сообщает достоверную долю готовности.
- Если runner уже закончил содержательную работу, но локальная сборка proposal упала, повторный
  запуск сначала проверяет и восстанавливает сохранённый stage без нового модельного вызова.
- Поддерживаемые тексты передаются в один proposal; URL пока ожидают importer, а no-op обработка не
  создаёт идентичный snapshot.
- Лог runner, model, CLI version, prompt version, duration, stdout/stderr и failure; секреты не
  записываются.
- Приватные Codex/Claude reference Wikis законсервированы в ignored nested Git + bundle. Публичный
  полностью синтетический benchmark хранится в репозитории.

## Preserved provenance-compiler prototype

Реализация до поворота не сломана и отмечена тегом `provenance-compiler-prototype`. В ней остаются:

- metadata-gated inbox и Web UI;
- atomic claims, lifecycle, exact evidence offsets;
- entities, typed relations, contradiction/supersedes review;
- user overrides, dossier, FTS query, answer validation;
- isolated model evaluation and OpenAI-compatible providers.

Эта система — источник будущих компонентов, но не обязательная основа Wiki-first.

## Deliberately deferred

- Telegram-specific parser, threads/replies and media;
- Telegram bot, системные уведомления и внешние каналы Attention;
- PDF/DOCX/HTML;
- embeddings, vector DB, GraphRAG, communities and typed Wiki ontology;
- automatic activation, per-claim lifecycle and mandatory exact quote;
- cost routing or replacement of the strong semantic administrator by small/local models;
- GigaChat runtime adapter. `/v2` remains a research target and is not discarded after one weak
  contract run.

## Next evidence-driven work

1. Проверить Reliable Capture на реальном потоке: закрыть приложение посреди анализа, запустить
   снова и убедиться, что Packet дообрабатывается без потери или дубликата.
2. Проверить Packets на коротких заметках и связанных наборах файлов, включая материал, который
   агент разумно не переносит в Wiki.
3. Отдельно согласовать минимальный слой Commitments с provenance и коротким подтверждением
   неуверенных действий; не добавлять Attention/reminder delivery заранее.
4. Freeze the pending Claude output on the tracked synthetic fixture after its CLI quota resets;
   the Codex baseline, incremental Wiki and query answer are already frozen.
5. Use the CLI on a small real ignored corpus; inspect the proposal in Obsidian and apply only after
   human review.
6. Add a second real material on existing topics and judge accumulation, bloat, contradictions and
   provenance honestly.
7. Compare new outputs against the frozen Codex/Claude references. Change prompts or organization
   before adding retrieval infrastructure if the Wiki is not intelligent enough.
8. Extract a stable application-service boundary for a future Telegram administrator only after the
   CLI interaction proves useful.
