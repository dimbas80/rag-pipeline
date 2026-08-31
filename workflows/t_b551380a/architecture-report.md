# Architecture-report — живой стриминг узлов чата по WS + индикатор «Думаю…» (t_b551380a)

> Задача: превратить «фиктивный» стриминг в настоящий — `node`-события должны уходить
> клиенту ПО МЕРЕ выполнения графа, а не пачкой после его завершения; вернуть мгновенный
> индикатор «Думаю…» в `sendChat()` (регрессия `d75fa1c`).
>
> Границы: протокол WS (типы `answer`/`clarification`/`error`/`node` и их поля) НЕ меняется;
> reply-ветка `app.py:360-363` НЕ трогается; `_inject_env_file`/`_pipeline_paths`/`qdrant_path`/
> `DeployConfig` НЕ трогаются; дедуп источников и переименование шапки — в отдельной задаче кодера.
> Здесь код не пишется — только схема + точные псевдодиффы.

---

## 1. Корневая причина (подтверждено по коду)

`firmware/src/app.py:364`:

```python
updates = await asyncio.to_thread(lambda: list(session.stream(text)))
```

`list(...)` **материализует весь LangGraph-стрим** в памяти worker-потока ДО того, как
хоть одно событие уйдёт клиенту. `session.stream(text)` → `chat_api.py:96-98` → `qa_graph.py:1525-1541`
возвращает **ленивый** sync-генератор `self.graph.stream(..., stream_mode="updates")`: каждый
`next()` выполняет ровно один узел (LLM-вызов / поиск Qdrant / rerank — блокирующий). `list()`
проходит генератор до конца за один вызов → цикл `for update in updates` (`app.py:367-380`)
стартует только после того, как весь граф отработал.

Следствие: `node`-события («Обрабатывается узел: X») приходят клиенту пачкой уже ПОСЛЕ
завершения графа; во время обработки фронт молчит (нет ни node-событий, ни «Думаю…», который
убран регрессией `d75fa1c`).

---

## 2. Требования → проектные решения

| # | Требование | Решение |
|---|---|---|
| 1 | `node`-события уходят по мере выполнения графа | Ленивый producer в worker-потоке + потокобезопасная передача чанков обратно в event-loop (§4) |
| 2 | Семантика `__interrupt__` → `clarification` + финальный ответ НЕ шлётся | Сохранена (§5.2) |
| 3 | Исключение в генераторе → закрыть WS, не молчать | Producer пробрасывает исключение как item очереди; consumer шлёт `error` затем закрывает (§6.1) |
| 4 | Клиент отключился mid-stream → корректно оборвать поток | `stop`-флаг + `gen.close()` + `producer.cancel()`, без блокирующего `await producer` (§6.2) |
| 5 | Повторы/порядок node-событий не важны | Никакой дедупликации и сортировки — шлём как пришло (§6.3) |
| 6 | Индикатор «Думаю…» мгновенно в `sendChat()`, снятие на answer/clarification/error | Правки `app.js` (§8) |
| 7 | Протокол WS неизменен | Ни один тип/поле не меняется |

---

## 3. Ключевой факт: `asyncio.Queue` не потокобезопасна

`session.stream(text)` — **sync**-генератор, его итерация блокирует (LLM/Qdrant), поэтому он
обязан крутиться в отдельном потоке (executor). Но передавать чанки обратно в event-loop
нельзя через `asyncio.Queue.put_nowait`, вызванный из worker-потока — `asyncio.Queue`
не потокобезопасна (мутация из чужого потока = гонка данных).

### 3.1 Варианты

| Вариант | Механизм | Оценка |
|---|---|---|
| **A. `asyncio.Queue` + `loop.call_soon_threadsafe(q.put_nowait, item)`** | producer-поток НЕ трогает очередь напрямую: каждый чанк маршалируется в event-loop через `call_soon_threadsafe`, где `put_nowait` выполняется уже на loop-потоке. Consumer — `await q.get()` | ✅ **рекомендуется** (проще всего, нет ручного wake/clear) |
| B. stdlib `queue.Queue` + `asyncio.Event` + `get_nowait`-цикл | thread-safe `queue.Queue`; producer кладёт item и будит loop через `loop.call_soon_threadsafe(wake.set)`; consumer сливает `get_nowait()` до `Empty`, затем `await wake.wait()` | рабочий, но ручной wake/clear/drain — больше места для гонки |
| C. `asyncio.Queue` + `put_nowait` из worker-потока напрямую | — | ❌ **запрещено** (не потокобезопасна) |

### 3.2 Решение — вариант A

