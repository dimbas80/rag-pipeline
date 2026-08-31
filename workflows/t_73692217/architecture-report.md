# Architecture-report — md-переиндексация, чистый прод, бот-сервис (решения 33–39)

> Задача: спроектировать 3 нетривиальных фикса (решения **35 / 36 / 39**) и подтвердить
> подход к **33 / 34**. UI (**37 / 38**) уже решён оркестратором — здесь только флаг, если
> вижу проблему.
>
> Границы: НЕ менять протокол WS, `deploy_config.py`, `config_ui.py`, `registration.py`,
> `fs_perms.py`, значения `.env`/`config/.env`; НЕ трогать UI (37/38) и НЕ писать
> production-код — только `workflows/t_73692217/architecture-report.md` с псевдодиффами.

---

## 0. Карта решений (сводно)

| # | Решение | Вердикт | Ключевые файлы |
|---|---|---|---|
| 39 | md-переиндексация без пересохранения | ✅ проектирую (интерфейс-side, `create_markdown.py` НЕ меняю) | `firmware/src/app.py` (upload/convert) |
| 35 | Чистый прод (runtime-манифест + cleanup) | ✅ проектирую | `scripts/deploy.sh` |
| 36 | Бот как systemd-сервис | ✅ проектирую | `request_bot.py`, `scripts/deploy.sh`, новый `scripts/interface-rag-bot.service` |
| 33 | Пустой ответ → fallback | ✅ подтверждаю (место + текст) | `qa_graph.py`, `chat_api.py` |
| 34 | `max_tokens` 2048→4096 | ✅ подтверждаю (4096, не больше) | `config/search_config.yaml` |
| 37/38 | UI | ✅ флаг: hot-apply бота требует restart | — |

---

## 1. Решение 39 — md-переиндексация, не пересохранять

### 1.1 Первопричина (подтверждено по коду)

Прод-конфиг (`config.yaml:13-23`):

```yaml
prod:
  upload: {base_dir: /mnt/sdb/!База_ГОСТ, ...}
  base_markdown: /mnt/sdb/!База_ГОСТ/Markdown
```

Цепочка для `.md`-входа (текущее поведение):

1. **upload** (`app.py:79-97`): `target = cfg.upload_base_dir / name` → файл ложится в
   **корень** `!База_ГОСТ` (`/mnt/sdb/!База_ГОСТ/<stem>.md`), а НЕ в `Markdown/<stem>/<stem>.md`.
2. **convert** (`app.py:143-163`): `create_markdown.py -i <корень>/<stem>.md --ai`.
   `create_markdown.py:_classify_input` (`6205-6224`) для `.md` **вне** `Markdown/` → режим
   `md_standalone`; его AI-постобработка (`6524-6536`) пишет результат в
   `input_path.parent/<stem>_ai.md` → **`/mnt/sdb/!База_ГОСТ/<stem>_ai.md`** (снова корень).
3. **index** (`app.py:165-178`): проверяет `s["md"].is_file()`, где
   `s["md"] = base_markdown/<stem>/<stem>.md` = `Markdown/<stem>/<stem>.md` — **этого файла
   никогда не создаётся** (AI-выход ушёл в `<stem>_ai.md` в корне). Index-шаг в тупике.

Дополнительно: для `.md` **внутри** `Markdown/` (`_classify_input` → `md_rag`, `6482-6507`)
флаг `--ai` отклоняется («требуется --rag/--reg»); AI-постобработка в `md_rag`-режиме
не предусмотрена. Т.е. «положить `.md` в `Markdown/` и прогнать `--ai`» **невозможно**
без правки пайплайна.

### 1.2 Решение: менять пайплайн или интерфейс?

**Решаю: обрабатывать на стороне интерфейса, `create_markdown.py` НЕ менять.** Обоснование:

| Вариант | Суть | Вердикт |
|---|---|---|
| A. Правка `create_markdown.py` (`md_rag` + `--ai`, или `--output`) | заставить пайплайн писать AI-выход в `Markdown/<stem>/<stem>.md` | ❌ трогает контракт другой репы (`Create_Markdown_YA`, свой AGENTS.md: «.md внутри Markdown/ … не изменяются»); риск для прочих вызовов пайплайна |
| **B. Интерфейс-side** | интерфейс кладёт `.md` сразу в канонический `Markdown/<stem>/<stem>.md`; convert для `.md` — no-op; index — без изменений | ✅ **рекомендуется** (минимально, в границах interface_RAG) |
| C. Интерфейс-side + staging под `--ai` | для НОВОГО `.md` прогнать `--ai` на копии в scratch-каталоге и `os.replace` `_ai.md` → канон | рабочий, но лишняя машинерия (см. §1.5) |

