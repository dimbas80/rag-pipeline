# interface_RAG — Архитектура веб-оркестратора

> Статус: архитектура для реализации (Coder). Источник требований — `docs/product/requirements.md`
> и `docs/product/decision-log.md`. Ниже зафиксированы результаты reverse-engineering двух
> пайплайнов (`Create_Markdown_YA`, `Build_Search_index`) и проектные решения.
>
> Пайплайны НЕ изменяются — только чтение. `interface_RAG` — тонкий оркестратор поверх их CLI
> и публичного API `qa_graph.py`.

---

## 1. Цель и границы

`interface_RAG` — локальная веб-страница (LAN) с тремя разделами:

1. **Чат** — вопрос → ответ с цитатами и картинками таблиц/рисунков (импорт `qa_graph.py`).
2. **Добавить документ** — пошаговый wizard: загрузка → регистрация → конвертация → индексация.
3. **Настройки** — управление конфигами обоих пайплайнов и общим `.env`.

**Границы (жёсткие):**
- НЕ редактировать `Create_Markdown_YA/` и `Build_Search_index/` — только чтение их файлов
  и запуск их CLI через `subprocess`.
- НЕ хардкодить пути к пайплайнам/базе — всё в конфиге деплоя.
- Запись в Qdrant — только прод-инстанс (в dev кнопка индексации заблокирована флагом конфига).
- Стек: FastAPI + статический HTML/vanilla JS (без Node/React).

---

## 2. Итоги reverse-engineering (что выяснено)

### 2.1 `create_markdown.py` (CLI, Create_Markdown_YA)

Файл `firmware/src/create_markdown.py` (~6980 строк). Точка входа `main()`; вход — ровно один файл
(папки не поддерживаются).

**Аргументы (argparse, `parse_args()`):**

| Флаг | Значение |
|---|---|
| `-i, --input` | обязательный; PDF/DOCX/DOC/MD |
| `--ai` | полный AI-цикл (vision-таблицы + gap-filling + AI-постобработка) |
| `--json-native` | структурный JSON-native рендеринг (опционально) |
| `--config` | единый конфиг промптов/RAG, default `./create_markdown_config.yaml` |
| `--providers-config` | реестр провайдеров, default `./providers.yaml` |
| `--rag` | сгенерировать/перезаписать RAG-файлы (только из проверенного MD) |
| `--reg` | интерактивная регистрация в `<stem>_reg.yaml` (требует TTY) |

**Выходы:**
- OCR-конвертация: `<input_dir>/Markdown/<stem>/<stem>.md` + `<input_dir>/Markdown/<stem>/image/`
  (таблицы/рисунки). Временные: `<input_dir>/tmp/<stem>/`. Лог: `<input_dir>/tmp/Create_Markdown_VisionOCR.log`.
- RAG (фактический код `run_rag_pipeline()`, строки 6320/6324): **`<stem>_chunks.jsonl`** и
  **`<stem>_assets.json`** в папке `Markdown/<stem>/` рядом с `.md`.

  ⚠️ **Расхождение с текстом помощи**: в `--help`/docstring указаны `rag_chunks.jsonl`/`rag_assets.json`,
  но реально пишутся `<stem>_chunks.jsonl` / `<stem>_assets.json`. Доверять коду, а не help-строке.
  `create_index.py` ищет именно `*_chunks.jsonl` / `*_assets.json`.

**Загрузка конфигов:**
- `.env` читается жёстко из `Path(__file__).parent / ".env"` = `firmware/src/.env` (не от cwd).
- `providers` загружаются только при `--ai` или `--reg` (`load_providers_config` + `resolve_role`).
  Роли: `roles.create_markdown.table_vision`, `roles.create_markdown.ai_postprocess`.
- RAG-конфиг загружается при `--rag`/`--reg` через `load_rag_config(args.config, reg_path)`
  (см. §2.3). При ошибке: `--rag` → warning и пропуск; `--reg` → `sys.exit(1)`.
- `--reg` без TTY → ошибка, конфиг не мутируется, `exit 1`. **В вебе `--reg` не используется** —
  веб пишет `<stem>_reg.yaml` сам (см. §5).

**Точный вызов шага конвертации (wizard, шаг 3):**

```bash
python3 <Create_Markdown_YA>/firmware/src/create_markdown.py \
  -i "<base_dir>/<file>.pdf" \
  --ai \
  --config <Create_Markdown_YA>/firmware/src/create_markdown_config.yaml \
  --providers-config <Create_Markdown_YA>/firmware/src/providers.yaml
```

cwd процесса = `<Create_Markdown_YA>/firmware/src` (для любых относительных путей); env инжектируется
явно (см. §8).

**Точный вызов шага индексации (wizard, шаг 4) — RAG-чанкование из готового `.md`:**

