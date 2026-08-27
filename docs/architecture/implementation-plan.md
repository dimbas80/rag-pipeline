# interface_RAG — План реализации (для Coder)

> Сопутствующий документ: `docs/architecture/architecture.md` (обязательно прочитать перед началом).
> Порядок этапов ниже построен так, чтобы каждый этап давал проверяемый результат и зависел только от
> предыдущих. Критерии готовности каждого этапа обязательны перед переходом дальше.

---

## Конвенции

- Код — `firmware/src/`, тесты — `firmware/tests/`, статика — `firmware/src/static/`.
- Python ≥ 3.11, venv в корне репозитория. Зависимости: `firmware/src/requirements.txt` (веб) —
  список §2.7 архитектуры (`fastapi`, `uvicorn[standard]`, `python-multipart`, `pyyaml`,
  `python-dotenv`, `requests`, `pydantic`, `pymupdf`, `qdrant-client`, `fastembed`,
  `langgraph>=1.2`, `transformers>=4.51.0`, `httpx`, `beautifulsoup4`, `tqdm`).
- Все пути — только из `deploy_config.DeployConfig` (никаких хардкодов). Dev-значения в `config.yaml`.
- Каждый запуск пайплайна — `subprocess.Popen(argv: list[str], shell=False)` (см. §9 архитектуры).
- Атомарные записи — temp + `os.replace`; конфиги веба дополнительно пишут `.bak` перед перезаписью.
- Логи/комментарии — русский; type hints; `snake_case` (конвенция пайплайнов-соседей).

---

## Этап 1. Каркас: конфиг + deploy_config + структура репо

**Файлы:**
- `config.yaml` (секции `dev:`/`prod:`, `active: dev`, `prompts.registration_vision`, `model_tags` — §8
  архитектуры).
- `firmware/src/deploy_config.py` — `DeployConfig` (dataclass) + `load()` (env `INTERFACE_RAG_ENV`
  переопределяет `active`; валидация обязательных полей: `pipelines.*`, `qdrant.path`, `upload.base_dir`,
  `base_markdown`, `env_file`).
- `firmware/src/requirements.txt`.
- `firmware/src/.gitignore`-дополнение (игнор `__pycache__`, `.env`, `uploads/`, `.bak`).
- `firmware/tests/test_deploy_config.py`.

**Критерии готовности:**
- `deploy_config.load()` возвращает полный конфиг для dev; переключение `INTERFACE_RAG_ENV=prod` даёт
  prod-секцию.
- Тест: отсутствие обязательного поля → ошибка с понятным текстом; dev-значения совпадают с config.yaml.

---

## Этап 2. config_ui + llm_client + providers_api (без FastAPI)

**Файлы:**
- `firmware/src/config_ui.py` — `read_yaml`, `write_yaml` (с `.bak`, гейт `yaml.safe_load`),
  `read_env`/`write_env` (маскирование), `mask_key`, валидаторы: `validate_providers`,
  `validate_search_config` (nodes, temperature/max_tokens), `validate_reg_record`.
- `firmware/src/llm_client.py` — `resolve_role`, `chat_completion`, `vision_completion`,
  `run_with_fallback` (OpenAI-совместимо; vision через `image_url` data-URI).
- `firmware/src/providers_api.py` — `scan_models(base_url, api_key)`, `tag_model(name)`
  (правила `config.model_tags`, порядок §4.2), `add_provider(...)`.
- `firmware/tests/test_config_ui.py`, `test_providers_api.py`, `test_llm_client.py` (LLM-вызовы мокаются).

**Критерии готовности:**
- `write_yaml` сохраняет структуру/комментарии неразрушимо для целевых файлов (проверить на копии
  реального `providers.yaml`); `.bak` создаётся; битый YAML не пишется (гейт).
- `tag_model` даёт ожидаемые теги для имён из реального `providers.yaml` (§4.2: embedding/rerank/vision/
  chat).
