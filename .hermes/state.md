# Build_Search_index — состояние проекта

Обновлено: 2026-08-14 (Orchestrator)

## Работающая функциональность
- Поисковый индекс ГОСТ/СП в Qdrant: `create_index.py` + `search.py` (SiliconFlow embeddings/rerank, fastembed BM25, валидация `--strict`). СП 89.13330.2016 переиндексирован (t_d6074b7e).
- LangGraph QA-система: `qa_graph.py` (6 узлов), configurable LLM через `firmware/src/llm_config.yaml` (чат — DeepSeek API), расчёты по формулам `execute_calculation` (изолированный exec).
- Telegram-бот: `firmware/src/telegram_bot/bot.py` — citation-based image routing + точечный fallback изображений по caption при пустом `cited_chunk_ids` (t_de873a2d, ревью PASS t_b3608b01): пачку search_results не шлёт.
- Бот запущен (PID 22318), Qdrant-база: `/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data` (`QDRANT_PATH` в .env).

## Known issues
- 4 предсуществующих падения в `firmware/tests/` (test_qa_graph.py) — не связаны с фиксом бота (подтверждено git stash); связаны с незакоммиченными правками qa_graph.py/create_index.py.
- Карточка t_290d5d7c (blocked) — устарела: работа выполнена в t_de873a2d, ревью PASS t_b3608b01. Ребёнка t_3ca67835 не диспатчить (дубликат ревью). Можно архивировать.

## Recent decisions
- 2026-08-06: чат-LLM — DeepSeek API через `llm_config.yaml` (configurable), не SiliconFlow.
- 2026-08-06: расчёты по формулам в generate_answer через изолированный exec (timeout 5 с, запрет input()/while True).
- 2026-08-07: переиндексация документа с дедупом — удаление старых точек перед upsert.
- 2026-08-08: при `cited_chunk_ids=[]` бот НЕ шлёт все search_results; извлекает «Таблица N»/«Рисунок N» из answer/query и шлёт только однозначный asset по caption; при неоднозначности — не шлёт.

## Незакоммиченные изменения (master)
- Изменены: `README.md`, `firmware/src/create_index.py`, `firmware/src/llm_config.yaml`, `firmware/src/qa_graph.py`
- Новые: `firmware/src/telegram_bot/` (бот + asset_helpers), `firmware/tests/test_bot_assets.py`, `workflows/t_b3608b01/`, `workflows/t_d6074b7e/`, `workflows/t_de873a2d/`
