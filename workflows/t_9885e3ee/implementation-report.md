# Implementation Report — t_9885e3ee

## Summary

Юнит 2 завершён: в `firmware/src/qa_graph.py` (поверх юнита 1) реализованы
все 6 узлов графа — `analyze_query` (5.1), `search_node` (5.2),
`evaluate_results` (5.3), `reformulate_query` (5.4), `ask_clarification`
(5.5, с `interrupt()`), `generate_answer` (5.6, с пост-валидацией цитат).
Промпты скопированы из архитектуры дословно (проверено скриптом,
4/4 verbatim-совпадения), параметры LLM по узлам — точно по таблице
раздела 6.2. Добавлены unit-тесты узлов с моками LLM и Qdrant:
в `firmware/tests/test_qa_graph.py` теперь 77 тестов (38 юнит 1 +
39 юнит 2), все зелёные; весь набор `firmware/tests/` — 99 passed.
`create_index.py` и `search.py` не изменялись.

## Files changed

| File | Status |
|---|---|
| `firmware/src/qa_graph.py` | modified (юнит 1 + юнит 2) |
| `firmware/tests/test_qa_graph.py` | modified (+39 тестов узлов) |

`create_index.py` и `search.py` **не изменялись** (проверено `git diff` — пусто).

## Реализовано (разделы архитектуры)

### Узел 1: `analyze_query(state, config)` (5.1)
- LLM-вызов с системным промптом 5.1 (verbatim), история диалога
  из `state["messages"]` конвертируется через `_message_to_dict`
  (human→user, ai→assistant) и подставляется перед текущим запросом.
- Парсинг `is_concrete` / `key_terms` / `suggested_clarification`
  через `_parse_json_response` (юнит 1). Не-JSON / ошибка LLM →
  fallback `is_concrete=True` (валидация 5.1).
- `active_query = query`; `needs_clarification` здесь НЕ ставится (шаг 4).
- Параметры: temperature 0.0, max_tokens 256 (6.2).

### Узел 2: `search_node(state, config)` (5.2)
- Прямой импорт функций из `search.py` (раздел 2.2, не subprocess):
  `hybrid_search`, `filter_by_rrf_score`, `rerank_siliconflow`,
  `result_to_dict`.
- Последовательность: `hybrid_search(query=active_query, top_k=retrieve_k,
  collection=cfg.collection)` → `filter_by_rrf_score(threshold=rrf_threshold)`
  → `rerank_siliconflow(top_n=final_k)` → `result_to_dict`.
- Конфигурируемые параметры (раздел 5.2/8): `retrieve_k=30`, `final_k=6`,
  `rrf_threshold=0.15`, `collection="technical_standard"`.
- Обработка ошибок: пустые кандидаты → `search_results=[]`; падение
  rerank → fallback на RRF-порядок (сортировка по RRF-score, убывание);
  падение hybrid_search (Qdrant недоступен) → `error` + `search_results=[]`
  (раздел 7.1). Retry 3× — внутри функций `search.py`, как в архитектуре.
- Ленивые синглтоны `_get_qdrant_client(path)` / `_get_sparse_model()` —
  один экземпляр на вызов графа (раздел 2.2).

### Узел 3: `evaluate_results(state, config)` (5.3)
- Чистая функция без LLM, возвращает строку-маршрут:
  пусто → `ask_clarification`; `max_score > 0.7` → `generate_answer`;
  `0.4 <= max_score <= 0.7` и `reformulate_count < 2` → `reformulate_query`;
  иначе (попыток ≥ 2 или `max_score < 0.4`) → `ask_clarification`.
- Пороги берутся из конфига (`score_good_threshold=0.7`,
  `score_medium_threshold=0.4`, `max_reformulate_attempts=2`) — значения
  по умолчанию совпадают с архитектурой. Может использоваться как
  conditional-edge функция в юните 3.

### Узел 4: `reformulate_query(state, config)` (5.4)
- Системный промпт 5.4 (verbatim) с подстановкой `{query}` (активный
  запрос) и `{top_documents_summary}` (сводка лучших документов:
  title, document_id, score, первые 200 символов текста; fallback
  «(результаты поиска отсутствуют)»).
- Пустой ответ / ошибка LLM → `reformulate_count` +1, `active_query`
  без изменений; иначе `active_query` = ответ (снимаются обрамляющие
  кавычки), `reformulate_count` +1.
- Параметры: temperature 0.3, max_tokens 256 (6.2).

### Узел 5: `ask_clarification(state, config)` (5.5)
- Системный промпт 5.5 (verbatim) с `{query}`; LLM возвращает JSON
  `{"questions": [...]}`. Не-JSON / ошибка → дефолтный вопрос
  («Уточните, пожалуйста, номер или название документа...»).
- `interrupt("\n".join("• " + q ...))` — граф останавливается (механика
  из раздела 5.5). При возобновлении: ответ пользователя добавляется
  в `messages` (HumanMessage), `active_query = f"{query}. Уточнение:
  {user_response}"`, `needs_clarification=None`, `reformulate_count=0`.