```bash
python3 <Create_Markdown_YA>/firmware/src/create_markdown.py \
  -i "<base_dir>/Markdown/<stem>/<stem>.md" \
  --rag \
  --config <Create_Markdown_YA>/firmware/src/create_markdown_config.yaml \
  --providers-config <Create_Markdown_YA>/firmware/src/providers.yaml
```

Режим `md_rag` (`file_type == "md_rag"`): не запускает OCR/AI, читает `.md` read-only, вызывает
`_run_rag_only()` → атомарно перезаписывает `<stem>_chunks.jsonl` и `<stem>_assets.json`.
`providers` для `--rag` не нужны (нужен только токенизатор Hugging Face `Qwen/Qwen3-Embedding-8B`,
`allow_degraded_fallback: false`).

### 2.2 `create_index.py` (CLI, Build_Search_index)

`firmware/src/create_index.py` индексирует `<stem>_chunks.jsonl` + `<stem>_assets.json` в локальный
Qdrant (dense SiliconFlow + sparse fastembed BM25).

**Аргументы:**

| Аргумент | Значение |
|---|---|
| `input_dir` (позиционный) | папка документа; авто-поиск ровно одного `*_chunks.jsonl` и `*_assets.json` |
| `--chunks` / `--assets` | альтернатива `input_dir` (взаимоисключающе) |
| `--collection` | имя коллекции; **без него при >1 коллекции интерактивный выбор** — веб обязан передавать явно |
| `--qdrant-path` | путь к локальному хранилищу; default `./qdrant_data`; **веб передаёт абсолютный** |
| `--strict` | прервать при ошибках валидации (веб использует) |
| `--providers_config` | default = `providers.yaml` рядом с модулем |
| `--batch-size` | default 16 |
| `--api-key` | переопределение ключа (иначе из `api_key_env` → `.env`) |

**Точный вызов (wizard, шаг 4, после `--rag`):**

```bash
python3 <Build_Search_index>/firmware/src/create_index.py \
  "<base_dir>/Markdown/<stem>/" \
  --qdrant-path "<abs qdrant_path>" \
  --collection "<collection>" \
  --strict
```

**Поведение:** перед индексацией удаляет старые точки документа с тем же `document_id`
(переиндексация безопасна). Создаёт коллекцию с dense (COSINE) + sparse векторами при отсутствии.

**Qdrant payload** (`build_payload()`): `chunk_id`, `document_id`, `document_type`, `domain`, `title`,
`status`, `chapter`, `section`, `clause`, `section_path`, `heading_texts`, `text`, `references`,
`assets` (резолвнутые, с `image_path`/`image_paths`), `chunk_tokens`, `row_index`.

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

**Slug-генерация** (`_reg_make_slug`, §5.1 rag-register-flag.md): `{PREFIX}[_{num}][_{word}]`,
префикс `ГОСТ→GOST, СП→SP, СО→SO, СНиП→SNIP, ПУЭ→PUE`, номер = первая группа цифр, хвост = транслит
первого слова domain; коллизия → суффикс `_2`, `_3`, …. Паттерн `^[A-Za-z0-9_]+$`.

**Валидация `validate_rag_config`**: `defaults.max_chunk_tokens` int 100..100000; `status` ∈
{active,inactive}; при `active` поля `status_reason`/`replaced_by_*` обязаны быть `null`.

### 2.4 `providers.yaml` (общая схема)

Оба пайплайна используют `firmware/src/providers.yaml` (реестр провайдеров + роли). Секреты не
хранятся — `api_key_env` содержит имя переменной `.env`.

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
  build_search_index:
    query_processing: {provider, model, fallback: {...}}
    embedding:        {provider, model, fallback: {...}}
    rerank:           {provider, model, fallback: {...}}