- `add_provider` отклоняет дубль `api_key_env` и невалидный `base_url`.

---

## Этап 3. jobs.py — запуск пайплайнов, лог, стрим, стоп

**Файлы:**
- `firmware/src/jobs.py` — `Job`, `JobRunner` (§3.1 архитектуры): `start(argv, cwd, env, kind)`,
  `stop`, `get`, `subscribe` (очередь событий для SSE); чтение stdout/stderr построчно в буфер + очередь;
  `stop` = SIGTERM → 5s → SIGKILL; сериализация N=1.
- `firmware/tests/test_jobs.py` — фейковый argv (например `python3 -c`), проверка лог-строк, стопа,
  exit-кода.

**Критерии готовности:**
- Запуск фейковой долгой команды; `subscribe` получает строки; `stop` переводит в `stopped`, процесс убит.
- env инжектируется (проверить, что дочерний процесс видит переменную из `read_env`).
- Нет `shell=True` нигде (assert в тесте на структуру argv).

---

## Этап 4. registration.py — вырезка 1-й страницы + vision + slug + запись reg

**Файлы:**
- `firmware/src/registration.py` — `extract_first_page` (PyMuPDF; DOCX/DOC → libreoffice headless → PDF;
  иначе None), `vision_prefill` (роль `create_markdown.registration_vision`, промпт из конфига),
  `make_slug` (точная реплика `_reg_make_slug`: префикс-карта, номер, транслит, коллизии `_2`),
  `write_reg_yaml` (рендер `documents.<slug>` по порядку полей §2.3 + `.bak` + гейт).
- `firmware/tests/test_registration.py` — slug (префикс/номер/транслит/коллизия), рендер YAML
  (совпадает с форматом пайплайна; `yaml.safe_load` корректен), fallback при отсутствии vision.

**Критерии готовности:**
- Сгенерированный `<stem>_reg.yaml` парсится `yaml.safe_load`, секция `documents.<slug>` содержит все
  поля §2.3, `source_file` = имя файла, `status: active`.
- Slug для `ГОСТ 839—80`+`Кабели` → `GOST_839_kabel`; коллизия → суффикс.
- `write_reg_yaml` на существующем файле делает `.bak` и не повреждает чужие записи (тест на копии
  реального reg-файла).

---

## Этап 5. qdrant_api + chat_api — импорт qa_graph

**Файлы:**
- `firmware/src/qdrant_api.py` — `list_collections`, `distinct_documents` (open/close на запрос).
- `firmware/src/chat_api.py` — `sys.path` к `Build_Search_index/firmware/src` (+ `telegram_bot`),
  `ChatSession` (один `QAGraph` на сессию), `answer`/`resume`, `select_images` (приоритет бота через
  `telegram_bot.asset_helpers`), `list_documents`.
- `firmware/tests/test_chat_api.py` — мок `QAGraph.run/resume`; проверка: interrupt → clarification,
  final_answer → answer; `select_images` выбирает только процитированные/явные ассеты.

**Критерии готовности:**
- Импорт `from qa_graph import QAGraph, QAGraphConfig` работает с dev-конфигом (qdrant_path/collection/
  providers_path/search_config_path из deploy_config).
- Мок-тест: answer → dict с final_answer+cited_chunk_ids+images; interrupt → clarification.
- `distinct_documents` возвращает уникальные `document_id` (тест на in-memory fake Qdrant или моке).

---

## Этап 6. app.py — FastAPI-роуты, SSE, WebSocket, статика

**Файлы:**
- `firmware/src/app.py` — маршруты §7 архитектуры; `DocumentSession` (in-memory) с gating §4.4;
  SSE `GET /api/jobs/{id}/events`; WebSocket `ws /ws/chat` (§6); `GET /api/files/...`,
  `GET /api/images/...` (безопасная отдача §9); `mount /static`.