Почему B, а не C (по умолчанию):

1. `.md` — это **уже готовый Markdown**; AI-постобработка (`--ai`) в пайплайне существует
   для очистки OCR-выхода PDF/DOCX (таблицы/формулы). Для загруженного `.md` она избыточна.
2. Требование (в) «выход AI-постобработки ложился в `Markdown/<stem>/<stem>.md` **или
   эквивалент**» удовлетворяется эквивалентом: для `.md` роль «выхода AI-постобработки»
   играет сам загруженный Markdown, который интерфейс кладёт сразу в каноническое место.
3. `docs/architecture/architecture.md:554` уже кодифицирует «если `.md` уже есть — шаг
   выполнен (без пере-конвертации)».

### 1.3 Псевдодифф `firmware/src/app.py` — upload (строки 79-97)

```python
@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if name != file.filename or suffix not in {".pdf", ".docx", ".doc", ".md"}:
        raise HTTPException(400, "Недопустимый файл")
    if len(name) > 255:
        raise HTTPException(400, "Слишком длинное имя")
    limit = cfg.upload_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "Файл превышает допустимый размер")
    stem = Path(name).stem
    md_path = cfg.base_markdown / stem / f"{stem}.md"
    if suffix == ".md":
        # Решение 39: .md кладём сразу в канонический Markdown/<stem>/<stem>.md,
        # НЕ в корень upload_base_dir (не оставляем <stem>.md/<stem>_ai.md в корне базы).
        # Если файл уже в базе — НЕ пересохраняем (переиндексация существующего).
        if md_path.exists():
            source = md_path          # (б) существующий .md — индексируем его как есть
        else:
            fs_perms.ensure_dir(md_path.parent)   # 0777 (решение №22)
            fs_perms.write_bytes(md_path, data, 0o666)  # 0666 (решение №22)
            source = md_path
    else:
        fs_perms.ensure_dir(cfg.upload_base_dir)
        source = cfg.upload_base_dir / name
        fs_perms.write_bytes(source, data, 0o666)  # 0666 (решение №22)
    sid = uuid.uuid4().hex
    sessions[sid] = {"session_id": sid, "name": name, "stem": stem,
                     "source": source, "md": md_path,
                     "reg": cfg.base_markdown / stem / f"{stem}_reg.yaml"}
    return {"session_id": sid, "stem": stem}
```

Изменения: `suffix` вычисляется один раз; для `.md` `source` == `md` == канонический путь;
`reg` без изменений (`Markdown/<stem>/<stem>_reg.yaml`). Для PDF/DOCX — дословно прежняя
логика. `pre_existing`-флаг не нужен: обе ветки сходятся к «convert = no-op, index по канону».

### 1.4 Псевдодифф `firmware/src/app.py` — convert (строки 143-163)

```python
@app.post("/api/documents/{sid}/convert")
def convert(sid):
    s = _session(sid)
    if Path(s["name"]).suffix.lower() == ".md":
        # Решение 39: .md уже лежит в Markdown/<stem>/<stem>.md (загружен на шаге upload).
        # OCR/AI-постобработка не требуется; пересохранение не выполняется. Возвращаем
        # мгновенно-завершённую job (единая инфраструктура SSE/лога), затем index.
        return runner.start(
            [sys.executable, "-c",
             "print('md уже готов — конвертация не требуется (переиндексация без пересохранения)')"],
            kind="convert").__dict__
    # ---- ниже — существующая логика PDF/DOCX без изменений (проверка ролей, argv --ai) ----
    providers = config_ui.read_yaml(cfg.providers_path)
    ...
```

Замечания:

- Тривиальная `[sys.executable, "-c", ...]`-job переиспользует `JobRunner` → SSE/лог/`stop`
  работают без нового кода в `jobs.py`. `jobs.py` **не меняется**.
- Фронт (`app.js:491-494`) вызывает convert для `.md` — теперь это безвредный no-op;
  пользователь может и вовсе пропустить convert (см. ниже).
