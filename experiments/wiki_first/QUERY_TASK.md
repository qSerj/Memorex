# TASK — answer by navigating one candidate Wiki

Prompt version: `wiki-first-query-probe-v1`

Use the candidate `wiki/` as accumulated knowledge. Start with its landing page or filename/title
search, read only relevant pages, follow useful `[[links]]`, and open `raw/` only when the Wiki is
insufficient or a statement needs verification. Do not use Internet knowledge.

Write `answers.md`. For every question include:

- a concise answer with citations already present on Wiki pages;
- `Wiki pages visited`;
- `Raw sources opened`, explicitly `none` when no fallback was needed;
- whether the Wiki was sufficient and what knowledge was missing.

Questions:

1. Какое основное архитектурное решение принято и какие альтернативы остались предложениями?
2. Какая предметная путаница оказалась глубже первоначальной формулировки проблемы?
3. На каких этапах наблюдаемого процесса повторяется ручной труд?
4. Какие идеи выглядят быстрыми улучшениями, а какие требуют серьёзной интеграции?
5. Какие позиции, предположения или приоритеты уточнялись по ходу обсуждения?
6. Какая отдельная идея не вошла в основной проект и насколько она проработана?
