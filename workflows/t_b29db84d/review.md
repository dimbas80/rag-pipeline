# Review — interface_RAG web-ui

## Verdict

CHANGES_REQUESTED

## Scope

Ревизия реализации веб-оркестратора `interface_RAG` против:
- `docs/product/requirements.md`
- `docs/product/decision-log.md`
- `docs/architecture/architecture.md` (§1–§12)
- `docs/architecture/implementation-plan.md` (этапы 1–7)

Проверялись фактические артефакты: `firmware/src/{app,deploy_config,jobs,config_ui,providers_api,llm_client,registration}.py`,
`firmware/src/static/{index.html,app.js,style.css}`, `config.yaml`, `firmware/tests/`, `workflows/t_30ddef39/implementation-report.md`.
Оба соседних репозитория (`Create_Markdown_YA`, `Build_Search_index`) проверены `git status`.

## Что проверялось вживую

- `python3 -m pytest firmware/tests -q` → **2 passed** (только `test_deploy_config.py`).
- `python3 -m compileall -q firmware/src` → OK.
- Импорт `firmware.src.app` из корня → OK; маршруты перечислены ниже.
- Запуск по документированной команде `uvicorn app:app` из `firmware/src/` → **FAIL**
  (`ImportError: attempted relative import with no known parent package`).

## Итог

Реализован **каркас этапа 1 + фрагменты этапов 2/3/6**. Ключевые модули и маршруты архитектуры
отсутствуют целиком. Против критериев ревью (1–7) реализация не проходит большинство пунктов.
Отчёт кодера это честно фиксирует как `known_limitations` («полная интеграция … не завершена»), но
это не делает результат приёмкой по критериям данной задачи.

---

## Requirements compliance

| Критерий | Статус | Комментарий |
|---|---|---|
| Три раздела UI | PARTIAL | Вкладки Чат/Добавить документ/Настройки есть (`static/index.html`), но Чат и Настройки — заглушки |
| Wizard по шагам | FAIL | Есть upload→register→convert, но нет шага индексации, нет vision-prefill, кнопки не гейтованы состоянием шагов |
| «Добавить в базу» активна только при `.md`+`_reg.yaml` | FAIL | Кнопка `#index` в `app.js` постоянно `disabled`, нет `/api/documents/{sid}/index` и нет её включения по `can_index` |
| Чат (qa_graph, цитаты, картинки) | FAIL | `/ws/chat` отсутствует; `#ask` пишет строку-заглушку |
| «Показать документы в базе» (distinct Qdrant) | FAIL | `/api/documents-in-base` отсутствует; `qdrant_api.py` не создан |
| Настройки (провайдеры, теги, роли, ключи, узлы) | FAIL | Все `/api/settings/*` отсутствуют; секция Настройки в HTML пустая |
| «Один провайдер» ai_postprocess+query_processing синхронно | FAIL | Не реализовано нигде |
| Vision-модель синхронно в `table_vision`+`registration_vision` (решение №9) | FAIL | Нет секции «vision»; grep по `config_ui.py`/`app.js`/`index.html` не находит ни `vision`, ни `table_vision`, ни `registration_vision` |
| «Стоп» прерывает локальный процесс | PARTIAL | Backend `jobs.stop()` есть; в UI кнопка «Стоп» не подключена |
| Запись в Qdrant только прод | PASS (тривиально) | `write_enabled: false` (dev), маршрута индексации нет вообще |
| Пайплайны не изменены | PASS | `git status` в обоих соседних репозиториях чистый |

## Architecture compliance

- **Отсутствуют модули** `qdrant_api.py` и `chat_api.py` (арх. §3.1). `app.py` их не импортирует.
- **Отсутствуют маршруты** (арх. §7): `POST /api/documents/{sid}/register/prefill`,
  `GET /api/documents/{sid}/reg`, `POST /api/documents/{sid}/index`, `GET /api/images/...`,
  `WS /ws/chat`, `GET /api/documents-in-base`, все `/api/settings/*`.
- **Инъекция env в subprocess не реализована** (арх. §8): `app.convert()` (app.py:41) вызывает
  `runner.start(argv, cwd=..., kind=...)` **без** `env=os.environ | read_env(env_file)`. Ключи
  `.env` (YANDEX_*, DEEPSEEK_API_KEY и т.д.) не попадают в дочерний процесс — конвертация с `--ai`
  на реальных данных упадёт без ключей.
- **Роль `registration_vision` не используется**: в `registration.py` есть `extract_first_page`, но
  `vision_prefill()` (арх. §4.1) отсутствует — vision-извлечение полей формы не реализовано.
- Имена файлов/компонентов, которые присутствуют (`deploy_config.py`, `jobs.py`, `config_ui.py`,
  `providers_api.py`, `llm_client.py`, `registration.py`), соответствуют арх. §3.
- `deploy_config.py` + `config.yaml` (dev/prod, `INTERFACE_RAG_ENV`) — OK, пути не хардкожены.

## Security