- `state()`/`can_index` (`app.py:100-105`) не требуют правок: после upload `.md` уже в
  каноне → `md_exists=true`; после register → `can_index=true` (index доступен сразу).

### 1.5 Альтернатива (только если оркестратор настоит на AI-постобработке НОВОГО `.md`)

Если AI-чистку `.md` всё же нужно оставить: стадия вне `Markdown/` (иначе `md_rag` отклонит
`--ai`) + перенос выхода в канон. Требует пост-шага в `jobs.py` (чего вариант B избегает):

```python
# convert для .md (вариант C):
stage = Path(tempfile.mkdtemp(prefix="md-ai-")) / stem
stage.mkdir(parents=True)
(stage / f"{stem}.md").write_bytes(md_path.read_bytes())   # копия вне Markdown/
argv = [sys.executable, str(src), "-i", str(stage / f"{stem}.md"), "--ai",
        "--config", ..., "--providers-config", ...]
# после успеха фазы 1: os.replace(stage / f"{stem}_ai.md", md_path); shutil.rmtree(stage.parent)
```

Это требует расширения `JobRunner.start_sequence` на «argv + пост-действие» (не argv-списком),
что раздувает объём без подтверждённой потребности. **Рекомендую вариант B.**

### 1.6 Что проверяется после фикса

- Новый `.md`: `Markdown/<stem>/<stem>.md` создаётся при upload; в корне `!База_ГОСТ` нет
  ни `<stem>.md`, ни `<stem>_ai.md`.
- Повторный `.md` (тот же stem): canonical-файл **не перезаписывается** (mtime/content без
  изменений); index переиндексирует существующее содержимое.
- `GET /api/files/markdown/<stem>` отдаёт канонический `.md` (маршрут без изменений).

---

## 2. Решение 35 — чистый прод (runtime-манифест + cleanup)

### 2.1 Текущее состояние `scripts/deploy.sh`

Уже сделано: rsync `config.yaml`, `firmware/src` (целиком), `create_markdown.py`, 4 файла BSI,
`telegram_bot/asset_helpers.py`; симлинк `providers.yaml`; cleanup-блок (§4). Чего **не хватает**
относительно точного runtime-манифеста:

1. `firmware/__init__.py` (0 B, маркер пакета) — **не rsync'ится** (синхронизируется только
   `firmware/src`, т.е. `firmware/src/__init__.py` попадает, а `firmware/__init__.py` — нет).
   Нужен только при запуске формой `uvicorn firmware.src.app:app`; при текущем конвенте
   (`uvicorn app:app` из `firmware/src`) — опционален. Добавляем для полноты (0 B, безвреден).
2. `requirements.txt` BSI/CMY — **не синхронизируются** (только interface_RAG внутри
   `firmware/src`). Для воспроизводимости venv — добавить в манифест (см. §2.2).
3. Cleanup не удаляет: `firmware/include/`, `firmware/lib/`, `example/`, `snapshots/`,
   `.worktrees/`, `AGENTS.md`, `*.gitkeep` (часть перечисленного оркестратор зафиксировал).

### 2.2 Точный runtime-манифест (эталон на LXC `/root/RAG/`)

```
/root/RAG/
  venv/                                        # общий venv (подтверждено на LXC)
  config/
    providers.yaml  create_markdown_config.yaml  search_config.yaml  .env
  interface_RAG/
    config.yaml                                # active: prod
    firmware/__init__.py
    firmware/src/*.py                          # app, deploy_config, jobs, config_ui,
                                               # providers_api, registration, llm_client,
                                               # qdrant_api, chat_api, __init__.py
    firmware/src/static/                       # index.html settings.html app.js settings.js
                                               # style.css favicon.svg
    firmware/src/requirements.txt
  Create_Markdown_YA/
    firmware/src/create_markdown.py
    firmware/src/requirements.txt              # (добавить)
  Build_Search_index/
    firmware/src/create_index.py qa_graph.py search.py llm_providers.py
    firmware/src/telegram_bot/asset_helpers.py request_bot.py   # (request_bot.py — решение 36)
    firmware/src/providers.yaml    -> /root/RAG/config/providers.yaml      # симлинк
    firmware/src/search_config.yaml -> /root/RAG/config/search_config.yaml # симлинк (решение 36)
    firmware/src/requirements.txt              # (добавить)
```

