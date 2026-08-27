# interface_RAG — Архитектура веб-оркестратора

> Статус: архитектура для реализации (Coder), ревизия 2026-08-27 (реворк: единый конфиг +
> чистый прод + новый UI). Источник требований — `docs/product/requirements.md` (§5.0/5.2/5.3/§8)
> и `docs/product/decision-log.md` (решения 10–19).
>
> Пайплайны (`Create_Markdown_YA`, `Build_Search_index`) НЕ изменяются — только чтение их исходников
> и запуск через `subprocess`. `interface_RAG` — тонкий оркестратор поверх их CLI и публичного
> API `qa_graph.py`.
>
> Изменения ревизии относительно прошлой версии документа: (1) единый общий каталог конфигов с
> передачей пайплайнам только через CLI-ключи + инъекция `.env` в окружение subprocess;
> (2) «чистая» структура прода + rsync-манифест + deploy-скрипт; (3) схема нового UI
> (шапка, «Добавить документ» одной страницей с областями, MD-вьювер, настройки без сырых дампов).

---

## 1. Цель и границы

`interface_RAG` — локальная веб-страница (LAN) с тремя разделами:

1. **Чат** — вопрос → ответ с цитатами и картинками таблиц/рисунков (импорт `qa_graph.py`).
2. **Добавить документ** — одна страница с тремя визуальными областями: загрузка → регистрация
   (статичная форма) → лог/прогресс (конвертация + индексация).
3. **Настройки** — управление общими конфигами обоих пайплайнов и `.env` (структурированные поля,
   без сырых дампов).

**Границы (жёсткие):**
- НЕ редактировать `Create_Markdown_YA/` и `Build_Search_index/` — только чтение и запуск их CLI.
- НЕ хардкодить пути к пайплайнам/базе/конфигам — всё в конфиге деплоя (`config.yaml`).
- Запись в Qdrant — только прод-инстанс (в dev кнопка индексации заблокирована `qdrant.write_enabled`).
- **Единый общий конфиг**: один `providers.yaml`, один `.env`, один `create_markdown_config.yaml`,
  один `search_config.yaml` в общем каталоге; пайплайнам конфиг передаётся ТОЛЬКО через CLI-ключи,
  ключи `.env` инжектируются в окружение subprocess. У пайплайнов нет своих копий конфигов.
- Стек: FastAPI + статический HTML/vanilla JS (без Node/React).

---

## 2. Итоги reverse-engineering (что выяснено)

### 2.1 `create_markdown.py` (CLI, Create_Markdown_YA)

Файл `firmware/src/create_markdown.py` (самодостаточный, без локальных импортов соседних модулей).
Точка входа `main()`; вход — ровно один файл (папки не поддерживаются).

**Аргументы (argparse, `parse_args()`, строки 6353–6394):**

| Флаг | Значение |
|---|---|
| `-i, --input` | обязательный; PDF/DOCX/DOC/MD |
| `--ai` | полный AI-цикл (vision-таблицы + gap-filling + AI-постобработка) |
| `--json-native` | структурный JSON-native рендеринг (опционально) |
| `--config` | единый конфиг промптов/RAG, default `./create_markdown_config.yaml` |
| `--providers-config` | реестр провайдеров, default `./providers.yaml` |
| `--rag` | сгенерировать/перезаписать RAG-файлы (только из проверенного MD) |
| `--reg` | интерактивная регистрация в `<stem>_reg.yaml` (требует TTY) |

**Механизм загрузки `.env` (точно, по коду):**
- `main()` (строка 6895): `env_path = Path(__file__).parent / ".env"` = `firmware/src/.env`, затем
  `load_env(env_path)`.
- `load_env()` (строки 498–511) разбирает `KEY=VAL` и делает **`os.environ.setdefault(key, value)`** —
  то есть **НЕ перезаписывает уже заданные переменные окружения**. После этого читает
  `os.environ.get("YANDEX_API_KEY"/"YANDEX_FOLDER_ID")`.

**Вывод для веба:** инъекция ключей через `env=` в `subprocess.Popen` (уже реализовано как
`env = os.environ | read_env_raw(env_file)`) **достаточна и имеет приоритет** — собственная копия
`firmware/src/.env` пайплайну не нужна; её отсутствие безвредно (`load_env` вернёт пустой dict).

**Загрузка конфигов (не при импорте):**
- `providers` загружаются только при `--ai`/`--reg` (`load_providers_config(args.providers_config)` +
  `resolve_role`). Роли: `roles.create_markdown.table_vision`, `roles.create_markdown.ai_postprocess`.
- `create_markdown_config.yaml` загружается при `--ai` (`load_config(args.config)`) и при `--rag`/`--reg`
  (`load_rag_config(args.config, reg_path)`). При ошибке: `--rag` → warning и пропуск; `--reg` → `exit(1)`.
- `--reg` без TTY → `exit 1`. **В вебе `--reg` не используется** — веб пишет `<stem>_reg.yaml` сам (§5).
- **Импорт-тайм зависимостей на конфиги нет** — можно запускать без локальных `providers.yaml`/
  `create_markdown_config.yaml`, передавая всё через `--config`/`--providers-config`.

**Выходы:**
- OCR-конвертация: `<input_dir>/Markdown/<stem>/<stem>.md` + `image/`. Временные: `<input_dir>/tmp/<stem>/`.
  Лог: `<input_dir>/tmp/Create_Markdown_VisionOCR.log`.
- RAG (`run_rag_pipeline()`): **`<stem>_chunks.jsonl`** и **`<stem>_assets.json`** рядом с `.md`.
  ⚠️ help-строка пишет `rag_chunks.jsonl`/`rag_assets.json`, но реально — `<stem>_chunks.jsonl`/
  `<stem>_assets.json` (доверять коду, не help).

**Токенизатор (для `--rag`, строки 5549–5604):** `transformers.AutoTokenizer.from_pretrained(
"Qwen/Qwen3-Embedding-8B", local_files_only=False)`. При `allow_degraded_fallback: false` (дефолт в
реальном конфиге) отсутствие токенизатора/доступа к HF — жёсткая ошибка RAG. Кэш — стандартный
HF-кэш (`~/.cache/huggingface` или `$HF_HOME`). Для прода — prerequisite: предзагрузить токенизатор
или дать доступ к HF Hub (см. §10).

### 2.2 `create_index.py` (CLI, Build_Search_index)

Индексирует `<stem>_chunks.jsonl` + `<stem>_assets.json` в локальный Qdrant (dense SiliconFlow +
sparse fastembed BM25).

**Аргументы (argparse, строки 412–433):**