- subprocess argv-списком, `shell=False` — **OK** (jobs.py:30-43, проверка `argv` — список строк).
- Path traversal при загрузке — **OK** (app.py:20-21: `basename` + `name != file.filename`).
- Отдача markdown — **OK** (app.py:56-61: regex + `resolve()` + проверка префикса `base_markdown`).
- Безопасная отдача `image/` — **FAIL**: маршрута `/api/images/...` нет вообще.
- **Лимит загрузки не enforced** — **FAIL**: `upload.max_mb` есть в конфиге, но `await file.read()`
  (app.py:24) читает тело целиком в память без проверки размера.
- Маскирование ключей — **NOT VERIFIED**: `config_ui.mask_key/read_env` реализованы, но не вызываются
  ни одним маршрутом (нет `/api/settings/env`). Утилита — мёртвый код.
- Атомарная запись с `.bak` — **OK** для `write_yaml` (temp + `os.replace` + `safe_load`-гейт) и
  `write_reg_yaml`. Замечания к `write_env` — см. Findings.

## Model tags / providers

- `tag_model()` (providers_api.py:6-10) реализует порядок §4.2: embedding→rerank→vision→chat,
  case-insensitive substring + regex `qwen.*-vl|gemini.*vision|claude.*vision`. Сверка с примерами
  §4.2 (`Qwen3-Embedding-8B`→embedding, `glm-4.5v`→vision, `deepseek-v4-pro`→chat) — **OK**.
- Однако ни `scan_models`, ни `tag_model`, ни `add_provider` **не подключены к UI** (нет
  `/api/settings/providers/scan|add`), поэтому функция «добавить провайдера» для пользователя
  отсутствует.

## Tests

- Только `firmware/tests/test_deploy_config.py` (2 теста). Отсутствуют: `test_jobs`, `test_config_ui`,
  `test_providers_api`, `test_llm_client`, `test_registration`, `test_chat_api`, `test_app`
  (gating/traversal/settings), `test_integration` — всё это требует implementation-plan (§2–§7).

---

## Findings

### CRITICAL

1. **Проект не запускается документированной командой.**
   Location: `firmware/src/app.py` (относительные импорты) vs arch §8 / handoff `run_commands`.
   Проблема: `uvicorn app:app` из `firmware/src/` → `ImportError: attempted relative import with no
   known parent package`. Работает только `uvicorn firmware.src.app:app` из корня.
   Expected: команда запуска из архитектуры работает, либо архитектура/handoff исправлены на
   `uvicorn firmware.src.app:app --host ... --port ...` из корня репозитория.

2. **Чат не реализован** (критерий 5). Нет `chat_api.py`, `qdrant_api.py`, `WS /ws/chat`,
   `GET /api/documents-in-base`. `static/app.js` выводит заглушку
   «Чат подключается к QAGraph после настройки Build_Search_index.»

3. **Индексация отсутствует** (критерии 1, 7). Нет `POST /api/documents/{sid}/index`, нет фазы
   `--rag` + `create_index.py`. Кнопка «Добавить в базу» мёртвая (`disabled`, без обработчика).

4. **Настройки отсутствуют** (критерий 1, 4). Все `/api/settings/*` не реализованы; секция
   «Настройки» в `index.html` пуста. Нет маскированного просмотра/правки `.env`, providers,
   search_config, «добавить провайдера», синхронной записи `ai_postprocess`+`query_processing`.

### HIGH

5. **Регистрация без vision-prefill и без гейта полноты.**
   Location: `firmware/src/registration.py` (нет `vision_prefill`), `app.py:31-35`,
   `static/app.js` (register шлёт `{document_id:'', title:'', ...}`).
   Проблема: vision-извлечение полей первой страницы (роль `registration_vision`, арх. §4.1)
   не реализовано; обязательные поля не валидируются.
   Expected: `POST /api/documents/{sid}/register/prefill` + проверка обязательных полей перед записью.

6. **Инъекция `.env` в subprocess отсутствует.** Location: `app.py:41`. Дочерний `create_markdown.py --ai`
   не получит `YANDEX_API_KEY` и т.п. Expected: `env=os.environ | read_env(cfg.env_file)` (арх. §8).

7. **Лимит загрузки не enforced.** Location: `app.py:24`. Expected: проверка
   `len(await file.read()) <= upload.max_mb` до записи, чтение в ограниченный буфер.

### MEDIUM

8. **`make_slug` — не «точная реплика» `_reg_make_slug`.** Location: `registration.py:8-15`.
   Проблема: транслитерация только частного случая `кабели→kabel`; прочий кириллический `domain`
   (`Электроснабжение` и т.п.) даёт пустой хвост (`re.sub(r"[^a-z0-9]+","_", ...)`), slug теряет
   слово домена. Expected: полноценная транслит-карта, как в пайплайне (impl-plan этап 4).

9. **`write_env` — dead code + нет «удалить ключ».** Location: `config_ui.py:43-47`.
   Проблема: строка 44 (`write_yaml` во временный файл) перезаписывается и бесполезна;
   `{k: v for k, v in values.items() if v}` отбрасывает пустые значения → ключ нельзя очистить/удалить.
   Expected: поддержка удаления ключа (арх. §7 «удалить ключ»).