`call_soon_threadsafe` гарантирует, что **единственная** мутация `asyncio.Queue` происходит на
потоке event-loop, поэтому очередь фактически потокобезопасна. Никакого ручного wake/clear:
`await q.get()` сам блокирует loop до появления item, а `call_soon_threadsafe` корректно будит
его при каждом `put_nowait` из чужого потока. Очередь неограниченная (unbounded) — для чата это
нормально (несколько чанков на запрос), `put_nowait` на unbounded-очереди никогда не блокирует.

Альтернатива B (её буквально называет задача) полностью описана в §9 для случаев, когда кодеру
потребуется жёсткий backpressure (не требуется здесь).

---

## 4. Дизайн сервера (псевдодифф `firmware/src/app.py`)

### 4.1 Импорты / module-level (верх файла)

```python
# строки 1-13: import asyncio (уже есть), import threading (уже есть — строка 8) — ОСТАВИТЬ
# НОВОЕ (module-level, рядом с app = FastAPI(...)):
_SENTINEL = object()          # маркер «генератор исчерпан»
```

Никаких новых внешних зависимостей: `asyncio`, `threading`, `object()` — stdlib.

### 4.2 Обработчик `chat` (строки 349-383)

```python
@app.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    await websocket.accept()
    session = ChatSession(cfg)
    try:
        while True:
            message = await websocket.receive_json()
            kind, text = message.get("type"), str(message.get("text", "")).strip()
            if kind not in {"query", "reply"} or not text:
                await websocket.send_json({"type": "error", "text": "Ожидается непустой текст"})
                continue
            if kind == "reply":
                # ── reply-ветка: НЕ трогать (синхронный resume + одиночный ответ) ──
                result = await asyncio.to_thread(session.resume, text)
                await websocket.send_json(result)
                continue
            # ── query-ветка: потоковая отдача (НОВОЕ) ──
            await _stream_query(websocket, session, text)
    except Exception:
        await websocket.close()
```

Что меняется относительно текущего кода:
- Строки 364-380 (материализация + цикл) **заменяются** на один вызов `await _stream_query(...)`.
- Reply-ветка (360-363) и валидация (356-359) — без изменений.
- Внешний `except Exception: await websocket.close()` остаётся страховкой: он ловит
  проброшенные из `_stream_query` ошибки (producer-error после отправки `error`, либо
  disconnect после `stop`/`cancel`) и закрывает сокет.

### 4.3 Новая функция `_stream_query` (module-level, тестируемая)

```python
async def _stream_query(websocket: WebSocket, session: ChatSession, text: str) -> None:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()       # трогается ТОЛЬКО loop-потоком
    stop = threading.Event()                 # «клиент ушёл» → producer прекращает тянуть

    def produce() -> None:                   # выполняется в worker-потоке (executor)
        gen = session.stream(text)           # ленивый sync-генератор LangGraph
        try:
            for update in gen:
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(q.put_nowait, update)   # marshal на loop-поток
        except Exception as exc:             # узел графа бросил → item-исключение в очередь
            loop.call_soon_threadsafe(q.put_nowait, exc)
        finally:
            gen.close()                      # корректно оборвать генератор (finally-цепочку)
            loop.call_soon_threadsafe(q.put_nowait, _SENTINEL)

    producer = loop.run_in_executor(None, produce)

    state: dict = {}
    interrupted = False
    try:
        while True:
            item = await q.get()
            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                # (A) ошибка графа: сообщить клиенту (снять «Думаю…»), затем пробросить
                try:
                    await websocket.send_json({"type": "error", "text": str(item)})
                except Exception:
                    pass
                raise item
            for node, values in item.items():
                if node == "__interrupt__":
                    await websocket.send_json(
                        {"type": "clarification",
                         "text": str(getattr(values[0], "value", values[0]))})
                    interrupted = True
                    continue
                await websocket.send_json({"type": "node", "node": node})
                if isinstance(values, dict):
                    state.update(values)
        if not interrupted:
            await websocket.send_json(session._format(state))
    except Exception:
        # (C) любое исключение из send_json / проброшенная ошибка графа:
        # оборвать поток, не ждать producer (LLM-вызов может идти долго)
        stop.set()
        producer.cancel()
        raise
    finally:
        if not stop.is_set():
            await producer          # нормальное завершение — джойн потока (уже вернулся)
```

Ключевые свойства:

1. **Потокобезопасность**: `q.put_nowait` вызывается только через `call_soon_threadsafe`,
   т.е. всегда на loop-потоке; `await q.get()` — единственный потребитель. Никакой гонки.
