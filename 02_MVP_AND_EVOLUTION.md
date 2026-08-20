# Практический MVP и направление развития

## Цель MVP

Получить систему, которой можно пользоваться сразу, а не архитектурный эксперимент ради архитектуры.

Первый полезный сценарий:
1. бросить несколько текстовых/Markdown/PDF/транскрипционных источников;
2. система надежно понимает, что новое;
3. извлекает текст;
4. создает структурированные claims/entities/relations;
5. позволяет задать вопрос;
6. возвращает ответ с нормальными ссылками на evidence;
7. генерирует читаемые Markdown-страницы;
8. повторный ingest не ломает и не дублирует базу.

## Этап A — надежный ingest

Сначала вообще не нужен сложный граф.

Нужно:
- source registry;
- sha256;
- статусы обработки;
- normalized text;
- segments;
- provenance;
- идемпотентность.

Критерий успеха: один и тот же корпус можно прогнать повторно без изменения результата.

## Этап B — claims и entities

Добавить сущности, атомарные claims, evidence links, confidence, timestamps и базовый entity resolution.

Критерий успеха: можно открыть утверждение и дойти до точного места в источнике.

## Этап C — простые связи

Добавить typed relations, provenance, confidence и несколько базовых relation types.

Не пытаться заранее придумать полную ontology.

## Этап D — retrieval без гигантского index.md

Сначала:
- FTS5;
- поиск по entity;
- графовые соседи;
- простой router.

Позже:
- embeddings;
- reranker;
- hybrid retrieval.

## Этап E — generated Wiki

Генерировать страницы сущностей, тем, решений, индекс разделов и summaries.

Wiki должна быть полностью восстанавливаема из DB.

## Этап F — temporal knowledge

Добавить `valid_from`, `valid_to`, `supersedes`, `current/historical`, contradiction candidates.

## Этап G — trails

Сохранять полезный маршрут retrieval, набор evidence, результат synthesis, время, запрос и зависимости.

## Этап H — hierarchical summaries

Добавить section/topic/project/global summaries тогда, когда база до этого реально доросла.

## Минимальное хранилище

На старте SQLite.

Минимальные таблицы по смыслу:

```text
sources
source_versions
segments
entities
claims
claim_evidence
relations
jobs
syntheses
trails
trail_steps
```

Не обязательно реализовать все сразу.

## Пример Source

```text
id
path
sha256
mime_type
size
created_at
first_seen_at
last_seen_at
status
parser_name
parser_version
```

## Пример Segment

```text
id
source_id
ordinal
text
page
section
char_start
char_end
token_count
```

## Пример Claim

```text
id
subject
predicate
object
confidence
observed_at
valid_from
valid_to
status
created_by_model
extraction_version
```

## Пример Relation

```text
id
source_entity_id
predicate
target_entity_id
confidence
evidence_id
valid_from
valid_to
```

## Пример Synthesis

```text
id
type
title
body
generated_at
generator_model
generator_version
input_claim_ids
```

Главное: Synthesis не становится Claim автоматически.

## Первые реальные тесты

### Идемпотентность

Дважды добавить один источник. Второго ingest и дублей быть не должно.

### Изменение файла

Добавить измененную версию. Должна появиться новая revision, старая остается доступна, зависимые данные помечаются для пересчета.

### Отмена решения

Источник 1: «используем X».

Источник 2: «от X отказались, используем Y».

Ожидание: X остается историческим, Y становится current.

### Provenance

Ответ по факту должен вести до конкретного сегмента/страницы/строк.

### Глобальный вопрос

Несколько документов описывают разные проявления одной проблемы. Система должна уметь собрать их вместе, а не выдать один top-k chunk.

### Ложный synthesis

Гипотеза модели сохраняется как synthesis/idea, но не превращается в подтвержденный Claim.

## Практический принцип развития

Не строить заранее идеальную knowledge system.

Добавлять слой только при появлении конкретной боли:
- пока FTS хватает — embeddings не нужны;
- пока SQLite graph traversal хватает — Neo4j не нужен;
- пока summaries мало — communities не нужны;
- пока relation types простые — ontology framework не нужен.