**Не на проде (удаляется):** `tests/`, `__pycache__/`, `*.pyc`, `docs/`, `workflows/`, `.hermes/`,
`.pytest_cache/`, `README*`, `AGENTS.md`, `example/`, `snapshots/`, `.worktrees/`, `.gitignore`,
`*.gitkeep`, `firmware/include/`, `firmware/lib/`, `firmware/src/tmp/`, `bot.log*`, `*.log`,
копии `.env`/`providers.yaml` в каталогах пайплайнов.

### 2.3 Точный diff `scripts/deploy.sh`

**(a) Константы/манифест — rsync BSI становится явным списком (строки 168-178):**

```bash
# Create_Markdown_YA: + requirements.txt
run_local "rsync Create_Markdown_YA: create_markdown.py + requirements.txt" \
  rsync -az -e "$RSYNC_E" \
    "$DEV_CMY/firmware/src/create_markdown.py" \
    "$DEV_CMY/firmware/src/requirements.txt" \
    "root@$LXC:/root/RAG/Create_Markdown_YA/firmware/src/"

# Build_Search_index: + requirements.txt, request_bot.py (решение 36), search_config.yaml
run_local "rsync Build_Search_index: *.py + telegram_bot/*.py + requirements.txt" \
  rsync -az -e "$RSYNC_E" \
    "$DEV_BSI/firmware/src/create_index.py" \
    "$DEV_BSI/firmware/src/qa_graph.py" \
    "$DEV_BSI/firmware/src/search.py" \
    "$DEV_BSI/firmware/src/llm_providers.py" \
    "$DEV_BSI/firmware/src/requirements.txt" \
    "root@$LXC:/root/RAG/Build_Search_index/firmware/src/"
run_local "rsync Build_Search_index: telegram_bot/{asset_helpers,request_bot}.py" \
  rsync -az -e "$RSYNC_E" \
    "$DEV_BSI/firmware/src/telegram_bot/asset_helpers.py" \
    "$DEV_BSI/firmware/src/telegram_bot/request_bot.py" \
    "root@$LXC:/root/RAG/Build_Search_index/firmware/src/telegram_bot/"
```

**(b) interface_RAG: добавить `firmware/__init__.py` (после строки 167):**

```bash
run_local "rsync interface_RAG: firmware/__init__.py" \
  rsync -az -e "$RSYNC_E" "$DEV_ROOT/firmware/__init__.py" \
    "root@$LXC:/root/RAG/interface_RAG/firmware/"
```

