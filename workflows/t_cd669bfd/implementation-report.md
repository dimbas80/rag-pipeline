# Implementation Report — t_cd669bfd

## Summary

Юнит 1: создан `firmware/src/qa_graph.py` с базовой инфраструктурой
LangGraph QA-системы — состояние, конфигурация, вызов LLM через
SiliconFlow Chat Completions с retry, разрешение API-ключа,
форматирование результатов поиска, безопасный парсинг JSON из ответа LLM
и пост-валидация цитат. Добавлены unit-тесты
`firmware/tests/test_qa_graph.py` (38 тестов, все проходят).

## Files changed

| File | Status |
|---|---|
| `firmware/src/qa_graph.py` | added (новый, юнит 1) |
| `firmware/tests/test_qa_graph.py` | added (новый, 38 тестов) |

`create_index.py` и `search.py` **не изменялись** (проверено `git status`).
`requirements.txt` уже содержал `langgraph>=1.2` (добавлен архитектором до
этой задачи) — не трогался.

## Реализовано (разделы архитектуры)

### QAGraphState (раздел 3)
TypedDict с полями: `query`, `messages` (Annotated[Sequence[BaseMessage],
add_messages]), `search_results`, `reformulate_count`, `query_analysis`,
`active_query`, `final_answer`, `needs_clarification`, `error`.

### QAGraphConfig (раздел 8)
`@dataclass` с параметрами по умолчанию строго по разделу 8:
`qdrant_path="./qdrant_data"`, `collection="technical_standard"`,
`retrieve_k=30`, `final_k=6`, `rrf_threshold=0.15`,
`chat_model="Qwen/Qwen3.5-35B-A3B"`, `chat_temperature=0.0`,
`chat_max_tokens=2048`, `siliconflow_api_key=None`,
`siliconflow_base_url="https://api.siliconflow.com"`,
`score_good_threshold=0.7`, `score_medium_threshold=0.4`,
`max_reformulate_attempts=2`.

### llm_chat (раздел 6.1)
Сигнатура и payload точно по разделу 6.1:
`POST {SILICONFLOW_CHAT_URL}` с `model/messages/temperature/max_tokens`,
headers `Authorization: Bearer` + `Content-Type: application/json`.
Retry-логика идентична `search.py`: 3 попытки, экспоненциальная задержка
(`API_RETRY_BACKOFF * attempt`), timeout 120. Ошибки ретраятся:
`requests.RequestException`, `KeyError`, `ValueError`, `IndexError`
(добавлен IndexError — пустой `choices`). При исчерпании —
`RuntimeError("SiliconFlow chat failed after N attempts: ...")`.
`SILICONFLOW_CHAT_URL` формируется из `SILICONFLOW_BASE_URL` env
(как `EMBED_API_URL`/`RERANK_API_URL` в search.py), что позволяет
тестам фиксировать URL до импорта.

### resolve_api_key (раздел 6.4)
Порядок: аргумент → `SILICONFLOW_API_KEY` env → `.env` (python-dotenv).
**Осознанное отклонение** от `search.py`: вместо `sys.exit(2)` бросается
`ValueError`. Причина: `qa_graph.py` — импортируемый модуль (класс
`QAGraph` в юните 3 вызывает `resolve_api_key` из конструктора);
`sys.exit` внутри библиотечной функции убил бы процесс хоста. Добавлен
опциональный параметр `env_path` (только для тестов/явного указания
файла; default-поведение `load_dotenv()` без изменений).

### _format_search_results (раздел 5.6)
Формат:
```
[1] {document_id} | {section_path} | score: {score}
   {heading_texts}
   {text}
   (источник: {title})
```
`heading_texts` нормализуется: dict → `" → ".join(values)` (как
`build_embed_text` в create_index.py), str — как есть. Отсутствующие поля
не роняют функцию: `document_id`/`title` → "—", пустой `section_path`
пропускается, пустой список → "".

### _parse_json_response
Три стратегии по порядку: (1) `json.loads` всего текста; (2) снятие
markdown-обёртки ```json ... ``` / ``` ... ```; (3) извлечение первого
валидного JSON-объекта `{...}` через `json.JSONDecoder.raw_decode`
(корректно обрабатывает строки/экранирование/вложенность, умеет
пропускать битые объекты). Не-объекты (list) → None. Возвращает
`dict | None`; fallback на значения по умолчанию — ответственность
вызывающего кода (например, `is_concrete=True` в `analyze_query`,
раздел 5.1).

### has_citations (раздел 5.6)
Регэксп `CITATION_PATTERN` скопирован из раздела 5.6 дословно.

## Validation performed

- `python -m pytest firmware/tests/test_qa_graph.py -v` → **38 passed**
- `python -m pytest firmware/tests/ -q` → **60 passed** (22 старых +
  38 новых), 9.9s
- Модуль импортируется и работает в рантайме (проверено отдельно:
  `SILICONFLOW_CHAT_URL`, `has_citations`, `QAGraphConfig`).

## Тестовое покрытие (test_qa_graph.py)

- `llm_chat` (mock `requests.post`): URL/payload/headers/timeout,
  default-параметры, retry до исчерпания → RuntimeError, retry при
  HTTP 500 → успех, malformed-ответ (`choices: []`) → retry.
- `resolve_api_key`: аргумент > env > .env, отсутствие → ValueError.
- `_format_search_results`: базовый формат, нумерация, пустой список,
  отсутствующие поля, heading_texts dict/str/None.
- `_parse_json_response`: plain, whitespace, markdown-обёртки (json/plain),
  проза вокруг JSON, скобки внутри строк, невалидный/пустой/list →
  None, битый-затем-валидный объект.
- `has_citations`: п., табл., таблица, ";", отрицательные кейсы
  (нет цитат, неверный формат, пустая строка).
- `QAGraphState`: конструкция, полный набор полей, Annotated
  c `add_messages`. `QAGraphConfig`: defaults + override.

## Known limitations

- Живой вызов SiliconFlow Chat Completions не выполнялся: известный
  факт проекта — `SILICONFLOW_API_KEY` из `.env` отклоняется API
  (`401 Api key is invalid`). Контракт проверен на mock'е requests,
  как в предыдущих юнитах (t_8f9616fe, t_33abf34d).
- Узлы графа (юнит 2), сборка StateGraph (юнит 3), CLI (юнит 4) — вне
  рамок этой задачи, не реализованы.

## Unresolved issues

- Нет. Отклонений от архитектуры нет, кроме осознанного `ValueError`
  вместо `sys.exit(2)` в `resolve_api_key` (см. выше).