| Аргумент | Значение |
|---|---|
| `input_dir` (позиционный) | папка документа; авто-поиск ровно одного `*_chunks.jsonl` и `*_assets.json` |
| `--chunks` / `--assets` | альтернатива `input_dir` (взаимоисключающе) |
| `--collection` | имя коллекции; без него при >1 коллекции интерактивный выбор — веб обязан передавать явно |
| `--qdrant-path` | путь к локальному хранилищу; default `./qdrant_data`; веб передаёт абсолютный |
| `--providers_config` | **underscore**; default = `firmware/src/providers.yaml` |
| `--batch-size` | default 16 |
| `--api-key` | переопределение ключа (иначе из `api_key_env` → окружение/`.env`) |
| `--strict` | прервать при ошибках валидации (веб использует) |

**Механизм загрузки ключей:** `resolve_api_key()` → `load_dotenv()` (python-dotenv, **override=False**);
`llm_providers.get_api_key()` → `load_dotenv()` + `os.environ.get(env_name)`. → инъекция env в subprocess
**выигрывает**; своя копия `.env` пайплайну не нужна.

**⚠️ КРИТИЧНО для «чистого» прода (импорт-тайм):** `create_index.py` на строке 53 выполняет
`_DEFAULT_PROVIDER_CFG = _get_providers()` **на уровне модуля** (при импорте, до `main()` и до
разбора `--providers_config`). `_get_providers()` с дефолтным путём `firmware/src/providers.yaml`
бросает `ProviderConfigError`, если файла нет или он невалиден. **Значит `providers.yaml` обязан
существовать и быть валидным по адресу `Build_Search_index/firmware/src/providers.yaml` даже тогда,
когда веб передаёт явный `--providers_config`.** Разрешение — симлинк (см. §4.9).

**Поведение:** перед индексацией удаляет старые точки документа с тем же `document_id`
(переиндексация безопасна). Создаёт коллекцию с dense (COSINE) + sparse векторами при отсутствии.

**Qdrant payload** (`build_payload()`): `chunk_id`, `document_id`, `document_type`, `domain`, `title`,
`status`, `chapter`, `section`, `clause`, `section_path`, `heading_texts`, `text`, `references`,
`assets` (с `image_path`/`image_paths`), `chunk_tokens`, `row_index`.

### 2.3 Per-document `<stem>_reg.yaml` и `load_rag_config`

Per-document конфиг живёт рядом с итоговым Markdown: `Markdown/<stem>/<stem>_reg.yaml`
(для PDF/DOCX; для `.md` — рядом с самим `.md`). Путь вычисляется `_derive_reg_path()`.

`load_rag_config(config_path, reg_path)`:
1. читает единый конфиг `create_markdown_config.yaml` (`defaults`, `references`);
2. если `reg_path` существует — читает его секцию `documents` и **накладывает поверх** в
   `config["documents"]`;
3. прогоняет `validate_rag_config` (warnings, не падает).

Сопоставление документа по `source_file` (`_find_doc_key()`): имя входного файла сравнивается с
`source_file` каждой записи **без регистра и разделителей** (`[^0-9a-zа-яё]` вырезается), по полному
имени и по stem. Если doc_key не найден — `--rag` пишет warning и пропускает генерацию.

**Схема записи `documents.<slug>` (порядок полей = `_REG_FIELD_ORDER`):**

| Поле | Класс | Поведение |
|---|---|---|
| `document_id` | авто + подтверждение | обозначение (LLM/regex), напр. `ГОСТ 18410—73` |
| `document_id_alt` | опц. | старое обозначение (СНиП → СП) |
| `document_type` | авто | `ГОСТ`/`СП`/`СО`/`СНиП`/`ПУЭ` (префикс из document_id) |
| `domain` | опц. | 1 слово тематики (напр. «Кабели») |
| `title` | авто + подтверждение | заголовок |
| `edition` | авто | год (последняя группа цифр; 2 цифры → век: ≥50→19xx) |
| `date_enacted` | опц. | `ГГГГ-ММ-ДД` |
| `date_amended` | опц. | `ГГГГ-ММ-ДД` |
| `amended_by` | опц. | чем изменён |
| `source_file` | авто, 100% | имя входного файла |
| `status` | дефолт `active` | `active`/`inactive` |
| `status_reason` | обяз. при inactive | причина недействования |
| `replaced_by_document_id` | опц. при inactive | номер преемника |
| `replaced_by_doc_key` | опц. при inactive | slug преемника |
| `ignore_sections` | авто | дефолт `["Предисловие", "Содержание"]` |

**Обязательные для «полной записи»** (`_REG_COMPLETENESS_FIELDS`): `document_id`, `title`,
`document_type`, `domain`, `edition`, `date_enacted`, `source_file`.

**Slug-генерация** (`_reg_make_slug`): `{PREFIX}[_{num}][_{word}]`, префикс `ГОСТ→GOST, СП→SP,
СО→SO, СНиП→SNIP, ПУЭ→PUE`, номер = первая группа цифр, хвост = транслит первого слова domain;
коллизия → суффикс `_2`, `_3`, …. Паттерн `^[A-Za-z0-9_]+$`. (В вебе — точная реплика `make_slug`
в `registration.py`.)

**Валидация `validate_rag_config`**: `defaults.max_chunk_tokens` int 100..100000; `status` ∈
{active,inactive}; при `active` поля `status_reason`/`replaced_by_*` обязаны быть `null`.

### 2.4 `providers.yaml` (общая схема)

Единый реестр провайдеров + роли. Секреты не хранятся — `api_key_env` содержит имя переменной `.env`.

```yaml
providers:
  <name>:
    base_url: https://service.example/v1   # всегда с /v1; endpoint добавляет вызывающий код
    api_key_env: SERVICE_API_KEY
    models:
      <model-name>: <tag>                  # chat | vision | embedding | rerank
roles:
  create_markdown:
    table_vision:    {provider, model, fallback: {provider, model}}
    ai_postprocess:  {provider, model, fallback: {provider, model}}
    registration_vision: {provider, model, fallback: {...}}   # новая роль (веб, §4.1)
  build_search_index:
    query_processing: {provider, model, fallback: {...}}
    embedding:        {provider, model, fallback: {...}}
    rerank:           {provider, model, fallback: {...}}
```

**Endpoint'ы по capability** (`llm_providers.CAPABILITY_ENDPOINT`): `chat → /chat/completions`,
`embedding → /embeddings`, `rerank → /rerank`. `vision` использует `chat`-endpoint с image-контентом.

