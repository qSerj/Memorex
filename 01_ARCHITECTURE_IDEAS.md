# Архитектурные идеи и соседние подходы

Этот документ не является окончательной архитектурой. Это карта идей, которые стоит учитывать при проектировании.

## LLM Wiki / Karpathy-style Wiki

Полезные идеи:
- raw sources отдельно;
- wiki отдельно;
- отдельная схема поведения агента;
- ingest / query / lint как разные workflow;
- агент поддерживает производную базу знаний;
- человек не обязан вручную размечать все материалы;
- Wiki должна быть читаема человеком.

Слабые места минимальной версии: глобальный index, недостаток машиночитаемого состояния, слабая provenance-модель, отсутствие temporal semantics, риск рекурсивного загрязнения синтезами и слишком сильная зависимость навигации от LLM.

Вывод: сохранять философию, но не буквальную структуру.

## Memex

Исторически важная идея — не столько «страницы», сколько **ассоциативные trails**.

Практический вывод:
- сохранять не только ответы;
- сохранять путь через факты/документы/решения;
- trail должен быть именованным объектом;
- trail можно повторно использовать;
- trail можно проверять на устаревание;
- trail можно передавать другому человеку/агенту.

## RAG

Обычный RAG остается полезным. Не нужно строить систему по принципу «Wiki вместо RAG».

Лучше:
- RAG для similarity retrieval;
- graph для отношений;
- FTS для точного поиска;
- hierarchical summaries для глобальных вопросов;
- raw sources для проверки первоисточника.

## GraphRAG

Полезные идеи:
- сущности;
- отношения;
- claims;
- communities;
- summaries сообществ;
- разные режимы поиска для локальных и глобальных вопросов.

Особенно важная мысль: глобальный вопрос вроде «какие основные проблемы повторялись за полгода?» нельзя надежно решить простым top-k similarity search.

## RAPTOR / иерархические summaries

```text
evidence
↓
local summaries
↓
topic summaries
↓
higher-level summaries
↓
global overview
```

Запрос может спускаться к деталям только при необходимости.

## Temporal Knowledge Graph

Очень желательно поддерживать версии, период действия факта, `supersedes`, наблюдение во времени и provenance.

Особенно важно для решений, проектной архитектуры, ролей, цен, планов и статусов.

## Graph + vector + incremental update

Интересен не конкретный продукт, а принцип:
- low-level retrieval;
- high-level retrieval;
- hybrid retrieval;
- incremental update.

## Provenance

Каждый Claim должен ссылаться не просто на документ, а желательно на конкретный Evidence span:

```text
source_id
segment_id
page
section
line range
char range
```

Цель — проверяемость и возможность повторного анализа маленького фрагмента.

## Typed relations

`[[A]] → [[B]]` недостаточно.

Желательно:

```text
Project A ──USES──> PostgreSQL
Decision 17 ──MADE_AT──> Meeting 2026-08-10
Library X ──REJECTED_BECAUSE──> Licensing
Claim A ──SUPERSEDES──> Claim B
```

Не нужно проектировать идеальную ontology заранее.

## Immutable source, replaceable synthesis

- источники — immutable;
- evidence — derived, но проверяемый;
- claims — derived и версионируемый;
- synthesis — перестраиваемый;
- UI representations — перестраиваемые.

Если завтра изменится модель, Wiki можно перестроить без потери исходного корпуса.

## Ingest как pipeline

```text
discover
→ fingerprint
→ parse
→ normalize
→ segment
→ extract
→ resolve
→ link
→ validate
→ persist
→ optional render
```

Не все этапы должны использовать LLM.

## Query как pipeline

```text
question
→ classify intent
→ choose retrieval modes
→ retrieve candidates
→ merge
→ rerank
→ evidence check
→ token-budgeted context
→ synthesize
→ cite
```

## Lint нужно разделить

Детерминированный lint без LLM:
- broken links;
- missing evidence;
- duplicate checksum;
- invalid schema;
- missing fields;
- stale derived view;
- failed jobs;
- orphan records.

Семантический lint с LLM:
- возможные противоречия;
- дублирующие сущности;
- устаревшие claims;
- пропущенные связи;
- потенциально неверные merges;
- пробелы в знаниях.

Семантический lint должен создавать **proposal**, а не бесконтрольно менять базу.

## Инкрементальность

Система должна знать:
- что уже обработано;
- какой версией parser;
- какой версией extraction prompt/model;
- что нужно пересчитать после изменения pipeline.

## Что пока не нужно

На старте не обязательны Neo4j, отдельный vector DB, Kubernetes, микросервисы, сложный web UI, идеальная ontology, десять агентов и полностью автономный auto-fix.

SQLite + FTS5 + обычные файлы достаточно для первого полезного варианта.