```

**Endpoint'ы по capability** (`llm_providers.CAPABILITY_ENDPOINT`): `chat → /chat/completions`,
`embedding → /embeddings`, `rerank → /rerank`. `vision` использует `chat`-endpoint с image-контентом.

**Разрешение ролей:** `resolve_subrole(cfg, pipeline, subrole)` (BSI) / `resolve_role` (CM) проверяет,
что провайдер и модель существуют, и возвращает плоскую структуру `{provider, model, api_key_env,
base_url, fallback}`. Неизвестный провайдер/модель/роль, дубль `api_key_env` — ошибка.

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

`firmware/src/qa_graph.py` — LangGraph-граф из 6 узлов
(`analyze_query → search → evaluate_results → reformulate_query/ask_clarification → generate_answer`).

**`QAGraphConfig` (dataclass):** `qdrant_path` (default `./qdrant_data`), `collection`
(default `technical_standard`), `retrieve_k=30`, `final_k=6`, `rrf_threshold=0.15`,
`search_config_path`, `providers_path`, `llm_provider`, `llm_model`, `llm_api_key`,
`score_good_threshold=0.7`, `score_medium_threshold=0.4`, `max_reformulate_attempts=2`.

**`QAGraph(config, checkpointer=None)`:**

| Метод | Вход/выход |
|---|---|
| `run(query) -> dict` | финальное состояние графа: `final_answer`, `cited_chunk_ids`, `search_results` (с `doc_dir`), `needs_clarification`, `error`; при уточнении — ключ `__interrupt__` (список `Interrupt`), `final_answer` отсутствует |
| `stream(query)` | генератор `{node_name: update}` (`stream_mode="updates"`); на interrupt чанк `{"__interrupt__": ...}` |
| `resume(user_response) -> dict` | возобновление после interrupt в том же `thread_id` |
| `resume_stream(user_response)` | стриминговое возобновление |
| `list_documents() -> list[{document_id, title}]` | distinct-метаданные из коллекции (scroll `with_payload=["document_id","title"]`, dedup по `document_id`) |

**Механика цитат:** `generate_answer` матчит ответ с `search_results` через `CITATION_MATCH_PATTERN`
(формат `[Документ, п. X.Y]` / `табл. N`) → `cited_chunk_ids`. `has_citations()` + однократная
перегенерация с доп. инструкцией при отсутствии цитат.

**Механика картинок (аналог telegram_bot):**
1. явная ссылка «Таблица N»/«Рисунок N» в query → точечный выбор;
2. иначе явная ссылка в answer;
3. иначе cited-поведение: asset'ы процитированных чанков по интенту («рисунок»/«таблица»);
4. неоднозначность/нет совпадения → картинки не показываются (fail closed).

Резолв пути картинки: `search_results[].doc_dir` + `assets[].image_path` (или `image_paths` для
многостраничных таблиц). `doc_dir` проставляется `_enrich_results_with_dirs()` из
`_load_assets(document_id)`.

**Жёсткая константа:** `BASE_MARKDOWN = "/mnt/sdb/!База_ГОСТ/Markdown"` (строка 333) — используется
для поиска `<dir>_assets.json` и резолва `doc_dir`. **Не изменяем.** На prod путь совпадает; на dev
картинки не резолвятся, пока оператор не даст доступ к этому пути (симлинк/монтирование) — см. §10.

**Concurrency-ограничение:** локальный Qdrant держит эксклюзивный файловый замок; `qa_graph` создаёт
`QdrantClient` на время одного `search_node` и закрывает в `finally`. Веб обязан придерживаться того же
паттерна (открыть→прочитать→закрыть) и НЕ держать клиент открытым, иначе `create_index.py` не сможет
открыть базу во время индексации.

### 2.7 Зависимости пайплайнов (для venv веба)

- Create_Markdown_YA: `httpx, pyyaml, pymupdf, beautifulsoup4, python-dotenv, transformers>=4.51.0`.
- Build_Search_index: `qdrant-client, fastembed, requests, python-dotenv, tqdm, langgraph>=1.2, pyyaml`.
- Плюс для веба: `fastapi, uvicorn[standard], python-multipart`.

Требуется Python ≥ 3.11 (README BSI).

---

## 3. Компоненты и размещение файлов

Код веба размещается в `firmware/src/` репозитория `interface_RAG` (конвенция «Source of Truth:
firmware/src/» из README; пайплайны-соседи используют `firmware/src/`). Статика — `firmware/src/static/`.
Тесты — `firmware/tests/`.

```
interface_RAG/
  config.yaml                     # конфиг деплоя (секции dev/prod, см. §8)
  requirements.txt                # веб-зависимости (см. §2.7)
  firmware/src/
    app.py                        # FastAPI: роуты, SSE, WebSocket, mount static
    deploy_config.py              # загрузка config.yaml (env-селекция dev/prod)
    jobs.py                       # очередь upload→convert→index; subprocess argv-списком; лог/стрим; стоп
    config_ui.py                  # чтение/валидация/запись YAML с .bak (providers, search_config,
                                  #   create_markdown_config, .env); маскирование ключей
    providers_api.py              # сканирование /v1/models; теги по имени; добавление провайдера
    registration.py               # вырезка 1-й страницы + vision-извлечение + slug + запись <stem>_reg.yaml
    llm_client.py                 # OpenAI-совместимый chat/vision клиент + резолв ролей (переиспользует providers.yaml)
    qdrant_api.py                 # read-only Qdrant: коллекции, distinct-документы (open/close на запрос)
    chat_api.py                   # импорт qa_graph; WebSocket-чат; цитаты + картинки; list documents
    static/
      index.html                  # одна страница, 3 вкладки
      app.js                      # vanilla JS (SSE-клиент, WS-клиент, wizard-состояние)
      style.css
  firmware/tests/                 # тесты веба
  docs/architecture/              # этот документ + implementation-plan.md