**Разрешение ролей:** `resolve_subrole(cfg, pipeline, subrole)` (BSI) / `resolve_role` (CM) проверяет,
что провайдер и модель существуют, возвращает `{provider, model, api_key_env, base_url, fallback}`.
Неизвестный провайдер/модель/роль, дубль `api_key_env` — ошибка.

### 2.5 `search_config.yaml` (параметры узлов графа)

```yaml
nodes:
  analyze_query:       { temperature: 0.0, max_tokens: 256 }
  reformulate_query:   { temperature: 0.3, max_tokens: 256 }
  ask_clarification:   { temperature: 0.3, max_tokens: 512 }
  generate_answer:     { temperature: 0.0, max_tokens: 2048 }
```

Каждый узел требует числовые `temperature` и `max_tokens` (`load_search_config` валидирует).
Секция `nodes` обязательна и непуста.

### 2.6 `qa_graph.py` (публичный интерфейс чата)

LangGraph-граф из 6 узлов (`analyze_query → search → evaluate_results →
reformulate_query/ask_clarification → generate_answer`).

**Импорт-тайм зависимости `qa_graph.py`:** строка 64 `from search import (...)` (а `search.py`
на строке 38 делает `_get_providers()` на уровне модуля → нужен валидный `firmware/src/providers.yaml`);
строка 75 `from telegram_bot.asset_helpers import ...` (нужен `telegram_bot/asset_helpers.py`).
Сам `qa_graph.py` загружает providers/search-config **лениво** (кэш по пути): `_get_providers(cfg.providers_path)`
и `_get_search_config(cfg)` (→ `load_search_config(cfg.search_config_path)`).

**`QAGraphConfig` (dataclass):** `qdrant_path` (default `./qdrant_data`), `collection`
(default `technical_standard`), `retrieve_k=30`, `final_k=6`, `rrf_threshold=0.15`,
`search_config_path` (default `firmware/src/search_config.yaml`), `providers_path` (default
`firmware/src/providers.yaml`), `llm_provider`, `llm_model`, `llm_api_key`,
`score_good_threshold=0.7`, `score_medium_threshold=0.4`, `max_reformulate_attempts=2`.

**`QAGraph(config, checkpointer=None)`:**

| Метод | Вход/выход |
|---|---|
| `run(query) -> dict` | `final_answer`, `cited_chunk_ids`, `search_results` (с `doc_dir`), `needs_clarification`, `error`; при уточнении — `__interrupt__` (список `Interrupt`), `final_answer` отсутствует |
| `stream(query)` | генератор `{node_name: update}` (`stream_mode="updates"`); на interrupt чанк `{"__interrupt__": ...}` |
| `resume(user_response) -> dict` | возобновление после interrupt в том же `thread_id` |
| `resume_stream(user_response)` | стриминговое возобновление |
| `list_documents() -> list[{document_id, title}]` | distinct-метаданные из коллекции |

**Механика цитат:** `generate_answer` матчит ответ с `search_results` через `CITATION_MATCH_PATTERN`
(`[Документ, п. X.Y]` / `табл. N`) → `cited_chunk_ids`; при отсутствии цитат — однократная
перегенерация с доп. инструкцией.

**Механика картинок (аналог telegram_bot):** явная ссылка в query → явная в answer → cited-ассеты
по интенту → иначе не показываются (fail closed). Резолв пути: `search_results[].doc_dir` +
`assets[].image_path`/`image_paths`; `doc_dir` из `_load_assets(document_id)`.

**Жёсткая константа:** `BASE_MARKDOWN = "/mnt/sdb/!База_ГОСТ/Markdown"` (строка 333) — для поиска
`<dir>_assets.json` и резолва `doc_dir`. **Не изменяем.** На prod совпадает; на dev картинки в чате
не резолвятся без доступа к этому пути (симлинк/монтирование) — зафиксировано, не блокирует.

**Concurrency:** локальный Qdrant держит эксклюзивный файловый замок; клиент открывается на время
одного `search_node` и закрывается в `finally`. Веб придерживается того же паттерна (открыть→прочитать→
закрыть), не держит клиент открытым.

**CLI `qa_graph.py` (`build_parser()`, строки 1702–1731):** `--query`, `--interactive`, `--qdrant-path`
(default `./qdrant_data`), `--config` (search_config.yaml), `--providers_config` (**underscore**),
`--llm-provider`, `--llm-model`, `--api-key`, `--verbose`. Веб импортирует библиотеку напрямую
(не через этот CLI).

### 2.7 Зависимости (для venv веба)

- Create_Markdown_YA: `httpx, pyyaml, pymupdf, beautifulsoup4, python-dotenv, transformers>=4.51.0`.
- Build_Search_index: `qdrant-client, fastembed, requests, python-dotenv, tqdm, langgraph>=1.2, pyyaml`
  (+ транзитивно `langchain-core` из `langgraph`).
- Веб дополнительно: `fastapi, uvicorn[standard], python-multipart`.
- Требуется Python ≥ 3.11.

---

## 3. Компоненты и размещение файлов

Код веба — `firmware/src/` (конвенция «Source of Truth: firmware/src/»). Статика — `firmware/src/static/`.
Тесты — `firmware/tests/`. Общий каталог конфигов — отдельно от дерева веба (см. §8).

```
interface_RAG/
  config.yaml                     # конфиг деплоя (dev/prod, + config_dir — §8)
  scripts/
    deploy.sh                     # деплой на LXC 192.0.2.21 (dry-run; §4.10, §8)
  firmware/src/
    app.py                        # FastAPI: роуты, SSE, WebSocket, mount static
    deploy_config.py              # загрузка config.yaml (env-селекция dev/prod) + config_dir
    jobs.py                       # очередь; subprocess argv-списком; лог/стрим; стоп
    config_ui.py                  # чтение/валидация/запись YAML+".env" с .bak; маскирование
    providers_api.py              # scan /v1/models; теги по имени; add/refresh провайдеров
    registration.py               # вырезка 1-й страницы + vision-prefill + slug + запись <stem>_reg.yaml
    llm_client.py                 # OpenAI-совместимый chat/vision клиент + резолв ролей
    qdrant_api.py                 # read-only Qdrant: коллекции, distinct-документы (open/close на запрос)
    chat_api.py                   # импорт qa_graph; WS-чат; цитаты + картинки; list documents
    static/
      index.html                  # одна страница: шапка + 3 вкладки
      app.js                      # vanilla JS (SSE/WS, области документа, настройки)
      style.css                   # единый стиль
      favicon.svg                 # фавиконка (inline-SVG или файл; см. §4.8)
  firmware/tests/                 # тесты веба
  docs/architecture/              # этот документ + implementation-plan.md
```

