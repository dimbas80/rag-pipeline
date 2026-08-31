# Implementation report — решения 33–39 (t_2e5cdde9)

> Реализация по спецификации `workflows/t_73692217/architecture-report.md` (§1–§6).
> Работа в двух репозиториях; коммиты раздельные.
> Бот НЕ запускался, deploy НЕ выполнялся (это делает оркестратор).

---

## Сводка

| # | Решение | Статус | Где |
|---|---------|--------|-----|
| 33 | Пустой ответ → fallback | ✅ реализовано | BSI `qa_graph.py`, interface_RAG `chat_api.py` |
| 34 | `max_tokens` 2048→4096 | ✅ реализовано | interface_RAG `config/search_config.yaml` + BSI `firmware/src/search_config.yaml` (см. Отклонения) |
| 35 | Чистый прод (runtime-манифест + cleanup) | ✅ реализовано | interface_RAG `scripts/deploy.sh` |
| 36 | Бот как systemd-сервис | ✅ реализовано | новый `scripts/interface-rag-bot.service`, `scripts/deploy.sh`, BSI `request_bot.py`, BSI `requirements.txt` |
| 37 | UI «Телеграм» (settings.js) | ✅ реализовано | interface_RAG `static/settings.js` |
| 38 | UI datalist типа документа | ✅ реализовано | interface_RAG `static/index.html` + `static/app.js` |
| 39 | md-переиндексация без пересохранения | ✅ реализовано | interface_RAG `firmware/src/app.py` (upload/convert), `jobs.py` НЕ менялся |

---

## Repo 1: /root/projects/interface_RAG

### 1. firmware/src/chat_api.py (решение 33, §4.2)
- Добавлена module-level константа `_EMPTY_ANSWER_FALLBACK` (текст дословно из §4.2;
  НЕ импортируется из qa_graph — другая репа, дублирование намеренное).
- В `ChatSession._format` после проверки `answer is None` добавлен guard:
  `if not str(answer).strip(): return {"type": "answer", "answer": _EMPTY_ANSWER_FALLBACK,
  "cited_chunk_ids": [], "sources": [], "images": []}`.
  Тип `answer` (не `error`), цитаты/источники/изображения пустые — «не пустой пузырь».

### 2. config/search_config.yaml (решение 34, §5)
- `generate_answer.max_tokens: 2048 → 4096`.

### 3. firmware/src/app.py (решение 39, §1.3–§1.4)
- `upload`: `suffix` вычисляется один раз; для `.md` файл кладётся СРАЗУ в канонический
  `cfg.base_markdown / stem / f"{stem}.md"` (через `fs_perms.ensure_dir`/`write_bytes`,
  0777/0666 как решение №22). Если канонический файл уже существует — НЕ пересохраняется,
  `source = md_path` (переиндексация существующего). Для PDF/DOCX — прежняя логика
  (`cfg.upload_base_dir / name`). `source` для `.md` == `md` == канонический путь.
- `convert`: для `.md` — мгновенно-завершённая job
  `runner.start([sys.executable, "-c", "print(...)"], kind="convert").__dict__`
  (единая инфраструктура SSE/лога; проверка ролей провайдеров пропускается).
  `jobs.py` НЕ менялся. PDF/DOCX-ветка без изменений.

### 4. static/settings.js (решение 37, §6)
- `KNOWN_ENV`: `TELEGRAM_BOT_TOKEN` переведён в секцию `telegram`; добавлен
  `{ key: "TELEGRAM_ALLOWED_USERS", label: "Допустимые чаты (ID через запятую)", section: "telegram" }`.
- `NAV_SECTIONS`: `{ id: "other-keys", label: "Прочие ключи" }` → `{ id: "telegram", label: "Телеграм" }`
  (id тоже переименован — фильтр карточки и data-section навигации согласованы).
- `render()`: `sectionHtml("other-keys", ...)` → `sectionHtml("telegram", ...)`.
- `renderOtherKeysCard`: фильтр `section === "telegram"`, заголовок → «Телеграм»,
  добавлено пояснение: «Изменения применяются после перезапуска бота:
  systemctl restart interface-rag-bot.service (не hot-apply)».

### 5. static/index.html + static/app.js (решение 38)
- `index.html`: `<select id="reg-document_type">` → `<input id="reg-document_type"
  name="document_type" type="text" list="document-type-list" placeholder="ГОСТ / СП / СО / СНиП / ПУЭ">`
  + `<datalist id="document-type-list">` (ГОСТ/СП/СО/СНиП/ПУЭ). `name` сохранён —
  `collectRegistrationFields` читает `form.elements["document_type"].value` (input.value работает).