2. **Ленивость**: `produce` не материализует — `for update in gen` тянет по одному чанку;
   каждый чанк немедленно доставляется consumer'ом как `node`/`clarification`.
3. **Порядок**: чанки уходят в том же порядке, что выдал генератор (повторы/порядок не важны —
   §6.3, дедуп не делаем).
4. **`gen.close()`** в `finally` гарантирует, что при `break` (disconnect) и при ошибке
   LangGraph-генератор закрывается (его собственные `finally`/cleanup отрабатывают), а не
   оставляется «висеть» до GC.

### 4.4 Почему НЕ `await producer` в disconnect-пути

`producer.cancel()` на уже бегущем executor-потоке **не** убивает поток (Python не умеет
принудительно останавливать нить) — он лишь помечает future отменённым и не ждёт. Поэтому в
пути `except Exception` мы НЕ делаем `await producer` (иначе повисли бы до конца текущего
LLM-вызова). Поток сам остановится: `stop.set()` → на следующей итерации `break` → `gen.close()`.
`await producer` выполняется только в `finally` нормального пути (`stop` не выставлен), где
producer уже дошёл до `_SENTINEL` и фактически завершился — джойн мгновенный.

---

## 5. Сохранение семантики (пункт 2 задачи)

### 5.1 Накопление state и финальный ответ

Дословно как в текущем коде (`app.py:365-380`):

- `state` инициализируется пустым словарём на каждый query-запрос;
- для каждого `node → values` (не `__interrupt__`) шлём `{"type":"node","node":node}` и, если
  `values` — dict, делаем `state.update(values)` (аккумуляция `values`-словарей по узлам);
- в конце, если `not interrupted`, вызываем `session._format(state)` и шлём результат.

`_format` (`chat_api.py:74-91`) сохраняет текущее поведение: `error` в state → `{"type":"error"}`;
`__interrupt__` в state (не бывает в этом пути) → `clarification`; `final_answer` + цитирования +
`select_images` → `{"type":"answer"}`. Никаких изменений в `chat_api.py` не требуется.

### 5.2 `__interrupt__` → `clarification`, финальный ответ НЕ шлётся

В `stream_mode="updates"` LangGraph при `interrupt()` отдаёт чанк
`{"__interrupt__": (Interrupt(value=...),)}` (`qa_graph.py:1529`). В цикле:

- ветка `node == "__interrupt__"`: `values` — это кортеж `(Interrupt, ...)`;
  `getattr(values[0], "value", values[0])` извлекает текст уточнения (дословно как
  `chat_api.py:71-72` и текущее `app.py:371`); шлём `{"type":"clarification","text":str(value)}`,
  ставим `interrupted = True`, `continue`;
- так как `interrupted == True`, финальный `session._format(state)` **не вызывается** и
  `answer` не шлётся.

`resume()` после стрима работает как раньше: `_thread_id` фиксируется в `stream()`
(`qa_graph.py:1533`, до итерации), поэтому reply-ветка `session.resume(text)` продолжает тот же
поток — её не трогаем.

---

## 6. Краевые случаи (пункт 3 задачи)

### 6.1 Исключение в генераторе (узел графа бросил)

Путь в `_stream_query`: `produce` ловит `Exception` из `for update in gen`, кладёт его как
item в очередь; consumer видит `isinstance(item, Exception)` → шлёт `{"type":"error","text":...}`
(фронт снимает «Думаю…» и показывает ошибку), затем `raise item` → внешний
`except Exception: await websocket.close()`. **Не молчим**: клиент получает error ДО закрытия.

### 6.2 Клиент отключился mid-stream

Следующий `await websocket.send_json(...)` после отключения клиента бросает
(`WebSocketDisconnect`/`ClientDisconnected`/`RuntimeError` — зависит от версии Starlette/uvicorn;
все покрываются `except Exception`). Путь `(C)`: `stop.set()` → producer на следующем чанке
`break` → `gen.close()`; `producer.cancel()` помечает future; проброс во внешний `except` закрывает
сокет. Никаких дальнейших отправок, никакого блокирующего ожидания потока.

Замечание: если клиент отключается во время **длительного** LLM-вызова (пока producer внутри
`next()`), остановка наступает только после возврата из этого вызова — Python не прерывает поток
принудительно. Это приемлемо: дальнейшие `send` уже невозможны, поток завершится сам на границе
чанка.

### 6.3 Повторы / порядок node-событий

Не важны для фронта — не дедуплицируем, не сортируем, не кэшируем. Каждый чанк шлётся ровно
один раз, как выдал генератор.

---

## 7. Что НЕ трогаем (жёсткие границы)