- `firmware/src/static/index.html`, `app.js`, `style.css` — три вкладки (Чат / Добавить документ /
  Настройки), wizard-состояние, SSE-клиент (EventSource), WS-клиент, маскированные поля ключей.
- `firmware/tests/test_app.py` — TestClient: загрузка, register (мок vision), convert/index (мок jobs),
  gating (index без `.md`/reg → 4xx + сообщение), файлы (traversal отклоняется), settings (маски).

**Критерии готовности:**
- `uvicorn app:app` стартует; открывается `index.html`, три вкладки работают.
- Wizard: кнопка «Добавить в базу» неактивна без `.md`+`_reg.yaml` и при `write_enabled=false`.
- `GET /api/images/../../etc/passwd`-подобный запрос → 404/400 (нет выхода за `base_markdown`).
- SSE доставляет строки лога фейковой задачи; «Стоп» прерывает.

---

## Этап 7. Интеграционные smoke-тесты на реальных пайплайнах

**Файлы:** `firmware/tests/test_integration.py` (помечены `@pytest.mark.integration`, запускаются явно).

**Критерии готовности (прогон вручную на dev):**
1. Положить тестовый PDF в `uploads/`; шаг 3 → появляется `Markdown/<stem>/<stem>.md` + `image/`.
2. Шаг 2 регистрация → `Markdown/<stem>/<stem>_reg.yaml` (валидный).
3. `create_markdown.py -i Markdown/<stem>/<stem>.md --rag` → `<stem>_chunks.jsonl` + `<stem>_assets.json`.
4. `create_index.py Markdown/<stem>/ --qdrant-path <dev> --collection technical_standard --strict` →
   «Готово: N чанков» (только в dev-базу).
5. Чат: вопрос по документу → ответ с цитатами (картинки — при доступности `base_markdown`).
6. `GET /api/documents-in-base` → документ в списке.

---

## Итоговые критерии приёмки (весь проект)

- [ ] Три раздела UI работают; wizard gating по шагам и по наличию файлов (§4.4).
- [ ] Пайплайны не изменены (`git status` в обоих репозиториях чистый относительно старта).
- [ ] Ни одного хардкода пути в `firmware/src/` (кроме `__file__`-относительных дефолтов).
- [ ] Все subprocess-вызовы — argv-списком, `shell=False`.
- [ ] Конфиги пишутся атомарно с `.bak`; ключи `.env` маскированы в UI.
- [ ] «Стоп» прерывает локальный процесс (SIGTERM→SIGKILL).
- [ ] Dev не пишет в боевую базу (`write_enabled=false`, кнопка индексации заблокирована).
- [ ] `python -m pytest firmware/tests/ -q` зелёный (интеграционные — отдельным прогоном).

---

## Замечания и ловушки для Coder

1. **Имя RAG-файлов**: пайплайн пишет `<stem>_chunks.jsonl` / `<stem>_assets.json` (НЕ `rag_chunks.jsonl`).
   Не доверять help-строке `create_markdown.py` (§2.1).
2. **`--reg` (TTY) не использовать** — веб пишет `<stem>_reg.yaml` сам (registration.py). Не вызывать
   `create_markdown.py --reg` из веба.
3. **`--collection` и `--qdrant-path` для `create_index.py` передавать явно**, иначе интерактивный выбор
   коллекции и относительный путь (§2.2).
4. **Qdrant-замок**: не держать `QdrantClient` открытым между запросами (открыть→прочитать→закрыть).
5. **QAGraph на WebSocket-сессию**, не глобальный синглтон (изоляция `thread_id`).
6. **`BASE_MARKDOWN`** в `qa_graph.py` захардкожен — не менять; картинки на dev могут не резолвиться (§10).
7. **Токенизатор HF** скачивается при первом `--rag`; на LXC нужен доступ к HF или предзагруженный кэш.
8. **Vision-роль** `create_markdown.registration_vision` — новая, аддитивная; пайплайны её игнорируют (§4.1).
