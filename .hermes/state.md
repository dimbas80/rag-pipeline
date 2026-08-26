# Build_Search_index — состояние проекта

Обновлено: 2026-08-26 (Orchestrator)

## Работающая функциональность
- Поисковый индекс ГОСТ/СП в Qdrant: `create_index.py` + `search.py` (гибрид dense+sparse + реранк, валидация `--strict`). Упрощённый CLI (папка + авто-выбор коллекции).
- LangGraph QA-система: `qa_graph.py` (6 узлов), расчёты `execute_calculation`. Конфигурация LLM разделена: `search_config.yaml` (только `nodes`) + `providers.yaml` (`roles.build_search_index`: `query_processing`/`embedding`/`rerank`, fallback) через общий модуль `llm_providers.py`.
- Telegram-бот: `firmware/src/telegram_bot/request_bot.py` — citation-based image routing, команда `/list`, вырезание markdown-таблиц, авторизация по `TELEGRAM_ALLOWED_USERS` (`update.effective_user.id`).
- Тесты: 240 passed / 0 failed (вкл. `test_llm_providers.py` — 14 контрактных тестов fallback).
- Бот НЕ запущен. Перезапуск вручную: `python3 firmware/src/telegram_bot/request_bot.py`. Qdrant-база: `/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data` (`QDRANT_PATH` в корневом `.env`).

## Документы в базе (эталон для «перечня документов»)
- СО 153-34.21.122-2003 — Инструкция по устройству молниезащиты зданий, сооружений и промышленных коммуникаций
- ГОСТ 31996—2012 — Кабели силовые с пластмассовой изоляцией на номинальное напряжение 0,66; 1 и 3 кВ
- СП 89.13330.2016 — Котельные установки
- ГОСТ 18410—73 — КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ. ТЕХНИЧЕСКИЕ УСЛОВИЯ
(document_id содержит длинное тире «—», не дефис.)

## Known issues
— (нет открытых)

## Recent decisions
- 2026-08-26: разделение конфигурации LLM — `search_config.yaml` (nodes) + `providers.yaml` (роли `build_search_index` + fallback) + `llm_providers.py`; CLI `--config`/`--providers_config`; `bot.py` → `request_bot.py` + `TELEGRAM_ALLOWED_USERS`; миграция тестов (240 passed).
- 2026-08-15: список документов берётся из Qdrant (уникальные document_id+title); интент-детект чистых фраз; вырезание markdown-таблиц — пост-обработка в боте.
- 2026-08-08: при `cited_chunk_ids=[]` бот шлёт только однозначный asset по caption.
