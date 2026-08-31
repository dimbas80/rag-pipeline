# Review

## Verdict

PASS

## Scope

Независимая проверка реализации решений 30–32 (третья итерация interface_RAG):
живой стриминг node-событий чата по WS, индикатор «Думаю…», дедуп источников,
переименование шапки. Проверялся КОД (git diff `2b0da76` vs `db07512`) и ЖИВЫЕ
запуски (pytest через TestClient поверх реального `app.websocket("/ws/chat")`,
`node --check app.js`), а не отчёт кодера.

Diff: 7 файлов, +248/−22 — ровно те файлы, что заявлены (app.py, chat_api.py,
app.js, index.html, settings.html, test_chat_api.py, test_ws_chat.py).

## Requirements

### 1. app.py — живой стриминг node-событий по WS (решение 30)
PASS.
- Материализация `list(session.stream(text))` удалена; query-ветка теперь
  вызывает `await _stream_query(websocket, session, text)` (app.py:364).
- `_stream_query` (app.py:370–433): ленивый sync-генератор `session.stream()`
  крутится в `loop.run_in_executor`; чанки маршалируются в event-loop через
  `loop.call_soon_threadsafe(q.put_nowait, ...)` (asyncio.Queue трогается только
  loop-потоком) — вариант A архитектуры t_b551380a §3–4.
- `__interrupt__` → `clarification` (снятие `value` из `interrupts[0]`), флаг
  `interrupted=True`, финальный `session._format(state)` НЕ шлётся.
- Иначе после `_SENTINEL` шлётся `session._format(state)` с накопленным `state`
  (`state.update(values)` только для dict).
- Ошибка графа → `{"type":"error"}` клиенту + `raise`; disconnect → `stop.set()`
  + `producer.cancel()` без блокирующего `await producer` (finally с проверкой
  `stop.is_set()`).
- Reply-ветка (`session.resume`) не тронута.

### 2. app.js — «Думаю…» + сброс статуса (решение 30)
PASS.
- `sendChat()`: `setStatus("chat-status", "Думаю…", "")` сразу (app.js:170),
  до `addBubble`/отправки.
- `onmessage`: сброс `setStatus(..., "", "")` на `answer` (148), `clarification`
  (152), `error` (157).
- `node` → `setStatus("chat-status", "Обрабатывается узел: " + message.node, "")`
  (159).

### 3. chat_api.py — дедуп источников (решение 31)
PASS.
- `_format` (chat_api.py:89): `"sources": _dedup_sources(result.get("search_results") or [])`.
- `_dedup_sources` (107–131): ключ `document_id` (приоритет, strip) → fallback
  `title` (strip + lower); порядок первого вхождения сохраняется; записи без
  обоих ключей не дедуплицируются.
- `search_results` для images/цитат НЕ тронут: `select_images(result.get("search_results") or [], ...)`
  (строка 84) получает полный список.

### 4. index.html / settings.html — переименование шапки (решение 32)
PASS.
- index.html: `<title>` и `<h1>` → «База данных нормативно-технических
  документов»; подзаголовок сокращён до «Загрузка нормативных документов
  (ГОСТ / СП / СО / СНиП / ПУЭ) с распознаванием OCR→Markdown и семантическим
  поиском по базе.» (убрано «Три раздела: …»).
- settings.html: `<title>` → «Настройки — База данных нормативно-технических
  документов».

Примечание (не блокирует): в answer key задачи шапка записана как «нормативно
технических» (без дефиса). Авторитетный decision-log.md (решение 32) и
комментарий/commit `f52a93e` явно фиксируют написание через дефис
«нормативно-технических» — реализация соответствует авторитетному источнику, а
дефисная форма грамматически корректна. Расцениваю расхождение как
транскрипцию в answer key, не как дефект.

## Architecture

Соответствует архитектуре t_b551380a:
- Вариант A (asyncio.Queue + `loop.call_soon_threadsafe`) реализован точно (§3–4).
- Граничные условия: ошибка графа → error+close (§6.1), disconnect mid-stream →
  `stop`+`cancel` без блокирующего джойна (§6.2), без дедупа/сортировки
  node-событий (§6.3).
- Запрещённые к изменению модули (`chat_api.py` целиком, reply-ветка, протокол
  WS, `DeployConfig`/`qdrant_path`) не тронуты.

## Tests

- `python3 -m pytest firmware/tests -q` → **76 passed** (69 baseline + 7 new).
- test_ws_chat.py (4 теста) — реальный `app.websocket` через TestClient с
  FakeSession: порядок node→answer и накопление state в `_format`; interrupt →
  clarification без `_format`; ошибка графа → error + закрытие соединения;
  disconnect mid-stream (delay=0.05) не вешает сервер.
- test_chat_api.py (+3 теста) — дедуп по document_id (порядок первого
  вхождения), fallback title trim+case, и что `select_images` видит ПОЛНЫЙ
  `search_results` (n==2), а `sources` дедуплицирован (len==1).
- `node --check firmware/src/static/app.js` — OK.

## Findings

Нет блокирующих находок.

## Required Changes

Нет.

## Risks

- Предсуществующее (задокументировано в архитектуре §8.4, вне scope): при жёстком
  обрыве сети между чанками, когда сервер не пытается send, «Думаю…» на клиенте
  может остаться висеть. Не относится к приёмке решений 30–32.
- `producer.cancel()` на executor-future не прерывает поток (can't cancel thread) —
  фактический механизм остановки это `stop`-флаг + проверка между чанками;
  корректно для текущей семантики.

## Notes

- Запрещённые файлы (deploy_config.py, config_ui.py, jobs.py, registration.py,
  fs_perms.py, пайплайны) не тронуты: `git diff --name-only HEAD~1 HEAD` чисто по
  этим путям.
- Diff ограничен 7 заявленными файлами; scope creep отсутствует.