- `app.js`: добавлена функция `fillInput(id, value)` — заполнение input с нормализацией
  по опциям datalist регистронезависимо (сохраняет прежнее поведение `fillSelect`,
  важно для `make_slug`: `PREFIX.get(document_type)` в registration.py требует точное
  верхнего регистра значение). Оба вызова `fillSelect("reg-document_type", ...)`
  (строки 302 и 392) заменены на `fillInput("reg-document_type", ...)`.
  `fillSelect` остался для `reg-status`. `validateRegistration`/`collectRegistrationFields`
  не менялись — работают с `input.value`.

### 6. scripts/interface-rag-bot.service (НОВЫЙ, §3.3)
- Unit-шаблон дословно: `WorkingDirectory=/root/RAG/Build_Search_index/firmware/src/telegram_bot`,
  `EnvironmentFile=/root/RAG/config/.env`,
  `ExecStart=/root/RAG/venv/bin/python request_bot.py`, `Restart=always`, `RestartSec=5`,
  `After=network-online.target interface-rag.service`.

### 7. scripts/deploy.sh (решения 35/36, §2.3 + §3.5 + §3.6)
- CMY-rsync: добавлен `requirements.txt`.
- BSI-rsync: добавлен `requirements.txt`; `telegram_bot/` теперь синхронизирует
  `{asset_helpers,request_bot}.py` (request_bot.py разворачивается явно).
- Добавлен rsync `firmware/__init__.py` (маркер пакета).
- Из EXCLUDES убран `--exclude='request_bot.py'` (был инертным, вводил в заблуждение).
- Шаг 4: добавлен симлинк `$CONFIG_DIR/search_config.yaml → Build_Search_index/firmware/src/search_config.yaml`.
- Cleanup расширен: `firmware/include`, `firmware/lib`, `firmware/__pycache__`,
  BSI `example/`, `snapshots/`, `.worktrees/`; глобальный `find` по трём репозиториям:
  `__pycache__` любой вложенности + `*.gitkeep`/`AGENTS.md`/`README*`; комментарий
  про request_bot.py (строки 202-204) исправлен под новое решение.
- Новый блок «5.5»: pip install `python-telegram-bot>=21` в `/root/RAG/venv`,
  rsync юнит-шаблона → `/root/RAG/`, `cp` → `/etc/systemd/system/` + `chmod 644`,
  `daemon-reload`, `enable`, `restart`, `is-active`.
  (В псевдодиффе §3.6 `cp $DEV_ROOT/...` выполнялся бы удалённо — $DEV_ROOT это
  ЛОКАЛЬНЫЙ путь dev-машины; реализовано корректно: rsync на LXC, затем удалённый cp.)

---

## Repo 2: /root/projects/Build_Search_index

### 8. firmware/src/qa_graph.py (решение 33, §4.1)
- Новая константа `FALLBACK_EMPTY_ANSWER` рядом с `FALLBACK_LLM_ERROR` (текст из §4.1).
- В `generate_answer` после `if last_exc is not None: return {...}` и ПЕРЕД
  `cited = _match_citations_to_chunks(...)` добавлен guard:
  `if not answer.strip(): return {"final_answer": FALLBACK_EMPTY_ANSWER, "cited_chunk_ids": []}`.
  Ключ `error` не ставится; цитат честно нет.

### 9. telegram_bot/request_bot.py (решение 36, §3.4)
- Удалены `from dotenv import load_dotenv` (строка 25) и `load_dotenv(...)` (строка 36).
  Больше ничего не менялось. Ключи теперь приходят из systemd `EnvironmentFile`.

### 10. firmware/src/requirements.txt (решение 36, §3.5)
- Добавлено `python-telegram-bot>=21`.

---

## Отклонения / замечания (документирую явно)

1. **BSI `firmware/src/search_config.yaml` тоже изменён (2048→4096)** — задача указывала
   только `interface_RAG/config/search_config.yaml`, но `scripts/consolidate_config.py:125`
   копирует на прод `/root/RAG/config/search_config.yaml` ИМЕННО из BSI-файла.
   Без этой правки решение 34 (снятие обрезки длинных ответов) на проде не сработало бы:
   интерфейс dev читает `config/search_config.yaml`, прод — консолидированную копию BSI-файла.
   Обе копии приведены к 4096.
