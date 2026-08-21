# Memorex

Memorex — локальный knowledge compiler с проверяемым происхождением знаний. Он не
подменяет источники сгенерированной Wiki: сохраняет неизменяемые оригиналы, извлекает
атомарные утверждения с точными цитатами, отдельно хранит сущности, связи, решения и
пользовательские исправления, а Markdown/веб-интерфейс считает производными представлениями.

Рабочий вертикальный сценарий v0.2 рассчитан на коллекцию TXT/Markdown-файлов: диалоги,
заметки и внешние материалы превращаются в досье с проблемами, целями, идеями,
предложенными, действующими, отклонёнными и заменёнными решениями.

## Почему это не просто `raw/ → wiki/index.md`

- Новые и изменённые файлы определяются по SHA-256, а не рассуждением модели.
- Каждый проект — изолированный workspace со своей SQLite-базой и object store.
- LLM вызывается только после заполнения метаданных и явного нажатия «Обработать».
- Любой claim привязан к точному диапазону символов в неизменяемой версии источника.
- Summary — производный текст, он никогда не становится evidence.
- Противоречия и замена старого решения создают proposals; изменение графа требует review.
- Исправления пользователя авторитетнее машинного извлечения, имеют причину и историю.
- Поиск не читает единый растущий `index.md`: первичный retrieval выполняет SQLite FTS5.
- Кандидатные модели можно сравнивать в изолированном eval, не загрязняя рабочие знания.

## Быстрый старт с OpenRouter

Требуются Python 3.13, `uv` и SQLite с FTS5.

```bash
uv sync --locked --all-groups

export MEMOREX_LLM_BASE_URL=https://openrouter.ai/api/v1
export MEMOREX_LLM_API_KEY=ваш_ключ

uv run memorex workspace init ./my-knowledge --name "Моя база"
uv run memorex --workspace ./my-knowledge workspace models \
  --fast openai/gpt-4.1-mini \
  --strong openai/gpt-4.1 \
  --answer openai/gpt-4.1-mini

uv run memorex serve ./my-knowledge
```

Откройте `http://127.0.0.1:8765`, положите `.txt` или `.md` в
`my-knowledge/inbox/`, задайте тип, автора, доверенность и дату источника, затем
подтвердите платную обработку. Inbox обновляется автоматически, пока страница открыта.

Секреты не записываются в `memorex.toml`. Если переменные лежат в bash-файле:

```bash
source ./openrouter.env.sh
```

В самом файле должны быть строки `export MEMOREX_LLM_BASE_URL=...`,
`export MEMOREX_LLM_API_KEY=...`; ключ нельзя коммитить.

## Тот же workflow через CLI

```bash
uv run memorex --workspace ./my-knowledge inbox scan --json
uv run memorex --workspace ./my-knowledge inbox metadata 1 \
  --title "Разговор о продукте" --kind conversation --authority primary \
  --author "Иван" --from 2026-08-21 --tag business
uv run memorex --workspace ./my-knowledge inbox compile 1
uv run memorex --workspace ./my-knowledge dossier
uv run memorex --workspace ./my-knowledge ask "Почему мы отказались от первой идеи?"
```

Ручной model eval на одинаковых сегментах:

```bash
uv run memorex --workspace ./my-knowledge eval run 1 \
  --model openai/gpt-4.1-mini --model qwen/qwen3-30b-a3b
```

Eval проверяет strict schema, точность evidence и стоимость токенов/времени, но не
активирует полученные claims.

## Данные workspace

```text
my-knowledge/
├── memorex.toml            # имя, язык и роли моделей; без ключей
├── inbox/                  # контролируемая staging-зона
└── .memorex/               # runtime, не коммитить
    ├── memorex.db          # registry, claims, graph, review, eval
    └── objects/ab/<sha256> # неизменяемые snapshots источников
```

Перемещение уже обработанного файла распознаётся по checksum, если оно однозначно;
изменение содержимого создаёт новую revision. После прерванной обработки запись inbox
возвращается в состояние, из которого её можно безопасно повторить.

## Legacy CLI

Низкоуровневые команды первой итерации (`init`, `add`, `extract`, `claim`, `ask`)
сохранены для диагностики и обратной совместимости. Для реального использования лучше
создавать workspace и работать через `inbox compile`: этот pipeline извлекает типы
решений, сущности, связи и summaries.

## Разработка

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Тесты полностью офлайн и используют fake LLM providers. Реальный endpoint smoke test
должен выполняться только на несекретном тексте.

Проектные решения описаны в `00_PROJECT_CONTEXT.md`–`04_OPEN_QUESTIONS.md`, актуальный
срез реализации — в [`05_IMPLEMENTATION_STATUS.md`](05_IMPLEMENTATION_STATUS.md).