```

### 3.1 Модуль → ответственность → интерфейс

**`deploy_config.py`** — единственный источник путей/флагов.
- `load() -> DeployConfig` — читает `config.yaml`, секция выбирается ключом `active` или
  `INTERFACE_RAG_ENV` (приоритет env). Возвращает dataclass с полями §8.
- Никакие другие модули не читают пути напрямую.

**`jobs.py`** — запуск пайплайнов и жизненный цикл задачи.
- `class Job`: `id`, `doc_session_id`, `kind` (`convert`|`index`), `argv: list[str]`, `status`
  (`pending|running|done|error|stopped`), `exit_code`, `log_buffer`, `pid`.
- `class JobRunner`: `start(argv, cwd, env, kind) -> Job`; `stop(job_id)`; `get(job_id)`;
  `subscribe(job_id) -> queue.Queue` (для SSE). Запуск через `subprocess.Popen(argv, cwd=..., env=...,
  stdout=PIPE, stderr=STDOUT, text=True, bufsize=1)`; отдельный поток читает строки в `log_buffer` +
  пуш в очередь подписчиков. `stop()` = `SIGTERM` → grace 5s → `SIGKILL`.
- Ограничение: одновременно выполняется не более N задач (по умолчанию 1) — сериализация во
  избежание гонки за Qdrant-замок и файлы одного документа.

**`config_ui.py`** — безопасное редактирование YAML/.env.
- `read_yaml(path) -> dict` / `read_raw(path) -> str` (с маскированием ключей для UI).
- `write_yaml(path, data, comments_preserving=False)` — атомарно (temp + `os.replace`), перед записью
  `.bak` (копия текущего файла). `validate_*` функции для providers/search_config/reg-yaml.
- `read_env(path) -> dict[str,str]` (маска значений для UI); `write_env(path, dict)` атомарно с `.bak`.
- Никогда не отдаёт значения ключей наружу (только наличие/маску).

**`providers_api.py`** — «Добавить провайдера».
- `scan_models(base_url, api_key) -> list[str]` — `GET {base_url}/models` (OpenAI-совместимо).
- `tag_model(name) -> tag` — правила §4.2.
- `add_provider(providers_path, name, base_url, api_key_env, models) -> dict` — валидация уникальности
  `api_key_env`, запись через `config_ui.write_yaml`.

**`registration.py`** — регистрация документа (веб-эквивалент `--reg` без TTY).
- `extract_first_page(source_file) -> bytes` — PyMuPDF для PDF; для DOCX/DOC — libreoffice headless → PDF
  → стр.1; при неудаче возвращает `None` (UI уходит в ручной ввод).
- `vision_prefill(image_bytes) -> dict` — вызов vision-модели (роль `registration_vision`) с промптом
  извлечения полей; возвращает `{document_id, title, domain_hint, document_type}` (необязательно полные).
- `make_slug(document_id, document_type, domain, existing) -> str` — точная реплика `_reg_make_slug`.
- `write_reg_yaml(reg_path, fields) -> None` — рендер блока `documents.<slug>` (§6.3) + атомарная запись
  с `.bak` + гейт `yaml.safe_load` (как в пайплайне).

**`llm_client.py`** — вызовы LLM для веба (регистрация + «Тест» провайдера).
- `resolve_role(providers, pipeline, subrole) -> dict` — резолв как в пайплайне (reuse той же логики).
- `chat_completion(spec, messages, ...)` / `vision_completion(spec, image_bytes, prompt, ...) ->
  str` — OpenAI-совместимый `POST {base_url}/chat/completions` (vision: content = `[{type:"text"},
  {type:"image_url", image_url:{url:"data:image/png;base64,..."}}]`). `run_with_fallback`.

**`qdrant_api.py`** — read-only доступ к Qdrant (для «Показать документы в базе» и списка коллекций).
- `list_collections() -> list[str]`; `distinct_documents() -> list[{document_id, title, domain,
  document_type, status, count}]` — scroll с `with_payload=[...]`, dedup по `document_id`.
- Каждый вызов открывает `QdrantClient` и закрывает в `finally` (замок базы, §2.6).

**`chat_api.py`** — импорт `qa_graph` (как Telegram-бот).
- Добавляет `Build_Search_index/firmware/src` (и `.../telegram_bot`) в `sys.path`, импортирует
  `qa_graph.QAGraph/QAGraphConfig` и `telegram_bot.asset_helpers` (чистый stdlib) — read-only.
- Синглтон sparse-модели берётся из `qa_graph` (модульный кэш); QAGraph-инстанс — **на WebSocket-сессию**
  (изоляция `MemorySaver`/`thread_id`; дёшево, т.к. sparse-модель шарится).
- `answer(query) -> dict` и `resume(text) -> dict`; `select_images(search_results, query, answer,
  cited_chunk_ids) -> list[ImageRef]` — реплика приоритета бота с выдачей URL вместо путей.
- `list_documents()` — обёртка над `QAGraph.list_documents()` или `qdrant_api.distinct_documents()`.

**`app.py`** — маршруты (§7) + `mount("/static", StaticFiles(...))` + старт JobRunner/сессий.

---

## 4. Ключевые проектные решения

### 4.1 Роль vision-модели для регистрации — отдельная роль `registration_vision`

**Решение:** ввести **новую** роль `create_markdown.registration_vision` (`provider` + `model` +
`fallback`), НЕ переиспользовать `table_vision`.

**Обоснование:**
1. `table_vision` семантически заточена под «таблица → markdown» (у неё свой промпт и свой выбор
   модели `glm-4.5v`), а регистрация читает **титульную страницу** документа (обозначение + заголовок).
   Разные задачи, разные компромиссы «качество/цена/скорость».
2. Пользователь должен выбирать модель для регистрации независимо (можно поставить дешёвую/быструю
   vision-модель, не трогая качество распознавания таблиц). Требования §5.3 явно выделяют
   «регистрация» как отдельный селектор наравне с «ai постобработка» и «обработка запросов».
3. Добавление ключа в секцию `roles` — **аддитивное изменение конфига**: пайплайны читают только
   известные им роли (`table_vision`, `ai_postprocess`, `query_processing`, `embedding`, `rerank`) и
   не сломаются от неизвестного ключа.

**Ограничение:** модель в `registration_vision` обязана иметь тег `vision` (UI показывает в списке
регистрации только vision-модели, требования §5.3).

**Промпт регистрации** (vision, хранится в `config.yaml` веба, секция `prompts.registration_vision`):
«Извлеки из титульной страницы нормативного документа поля в JSON: document_id, title, domain_hint,
document_type. Если не определяется — null.» Аналог `reg_extract.prompt`, но на картинке.

**Fallback:** vision ничего не распознала / первая страница без текста / не-ГОСТ → UI переходит в
ручной ввод (обязательные поля помечены), веб всё равно пишет `<stem>_reg.yaml`.

### 4.2 Теги моделей «по имени» (правила матчинга)

При «Добавить провайдера» веб сканирует `/v1/models` и проставляет тег каждому имени модели.
Правила применяются **по порядку, первое совпадение побеждает** (substring, case-insensitive):

| Порядок | Тег | Правило (имя модели содержит) |
|---|---|---|
| 1 | `embedding` | `embedding`, `embed` |
| 2 | `rerank` | `rerank`, `reranker` |
| 3 | `vision` | `vision`, `-vl`, `vl-`, `4.5v`, `4.6v`, `4v`, `-v-flash`, `llava`, `gpt-4o`, `qwen.*-vl`, `gemini.*vision`, `claude.*vision` |
| 4 | `chat` | всё остальное (дефолт) |

Примеры (сверка с реальным `providers.yaml`): `Qwen/Qwen3-Embedding-8B`→embedding,
`Qwen/Qwen3-Reranker-8B`→rerank, `deepseek-v4-flash-vision-exp`→vision, `glm-4.5v`→vision,
`glm/glm-4.6v`→vision, `deepseek-v4-pro`/`glm-5.2`/`Qwen3-32B`/`google/gemini-2.5-flash`→chat.

Правила конфигурируемы (список в `config.yaml`); после скана пользователь может **вручную поменять**
тег любой модели перед записью (правила — подсказка, не догма).

### 4.3 «Один провайдер» для `ai_postprocess` + `query_processing` (синхронно)

UI показывает **одну** основную чат-модель (primary + fallback). При сохранении веб пишет выбранную
модель **в обе роли одновременно**: `roles.create_markdown.ai_postprocess` и
`roles.build_search_index.query_processing` (primary + fallback — одинаково). Реализуется в
`config_ui.py` (атомарная правка `providers.yaml` двух секций в одной записи). Это решение №6.

### 4.4 Wizard по шагам (gating на сервере, не только во фронте)

Состояние документа (`DocumentSession`) хранится в памяти `app.py` и содержит: `session_id`, исходное
имя файла, `stem`, `base_dir`, пути `reg_path`/`md_path`/`chunks_path`/`assets_path`, статусы шагов.
Gating выполняется на сервере проверкой **файлов на диске**:

| Шаг | Активен, если |
|---|---|
| 2 Регистрация | файл загружен (сессия существует) |
| 3 Конвертация | файл загружен; настройки провайдеров заданы (`providers.yaml` содержит нужные роли + ключи в `.env`) |
| 4 Индексация | `<stem>_reg.yaml` существует **и** `<stem>.md` существует; `qdrant.write_enabled == true` |

При невыполнении условия кнопка неактивна + сообщение «завершите предыдущие шаги». «Добавить в базу»
дополнительно требует `.md` И `<stem>_reg.yaml` (решение №4). Если `.md` уже есть — шаг 3 пропускается
(идти из готового Markdown; кнопка «Конвертировать» показана как «Пере-конвертировать (заново)»).

### 4.5 «Стоп» и облачная Yandex OCR

«Стоп» = `jobs.JobRunner.stop()` → `SIGTERM` локальному `subprocess`; через 5s — `SIGKILL`. Облачная
задача Yandex OCR, уже отправленная до `SIGTERM`, может завершиться на стороне облака — **принимается**
(решение №2); веб это не отменяет и не ждёт. Статус задачи становится `stopped`; производные файлы,
записанные до остановки, остаются (атомарность обеспечивает пайплайн).

### 4.6 Запись в Qdrant — только прод

`config.yaml` содержит `qdrant.write_enabled` (dev=false, prod=true) и `qdrant.path` (в dev — локальный
путь вне `/mnt/sdb`, по умолчанию отключён). Кнопка «Добавить в базу» активна только при
`write_enabled` (решение №3). Dev-инстанс физически не передаёт prod-путь (значения берутся из конфига
этой среды).

### 4.7 Формат и место конфига деплоя

Единый `interface_RAG/config.yaml` с секциями `dev:` и `prod:` и ключом `active: dev`. Выбор среды
переопределяется env `INTERFACE_RAG_ENV`. См. §8.

---

## 5. Поток «Добавить документ» (wizard)

Шаги 1–4. Каждый шаг — REST-вызов; прогресс шагов 3–4 — SSE-поток лога.

1. **Загрузка** (`POST /api/documents`): multipart-файл. Валидация: расширение ∈ {pdf, docx, doc, md},
   размер ≤ лимит конфига; имя файла = `basename` (path traversal отклоняется, допустимы кириллица/
   пробелы/`!`). Файл сохраняется в `<upload.base_dir>/<basename>`. Создаётся `DocumentSession`.
2. **Регистрация**:
   - `POST /api/documents/{sid}/register/prefill` — `registration.extract_first_page()` → PNG →
     `llm_client.vision_completion()` (роль `registration_vision`) → JSON-поля → ответ с подсказками
     (document_id, title, domain, document_type) и сгенерированным slug.
   - пользователь редактирует поля в форме.
   - `POST /api/documents/{sid}/register` — `registration.write_reg_yaml()` пишет
     `Markdown/<stem>/<stem>_reg.yaml` (с `.bak` + гейт `yaml.safe_load`). Повторная запись
     перезаписывает (last-writer-wins).
3. **Конвертация** (`POST /api/documents/{sid}/convert`): JobRunner запускает `create_markdown.py --ai`
   (§2.1). SSE транслирует лог. По завершении — ссылка на `.md` (вьювер: `GET /api/files/markdown/<stem>`).
   Если `.md` уже есть — шаг считается выполненным (без пере-конвертации).
4. **Индексация** (`POST /api/documents/{sid}/index`): сервер проверяет наличие `.md` и `_reg.yaml`,
   затем **две** под-задачи последовательно (одна job с двумя фазами):
   1. `create_markdown.py -i Markdown/<stem>/<stem>.md --rag` (§2.1) → `<stem>_chunks.jsonl` +
      `<stem>_assets.json`;
   2. `create_index.py Markdown/<stem>/ --qdrant-path <abs> --collection <name> --strict` (§2.2).

Кнопка «Стоп» (`POST /api/jobs/{id}/stop`) доступна во время выполнения шага 3/4.

---

## 6. Поток «Чат»

WebSocket `ws /ws/chat`. Жизненный цикл одного сообщения:

1. Клиент шлёт `{type:"query", text}`.
2. Сервер создаёт (или переиспользует) `QAGraph` сессии; запускает `qa.stream(text)` в фоновом потоке,
   шлёт события `{type:"node", node: ...}` по мере прохода узлов (прогресс).
3. На `__interrupt__` → `{type:"clarification", text}`; клиент шлёт `{type:"reply", text}` → сервер
   `qa.resume_stream(text)`.
4. По `final_answer` → `{type:"answer", answer, cited_chunk_ids, sources, images}`,
   где `images` = список `{url, caption, asset_type}`; `url` указывает на безопасный маршрут
   `GET /api/images/...` (§7). Выбор картинок — приоритет бота (§2.6) через
   `asset_helpers.extract_asset_references`/`resolve_images_to_send` (переиспользуем read-only).
5. Ошибка/пустой ответ → `{type:"error", text}`.

«Показать документы в базе» — `GET /api/documents-in-base` → `qdrant_api.distinct_documents()`
(или `QAGraph.list_documents()`), distinct по `document_id`.

История диалога — в памяти на сессию (как у бота, `MAX_HISTORY`), не персистится.

---

## 7. API-поверхность (маршруты FastAPI)

**Документы/wizard**
- `POST /api/documents` — загрузка файла (multipart) → `{session_id, stem}`.
- `GET  /api/documents/{sid}` — состояние сессии (наличие reg/md/chunks/assets, статусы шагов).
- `POST /api/documents/{sid}/register/prefill` — vision-подсказки полей.
- `GET  /api/documents/{sid}/reg` — текущие поля `_reg.yaml` (если есть).
- `POST /api/documents/{sid}/register` — запись `<stem>_reg.yaml`.
- `POST /api/documents/{sid}/convert` — запуск конвертации (job).
- `POST /api/documents/{sid}/index` — запуск индексации (job, 2 фазы).
- `GET  /api/jobs/{job_id}` — статус задачи.
- `GET  /api/jobs/{job_id}/events` — SSE-поток лога/прогресса.
- `POST /api/jobs/{job_id}/stop` — SIGTERM.

**Файлы (безопасная отдача)**
- `GET /api/files/markdown/{stem}` — отдать `<base_markdown>/<stem>/<stem>.md` (валидация `stem`:
  `^[A-Za-z0-9_.а-яА-ЯёЁ\- ]+$`, запрет `..`/`/`; `resolve()` → проверка префикса `base_markdown`).
- `GET /api/images/{doc_dir}/{rel_path:path}` — отдать картинку; `resolve()` и проверка, что путь внутри
  `base_markdown`; только расширения `.png/.jpg/.jpeg/.webp`.

**Чат**
- `WS  /ws/chat` — протокол §6.
- `GET /api/documents-in-base` — distinct-метаданные.

**Настройки**
- `GET/PUT /api/settings/providers` — реестр провайдеров + роли (значения ключей маскированы).
- `POST /api/settings/providers/scan` — `{base_url, api_key}` → список моделей с тегами.
- `POST /api/settings/providers/add` — добавить провайдера (имя, base_url, api_key_env, модели+теги).
- `GET/PUT /api/settings/search-config` — `search_config.yaml` (секция nodes).
- `GET/PUT /api/settings/create-markdown-config` — `create_markdown_config.yaml` (промпты/RAG-секции).
- `GET/PUT /api/settings/env` — ключи `.env` (маскированы; PUT пишет только переданные непустые значения;
  поддержка «удалить ключ»).
- `GET /api/settings/collections` — список коллекций Qdrant (read-only).
- `GET /api/settings/status` — сводка готовности (какие роли/ключи заданы).

---

## 8. Конфигурация деплоя

`interface_RAG/config.yaml`:

```yaml
active: dev                    # или INTERFACE_RAG_ENV=prod