- Протокол WS: типы и поля `answer`/`clarification`/`error`/`node` неизменны.
- `app.py:360-363` (reply-ветка, синхронный `to_thread(session.resume)`).
- `chat_api.py` целиком: `_inject_env_file`, `_pipeline_paths`, `stream`/`answer`/`resume`/`_format`,
  `select_images` — без изменений (стриминг реализуется поверх существующего `stream()`).
- `deploy_config.py` (`DeployConfig`, `qdrant_path`, `qdrant_path_override`) и `config_ui.py`.
- Дедуп источников и переименование шапки — отдельная задача кодера, здесь не затрагивается.

---

## 8. Дизайн фронта (псевдодифф `firmware/src/static/app.js`)

### 8.1 `sendChat()` — мгновенный индикатор «Думаю…» (строки 163-176)

```javascript
function sendChat() {
  var input = $("chat-input");
  var text = input.value.trim();
  if (!text) return;
  var type = state.chat.awaitingClarification ? "reply" : "query";
+ setStatus("chat-status", "Думаю…", "");   // НОВОЕ: индикатор сразу, до отправки
  addBubble("user", text);
  input.value = "";
  if (!state.ws || state.ws.readyState === WebSocket.CLOSED) {
    if (type === "query") { pendingQuery = text; openChat(); }
  } else {
    chatSend(type, text);
  }
}
```

Размещение сразу после валидации/вычисления `type` гарантирует, что индикатор виден мгновенно
независимо от ветки (открытое соединение ИЛИ reconnect через `openChat()`). `setStatus` уже
сбрасывает `className` в `status-line`, так что «Думаю…» рендерится нейтральным статусом.

### 8.2 `onmessage` — снятие индикатора (строки 140-161)

`answer` (148) и `error` (156) уже сбрасывают статус; `node` (158) уже перезаписывает
«Обрабатывается узел: X». Требуется **только** добавить сброс в `clarification`:

```javascript
} else if (message.type === "clarification") {
  addBubble("assistant", "Уточняющий вопрос: " + (message.text || ""));
  state.chat.awaitingClarification = true;
+ setStatus("chat-status", "", "");        // НОВОЕ: снять «Думаю…»
  $("chat-input").focus();
}
```

Итоговая машина индикатора:
- `sendChat()` → «Думаю…»;
- `node` → «Обрабатывается узел: X» (перезапись);
- `answer` / `clarification` / `error` → пусто (снятие).

### 8.3 Вне объёма (опционально, флаг кодеру/оркестратору)

На WS нет обработчика `onclose`/`onerror`. Если соединение оборвётся ДО того, как сервер успел
прислать `error`/`answer` (жёсткий обрыв сети), «Думаю…» останется висеть. Это предсуществующий
пробел, к данной задаче не относится; при желании — отдельной правкой добавить
`state.ws.onclose = function () { setStatus("chat-status", "", ""); }`. В объём этой задачи
НЕ входит.

---

## 9. Альтернативный механизм (вариант B) — если потребуется явный backpressure

Для полноты (задача называла его буквально). stdlib `queue.Queue` потокобезопасна сама по себе;
wake организуется через `asyncio.Event`, выставляемый `loop.call_soon_threadsafe`:

```python
import queue
from queue import Empty

async def _stream_query(websocket, session, text):
    loop = asyncio.get_running_loop()
    q = queue.Queue()                 # thread-safe
    wake = asyncio.Event()            # loop-владеемый; set() через call_soon_threadsafe
    stop = threading.Event()

    def produce():
        gen = session.stream(text)
        try:
            for update in gen:
                if stop.is_set():
                    break
                q.put(update)
                loop.call_soon_threadsafe(wake.set)
        except Exception as exc:
            q.put(exc)
            loop.call_soon_threadsafe(wake.set)
        finally:
            gen.close()
            q.put(_SENTINEL)
            loop.call_soon_threadsafe(wake.set)

    producer = loop.run_in_executor(None, produce)
    state, interrupted = {}, False
    try:
        while True:
            done = False
            while True:                        # drain без блокировки
                try:
                    item = q.get_nowait()
                except Empty:
                    break
                if item is _SENTINEL:
                    done = True; break
                if isinstance(item, Exception):
                    # ... send error, raise item ...
                # ... node / __interrupt__ обработка ...
            if done:
                break
            await wake.wait()
            wake.clear()
        if not interrupted:
            await websocket.send_json(session._format(state))
    except Exception:
        stop.set(); producer.cancel(); raise
    finally:
        if not stop.is_set():
            await producer
```