**(c) EXCLUDES (строка 161):** убрать `--exclude='request_bot.py'` (он был инертным — BSI
rsync'ится per-file, а не каталогом; теперь бот разворачивается явно). Остальные excludes
оставить.

**(d) Cleanup-блок (heredoc после строки 185):** дополнить строку interface_RAG и BSI:

```bash
# interface_RAG: добавить firmware/include, firmware/lib, __pycache__ на всех уровнях
rm -rf /root/RAG/interface_RAG/firmware/include /root/RAG/interface_RAG/firmware/lib \
       /root/RAG/interface_RAG/firmware/__pycache__ \
       /root/RAG/interface_RAG/firmware/src/__pycache__
# Build_Search_index: добавить example/, snapshots/, .worktrees/, AGENTS.md, *.gitkeep
rm -rf /root/RAG/Build_Search_index/example /root/RAG/Build_Search_index/snapshots \
       /root/RAG/Build_Search_index/.worktrees \
       /root/RAG/Build_Search_index/firmware/src/__pycache__ \
       /root/RAG/Build_Search_index/firmware/src/telegram_bot/__pycache__
find /root/RAG/interface_RAG /root/RAG/Create_Markdown_YA /root/RAG/Build_Search_index \
     -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find /root/RAG/interface_RAG /root/RAG/Create_Markdown_YA /root/RAG/Build_Search_index \
     \( -name '*.gitkeep' -o -name 'AGENTS.md' -o -name 'README.md' \) -delete 2>/dev/null || true
```

Замечание: `find … -delete` в dry-run **не выполняется** (обёрнут `run_remote_script`),
в apply — исполняется на LXC. Чистка `__pycache__` через `find -prune` покрывает любую
вложенность (в т.ч. `telegram_bot/__pycache__`).

---

## 3. Решение 36 — Телеграм-бот как systemd-сервис

### 3.1 Первопричина (подтверждено по коду)

`Build_Search_index/firmware/src/telegram_bot/request_bot.py`:

- строка 36: `load_dotenv(<...>/Build_Search_index/.env)` — файл **удалён** clean-prod'ом
  (`deploy.sh:205-206` `rm -f …/Build_Search_index/.env`), токен не резолвится;
- строка 39: `TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")` → при пустом токене
  `sys.exit(1)` (строки 65-67) — бот не стартует;
- `deploy.sh:161` держит `--exclude='request_bot.py'` (инертный, но вводит в заблуждение),
  и `request_bot.py` **не попадает** в BSI-rsync (список per-file без него).

### 3.2 Совместимость `config/.env` с `EnvironmentFile` (проверено)

`config/.env` — `KEY=VALUE`, **без** `export`, **без** кавычек, **без** пробелов вокруг `=`,
значения без пробелов. Это в точности формат systemd `EnvironmentFile` (читает `KEY=VALUE`,
`#` — комментарий, кавычки не снимает — их и нет). ✅ Совместимо.

Ключи, которые бот возьмёт из `EnvironmentFile=/root/RAG/config/.env` (после консолидации
на LXC — 10 ключей, подтверждено `workflows/t_7ee33160:144-145`):

| Переменная бота | Источник | Статус |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | config/.env | ✅ есть |
| `TELEGRAM_ALLOWED_USERS` | config/.env (консолидация) | ✅ есть; **до решения 37** может быть пусто → бот открыт всем (см. §5) |
| `QDRANT_PATH` | config/.env (если UI менял) | ✅; иначе fallback `request_bot.py:40` (совпадает с prod-дефолтом) |
| `*_API_KEY` (7 шт) | config/.env | ✅ — нужны in-process `qa_graph→llm_providers→get_api_key` (`os.environ.get`) |

### 3.3 Новый файл `scripts/interface-rag-bot.service` (шаблон в репо)

```ini
[Unit]
Description=interface_RAG Telegram QA bot
After=network-online.target interface-rag.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/RAG/Build_Search_index/firmware/src/telegram_bot
EnvironmentFile=/root/RAG/config/.env
ExecStart=/root/RAG/venv/bin/python request_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Пояснения:

- **WorkingDirectory** = каталог `request_bot.py`/`asset_helpers.py`: Python сам кладёт его в
  `sys.path[0]` (`from asset_helpers import ...` работает), а `request_bot.py:21` добавляет
  `firmware/src` для `from qa_graph import ...`. Запуск из любого места корректен.
- **`After=interface-rag.service`** — бот зависит от готовности веб-интерфейса (общий Qdrant
  и конфиги поднимает тот же хост); жёсткой зависимости от порта нет (бот не держит HTTP).
- **`EnvironmentFile=/root/RAG/config/.env`** — единый источник ключей; НЕ `Environment=`
  (иначе секреты в юните). `Restart=always` + `RestartSec=5` дублирует внутренний цикл
  `main()` (`request_bot.py:378-395`) — идемпотентно, не конфликтует.

### 3.4 Фикс `request_bot.py` — нужен ли по `load_dotenv`? Да, удалить

```python
# строка 25 — УДАЛИТЬ:
-from dotenv import load_dotenv
# строка 36 — УДАЛИТЬ:
-load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))
```

Обоснование: путь указывает на удалённый `Build_Search_index/.env` (мёртвый код), и это
**второй** источник истины, который при случайном воссоздании файла разойдётся с
`config/.env`. Ключи теперь идут из systemd `EnvironmentFile`. `python-dotenv` из
requirements **не удалять** — `llm_providers.py` (in-process `get_api_key`) им пользуется.

### 3.5 Зависимость `python-telegram-bot` (пробел, который надо закрыть)

`request_bot.py` импортирует `telegram`/`telegram.ext`, но **ни один** `requirements.txt`
(interface_RAG, CMY, BSI) его не содержит. Пакет обязан быть в том venv, где крутится бот
(`/root/RAG/venv` — общий, подтверждён на LXC).

- Добавить `python-telegram-bot` в `Build_Search_index/firmware/src/requirements.txt`.
- В `deploy.sh` — идемпотентный шаг (после rsync, до рестарта):

```bash
run_remote "установить python-telegram-bot в общий venv" \
  "/root/RAG/venv/bin/pip install --quiet 'python-telegram-bot>=21'"