2. **`fillInput` в задаче назван «уже есть, ~app.js:358-363»** — фактически там была
   `fillField` (устанавливает значение как есть, без нормализации). Функции `fillInput`
   не существовало. Добавлена новая `fillInput` с нормализацией по datalist (см. п.5 выше),
   чтобы сохранить регистронезависимую канонизацию `fillSelect` (важно для слага).
3. **Unit-юнит бота в deploy.sh**: псевдодифф §3.6 использовал `cp $DEV_ROOT/...` внутри
   `run_remote` (удалённое выполнение) — на LXC пути `/root/projects/...` нет. Реализовано
   через rsync шаблона на LXC + удалённый `cp` в `/etc/systemd/system/`.
4. **Тест BSI на пустой answer не добавлен**: в `Build_Search_index/tests/` нет
   `test_qa_graph.py` (каталог пуст, только .gitkeep), инфраструктуры pytest нет.
   Синтаксис проверен `py_compile`; регрессии быть не может (guard — чистое добавление).

---

## Тесты

- interface_RAG: добавлены/поправлены юнит-тесты.
  - `test_chat_api.py` +3: пустой answer → fallback-пузырь (type=answer, НЕ error,
    НЕ пустой); пробельный answer → fallback; `None` по-прежнему → error.
  - `test_app.py` +4: `.md` upload кладёт в канонический `Markdown/<stem>/<stem>.md`
    и не оставляет файл в корне upload_base_dir; существующий `.md` НЕ перезаписывается;
    PDF по-прежнему в upload_base_dir; `convert` для `.md` — мгновенная no-op job
    без проверки провайдеров.
  - Поправлен существующий `test_convert_argv_uses_config_dir`: мок `_session` дополнен
    ключом `name` (convert теперь читает `s["name"]` для проверки суффикса).
- Прогон: `python3 -m pytest firmware/tests -q` → **83 passed** (было 76).
- BSI: `python3 -m py_compile firmware/src/qa_graph.py firmware/src/telegram_bot/request_bot.py` — OK;
  `search_config.yaml` валиден (yaml.safe_load).
- JS: `node --check` для settings.js и app.js — OK; `bash -n scripts/deploy.sh` — OK.

## git diff --stat

interface_RAG:
```
 config/search_config.yaml       |  2 +-
 firmware/src/app.py             | 37 +++++++++++++++++++------
 firmware/src/chat_api.py        | 13 ++++++++++
 firmware/src/static/app.js      | 23 ++++++++++++---
 firmware/src/static/index.html  | 16 +++++-----
 firmware/src/static/settings.js | 16 +++++----
 firmware/tests/test_app.py      | 74 +++++++++++++++++++++++++++++++++++++++++--
 firmware/tests/test_chat_api.py | 30 ++++++++++++++++++
 scripts/deploy.sh               | 62 ++++++++++++++++++++++++++++++---------
 9 files changed, 233 insertions(+), 40 deletions(-)
 + новый файл: scripts/interface-rag-bot.service
```

Build_Search_index:
```
 firmware/src/qa_graph.py                 | 10 ++++++++++
 firmware/src/requirements.txt            |  1 +
 firmware/src/search_config.yaml          |  2 +-
 firmware/src/telegram_bot/request_bot.py |  6 +++---
 4 files changed, 15 insertions(+), 4 deletions(-)
```

## Границы соблюдены

- НЕ менялись: протокол WS, `deploy_config.py`, `config_ui.py`, `registration.py`,
  `fs_perms.py`, `jobs.py`, `create_markdown.py`, значения `.env`/`config/.env`.
- Бот НЕ запускался; deploy НЕ выполнялся.
- Коммиты — раздельные, только собственные изменённые файлы (без `git add .`/`-A`,
  без `.env`/секретов/workflows-отчётов).

## Известные ограничения

- `TELEGRAM_ALLOWED_USERS` пуст до заполнения через UI-37 → бот открыт всем (риск №1 из §8,
  принят архитектором; бот на LAN).
- `python-telegram-bot` ставится deploy-шагом 5.5 в `/root/RAG/venv`; без деплоя бот не стартует.
- AI-постобработка `.md` отключена (решение 39, вариант B); при сценарии «черновой .md +
  AI-чистка» — вернуться к варианту C (§1.5).