Почему рекомендуем A, а не B: B требует вручную держать инвариант «drain → wait → clear» (при
неверном порядке возможен либо потерянный wakeup, либо busy-loop). Вариант A снимает эту
ответственность, передав её `asyncio.Queue`/`await q.get()`. Разницы в производительности для
нескольких чанков на запрос нет.

---

## 10. Тесты

Инфраструктура: `firmware/tests/`, `TestClient(app.app)` + `monkeypatch` (FastAPI TestClient
поддерживает `websocket_connect`). Тяжёлый `ChatSession(cfg)` (импорт `qa_graph`, инъекция `.env`,
Qdrant) в тестах **подменяется** через `monkeypatch.setattr(app, "ChatSession", FakeSession)` —
`_stream_query` должна принимать любой объект с методами `stream()`/`_format()`.

### 10.1 Новый файл `firmware/tests/test_ws_chat.py`

`FakeSession`:

```python
class FakeInterrupt:
    value = "Уточните запрос"

class FakeSession:
    def __init__(self, chunks, error=None):
        self._chunks, self._error = chunks, error
    def stream(self, text):
        for c in self._chunks:
            yield c
        if self._error is not None:
            raise self._error
    def _format(self, state):
        return {"type": "answer", "answer": "ОТВЕТ", "cited_chunk_ids": [], "sources": [], "images": []}
```

Кейсы:

1. **Потоковая отдача + порядок**: `chunks = [{"search": {...}}, {"generate_answer": {...}}]`;
   через `client.websocket_connect("/ws/chat")` шлём `{"type":"query","text":"q"}`, читаем
   `receive_json()` три раза → `node:search`, `node:generate_answer`, `answer` (ровно в таком
   порядке; `answer` — последним). **Это регрессионный тест на баг `list(...)`: порядок
   node-до-answer доказывает, что чанки не материализуются.**

2. **Интеррупт не шлёт answer**: `chunks = [{"__interrupt__": (FakeInterrupt(),)}]` →
   получаем ровно один `{"type":"clarification","text":"Уточните запрос"}`; `answer` НЕ приходит.

3. **Ошибка графа → error + закрытие**: `chunks = [{"search": {...}}]`, `error=RuntimeError("boom")`
   → получаем `node:search`, затем `error:boom`, затем WS закрыт (дальнейший `receive_json`
   бросает). «Не молчим».

4. **Накопление state**: `chunks = [{"search": {"search_results": [...]}}, {"answer_node": {"final_answer": "X"}}]`
   → `_format(state)` вызван с объединённым dict (assert через подмену `_format`, что в state
   есть оба ключа).

5. (опционально) **disconnect mid-stream**: медленный fake-генератор (`time.sleep` между
   чанками) + `websocket.close()` после первого чанка → тест завершается без зависания
   (проверка, что `await producer` не блокирует навсегда).

### 10.2 Существующие тесты — без изменений

- `test_chat_api.py` (тестирует `_format`/`select_images`/`_inject_env_file`) — не затрагивается.
- `test_app.py` — WS-чата сейчас не тестирует; новые тесты идут в отдельный файл, существующие
  не меняются.

### 10.3 Проверка прогона

`python3 -m pytest firmware/tests -q` — зелёные (текущие + новые `test_ws_chat.py`).

---

## 11. Итог

| Пункт | Решение |
|---|---|
| Первопричина | `list(session.stream(text))` материализует ленивый LangGraph-стрим до отправки |
| Механизм потоковой отдачи | `asyncio.Queue` + `loop.call_soon_threadsafe(q.put_nowait, ...)` из executor-потока; consumer `await q.get()` (вариант A; альтернатива B — stdlib `queue.Queue` + `asyncio.Event` + `get_nowait`, §9) |
| Остановка потока | `threading.Event` `stop` + `gen.close()` + `producer.cancel()` без блокирующего `await` в disconnect-пути |
| Семантика | `__interrupt__` → `clarification` + `interrupted=True` (answer не шлётся); иначе `_format(state)` в конце; reply-ветка не трогается |
| Ошибка графа | `error` клиенту → проброс → `websocket.close()` (не молчим) |
| Фронт | `sendChat()`: `setStatus("chat-status","Думаю…","")`; `onmessage` clarification: сброс статуса; node перезаписывает «Обрабатывается узел: X» |
| Границы | протокол WS, reply-ветка, `chat_api.py`, `DeployConfig`/`qdrant_path`, дедуп/шапка — не трогаются |
| Файлы правок (для кодера) | только `firmware/src/app.py` (+ модульный `_stream_query`) и `firmware/src/static/app.js`; новый тест `firmware/tests/test_ws_chat.py` |