> `settings.html` больше не используется как отдельный фрагмент — настройки рендерятся в
> `index.html` (вкладка «Настройки»); при желании фрагмент можно оставить, но источник истины —
> `index.html` + `app.js`.

### 3.1 Модуль → ответственность → интерфейс

**`deploy_config.py`** — единственный источник путей/флагов.
- `load() -> DeployConfig` — читает `config.yaml`; секция выбирается `active` или `INTERFACE_RAG_ENV`
  (приоритет env). Поля: `host`, `port`, `upload_base_dir`, `upload_max_mb`, `create_markdown_dir`,
  `build_search_index_dir`, `qdrant_path`, `collection`, `write_enabled`, **`config_dir`**,
  `env_file` (= `<config_dir>/.env`), `base_markdown`, `prompts`, `model_tags`, `environment`.
- Производные свойства (реализовать здесь, не дублировать в других модулях):
  - `providers_path = config_dir / "providers.yaml"`
  - `create_markdown_config_path = config_dir / "create_markdown_config.yaml"`
  - `search_config_path = config_dir / "search_config.yaml"`
- Никакие другие модули не читают пути конфигов/пайплайнов напрямую.

**`jobs.py`** — запуск пайплайнов и жизненный цикл задачи (без изменений против прошлой версии).
- `class Job`: `id`, `kind` (`convert`|`index`|`sequence`), `argv: list[str]`, `status`
  (`pending|running|done|error|stopped`), `exit_code`, `log_buffer`, `pid`, `phases`.
- `class JobRunner`: `start(argv, cwd, env, kind)`, `start_sequence(phases, cwd, env, kind)`,
  `stop(job_id)` (SIGTERM → 5s → SIGKILL), `get`, `subscribe` (очередь для SSE). Запуск
  `subprocess.Popen(argv, cwd=..., env=..., shell=False, stdout=PIPE, stderr=STDOUT, text=True,
  bufsize=1)`; чтение строк в буфер + очередь. Сериализация N=1 (гонка за Qdrant-замок/файлы документа).

**`config_ui.py`** — безопасное редактирование YAML/`.env` в **общем каталоге**.
- `read_yaml(path)` / `write_yaml(path, data)` — атомарно (temp + `os.replace`) + `.bak`; гейт
  `yaml.safe_load`.
- `read_env(path)` (маска `••••<last4>`) / `read_env_raw(path)` (для инъекции) / `write_env(path, values)`
  — атомарно + `.bak`; пустое значение = удалить ключ.
- Валидаторы: `validate_providers`, `validate_search_config`, `validate_reg_record`.
- `sync_role_models(providers, spec, kind)` — запись выбранной модели сразу в обе роли capability
  (`chat → ai_postprocess + query_processing`; `vision → table_vision + registration_vision`).
- Никогда не отдаёт значения ключей наружу (только наличие/маску).

**`providers_api.py`** — провайдеры.
- `scan_models(base_url, api_key) -> list[str]` — `GET {base_url}/models`.
- `tag_model(name, rules) -> tag` — правила §4.2.
- `add_provider(providers_path, name, base_url, api_key_env, models) -> dict`.
- **Новое:** `refresh_all_models(providers_path, env) -> dict` — «Обновить»: для каждого провайдера с
  доступным ключом вызвать `scan_models` и обновить `models` (теги по имени); ошибки по конкретному
  провайдеру не роняют остальные, возвращают `{provider: {ok, models|error}}` (решение №11).

**`registration.py`** — регистрация документа (веб-эквивалент `--reg` без TTY).
- `extract_first_page(source_file) -> bytes` — PyMuPDF для PDF; DOCX/DOC → libreoffice headless → PDF
  → стр.1; при неудаче `None` (ручной ввод).
- `vision_prefill(image_bytes, *, config, providers_path, env) -> dict` — vision-модель (роль
  `create_markdown.registration_vision`) → `{document_id, title, document_type, domain_hint}`.
- `make_slug(document_id, document_type, domain, existing) -> str` — реплика `_reg_make_slug`.
- `write_reg_yaml(reg_path, fields) -> slug` — рендер `documents.<slug>` (§2.3) + атомарная запись с
  `.bak` + гейт.
- `FIELDS` — полный список полей схемы (источник для формы регистрации, §5).

**`llm_client.py`** — вызовы LLM для веба (регистрация + «Тест»).
- `resolve_role(providers, pipeline, subrole) -> dict`; `chat_completion` / `vision_completion`
  (vision: content = `[{type:"text"},{type:"image_url", image_url:{url:"data:image/png;base64,..."}}]`);
  `run_with_fallback`.

**`qdrant_api.py`** — read-only Qdrant (открыть→прочитать→закрыть на каждый запрос).
- `list_collections(qdrant_path)`; `distinct_documents(qdrant_path, collection)`.

**`chat_api.py`** — импорт `qa_graph` (как Telegram-бот).
- `sys.path` к `Build_Search_index/firmware/src` (+ `telegram_bot`); импорт
  `qa_graph.QAGraph/QAGraphConfig` и `telegram_bot.asset_helpers` (чистый stdlib) — read-only.
- `ChatSession` (один `QAGraph` на WebSocket-сессию) с `QAGraphConfig(providers_path=<config_dir>/providers.yaml,
  search_config_path=<config_dir>/search_config.yaml, qdrant_path=..., collection=...)`.
- `answer/stream/resume/list_documents`; `select_images(search_results, query, answer, cited,
  base_markdown)` — приоритет бота, выдаёт URL (`/api/images/...`).

**`app.py`** — маршруты (§7) + mount static + JobRunner/сессии. Читает/пишет конфиги **только** из
`cfg.config_dir` (один `providers.yaml`, без `_combined_providers`/`_write_combined` из прошлой версии).

---

## 4. Ключевые проектные решения

### 4.1 Роль vision-модели для регистрации — `registration_vision` (синхронно с vision)

Роль `create_markdown.registration_vision` (`provider` + `model` + `fallback`) **синхронно** получает
ту же vision-модель, что выбрана в секции «vision» настроек (решение №9): при сохранении vision-модели
веб атомарно пишет её в `roles.create_markdown.table_vision` И `roles.create_markdown.registration_vision`
(через `config_ui.sync_role_models(..., kind="vision")`). Аддитивно — пайплайны читают только известные
им роли и не сломаются от нового ключа. Промпт — `prompts.registration_vision` в `config.yaml`.
Fallback: vision ничего не распознала / первая страница без текста / не-ГОСТ → ручной ввод (обязательные
поля помечены), веб всё равно пишет `<stem>_reg.yaml`.

