# Implementation Report — t_bd91dad9

## Summary

Юнит 4 завершён: в `firmware/src/qa_graph.py` добавлен консольный
интерфейс (раздел 10 архитектуры langgraph-rag-architecture.md):
`build_parser()`/`main()` с аргументами `--query`, `--interactive`,
`--qdrant-path`, `--api-key`, `--verbose`, `__main__`-блок,
`run_single_query()` (один запрос с логированием узлов и циклом
уточнений) и `run_interactive()` (диалоговый режим с выходом по
`exit`/Ctrl+C/EOF). Для логирования шагов после interrupt добавлен
вспомогательный `QAGraph.resume_stream()`. Попутно исправлен баг
юнитов 1–2: `generate_answer` и `reformulate_query` отправляли в
SiliconFlow только system-сообщение, что давало HTTP 400
(code 20015 «No user query found in messages») — теперь рядом с
system-промптом передаётся user-сообщение с запросом.

Тесты: `firmware/tests/test_qa_graph.py` — 119 тестов (было 95,
+24 юнита 4), весь набор `firmware/tests/` — 141 passed.
Приёмка выполнена на реальном Qdrant (`/mnt/sdb/!База_ГОСТ/...`)
и живом SiliconFlow API: `python qa_graph.py --query "высота
молниеотвода зона Б" --qdrant-path <path>` возвращает корректный
ответ с цитатами; `--interactive` и Ctrl+C завершаются чисто
(exit 0, «До свидания!»).

## Files changed

| File | Status |
|---|---|
| `firmware/src/qa_graph.py` | modified (+~210 строк: юнит 4 + resume_stream) |
| `firmware/src/qa_graph.py` | modified (фикс 400: user-сообщение в generate_answer/reformulate_query) |
| `firmware/tests/test_qa_graph.py` | modified (+24 теста юнита 4, +2 обновления assert'ов по фиксу) |
| `workflows/t_bd91dad9/implementation-report.md` | new (этот файл) |

`create_index.py` и `search.py` **не изменялись** (git diff пуст).

## Реализовано (раздел 10 архитектуры)

### `build_parser()` / `main(argv, input_fn, print_fn)`
- `--query` — один запрос (без `--interactive`);
- `--interactive` — диалоговый режим;
- `--qdrant-path` (default `./qdrant_data`) — путь к Qdrant;
- `--api-key` — ключ SiliconFlow (иначе `resolve_api_key()`: env → .env);
- `--verbose` — подробный вывод шагов графа;
- коды возврата: 0 — успех/корректный выход, 1 — ошибка выполнения,
  2 — нет ключа/нет аргументов; Ctrl+C → 130 с сообщением;
- без `--query` и `--interactive` — печать help, возврат 0
  (конвенция search.py).

### `run_single_query(qa, query, verbose, input_fn, print_fn)`
- Первый проход через `qa.stream(query)` — логирование каждого узла
  (`_log_node_step`: analyze_query/search/evaluate_results/
  reformulate_query/ask_clarification/generate_answer; в `--verbose`
  дополнительно построчно результаты поиска со score);
- на `__interrupt__` — печать уточняющих вопросов, ввод ответа
  пользователя (`input_fn`), продолжение через `qa.resume_stream()`
  — так логируются и узлы после уточнения;
- цикл повторяется до `final_answer`; возвращает итоговый ответ.

### `run_interactive(qa, verbose, input_fn, print_fn)`
- цикл «запрос → ответ/уточнение → …» с приглашением «Вы: »;
- выход по `exit`/`quit`/`выход`/`q`, Ctrl+C, Ctrl+D (EOF) — всегда
  «До свидания!» без traceback;
- пустой запрос пропускается.

### `QAGraph.resume_stream(user_response)`
- аналог `resume()` через `graph.stream(Command(resume=...),
  stream_mode="updates")` — CLI продолжает логировать узлы после
  уточнения (не SSE/web-streaming; внутренний LangGraph-streaming
  как в `stream()` юнита 3).

### Фикс HTTP 400 SiliconFlow (юниты 1–2)
- Найден при приёмочном прогоне: `generate_answer` и
  `reformulate_query` отправляли `[{"role": "system", ...}]` без
  user-сообщения → SiliconFlow отвечает
  `400 {"code":20015,"message":"No user query found in messages."}`.
- Исправление (минимальное, промпты не изменены): рядом с
  system-промптом добавляется `{"role": "user", "content": <запрос>}`
  (active_query для reformulate, query для generate_answer).
  `analyze_query`/`ask_clarification` уже использовали
  `_build_llm_messages` (system+user) и работали.
- Тесты `test_reformulate_query_prompt_and_params` и
  `test_generate_answer_prompt_and_params` обновлены: теперь
  проверяют 2 сообщения и user-контент (было «только system»).

## Validation performed

- `python3 -m pytest firmware/tests/test_qa_graph.py -q` → **119 passed**.
- `python3 -m pytest firmware/tests/ -q` → **141 passed** (117 + 24 новых).
- `python3 -m py_compile qa_graph.py test_qa_graph.py` — OK.
- `python3 qa_graph.py --help` — все 5 аргументов видны.
- Приёмка (реальный Qdrant + живой SiliconFlow):
  `python3 firmware/src/qa_graph.py --query "высота молниеотвода зона Б"
  --qdrant-path "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data" --verbose`
  → analyze_query → search (6 результатов, top score 0.969) →
  evaluate_results → generate_answer → ответ с цитатами
  [СО 153-34.21.122-2003, …] и корректным выводом об отсутствии
  термина «зона Б» в индексе.
- `--interactive` с piped-вводом: запрос → ответ → `exit` →
  «До свидания!», exit code 0.
- Ctrl+C в интерактивном режиме: «До свидания!», exit code 0
  (отправлен SIGINT через pkill).
- `git diff --stat firmware/src/create_index.py firmware/src/search.py`
  — пусто (не тронуты).

## Known limitations

- Известный косметический шум при завершении процесса:
  `Exception ignored in QdrantClient.__del__ ... ImportError:
  sys.meta_path is None` — это поведение qdrant-client local mode
  при завершении интерпретатора, не связано с этим изменением
  (воспроизводится и без CLI, в create_index/search).
- Живой API иногда отвечает медленно (первые 60+ сек до ответа
  analyze_query), из-за чего запуск без кэша может выглядеть как
  зависание; API_TIMEOUT/API_RETRIES уже настроены.
- При запуске из каталога проекта `.env` подхватывается
  автоматически; при запуске из другого каталога нужен `--api-key`
  или `SILICONFLOW_API_KEY` в окружении.

## Unresolved issues

- Нет. Отклонений от архитектуры нет. Локальные решения в рамках
  архитектуры:
  1. `QAGraph.resume_stream()` — расширение API юнита 3 для
     логирования шагов после уточнения (тот же механизм, что
     `stream()`; не SSE).
  2. Фикс user-сообщения — исправление контракта с SiliconFlow
     (400 code 20015), промпты разделов 5.4/5.6 не изменены.
