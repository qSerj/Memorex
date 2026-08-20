# Memorex

Локальный provenance-first knowledge compiler. Memorex сохраняет неизменяемые копии
источников, извлекает из них проверяемые claims и отвечает на вопросы только с
ссылками на точные evidence spans.

Проект находится в первой рабочей итерации. Текущий статус и порядок продолжения
описаны в [`05_IMPLEMENTATION_STATUS.md`](05_IMPLEMENTATION_STATUS.md).

## Быстрый старт

Требуются Python 3.13, `uv` и SQLite с FTS5.

```bash
uv sync
uv run memorex init
uv run memorex add path/to/source.md
uv run memorex source list
```

Для extraction и query нужен сервер с OpenAI-compatible Chat Completions API и
поддержкой strict JSON Schema:

```bash
export MEMOREX_LLM_BASE_URL=http://localhost:11434/v1
export MEMOREX_LLM_MODEL=your-model
# export MEMOREX_LLM_API_KEY=...  # если сервер требует ключ

uv run memorex extract 1
uv run memorex claim list --source 1
uv run memorex claim show 1
uv run memorex ask "Какое хранилище использует Memorex?"
```

У всех команд просмотра есть `--json`. Другой каталог данных можно выбрать глобальным
флагом `--data-dir` или переменной `MEMOREX_DATA_DIR`.

## Локальное состояние

По умолчанию рабочее состояние находится в `.memorex/` и не попадает в Git:

```text
.memorex/
├── memorex.db
└── objects/<sha256-prefix>/<sha256>
```

`add` не перемещает и не изменяет исходный файл. Snapshot сохраняется один раз по
SHA-256, а последующая навигация идёт через SQLite, без сканирования object store.

## Разработка

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI выполняет те же проверки на Python 3.13. Тесты LLM pipeline используют fake
provider и не требуют сети или API key.

## Проектные документы

1. `00_PROJECT_CONTEXT.md` — цель и инварианты.
2. `01_ARCHITECTURE_IDEAS.md` — карта архитектурных подходов.
3. `02_MVP_AND_EVOLUTION.md` — этапы MVP.
4. `03_CODEX_STARTER_BRIEF.md` — инженерные ограничения.
5. `04_OPEN_QUESTIONS.md` — сознательно отложенные решения.
6. `05_IMPLEMENTATION_STATUS.md` — реализованное состояние и следующие шаги.