10. **`requirements.txt` неполон.** Location: `firmware/src/requirements.txt`. Отсутствуют
    `qdrant-client`, `fastembed`, `langgraph>=1.2`, `transformers>=4.51.0`, `httpx`,
    `beautifulsoup4`, `tqdm` (impl-plan «Конвенции»). Для чата/индексации они обязательны.

11. **В `config_ui.py` нет валидаторов.** Location: `firmware/src/config_ui.py` (47 строк —
    только `mask_key`, `read_yaml/write_yaml`, `read_env/write_env`). Отсутствуют требуемые
    арх. §3.1 `validate_providers`, `validate_search_config`, `validate_reg_record`, а также
    синхронная запись ролей (решения №6 и №9).

### LOW

11. `state()` (app.py:28-30): идиома `sessions.get(sid) or (_ for _ in ()).throw(...)` — работает,
    но нечитаема; заменить на явный `if sid not in sessions: raise HTTPException(404, ...)`.
12. `upload()` молча перезаписывает существующий файл (нет обработки коллизии) — принять как
    last-writer-wins (согласовано), но зафиксировать.
13. SSE-поток (app.py:49-54) busy-waits (`sleep(.1)`) при пустой очереди; события не кодируются
    в JSON (строки лога с `\n` нарушат протокол SSE). Не блокирует.

---

## Required Changes (до PASS)

1. Добавить модули `qdrant_api.py` и `chat_api.py` (арх. §3.1) и маршруты `/ws/chat`,
   `GET /api/documents-in-base`, `GET /api/images/...` (безопасная отдача по `base_markdown`,
   белый список расширений, `resolve()`+префикс).
2. Реализовать `POST /api/documents/{sid}/index` (фазы `--rag` → `create_index.py` с явными
   `--qdrant-path`, `--collection`, `--strict`) с gating `.md`+`_reg.yaml`+`write_enabled`.
3. Реализовать все `/api/settings/*` (providers CRUD с маскированием, scan/add с тегами,
   search-config, create-markdown-config, env с удалением ключей, collections, status) и
   синхронную запись одной чат-модели в `ai_postprocess`+`query_processing` (решение №6).
   **Решение №9:** в «Настройках» — **одна** секция «vision» (primary + fallback); при сохранении
   vision-модель пишется **одной атомарной записью** сразу в `roles.create_markdown.table_vision`
   И `roles.create_markdown.registration_vision`; отдельного селектора «регистрация» быть не должно.
4. Реализовать `registration.vision_prefill()` (роль `create_markdown.registration_vision`) +
   маршрут `POST /api/documents/{sid}/register/prefill` + fallback на ручной ввод.
5. Инъекция `.env` в subprocess (арх. §8) — `env=os.environ | read_env(cfg.env_file)` в `convert`
   и `index`.
6. Enforce лимит загрузки (`upload.max_mb`) и чтение в ограниченный буфер.
7. Подключить «Стоп» и SSE-лог к UI wizard; гейтовать кнопки шагов по фактическому состоянию
   (`can_index`, наличие провайдеров/ключей).
8. Исправить `make_slug` (полная транслитерация) и `write_env` (удаление ключей, убрать dead code).
9. Исправить команду запуска (арх. §8 / handoff): `uvicorn firmware.src.app:app ...` из корня,
   либо перевести импорты на абсолютные.
10. Дополнить `requirements.txt` и добавить тесты этапов 2–7 (`test_jobs`, `test_config_ui`,
    `test_providers_api`, `test_llm_client`, `test_registration`, `test_app` — traversal/gating/
    masking, `test_chat_api` с моком `qa_graph`).

## Risks

- Полнота списка правок большая: фактически это этапы 2–7 плана. Рекомендуется разбить на
  отдельные под-задачи (chat/qdrant, settings, wizard-index, security hardening) вместо одного
  «дописать всё».
- `BASE_MARKDOWN` в `qa_graph.py` захардкожен (`/mnt/sdb/...`) — на dev картинки не резолвятся
  (зафиксировано в арх. §10, не блокирует).
- Интеграция с реальными пайплайнами (smoke: PDF→OCR→RAG→Qdrant→чат) не проверялась — только
  после реализации этапов 5–7.

## Notes

- Отчёт кодера (`implementation-report.md`) правдиво фиксирует ограничения; расхождений между
  отчётом и кодом не обнаружено (код действительно — каркас).
- `tag_model` и `providers_api` корректны сами по себе, но не подключены к интерфейсу.
- Архитектура обновлена **во время ревью** коммитом `2b1f63f` (решение №9): §4.1 теперь требует
  одну секцию «vision» с синхронной записью в `table_vision`+`registration_vision`. Перепроверено:
  в `config_ui.py`/`static/app.js`/`static/index.html` никакой логики vision-ролей нет — критерий
  не выполнен.
