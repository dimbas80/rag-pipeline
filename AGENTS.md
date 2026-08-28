# Create_Markdown_YA

Проект конвертирует PDF/DOCX/DOC в структурированный Markdown через Yandex Vision OCR, выполняет скриптовую и опциональную AI-постобработку таблиц/формул/изображений и умеет строить RAG JSONL + реестр активов.

## Репозиторий и layout

- Рабочий код: `firmware/src/create_markdown.py`.
- Зависимости: `firmware/src/requirements.txt`; конфиги рядом с пайплайном: `create_markdown_config.yaml` (промпты + RAG-секции `defaults`/`references`) и `providers.yaml` (реестр LLM-провайдеров + назначение ролей). Per-document записи (`documents`) — в `<stem>_reg.yaml` рядом с Markdown.
- Тесты: `firmware/tests/`; тесты добавляют `firmware/src` в `sys.path`.
- Архитектурные решения: `docs/architecture/`; актуальный RAG-контракт — `docs/architecture/rag-v2-architecture.md` и ADR-010, реестр провайдеров — `docs/architecture/providers-registry.md`.
- Workflow-отчёты лежат в `workflows/`; `.hermes/STATE.md` — проектный snapshot, не производственный код.

## Dev environment

- Требуется Python 3.10+ (README); зависимости ставятся из `firmware/src/requirements.txt`.
- Из корня репозитория: `pip install -r firmware/src/requirements.txt`.
- Для DOCX/DOC нужен системный `libreoffice` (headless); PDF/DOCX-обработка требует доступа к Yandex Cloud.
- Секреты не коммитить. Для OCR нужны `YANDEX_API_KEY` и `YANDEX_FOLDER_ID`; для AI — ключи `DEEPSEEK_API_KEY` и/или `PROVOD_API_KEY`. Пайплайн читает `.env` из `firmware/src/.env`.

## Build, test, and checks

- Полный подтверждённый набор: `cd firmware && python3 -m pytest tests/ -q`.
- `test_ai_unified_stage.py` — тесты объединённого Этапа 6+7 (AI-коррекция таблиц + постобработка); входят в полный регрессионный прогон.
- RAG-тесты: `cd firmware && python3 -m pytest tests/test_rag_v2.py tests/test_rag_jsonl.py -q`.
- Синтаксическая проверка: `python3 -m py_compile firmware/src/create_markdown.py`.
- Отдельный запуск теста: `cd firmware && python3 -m pytest tests/<test_file>.py -q`.
- В репозитории не обнаружены Makefile, package manager scripts, tox-конфигурация или отдельный lint-конфиг; не добавляйте команды для них без соответствующей настройки.

## CLI

Запускать из `firmware/src` или указывать путь к скрипту:

- `python3 create_markdown.py -i document.pdf` — OCR и Markdown без AI.
- `python3 create_markdown.py -i document.pdf --ai` — OCR + vision/AI-постобработка.
- `python3 create_markdown.py -i result.md --ai` — только AI-постобработка готового Markdown.
- `python3 create_markdown.py -i Markdown/file/file.md --rag` — RAG только из проверенного Markdown.
- `python3 create_markdown.py -i document.pdf --ai --rag` — AI-постобработка и RAG-файлы.
- `--config` задаёт конфиг промптов/RAG (по умолчанию `./create_markdown_config.yaml`); RAG-секции читаются из него же.
- `--providers-config` задаёт реестр провайдеров (по умолчанию `./providers.yaml`); нужен для `--ai`/`--reg`.

Для `.md` внутри каталога `Markdown/` с `--rag` OCR, AI, `.md` и `image/` не используются/не изменяются; атомарно перезаписываются только производные RAG-файлы. Каталог входных файлов не поддерживается текущей реализацией, несмотря на старый текст README.

## Outputs and conventions

- Результат генерации: `<input-dir>/Markdown/<stem>/<stem>.md` и `image/`; временные данные — `<input-dir>/tmp/<stem>/`.
- RAG v2 создаёт `rag_chunks.jsonl` и `<stem>_assets.json` из Markdown; токенизатор — `Qwen/Qwen3-Embedding-8B` через `transformers.AutoTokenizer`.
- В `create_markdown_config.yaml` лимит `max_chunk_tokens: 7000`; `allow_degraded_fallback: false`, поэтому отсутствие токенизатора должно завершать RAG с ошибкой, а не молча переходить на chars/token.
- Код использует Python type hints, функции в `snake_case`, константы верхнего регистра и подробные русскоязычные docstring/комментарии. Запись результатов выполняется атомарно через временный файл и `os.replace()`.
- Таблицы вырезаются в `image/table_N.png` всегда для PDF/DOCX; `--ai` добавляет vision/LLM-коррекцию, а не управляет самой вырезкой.

## Pitfalls

- AI-конфиги требуют непустых промптов; при отсутствии prompt пайплайн завершает AI-проход ошибкой.
- Для PDF/DOCX без `YANDEX_API_KEY` или `YANDEX_FOLDER_ID` запуск завершается до OCR; для AI без primary-ключа используется настроенный fallback, если он доступен.
- RAG по готовому `.md` требует корректного единого конфига и, для v2, доступного токенизатора Hugging Face; документ сопоставляется по `source_file` из per-document `<stem>_reg.yaml` (overlay накладывается автоматически).
- Не редактировать `.env`, `__pycache__/`, `.pytest_cache/`, `tmp/` и `.worktrees/` как исходники; они игнорируются Git. Не помещать секреты в конфиги или отчёты.
- `--rag` безопасен для проверенного Markdown, но намеренно перезаписывает производные `rag_chunks.jsonl` и `*_assets.json`.
- Перед изменением RAG-контракта читать ADR-010: он заменяет старый page-based/char-based контракт и фиксирует semantic assets + token chunking.

## Change boundaries

- Производственный код размещать в `firmware/src/`, тесты — в `firmware/tests/`; не использовать `workflows/` для кода.
- После изменений пайплайна запускать `python3 -m py_compile firmware/src/create_markdown.py` и релевантные pytest-команды выше; для RAG проверять выходные JSONL/asset-файлы на реальном Markdown, а не только unit-тестами.