### 4.2 Теги моделей «по имени» (правила матчинга)

Порядок = приоритет, первое совпадение (substring, case-insensitive):

| Порядок | Тег | Правило (имя содержит) |
|---|---|---|
| 1 | `embedding` | `embedding`, `embed` |
| 2 | `rerank` | `rerank`, `reranker` |
| 3 | `vision` | `vision`, `-vl`, `vl-`, `4.5v`, `4.6v`, `4v`, `-v-flash`, `llava`, `gpt-4o`, `qwen.*-vl`, `gemini.*vision`, `claude.*vision` |
| 4 | `chat` | всё остальное (дефолт) |

Правила — в `config.yaml` (`model_tags`), после скана тег можно менять вручную.

### 4.3 «Один провайдер» для `ai_postprocess` + `query_processing` (синхронно)

Одна основная чат-модель (primary + fallback). При сохранении пишется в обе роли одновременно:
`roles.create_markdown.ai_postprocess` и `roles.build_search_index.query_processing`
(`sync_role_models(..., kind="chat")`). Решение №6.

### 4.4 Gating шагов на сервере (решение №4, сохраняется)

`DocumentSession` (in-memory) содержит `session_id`, `stem`, `base_dir`, пути `reg_path`/`md_path`/
`chunks_path`/`assets_path`. Gating — проверка файлов на диске:

| Шаг | Активен, если |
|---|---|
| Регистрация | файл загружен (сессия существует) |
| Конвертация | файл загружен; роли `table_vision`/`ai_postprocess` заданы и ключи есть в `.env` |
| Индексация | `<stem>_reg.yaml` И `<stem>.md` существуют; `qdrant.write_enabled == true` |

Невыполнение → кнопка неактивна + сообщение «завершите предыдущие шаги». Если `.md` уже есть — шаг
конвертации считается выполненным (кнопка «Пере-конвертировать (заново)»).

### 4.5 «Стоп» и облачная Yandex OCR

`JobRunner.stop()` → SIGTERM → 5s → SIGKILL. Облачная задача Yandex OCR, отправленная до SIGTERM, может
завершиться на стороне облака — принимается (решение №2). Статус задачи `stopped`.

### 4.6 Запись в Qdrant — только прод

`qdrant.write_enabled` (dev=false, prod=true). Кнопка «Добавить в базу» активна только при `write_enabled`
(решение №3).

### 4.7 Формат и место конфига деплоя

Единый `interface_RAG/config.yaml` с секциями `dev:`/`prod:` и `active: dev`; выбор среды
переопределяется `INTERFACE_RAG_ENV`. См. §8.

### 4.8 Новый UI (решения 10–17) — схема

Три вкладки одной страницы (`index.html`), единый стиль, без сырых дампов конфигов.

- **Шапка (§5.0, решение 16):** `<title>` с понятным названием (напр. «Оркестратор ГОСТ-базы:
  OCR→Markdown и семантический поиск»), `<link rel="icon">` на `favicon.svg` (inline SVG data-URI или
  файл `static/favicon.svg`), заголовок + подзаголовок на странице «что за инструмент и для чего».
- **Добавить документ (§5.2, решения 10, 17):** одна страница, три визуальные области (НЕ пошаговые
  модальные окна): **Загрузка** / **Регистрация (форма)** / **Лог и прогресс** (конвертация + индексация).
  Регистрация — статичная форма со ВСЕМИ полями `documents.<slug>` (§2.3), у каждого поля русское
  название + комментарий (назначение + пример, tooltip), обязательные помечены `*`, пустые — с
  placeholder = имя поля. Vision-prefill заполняет определённые поля (редактируемы). Кнопка
  «Зарегистрировать документ» — внизу области. После конвертации — MD-вьювер (кнопка/ссылка
  `GET /api/files/markdown/<stem>`; рендер/скачивание).
- **Настройки (§5.3, решения 11–15):** вверху единая кнопка **«Сохранить»** (пишет все секции одним
  действием) и **«Обновить»** (запрос `/v1/models` ко всем провайдерам с доступным ключом, обновление
  списка моделей). Без raw-дампов `providers.yaml`/`.env` — только структурированные поля (ключи
  маскированы). Секции chat/vision/embedding/rerank: подпись **«Основная модель»** + чекбокс
  **«Fallback»** (настройки fallback видны только при включённом чекбоксе) + пояснение назначения роли
  (tooltip/подпись). В списках vision/embedding/rerank — только модели с соответствующим тегом.
  Секция **«Параметры графа»** (`search_config.yaml`: analyze_query, reformulate_query,
  ask_clarification, generate_answer) с комментариями о назначении каждого узла. Yandex OCR —
  отдельная секция (ключи/настройки). «Добавить провайдера» — модальное окно (имя, api key, base_url;
  «Тест»/«Добавить»/«Отмена») с тегами по имени модели (решение №5).

### 4.9 Единый общий конфиг (решение 19) + импорт-тайм нюанс

**Каталог конфигов** (единственное место, куда пишет интерфейс):
- dev: `/root/projects/interface_RAG/config/`
- prod: `/root/RAG/config/`

Состав: `providers.yaml`, `create_markdown_config.yaml`, `search_config.yaml`, `.env`.

**Передача пайплайнам — только через CLI-ключи; ключи `.env` — через `env` subprocess:**
- `create_markdown.py` (конвертация): `--config <config_dir>/create_markdown_config.yaml`,
  `--providers-config <config_dir>/providers.yaml`.
- `create_markdown.py` (`--rag`): те же два ключа.
- `create_index.py`: `--providers_config <config_dir>/providers.yaml` (underscore) + `--qdrant-path`,
  `--collection`, `--strict`.
- `qa_graph` (чат, как библиотека): `QAGraphConfig(providers_path=<config_dir>/providers.yaml,
  search_config_path=<config_dir>/search_config.yaml, ...)`.
- `.env`: `env = os.environ | config_ui.read_env_raw(config_dir / ".env")` → `Popen(env=...)`.

**Импорт-тайм нюанс (важно для «чистого» прода):** `Build_Search_index` грузит `providers.yaml` при
импорте модулей (`create_index.py:53`, `search.py:38` — через дефолтный путь
`Build_Search_index/firmware/src/providers.yaml`). Поэтому в дереве пайплайна должен остаться
**валидный** `providers.yaml` по этому адресу. **Решение — симлинк** (не копия):
`Build_Search_index/firmware/src/providers.yaml` → `<config_dir>/providers.yaml`. Это удовлетворяет и
импорт-тайм загрузке, и требованию «у пайплайнов нет своих копий» (один файл-источник). Явные
`--providers_config`/`providers_path` веб всё равно передаёт (переопределяют дефолт в `main()`).

