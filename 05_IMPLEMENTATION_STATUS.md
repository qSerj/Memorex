# Implementation Status

Updated: 2026-08-21

## Current Milestone: Wiki-first vertical slice

Главный эксперимент теперь проверяет не строгость атомарного factual QA, а качество накопленной
Wiki:

```text
inbox → checksum/immutable objects → whole-document strong-agent understanding
      → staged Markdown Wiki proposal → human review/revise/apply
      → immutable active snapshot → read-only agent navigation/query
```

Первый milestone считается успешным продуктово только после проверки на реальных материалах:
«модель действительно поняла происходившее и разумно встроила новое содержание». Тесты и
валидаторы подтверждают механику, но не заменяют эту оценку.

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
- browser/Web UI and Telegram bot for the new service;
- PDF/DOCX/HTML;
- embeddings, vector DB, GraphRAG, communities and typed Wiki ontology;
- automatic activation, per-claim lifecycle and mandatory exact quote;
- cost routing or replacement of the strong semantic administrator by small/local models;
- GigaChat runtime adapter. `/v2` remains a research target and is not discarded after one weak
  contract run.

## Next evidence-driven work

1. Freeze the pending Claude output on the tracked synthetic fixture after its CLI quota resets;
   the Codex baseline, incremental Wiki and query answer are already frozen.
2. Use the CLI on a small real ignored corpus; inspect the proposal in Obsidian and apply only after
   human review.
3. Add a second real material on existing topics and judge accumulation, bloat, contradictions and
   provenance honestly.
4. Compare new outputs against the frozen Codex/Claude references. Change prompts or organization
   before adding retrieval infrastructure if the Wiki is not intelligent enough.
5. Extract a stable application-service boundary for a future Telegram administrator only after the
   CLI interaction proves useful.
