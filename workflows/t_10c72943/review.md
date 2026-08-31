# Review — решения 33–39 (четвёртая итерация, t_10c72943)

## Verdict

PASS

## Scope

Независимая проверка реализации решений 33–39 по спецификации
`workflows/t_73692217/architecture-report.md` в двух репозиториях:
`/root/projects/interface_RAG` (commit c45f493) и
`/root/projects/Build_Search_index` (commit d3e6f82).

Проверял КОД и ЖИВЫЕ запуски, не отчёт кодера.

## Requirements (answer key)

| # | Пункт | Вердикт | Доказательство |
|---|-------|---------|----------------|
| 33 | Пустой ответ → fallback | PASS | см. ниже |
| 34 | `generate_answer.max_tokens: 4096` | PASS | см. ниже |
| 39 | md-переиндексация без пересохранения | PASS | см. ниже |
| 37 | UI секция «Телеграм» + `TELEGRAM_ALLOWED_USERS` | PASS | см. ниже |
| 38 | `<input list>` + `<datalist>` + `fillInput` | PASS | см. ниже |
| 35/36 | Чистый прод + бот-сервис | PASS | см. ниже |

### #33 пустой ответ
- `Build_Search_index/firmware/src/qa_graph.py`:
  - константа `FALLBACK_EMPTY_ANSWER` (строки 1252–1255), текст дословно из §4.1;
  - guard `if not answer.strip(): return {"final_answer": FALLBACK_EMPTY_ANSWER, "cited_chunk_ids": []}`
    (1318–1319) — стоит ПОСЛЕ `if last_exc is not None` (1309–1314) и ПЕРЕД
    `cited = _match_citations_to_chunks(...)` (1322). Ключ `error` не ставится. ✅
- `interface_RAG/firmware/src/chat_api.py`:
  - module-level `_EMPTY_ANSWER_FALLBACK` (23–26), НЕ импортируется из qa_graph;
  - в `_format` (90–95): `if not str(answer).strip(): return {"type": "answer", ...}`
    → тип `answer` (НЕ error, НЕ пустой пузырь), `sources=[]`, `images=[]`. ✅
- **Живой тест** (независимый скрипт): пустой/пробельный answer → `type=answer`,
  непустой fallback-текст, `sources=[]`; `None` по-прежнему → `error`. ✅

### #34 обрезка
- `interface_RAG/config/search_config.yaml`: `generate_answer.max_tokens: 4096` (было 2048). ✅
- Дополнительно `Build_Search_index/firmware/src/search_config.yaml` тоже 4096 —
  НЕ отклонение: `scripts/consolidate_config.py:125` копирует на прод именно BSI-файл.
  Обе копии согласованы (подтверждено оператором; проверено по коду). ✅

### #39 md-переиндексация
- `app.py upload` (93–102): `.md` кладётся сразу в `cfg.base_markdown/<stem>/<stem>.md`
  через `fs_perms.ensure_dir`/`write_bytes` (0777/0666); при существовании файла
  `source = md_path` без записи (НЕ пересохраняет). PDF/DOCX — прежняя логика. ✅
- `app.py convert` (159–166): для `.md` — мгновенно-завершённая job через
  `runner.start([sys.executable, "-c", "print(...)"], kind="convert")`, проверка
  ролей провайдеров пропускается; `jobs.py` не менялся. ✅
- **Живой тест**: существующий канонический `.md` НЕ перезаписан — mtime и content
  сохранены; в корне `upload_base_dir` не остаётся ни `<stem>.md`, ни `<stem>_ai.md`. ✅
- `create_markdown.py` НЕ тронут: репозиторий `Create_Markdown_YA` чист
  (`git status --short` пусто). ✅

### #37 настройки
- `static/settings.js`: `KNOWN_ENV` — `TELEGRAM_BOT_TOKEN` и новый
  `TELEGRAM_ALLOWED_USERS` в секции `telegram`; `NAV_SECTIONS` — `telegram`/«Телеграм»
  (не «Прочие ключи»); карточка с пометкой «не hot-apply, systemctl restart …». ✅

### #38 тип документа
- `static/index.html`: `<input id="reg-document_type" ... list="document-type-list">`
  + `<datalist>` (ГОСТ/СП/СО/СНиП/ПУЭ), `name="document_type"` сохранён. ✅
- `static/app.js`: функция `fillInput` с регистронезависимой нормализацией по datalist;
  оба вызова `fillSelect("reg-document_type", …)` заменены на `fillInput`. ✅
  (Замечание: в задаче `fillInput` значилась «уже есть» — фактически была `fillField`
  без нормализации; добавление `fillInput` корректно, НЕ дефект — подтверждено оператором.)