`search_config.yaml` и `create_markdown_config.yaml` грузятся лениво по явному пути — локальные копии
пайплайнам НЕ нужны. `.env` — не нужен (инъекция env выигрывает, §2.1/§2.2).

### 4.10 Чистый прод + deploy-скрипт (решение 18)

На LXC `192.0.2.21` (ssh `root:root`, порт 80) — только нужное для работы:

```
/root/RAG/
  config/                          # единый каталог конфигов
    providers.yaml
    create_markdown_config.yaml
    search_config.yaml
    .env
  interface_RAG/                   # веб-интерфейс
    config.yaml                    # active: prod, config_dir: /root/RAG/config
    firmware/src/*.py
    firmware/src/static/
    firmware/src/requirements.txt
    venv/
  Create_Markdown_YA/
    firmware/src/create_markdown.py
  Build_Search_index/
    firmware/src/create_index.py
    firmware/src/qa_graph.py
    firmware/src/search.py
    firmware/src/llm_providers.py
    firmware/src/telegram_bot/asset_helpers.py
    firmware/src/providers.yaml -> /root/RAG/config/providers.yaml   # симлинк (импорт-тайм)
```

Без `.git/`, `docs/`, `workflows/`, `.hermes/`, `tests/`, `.pytest_cache/`, `README`, `__pycache__`,
`bot.log*`, `request_bot.py`, `tmp/`. **rsync-манифест (per-pipeline)** — в §8.3.

`scripts/deploy.sh` (bash) с флагом `--dry-run` (показать план, ничего не менять). Порядок операций:
1. **Бэкап** текущих конфигов прода перед изменением: `cp -a /root/RAG/config /root/RAG/config.bak.<ts>`
   (и копии `providers.yaml`/`.env` из каталогов пайплайнов, если они ещё есть).
2. **Консолидация** в `/root/RAG/config/`: эталон `providers.yaml` — «полный» вариант с LXC
   (Build_Search_index, с ролями `build_search_index`); долить отсутствующих провайдеров/модели из
   варианта Create_Markdown_YA; `.env` — собрать все 8 ключей из всех мест БЕЗ затирания реальных
   значений; `create_markdown_config.yaml` — из Create_Markdown_YA; `search_config.yaml` — из
   Build_Search_index.
3. **rsync** манифеста (§8.3) → пайплайны и веб-интерфейс.
4. **Симлинк** `Build_Search_index/firmware/src/providers.yaml` → `/root/RAG/config/providers.yaml`.
5. **Рестарт** systemd `interface-rag.service`.
6. **Smoke**: `curl -sf http://127.0.0.1/` → HTTP 200.

Реальный деплой выполняет оркестратор после ревью; Coder (фаза 1) доводит скрипт до рабочего
dry-run.

---

## 5. Поток «Добавить документ» (одна страница, 3 области)

Области: **Загрузка / Регистрация (форма) / Лог и прогресс**. Шаги те же, что раньше, но управление —
одной страницей, регистрация — статичной формой.

1. **Загрузка** (`POST /api/documents`): multipart; расширение ∈ {pdf, docx, doc, md}; размер ≤ лимит;
   имя = `basename` (path traversal отклоняется). Файл в `<upload.base_dir>/<basename>`. Создаётся
   `DocumentSession`. После загрузки автоматически выполняется `POST /api/documents/{sid}/register/prefill`
   (вырезка 1-й страницы → vision → подсказки полей; заполняют форму регистрации, редактируемы).
2. **Регистрация** — статичная форма со всеми полями `documents.<slug>` (§2.3): русское название +
   комментарий (tooltip: назначение + пример), обязательные помечены `*`, placeholder = имя поля.
   Кнопка «Зарегистрировать документ» (`POST /api/documents/{sid}/register`) пишет
   `Markdown/<stem>/<stem>_reg.yaml` (`.bak` + гейт). Повторная запись перезаписывает (last-writer-wins).
3. **Конвертация** (`POST /api/documents/{sid}/convert`): JobRunner запускает `create_markdown.py --ai`
   (§2.1, argv §8.4). SSE транслирует лог. По завершении — MD-вьювер: ссылка
   `GET /api/files/markdown/<stem>` (просмотр/скачивание). Если `.md` уже есть — шаг выполнен
   (без пере-конвертации).
4. **Индексация** (`POST /api/documents/{sid}/index`): сервер проверяет `.md` + `_reg.yaml`, затем две
   фазы последовательно (одна job, `start_sequence`):
   1. `create_markdown.py -i Markdown/<stem>/<stem>.md --rag` → `<stem>_chunks.jsonl` + `<stem>_assets.json`;
   2. `create_index.py Markdown/<stem>/ --qdrant-path <abs> --collection <name> --strict
      --providers_config <config_dir>/providers.yaml`.

Кнопка «Стоп» (`POST /api/jobs/{id}/stop`) — во время шага 3/4. Лог/ошибки подсвечиваются цветом.

---

## 6. Поток «Чат»

WebSocket `ws /ws/chat`. Жизненный цикл сообщения:

1. Клиент шлёт `{type:"query", text}`.
2. Сервер создаёт/переиспользует `QAGraph` сессии; `qa.stream(text)` в фоне; шлёт `{type:"node", node}`.
3. На `__interrupt__` → `{type:"clarification", text}`; клиент шлёт `{type:"reply", text}` →
   `qa.resume_stream(text)`.
4. По `final_answer` → `{type:"answer", answer, cited_chunk_ids, sources, images}`, где `images` =
   `{url, caption, asset_type}`; `url` → `GET /api/images/...`. Выбор картинок — приоритет бота (§2.6).
5. Ошибка/пустой ответ → `{type:"error", text}`.

«Показать документы в базе» — `GET /api/documents-in-base` → distinct по `document_id`.

История диалога — в памяти на сессию (как у бота, `MAX_HISTORY`), не персистится.

---

## 7. API-поверхность (маршруты FastAPI)

**Документы/wizard**
- `POST /api/documents` — загрузка (multipart) → `{session_id, stem}`.
- `GET  /api/documents/{sid}` — состояние сессии (наличие reg/md/chunks/assets, статусы, `can_index`).
- `POST /api/documents/{sid}/register/prefill` — vision-подсказки полей (+ `slug`, `image_available`).
- `GET  /api/documents/{sid}/reg` — текущие поля `_reg.yaml` (если есть).
- `POST /api/documents/{sid}/register` — запись `<stem>_reg.yaml` (payload: `{fields: {...}}`).
- `POST /api/documents/{sid}/convert` — конвертация (job).
- `POST /api/documents/{sid}/index` — индексация (job, 2 фазы).
- `GET  /api/jobs/{job_id}` — статус. `GET /api/jobs/{job_id}/events` — SSE. `POST /api/jobs/{job_id}/stop`.