dev:
  host: "127.0.0.1"
  port: 8081
  upload:
    base_dir: "/root/projects/interface_RAG/uploads"   # dev-корень (Markdown/ появится внутри)
    max_mb: 500
  pipelines:
    create_markdown_dir: "/root/projects/Create_Markdown_YA/firmware/src"
    build_search_index_dir: "/root/projects/Build_Search_index/firmware/src"
  qdrant:
    path: "/root/projects/interface_RAG/uploads/qdrant_data"   # dev-база (НЕ боевая)
    collection: "technical_standard"
    write_enabled: false        # dev не пишет в боевую базу
  env_file: "/root/projects/interface_RAG/.env"               # общий .env
  base_markdown: "/root/projects/interface_RAG/uploads/Markdown"

prod:
  host: "0.0.0.0"
  port: 80
  upload:
    base_dir: "/mnt/sdb/!База_ГОСТ"
    max_mb: 500
  pipelines:
    create_markdown_dir: "<LXC>/Create_Markdown_YA/firmware/src"
    build_search_index_dir: "<LXC>/Build_Search_index/firmware/src"
  qdrant:
    path: "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data"
    collection: "technical_standard"
    write_enabled: true
  env_file: "<LXC>/interface_RAG/.env"
  base_markdown: "/mnt/sdb/!База_ГОСТ/Markdown"