```

(Если оператор предпочитает однократную установку вручную — зафиксировать как prerequisite
деплоя; тогда шаг выше можно опустить, но оставить в комментарии.)

### 3.6 Diff `scripts/deploy.sh` — установка/рестарт бота (новый блок «5.5»)

```bash
# --- 5.5. systemd-юнит бота: установить, daemon-reload, enable+restart ---
run_remote "установить интерфейс-юнит бота" \
  "cp $DEV_ROOT/scripts/interface-rag-bot.service /etc/systemd/system/interface-rag-bot.service"
run_remote "daemon-reload" "systemctl daemon-reload"
run_remote "enable бота" "systemctl enable interface-rag-bot.service"
run_remote "restart бота" "systemctl restart interface-rag-bot.service"
run_remote "is-active бота" "systemctl is-active interface-rag-bot.service"
```

`DEV_ROOT` уже вычислен (`deploy.sh:36`); `run_remote`/`run_remote_script` показывают план в
dry-run. Строку 161 (`--exclude='request_bot.py'`) и комментарий на строках 202-204
(«request_bot.py … не разворачивается») обновить под новое решение.

---

## 4. Решение 33 — пустой ответ (подтверждение места и текста)

Подход оркестратора корректен. Точные места:

**4.1 Guard в `qa_graph.py` `generate_answer` (после ретраев, перед матчингом цитат).**

После строки 1309 (`if last_exc is not None: return {...}`), перед строкой 1312
(`cited = _match_citations_to_chunks(...)`):

```python
    if not answer.strip():
        return {"final_answer": FALLBACK_EMPTY_ANSWER, "cited_chunk_ids": []}
```

Новая константа рядом с существующими (`FALLBACK_EMPTY_RESULTS`/`FALLBACK_LLM_ERROR`,
строки 1241-1250):

```python
FALLBACK_EMPTY_ANSWER = (
    "К сожалению, не удалось сформулировать ответ по вашему запросу. "
    "Попробуйте переформулировать вопрос или уточнить номер документа."
)
```

Тон выдержан в стиле существующих fallback'ов. Ключ `error` не ставится (это не «LLM
недоступен», а «модель вернула пусто»); `cited_chunk_ids=[]` — честно, цитат нет.

**4.2 Страховка в `chat_api._format` (строки 80-84).**

Текущее:

```python
answer = result.get("final_answer")
if answer is None:
    return {"type": "error", "text": "Граф не вернул ответ"}
```

Замена (пустой/пробельный answer → fallback-текст, НЕ пустой пузырь и НЕ error):

```python
answer = result.get("final_answer")
if answer is None:
    return {"type": "error", "text": "Граф не вернул ответ"}
if not str(answer).strip():
    return {"type": "answer", "answer": _EMPTY_ANSWER_FALLBACK,
            "cited_chunk_ids": [], "sources": [], "images": []}
```

Module-level в `chat_api.py` (не импортируем константу из qa_graph — другая репа):

```python
_EMPTY_ANSWER_FALLBACK = (
    "К сожалению, не удалось сформулировать ответ по вашему запросу. "
    "Попробуйте переформулировать вопрос или уточнить номер документа."
)
```

Тип `answer` (а не `error`) выбран намеренно: фронт рендерит это обычным пузырём с полезным
текстом, «не пустой пузырь» (требование решения 33). Страховка покрывает и `resume`-путь
(где пустой answer может прийти, минуя `generate_answer`-guard).

---

## 5. Решение 34 — `max_tokens` 2048→4096 (подтверждение)

Подтверждаю **4096**, не больше. Обоснование:

- `generate_answer.max_tokens` идёт в `payload["max_tokens"]` (`qa_graph.py:259-260`) и
  применяется к роли `build_search_index.query_processing` = **`deepseek-v4-pro`**
  (`config/providers.yaml:258-260`), fallback — тоже чат-модель (`*id002`).
- `deepseek-v4-pro` держит выход до ~8192 токенов → **4096 безопасно** (не ломает лимит
  модели). Другие узлы (`analyze_query`/`reformulate_query`/`ask_clarification` = 256/512)
  не затрагиваются.
- 4096 снимает обрезку длинных ответов (решение 34) и уже выведено в UI «Параметры графа».
- Выше 4096 (напр. 8192) **не нужно**: риск упасть на лимит конкретного fallback-провайдера
  без соразмерной пользы.

Правка — одна строка в `config/search_config.yaml`:

```yaml
  generate_answer:
    temperature: 0
    max_tokens: 4096      # было 2048 (решение 34)