**Файлы (безопасная отдача)**
- `GET /api/files/markdown/{stem}` — отдать `<base_markdown>/<stem>/<stem>.md` (валидация `stem`:
  `^[A-Za-z0-9_.а-яА-ЯёЁ\- ]+$`, запрет `..`/`/`; `resolve()` → проверка префикса `base_markdown`).
- `GET /api/images/{doc_dir}/{rel_path:path}` — картинка; `resolve()` внутри `base_markdown`; только
  `.png/.jpg/.jpeg/.webp`.

**Чат**
- `WS  /ws/chat` — протокол §6.
- `GET /api/documents-in-base` — distinct-метаданные.

**Настройки (единый каталог конфигов)**
- `GET /api/settings/providers` — реестр + роли (ключи маскированы; ОДИН `providers.yaml`).
- `PUT /api/settings/providers` — записать весь `providers.yaml` (валидация `validate_providers`).
- `PUT /api/settings/providers/roles` — синхронная запись роли (`{kind, spec}` → `sync_role_models`).
- `POST /api/settings/providers/scan` — `{base_url, api_key}` → модели с тегами.
- `POST /api/settings/providers/add` — добавить провайдера (+ запись `api_key_env` в `.env`).
- `POST /api/settings/providers/refresh` — «Обновить»: `/v1/models` ко всем провайдерам с доступным
  ключом → обновить `models` (теги по имени). Ответ: `{provider: {ok, models|error}}`.
- `GET/PUT /api/settings/search-config` — `search_config.yaml` (секция nodes).
- `GET/PUT /api/settings/create-markdown-config` — `create_markdown_config.yaml`.
- `GET/PUT /api/settings/env` — ключи `.env` (маскированы; PUT пишет только переданные непустые
  значения + `delete`).
- `GET /api/settings/collections` — коллекции Qdrant (read-only).
- `GET /api/settings/status` — сводка готовности (роли/ключи заданы).

> Единая кнопка «Сохранить» на фронте собирает ВСЕ секции (providers + roles + graph nodes + env +
> create-markdown-config) и шлёт их одним батчем (PUT на соответствующие маршруты); отдельного
> сохранения на секцию нет (решение №11).

---

## 8. Конфигурация деплоя, манифест и argv

### 8.1 `config.yaml`

```yaml
active: dev                    # или INTERFACE_RAG_ENV=prod

dev:
  host: "127.0.0.1"
  port: 8081
  config_dir: "/root/projects/interface_RAG/config"   # единый каталог конфигов
  upload:
    base_dir: "/root/projects/interface_RAG/uploads"
    max_mb: 500
  pipelines:
    create_markdown_dir: "/root/projects/Create_Markdown_YA/firmware/src"
    build_search_index_dir: "/root/projects/Build_Search_index/firmware/src"
  qdrant: {path: "/root/projects/interface_RAG/uploads/qdrant_data", collection: technical_standard, write_enabled: false}
  env_file: "/root/projects/interface_RAG/config/.env"
  base_markdown: "/root/projects/interface_RAG/uploads/Markdown"

prod:
  host: "0.0.0.0"
  port: 80
  config_dir: "/root/RAG/config"
  upload: {base_dir: "/mnt/sdb/!База_ГОСТ", max_mb: 500}
  pipelines:
    create_markdown_dir: "/root/RAG/Create_Markdown_YA/firmware/src"
    build_search_index_dir: "/root/RAG/Build_Search_index/firmware/src"
  qdrant: {path: "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data", collection: technical_standard, write_enabled: true}
  env_file: "/root/RAG/config/.env"
  base_markdown: "/mnt/sdb/!База_ГОСТ/Markdown"

prompts:
  registration_vision: |
    Ты — библиограф нормативных документов. На изображении — титульная
    страница нормативного документа (ГОСТ/СП/СО/СНиП/ПУЭ).
    Извлеки строго в JSON без пояснений:
    {"document_id": "...", "title": "...", "document_type": "...", "domain_hint": "..."}
    Если поле не определяется — null. Только JSON.

model_tags:
  embedding: ["embedding", "embed"]
  rerank:    ["rerank", "reranker"]
  vision:    ["vision", "-vl", "vl-", "4.5v", "4.6v", "4v", "-v-flash", "llava", "gpt-4o"]
```