### #35/#36 инфра
- `scripts/interface-rag-bot.service` (новый): `WorkingDirectory=/root/RAG/Build_Search_index/firmware/src/telegram_bot`,
  `EnvironmentFile=/root/RAG/config/.env`, `ExecStart=/root/RAG/venv/bin/python request_bot.py`,
  `Restart=always`, `After=network-online.target interface-rag.service`. ✅
- `scripts/deploy.sh`:
  - явный runtime-манифест: CMY + `requirements.txt`; BSI + `requirements.txt` +
    `telegram_bot/{asset_helpers,request_bot}.py`; `firmware/__init__.py`. ✅
  - `--exclude='request_bot.py'` из EXCLUDES убран. ✅
  - симлинк `$CONFIG_DIR/search_config.yaml → BSI/src/search_config.yaml` (направление
    корректно: бот читает единый конфиг). ✅
  - cleanup: `firmware/include`, `firmware/lib`, `__pycache__` любой вложенности,
    BSI `example/`/`snapshots/`/`.worktrees/`, `*.gitkeep`/`AGENTS.md`/`README*`
    через `find … -prune/-delete`; `find -delete` в dry-run НЕ исполняется
    (heredoc обёрнут `run_remote_script`). ✅
  - `pip install 'python-telegram-bot>=21'` в общий venv. ✅
  - блок 5.5: rsync шаблона юнита → LXC, удалённый `cp` + `chmod 644`,
    `daemon-reload`, `enable`, `restart`, `is-active`. ✅
- `Build_Search_index/firmware/src/telegram_bot/request_bot.py`: `from dotenv import load_dotenv`
  и вызов `load_dotenv(...)` УДАЛЕНЫ (diff = −3/+3: комментарий вместо вызова). ✅
- `Build_Search_index/firmware/src/requirements.txt`: `python-telegram-bot>=21` добавлен. ✅

## Architecture compliance

Соответствует архитектуре. Отклонений, требующих обновления архитектуры, нет.
Задокументированные отклонения (BSI search_config, новая fillInput, тест BSI не добавлен,
реализация 5.5 через rsync+cp вместо `cp $DEV_ROOT`) обоснованы и согласованы — НЕ дефекты.

## Boundary compliance

НЕ тронуты (подтверждено `git diff --stat` обоих репозиториев): протокол WS,
`deploy_config.py`, `config_ui.py`, `registration.py`, `fs_perms.py`, `jobs.py`,
`create_markdown.py`, значения `.env`/`config/.env`. Бот не запускался, deploy не выполнялся.

## Tests (выполнены независимо)

- `python3 -m pytest firmware/tests -q` (interface_RAG) → **83 passed** (было 76). ✅
- `python3 -m py_compile firmware/src/app.py firmware/src/chat_api.py` → OK. ✅
- `python3 -m py_compile firmware/src/qa_graph.py firmware/src/telegram_bot/request_bot.py` (BSI) → OK. ✅
- `node --check firmware/src/static/settings.js firmware/src/static/app.js` → OK. ✅
- `bash -n scripts/deploy.sh` → OK. ✅
- `./scripts/deploy.sh --dry-run` → exit 0, план корректен (манифест/cleanup/симлинк/5.5). ✅
- yaml `search_config.yaml` (обе копии) валиден (`yaml.safe_load`). ✅

## Findings

Нет блокирующих замечаний.

LOW (не блокирует): BSI-тест на пустой answer отсутствует (в `Build_Search_index/tests/`
нет pytest-инфраструктуры, только `.gitkeep`); guard в `qa_graph.py` проверен `py_compile`
и ревью кода, регрессии быть не может (чистое добавление). Приемлемо — подтверждено оператором.

## Risks

- `TELEGRAM_ALLOWED_USERS` пуст до заполнения через UI-37 → бот открыт всем (риск принят
  в решении №8, бот на LAN).
- `python-telegram-bot` ставится шагом 5.5 в `/root/RAG/venv`; без деплоя бот не стартует.
- AI-постобработка `.md` отключена (вариант B); при сценарии «черновой .md + AI-чистка»
  вернуться к варианту C (§1.5).

## Notes

Проверка выполнена по реальным файлам и живым запускам (pytest, независимые скрипты
_format/upload, bash -n, deploy --dry-run, py_compile, node --check), не по отчёту кодера.