prompts:
  registration_vision: |
    Ты — библиограф нормативных документов. На изображении — титульная
    страница нормативного документа (ГОСТ/СП/СО/СНиП/ПУЭ).
    Извлеки строго в JSON без пояснений:
    {"document_id": "...", "title": "...", "document_type": "...", "domain_hint": "..."}
    Если поле не определяется — null. Только JSON.

model_tags:
  # правила §4.2 (порядок = приоритет)
  embedding: ["embedding", "embed"]
  rerank:    ["rerank", "reranker"]
  vision:    ["vision", "-vl", "vl-", "4.5v", "4.6v", "4v", "-v-flash", "llava", "gpt-4o"]
```

**Инъекция env в subprocess:** `jobs.py` строит `env = os.environ | read_env(env_file)` и передаёт в
`Popen(env=...)`. Так пайплайны гарантированно видят ключи независимо от cwd/`.env`-файла каждого
репозитория. Сам `.env`-файл (`env_file`) — единый, редактируется из «Настройки».

**Запуск dev:** `uvicorn app:app --host 127.0.0.1 --port 8081` из `firmware/src/` (venv из §2.7).
**Запуск prod:** systemd-юнит на LXC `192.0.2.21:80` (без авторизации), прод-значения в `config.yaml`.

---

## 9. Безопасность

1. **subprocess argv-списком, без shell.** Никогда `shell=True` и не конкатенировать команду в строку.
   Пути с `!`, кириллицей, пробелами безопасны (спецсимволы оболочки не интерпретируются).
2. **Path traversal:** имя файла = только `basename`; `stem`/`rel_path` валидируются regex + `resolve()`
   + проверка префикса `base_markdown`/`upload.base_dir` (см. §7).
3. **Безопасная отдача `image/` и `.md`:** только через маршруты §7 с проверкой реального пути внутри
   разрешённого каталога и белого списка расширений.
4. **Загрузка файлов:** валидация расширения и размера; тело читается в ограниченный буфер.
5. **Маскирование API-ключей:** значения из `.env`/`api_key_env` никогда не отдаются в UI (только
   наличие/маска `••••1234`). PUT `.env` принимает только новые/изменённые ключи.
6. **Атомарная запись:** все записи (YAML, `.env`, `.md` через пайплайн) — temp + `os.replace`; веб
   дополнительно делает `.bak` перед перезаписью конфигов.
7. **Без авторизации (принято):** прод на LAN `192.0.2.21:80` без auth — осознанное решение №8;
   риски смягчаются тем, что сервис не выходит в WAN.

---

## 10. Конкуренция, жизненный цикл, риски

- **Qdrant-замок:** `qdrant_api` и чат открывают/закрывают клиент на каждый запрос (не держат открытым),
  чтобы `create_index.py` мог открыть базу. Задачи `jobs` сериализуются (N=1) для одного документа.
- **QAGraph на сессию:** один инстанс на WebSocket (изоляция `thread_id`/`MemorySaver`); sparse-модель
  шарится через модульный синглтон `qa_graph`.
- **`BASE_MARKDOWN` захардкожен в `qa_graph.py`** (`/mnt/sdb/!База_ГОСТ/Markdown`). На prod совпадает;
  на dev картинки в чате не резолвятся без доступа к этому пути (симлинк/монтирование) — **зафиксировано,
  не блокирует** (ответ и цитаты работают).
- **Токенизатор HF** (`Qwen/Qwen3-Embedding-8B`, transformers) скачивается при первом `--rag`; на LXC
  должен быть доступ к HF или предзагруженный кэш — prerequisite деплоя.
- **DOCX/DOC для регистрации** требуют libreoffice (headless) для вырезки 1-й страницы; при отсутствии —
  fallback на ручной ввод.
- **Состояние сессий/задач в памяти** — не переживает рестарт процесса (принято для v1,
  однопользовательский LAN-инструмент). Логи задач пишутся в файл для аудита.
- **last-writer-wins** на `<stem>_reg.yaml` и конфигах — инструмент однопользовательский (как CLI).

---

## 11. Открытые вопросы / решения, ожидающие подтверждения

1. Точные prod-пути пайплайнов на LXC (`<LXC>/...`) заполнит Coder при деплое по факту размещения
   репозиториев на `192.0.2.21` (в конфиге — плейсхолдеры).
2. Имя коллекции `technical_standard` — default; если в базе иная коллекция, задаётся в «Настройках»
   (конфиг `qdrant.collection`).
3. Матчинг тегов (§4.2) — конфигурируем; правила можно уточнить после первого скана реальных моделей.

---

## 12. Трассируемость требований

| Требование (requirements.md) | Раздел архитектуры |
|---|---|
| Три раздела UI (§4–5) | §1, §3 (app.py/static), §6, §7 |
| Чат: импорт qa_graph, цитаты+картинки (§5.1, §7) | §2.6, §6 |
| Wizard по шагам, gating (§5.2, решение №4) | §4.4, §5 |
| Регистрация: 1-я страница → vision → веб пишет reg (§7) | §4.1, §5, registration.py |
| Добавить провайдера: /v1/models + теги по имени (решение №5) | §4.2, providers_api.py |
| Один провайдер для ai_postprocess+query_processing (решение №6) | §4.3 |
| Показать документы в базе — distinct из Qdrant (решение №7) | §2.6, §6, qdrant_api.py |
| Настройки: конфиги + ключи с маскированием (§5.3) | §3 config_ui.py, §7 |
| Пути не хардкодить; dev/prod конфиг (§7, §8) | §8 |
| Запись в Qdrant — только прод (решение №3) | §4.6, §8 |
| Стоп: SIGTERM, облачная OCR может не прерваться (решение №2) | §4.5 |
| Безопасность (argv без shell, traversal, маски, .env) | §9 |