- Параметры: temperature 0.3, max_tokens 512 (6.2).

### Узел 6: `generate_answer(state, config)` (5.6)
- Системный промпт 5.6 (verbatim) с `{query}` и `{search_results_formatted}`
  (формат `_format_search_results` из юнита 1).
- Пустые `search_results` → fallback-ответ без вызова LLM (5.6).
- Пост-валидация цитат `has_citations`; при отсутствии — однократная
  перегенерация с доп. инструкцией «В ответе ОБЯЗАТЕЛЬНО укажи источник...»
  (max 1 доп. попытка). Вторая попытка без цитат возвращается как есть.
- Ошибка LLM → fallback-ответ + `error` (раздел 7.1).
- Параметры: temperature 0.0, max_tokens 2048 (6.2).

### Вспомогательное
- `_get_qa_config(config)` — разрешение конфигурации: принимает
  `QAGraphConfig` напрямую (тесты) или LangGraph `RunnableConfig`
  `{"configurable": {"qa_config": cfg}}` (юнит 3); default — `QAGraphConfig()`.
- `_fill_template` — подстановка `{name}` без `str.format` (промпты 5.1/5.5
  содержат фигурные скобки в примерах JSON, которые сломали бы `.format`).
- Константы параметров LLM по узлам: `ANALYZE_/REFORMULATE_/CLARIFICATION_/
  ANSWER_TEMPERATURE`, `*_MAX_TOKENS` (6.2).

## Validation performed

- `python3 -m pytest firmware/tests/test_qa_graph.py -q` → **77 passed**
  (38 юнит 1 + 39 юнит 2), ~1.3s.
- `python3 -m pytest firmware/tests/ -q` → **99 passed** (22 старых +
  77 новых), ~9.9s.
- Промпты: скрипт-сверка всех 4 системных промптов с код-блоками
  архитектуры (5.1/5.4/5.5/5.6) — **4/4 verbatim-совпадения**.
- Параметры LLM по узлам: сверены с таблицей 6.2 (analyze 0.0/256,
  reformulate 0.3/256, clarification 0.3/512, answer 0.0/2048).
- `python3 -m py_compile` обоих файлов — OK.
- `git diff --stat firmware/src/create_index.py firmware/src/search.py` —
  пусто (не тронуты).
- Smoke: все 6 узлов импортируются и вызываются; `evaluate_results`
  работает как conditional-edge роутер.

## Тестовое покрытие (новые тесты юнита 2)

- `analyze_query`: конкретный/абстрактный запрос, не-JSON fallback,
  ошибка LLM, нормализация неверных типов, промпт+параметры+история.
- `search_node`: полный конвейер (мок hybrid_search/filter/rerank +
  реальный `result_to_dict` на FakePoint), пустые кандидаты, fallback
  при падении rerank (RRF-порядок), проброс конфига (retrieve_k/final_k/
  rrf_threshold/collection), ошибка hybrid_search → error, active_query
  fallback на query. Qdrant/sparse замоканы через `_patch_search_deps`
  (патч `_get_qdrant_client`/`_get_sparse_model`) — реальный Qdrant не нужен.
- `evaluate_results`: пусто, хорошо/средне/плохо, границы 0.7/0.4/0.71/0.39,
  отсутствующий score, кастомные пороги из конфига.
- `reformulate_query`: успех, пустой ответ, ошибка LLM, снятие кавычек,
  промпт (query + документы) и параметры.
- `ask_clarification`: interrupt-payload, слияние ответа в active_query,
  сброс счётчика, добавление HumanMessage в messages, промпт+параметры,
  не-JSON fallback-вопрос, пустой ответ пользователя.
- `generate_answer`: пустые результаты (LLM не вызывается), цитаты с
  первого раза, перегенерация при отсутствии цитат (2 вызова, доп.
  инструкция), 2 попытки без цитат, промпт+параметры, ошибка LLM → fallback.
- Вспомогательные: `_get_qa_config` (варианты), `_message_to_dict` (роли),
  `_fill_template` (сохранение JSON-скобок).

## Known limitations

- Живой вызов SiliconFlow Chat Completions не выполнялся: известный факт
  проекта — `SILICONFLOW_API_KEY` из `.env` отклоняется API
  (`401 Api key is invalid`). Контракт проверен на моках, как в юнитах 1
  и ранее (t_8f9616fe, t_33abf34d).
- `interrupt()` в тестах замокан; реальное поведение interrupt внутри
  скомпилированного StateGraph будет проверено в юните 3 (интеграционные
  тесты с checkpointer/resume).
- Сборка графа (юнит 3), CLI (юнит 4) — вне рамок этой задачи.

## Unresolved issues

- Нет. Отклонений от архитектуры нет. Отмечу два локальных решения в
  рамках архитектуры:
  1. Fallback RRF-порядка в `search_node` сортирует кандидатов по
     RRF-score (убывание) — «RRF-порядок» из раздела 5.2.
  2. `_fill_template` вместо `str.format` — только для того, чтобы
     фигурные скобки в примерах JSON промптов не ломали подстановку;
     текст промптов при этом дословный (проверено).