`env_file` всегда указывает на `<config_dir>/.env`. `.env` — 8 ключей (решения/§6 требований):
`YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `DEEPSEEK_API_KEY`, `PROVOD_API_KEY`, `ANYMODEL_API_KEY`,
`Z_AI_API_KEY`, `SILICONFLOW_API_KEY`, `TELEGRAM_BOT_TOKEN`.

### 8.2 Инъекция env

`jobs.py` строит `env = os.environ | config_ui.read_env_raw(config_dir / ".env")` и передаёт в
`Popen(env=...)`. Пайплайны загружают `.env` через `setdefault`/`load_dotenv(override=False)` (§2.1/§2.2),
поэтому инъекция имеет приоритет; собственные `.env`-файлы пайплайнов не нужны.

### 8.3 rsync-манифест «чистого» прода

**Веб-интерфейс (`interface_RAG` → `/root/RAG/interface_RAG/`):**
- `config.yaml`
- `firmware/src/*.py` (app, deploy_config, jobs, config_ui, providers_api, registration, llm_client,
  qdrant_api, chat_api, `__init__.py`)
- `firmware/src/static/` (index.html, app.js, style.css, favicon.svg)
- `firmware/src/requirements.txt`

**Create_Markdown_YA (`→ /root/RAG/Create_Markdown_YA/`):**
- `firmware/src/create_markdown.py` (самодостаточный; конфиги — только через CLI-ключи)

**Build_Search_index (`→ /root/RAG/Build_Search_index/`):**
- `firmware/src/create_index.py`
- `firmware/src/qa_graph.py`
- `firmware/src/search.py`
- `firmware/src/llm_providers.py`
- `firmware/src/telegram_bot/asset_helpers.py` (импортируется `qa_graph.py:75`)
- `firmware/src/providers.yaml` → **симлинк** на `/root/RAG/config/providers.yaml` (импорт-тайм, §4.9)

**Исключить везде:** `.git/`, `docs/`, `workflows/`, `.hermes/`, `tests/`, `.pytest_cache/`, `README*`,
`__pycache__/`, `*.pyc`, `tmp/`, `bot.log*`, `request_bot.py`, `.env` в каталогах пайплайнов.

### 8.4 Точные argv (для `jobs.py`)

**Конвертация (шаг 3):**
```text
[sys.executable, <create_markdown_dir>/create_markdown.py,
 "-i", <source>, "--ai",
 "--config", <config_dir>/create_markdown_config.yaml,
 "--providers-config", <config_dir>/providers.yaml]
```
cwd = `<create_markdown_dir>`; env = §8.2.

**Индексация (шаг 4) — фаза 1 (RAG):**
```text
[sys.executable, <create_markdown_dir>/create_markdown.py,
 "-i", <md_path>, "--rag",
 "--config", <config_dir>/create_markdown_config.yaml,
 "--providers-config", <config_dir>/providers.yaml]
```
**— фаза 2 (индекс):**
```text
[sys.executable, <build_search_index_dir>/create_index.py,
 <md_dir>, "--qdrant-path", <qdrant_path>, "--collection", <collection>,
 "--strict", "--providers_config", <config_dir>/providers.yaml]
```
cwd = `<create_markdown_dir>` (фаза 1) / `<build_search_index_dir>` (фаза 2); env = §8.2.

**Чат (`chat_api.py`):** `QAGraphConfig(qdrant_path=..., collection=...,
providers_path=<config_dir>/providers.yaml, search_config_path=<config_dir>/search_config.yaml)`.

**Запуск dev:** `uvicorn app:app --host 127.0.0.1 --port 8081` из `firmware/src/`.
**Запуск prod:** systemd `interface-rag.service` на LXC `192.0.2.21:80` (без авторизации).

---

## 9. Безопасность

1. **subprocess argv-списком, без shell** (`shell=False`; никогда не конкатенировать команду в строку).
2. **Path traversal:** имя файла = только `basename`; `stem`/`rel_path` — regex + `resolve()` + проверка
   префикса `base_markdown`/`upload.base_dir`.
3. **Безопасная отдача `image/` и `.md`** — только через маршруты §7 с проверкой реального пути и
   белого списка расширений.
4. **Загрузка файлов** — валидация расширения и размера; ограниченный буфер.
5. **Маскирование ключей** — значения `.env`/`api_key_env` не отдаются (только маска `••••1234`).
6. **Атомарная запись** — temp + `os.replace`; конфиги веба — с `.bak`.
7. **Без авторизации (принято, решение №8)** — LAN `192.0.2.21:80`; сервис не выходит в WAN.

---

## 10. Конкуренция, жизненный цикл, риски

- **Qdrant-замок:** клиент открывается/закрывается на каждый запрос; задачи `jobs` сериализуются (N=1).
- **QAGraph на сессию:** один инстанс на WebSocket; sparse-модель шарится через модульный синглтон.
- **`BASE_MARKDOWN` захардкожен** в `qa_graph.py` — не менять; на dev картинки могут не резолвиться.
- **Импорт-тайм `providers.yaml`** в BSI — обязателен симлинк (§4.9), иначе `create_index.py`/чат падают
  при старте.
- **Токенизатор HF** (`Qwen/Qwen3-Embedding-8B`) скачивается при первом `--rag`; на LXC нужен доступ к
  HF Hub или предзагруженный кэш (`HF_HOME`); `allow_degraded_fallback: false` → жёсткая ошибка при
  отсутствии. Prerequisite деплоя.
- **DOCX/DOC для регистрации** требуют `libreoffice` (headless); при отсутствии — ручной ввод.
- **Состояние сессий/задач в памяти** — не переживает рестарт (принято для v1, однопользовательский
  LAN-инструмент). Логи задач пишутся в файл для аудита.
- **last-writer-wins** на `<stem>_reg.yaml` и конфигах — инструмент однопользовательский.

---

## 11. Открытые вопросы / решения, ожидающие подтверждения

1. Точные prod-пути пайплайнов на LXC заданы как `/root/RAG/{Create_Markdown_YA,Build_Search_index,
   interface_RAG}`; Coder (фаза 1) сверяет их по факту размещения на `192.0.2.21` (в конфиге уже
   прописаны).
2. Имя коллекции `technical_standard` — default; иная коллекция задаётся в `qdrant.collection`.
3. Матчинг тегов (§4.2) — конфигурируем; уточняется после первого скана реальных моделей.

---

## 12. Трассируемость требований

| Требование (requirements.md / решение) | Раздел архитектуры |
|---|---|
| Три раздела UI (§4–5) | §1, §3, §6, §7 |
| Шапка: title + favicon + подзаголовок (§5.0, №16) | §4.8 |
| «Добавить документ» одной страницей, области (§5.2, №10) | §4.4, §4.8, §5 |
| Регистрация — статичная форма со всеми полями + комментарии (§5.2, №10) | §2.3, §4.8, §5 |
| MD-вьювер (§5.2, №17) | §4.8, §5, §7 |
| Единая кнопка «Сохранить» + «Обновить» (§5.3, №11) | §4.8, §7 (providers/refresh) |
| Fallback по чекбоксу (§5.3, №12) | §4.8 |
| Пояснения ролей (§5.3, №13) | §4.8 |
| Параметры узлов графа (§5.3, №14) | §2.5, §4.8, §7 |
| Без сырых дампов конфигов (§5.3, №15) | §4.8, §7 (маскирование) |
| Чистая структура прода (§8, №18) | §4.10, §8.3 |
| Единый общий конфиг через CLI-ключи + env (§8, №19) | §4.9, §8.1–8.4 |
| Чат: qa_graph, цитаты+картинки (§5.1, §7) | §2.6, §6 |
| Gating по шагам (§5.2, №4) | §4.4, §5 |
| Добавить провайдера: /v1/models + теги по имени (§5.3, №5) | §4.2, providers_api |
| Один провайдер ai_postprocess+query_processing (§5.3, №6) | §4.3 |
| Vision синхронно в table_vision+registration_vision (§5.3, №9) | §4.1 |
| Показать документы — distinct из Qdrant (§5.1, №7) | §2.6, §6, qdrant_api |
| Пути не хардкодить; dev/prod (§7, §8) | §8 |
| Запись в Qdrant — только прод (§7, №3) | §4.6, §8 |
| Стоп: SIGTERM, облачная OCR может не прерваться (§5.2, №2) | §4.5 |
| Безопасность (argv, traversal, маски, .env) | §9 |