```

---

## 6. UI 37/38 — флаг (не блокер)

Решения 37/38 сами по себе корректны и к бэкенду не относятся. Один интеграционный флаг:

- **37** добавляет поле `TELEGRAM_ALLOWED_USERS` в UI → пишется в `config/.env` через
  `PUT /api/settings/env` (`config_ui.write_env`). Но бот читает
  `os.environ.get("TELEGRAM_ALLOWED_USERS")` **только при старте** (`request_bot.py:43-47`).
  → изменение списка допустимых пользователей в UI **не применяется до `systemctl restart
  interface-rag-bot.service`**. Это ожидаемое поведение (env-inject через EnvironmentFile —
  только на старте процесса), но его стоит зафиксировать в описании поля/доке, чтобы
  пользователь не ждал hot-apply. То же касается `TELEGRAM_BOT_TOKEN`.
- **38** (`<datalist>`) — конфликтов с бэкендом нет.

---

## 7. Границы (жёсткие)

- НЕ меняются: протокол WS, `deploy_config.py`, `config_ui.py`, `registration.py`,
  `fs_perms.py`, значения `.env`/`config/.env`.
- НЕ меняется `create_markdown.py` (решение 39 — интерфейс-side).
- НЕ пишется production-код — только псевдодиффы в этом отчёте.
- UI (37/38) не трогается; `jobs.py` в решении 39 не меняется (no-op через `runner.start`).

---

## 8. Риски / открытые вопросы (для оркестратора)

1. **`TELEGRAM_ALLOWED_USERS` может быть пуст** на момент запуска бота (пока не заполнен
   через UI-37 / вручную) → `_is_authorized` вернёт `True` для всех. Бот на LAN (не WAN),
   риск принят в решении №8, но зафиксировать.
2. **`python-telegram-bot` отсутствует в venv** — обязательный prerequisite (§3.5), иначе
   бот упадёт при старте.
3. **AI-постобработка `.md` отключена** (решение 39, вариант B). Если появится реальный
   сценарий «загрузить черновой `.md` и прогнать AI-чистку» — вернуться к варианту C (§1.5).
4. **`search_config.yaml`/`providers.yaml` для бота** — через симлинки (§3); если симлинк не
   создан, `qa_graph` бота упадёт при инициализации (тот же риск, что §10 architecture.md
   для веб-чата).

---

## 9. Итог

| # | Решение | Объём правок |
|---|---|---|
| 39 | `.md` → канонический `Markdown/<stem>/<stem>.md` при upload (skip при существовании); convert для `.md` — no-op; index без изменений; `create_markdown.py` не трогаем | `firmware/src/app.py` (upload+convert), `jobs.py` — без изменений |
| 35 | Точный runtime-манифест + cleanup: добавить `firmware/__init__.py`, `requirements.txt` CMY/BSI; удалить `include/`,`lib/`,`example/`,`snapshots/`,`.worktrees/`,`AGENTS.md`,`*.gitkeep` | `scripts/deploy.sh` |
| 36 | systemd-юнит `interface-rag-bot.service` (EnvironmentFile=config/.env, After=interface-rag.service); удалить `load_dotenv` из `request_bot.py`; rsync `request_bot.py` + симлинк `search_config.yaml`; `python-telegram-bot` в venv | новый `scripts/interface-rag-bot.service`, `scripts/deploy.sh`, `request_bot.py`, BSI `requirements.txt` |
| 33 | guard `if not answer.strip()` в `generate_answer` → `FALLBACK_EMPTY_ANSWER`; страховка в `chat_api._format` → fallback-текст (type `answer`) | `qa_graph.py`, `chat_api.py` |
| 34 | `generate_answer.max_tokens` 2048→4096 (подтверждено, модель deepseek-v4-pro держит) | `config/search_config.yaml` |
| 37/38 | флаг: hot-apply бота требует `systemctl restart interface-rag-bot.service` | — |
