# Build_Search_index — состояние проекта

Обновлено: 2026-08-15 (Orchestrator)

## Работающая функциональность
- Поисковый индекс ГОСТ/СП в Qdrant: `create_index.py` + `search.py` (SiliconFlow embeddings/rerank, fastembed BM25, валидация `--strict`). Упрощённый CLI (папка + авто-выбор коллекции).
- LangGraph QA-система: `qa_graph.py` (6 узлов), configurable LLM через `llm_config.yaml` (чат — DeepSeek API), расчёты `execute_calculation`.
- Telegram-бот: `firmware/src/telegram_bot/bot.py` — citation-based image routing + точечный fallback по caption (t_de873a2d). Фиксы 3.1-3.3 (освобождение Qdrant per-request, query-приоритет 1 изображения, ответ на «покажи таблицу Б.1») + многостраничные таблицы (все image_paths) — reviewer PASS, всё done на доске.
- Бот запущен (PID 30640), Qdrant-база: `/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data` (`QDRANT_PATH` в корневом .env).

## Документы в базе (эталон для «перечня документов»)
- СО 153-34.21.122-2003 — Инструкция по устройству молниезащиты зданий, сооружений и промышленных коммуникаций
- ГОСТ 31996—2012 — Кабели силовые с пластмассовой изоляцией на номинальное напряжение 0,66; 1 и 3 кВ
- СП 89.13330.2016 — Котельные установки
- ГОСТ 18410—73 — КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ. ТЕХНИЧЕСКИЕ УСЛОВИЯ
(document_id содержит длинное тире «—», не дефис.)

## Known issues
- LLM-ответ может содержать markdown-таблицы (`| ... |`), которые Telegram parse_mode=Markdown не рендерит → мусор в тексте. Таблицы должны уходить только картинкой (механизм `_send_images` уже работает). → фикс A (задача ниже).
- На запрос «какие документы в базе» бот гонит запрос в LLM, который выдумывает список; фактического списка из Qdrant нет. → фикс B (задача ниже).
- 4 предсуществующих падения в `firmware/tests/test_qa_graph.py` (не чинить — вне объёма текущих фиксов; отчёт t_508d475b).

## In progress / Planned
- (coder) fix(bot): не вставлять markdown-таблицы в текст + список документов по запросу — фиксы A и B.
- (reviewer, child) ревью фиксов A+B.

## Recent decisions
- 2026-08-15: фиксы A+B правятся в слое бота без этапа Architect (точечные правки, не системная архитектура): `coder → reviewer`.
- 2026-08-15: список документов берётся из Qdrant (уникальные document_id+title, per-request client + close в finally); интент-детект — чистые фразы, не через LLM; вырезание markdown-таблиц — детерминированная пост-обработка в bot.py (промпты LLM не трогаем).
- 2026-08-06: чат-LLM — DeepSeek API через `llm_config.yaml`; расчёты через изолированный exec.
- 2026-08-08: при `cited_chunk_ids=[]` бот НЕ шлёт все search_results; шлёт только однозначный asset по caption.
