# Implementation report — живой стриминг + «Думаю…» + дедуп источников + шапка (t_2bad8413)

> Задача: реализовать 3 фикса (решения 30–32 из docs/product/decision-log.md)
> по дизайну архитектора t_b551380a.

## Сводка

1. **Стриминг узлов (решение 30, архитектура t_b551380a §4)** — `firmware/src/app.py`:
   - Убрана материализация `list(session.stream(text))` (было `app.py:364`).
   - Query-ветка WS вызывает новую module-level функцию `_stream_query(...)`:
     ленивый sync-генератор `session.stream(text)` крутится в executor-потоке;
     чанки маршалируются в event-loop через `loop.call_soon_threadsafe(q.put_nowait, ...)`
     (asyncio.Queue трогается только loop-потоком — вариант A архитектуры);
     consumer — `await q.get()`.
   - Семантика сохранена: `__interrupt__` → `{"type":"clarification"}` + `interrupted=True`
     (финальный `answer` НЕ шлётся); иначе в конце `session._format(state)` с накопленным
     state. Reply-ветка (синхронный `to_thread(session.resume)`) НЕ тронута.
   - Ошибка графа: item-исключение в очереди → `{"type":"error"}` клиенту → проброс →
     `websocket.close()` («не молчим»).
   - Disconnect mid-stream: `stop.set()` + `producer.cancel()` без блокирующего
     `await producer`; поток останавливается на границе чанка, `gen.close()` в finally.
   - `_SENTINEL = object()` — маркер «генератор исчерпан».

2. **Индикатор «Думаю…» (решение 30, фронт)** — `firmware/src/static/app.js`:
   - `sendChat()`: сразу после вычисления `type` — `setStatus("chat-status", "Думаю…", "")`
     (виден мгновенно и в ветке reconnect через `openChat()`).
   - `onmessage` clarification: добавлен сброс `setStatus("chat-status", "", "")`.
   - `answer`/`error` уже сбрасывали статус, `node` уже перезаписывал
     «Обрабатывается узел: X» — не менялись.

3. **Дедуп источников (решение 31)** — `firmware/src/chat_api.py`:
   - Новая функция `_dedup_sources(results)`: ключ `document_id` (приоритет, как есть),
     fallback `title` (trim + case-insensitive); порядок первого вхождения; записи без
     обоих ключей не дедуплицируются.
   - Применяется ТОЛЬКО к полю `sources` в `_format`; `search_results`, который уходит
     в `select_images`/цитаты, остаётся полным.

4. **Переименование шапки (решение 32)** — `index.html`/`settings.html`:
   - Точная строка по комментарию к задаче: **«База данных нормативно-технических
     документов»** (через дефис).
   - `index.html`: `<title>` и `<h1>` переименованы; из подзаголовка убран хвост
     «Три раздела: чат по базе, добавление документа, настройки.».
   - `settings.html`: `<title>` переименован (`<h1>` «Настройки» не меняется).

## Файлы

- `firmware/src/app.py` — стриминг (`_SENTINEL`, `_stream_query`, замена query-ветки)
- `firmware/src/chat_api.py` — `_dedup_sources` + применение в `_format`
- `firmware/src/static/app.js` — «Думаю…» в `sendChat`, сброс на clarification
- `firmware/src/static/index.html` — title/h1/подзаголовок
- `firmware/src/static/settings.html` — title
- `firmware/tests/test_ws_chat.py` — НОВЫЙ: 4 теста стриминга (FakeSession)
- `firmware/tests/test_chat_api.py` — +3 юнит-теста дедупа `_format`

## Тесты

`firmware/tests/test_ws_chat.py` (архитектура §10.1):
1. `test_streams_nodes_in_order_then_answer` — node:search → node:generate_answer → answer
   (ровно в этом порядке; регрессия на баг `list(...)`); state накоплен и передан в `_format`.
2. `test_interrupt_sends_clarification_without_answer` — clarification приходит,
   `_format` НЕ вызывался (answer не шлётся).
3. `test_graph_error_sends_error_then_closes` — node:search → error:boom → WS закрыт.
4. `test_disconnect_mid_stream_does_not_hang` — медленный fake-генератор + `ws.close()`
   после первого чанка; без зависания (stop/cancel не блокирует).

`firmware/tests/test_chat_api.py`:
- `test_format_dedups_sources_by_document_id` — дубль по document_id удалён, порядок
  первого вхождения (`c1, c3`).
- `test_format_dedups_sources_fallback_title_trim_case` — fallback title: trim + lower.
- `test_format_dedup_does_not_touch_search_results_for_images` — select_images видит
  ПОЛНЫЙ search_results (2 записи), sources — дедуп (1 запись).

## Валидация

- `python3 -m pytest firmware/tests -q` → **76 passed** (69 базовых + 7 новых; базовая
  была 69 до изменений).
- `node --check firmware/src/static/app.js` → синтаксис OK.
- `git diff --stat` по своим файлам (ниже).

## Границы соблюдены

- Протокол WS (типы `answer`/`clarification`/`error`/`node` и их поля) НЕ менялся.
- Reply-ветка `app.py` НЕ тронута; `chat_api.py` менялся только для решения 31
  (`_format` + `_dedup_sources`), `stream`/`resume`/`_format` сигнатуры не менялись.
- `_inject_env_file`, `deploy_config.py`, `config_ui.py`, `jobs.py`, `registration.py`,
  `fs_perms.py` — не тронуты.
- Пайплайны (`Create_Markdown_YA`, `Build_Search_index`) и значения `.env`/`config/.env`
  — не тронуты.
- `git add` — только свои 7 файлов, без `git add .`/`-A`; workflow-каталоги и секреты
  не коммитились.

## Известные ограничения

- Если клиент отключается во время длительного LLM-вызова (producer внутри `next()`),
  поток останавливается только после возврата из вызова — Python не прерывает нить
  принудительно (приемлемо, архитектура §6.2).
- Нет `onclose`/`onerror` на фронте: при жёстком обрыве сети «Думаю…» может остаться
  висеть — предсуществующий пробел, вне объёма (архитектура §8.3).

## Неразрешённые вопросы

Нет.

## Отклонения от архитектуры

Нет. `_stream_query` реализована дословно по псевдодиффу t_b551380a §4.3.

## git diff --stat

```
 firmware/src/app.py               | 88 ++++++++++++++++++++++++++++-------
 firmware/src/chat_api.py          | 29 +++++++++++-
 firmware/src/static/app.js        |  2 +
 firmware/src/static/index.html    |  6 +--
 firmware/src/static/settings.html |  2 +-
 firmware/tests/test_chat_api.py   | 47 +++++++++++++++++++
 firmware/tests/test_ws_chat.py    | 96 +++++++++++++++++++++++++++++++++++++++
 7 files changed, 248 insertions(+), 22 deletions(-)
```
