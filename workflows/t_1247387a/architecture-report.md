# Architecture report — t_1247387a

Ревизия архитектуры `interface_RAG` под реворк (единый общий конфиг + чистый прод + новый UI).
Обновлены `docs/architecture/architecture.md` и `docs/architecture/implementation-plan.md`.

## Что изменено в документах

1. **Единый общий конфиг** — новый раздел §4.9: общий каталог конфигов (dev
   `/root/projects/interface_RAG/config/`, prod `/root/RAG/config/`) с `providers.yaml`,
   `create_markdown_config.yaml`, `search_config.yaml`, `.env`; пайплайнам — только CLI-ключи,
   `.env` — инъекция в `env` subprocess.
2. **Чистый прод** — §4.10 + §8.3: rsync-манифест per-pipeline, исключения
   (`.git/docs/workflows/.hermes/tests/.pytest_cache/README/__pycache__/bot.log/request_bot.py`),
   схема `scripts/deploy.sh` (бэкап → консолидация → rsync → симлинк → рестарт → smoke, dry-run).
3. **Новый UI** — §4.8 + §5 + §7: шапка (title+favicon+подзаголовок), «Добавить документ» одной
   страницей (3 области, статичная форма со всеми полями `documents.<slug>`), MD-вьювер, настройки
   без raw-дампов с единой «Сохранить» + «Обновить», fallback по чекбоксу, пояснения ролей,
   параметры узлов графа.
4. **План реализации** разбит на Фазу 1 (конфиг + деплой, зад. t_7ee33160) и Фазу 2 (UI,
   зад. t_14fb20d5) с критериями готовности каждого этапа.

## Ключевые reverse-engineered факты (проверено по коду пайплайнов)

### 1. Механизм загрузки `.env` (→ инъекция env в subprocess достаточна)

- **`create_markdown.py`**: `main()` читает `.env` из `Path(__file__).parent/".env"`
  (`firmware/src/.env`), функция `load_env()` делает `os.environ.setdefault()` — **НЕ перезаписывает
  уже заданные переменные**. Вывод: инъекция `env=` в `Popen` имеет приоритет; отсутствие своей копии
  `.env` безвредно.
- **`create_index.py`**: `resolve_api_key()` → `load_dotenv()` (python-dotenv, `override=False`);
  `llm_providers.get_api_key()` → `load_dotenv()` + `os.environ.get()`. Инъекция env выигрывает; своя
  копия `.env` не нужна.
- **`qa_graph.py`**: ключи через `get_api_key` → тот же `load_dotenv()` (override=False).

### 2. Точные имена CLI-флагов

- `create_markdown.py`: `-i/--input`, `--ai`, `--json-native`, `--config` (default
  `./create_markdown_config.yaml`), `--providers-config` (**дефис**), `--rag`, `--reg`.
- `create_index.py`: позиционный `input_dir`, `--chunks`, `--assets`, `--collection`, `--qdrant-path`,
  `--providers_config` (**underscore**, default `firmware/src/providers.yaml`), `--batch-size`,
  `--api-key`, `--strict`.
- `qa_graph.py` (CLI): `--query`, `--interactive`, `--qdrant-path`, `--config` (search_config.yaml),
  `--providers_config` (**underscore**), `--llm-provider`, `--llm-model`, `--api-key`, `--verbose`.
- `qa_graph` (как библиотека): `QAGraphConfig(search_config_path=..., providers_path=...)`.
- `search_config.yaml` передаётся ТОЛЬКО через `search_config_path` (в `QAGraphConfig`) / `--config`
  (CLI qa_graph); у `create_index.py` отдельного флага для search_config нет (он его не использует).

### 3. ⚠️ Импорт-тайм нюанс (критично для «чистого» прода)

`Build_Search_index` грузит `providers.yaml` при импорте модулей:
- `create_index.py:53` — `_DEFAULT_PROVIDER_CFG = _get_providers()` (дефолтный путь
  `firmware/src/providers.yaml`) → при отсутствии/невалидности `ProviderConfigError` на импорте;
- `search.py:38` — то же; `qa_graph.py:64` импортирует `search` (каскад);
- `qa_graph.py:75` — `from telegram_bot.asset_helpers import ...` (нужен `telegram_bot/asset_helpers.py`).

`Create_Markdown_YA` (`create_markdown.py`) импорт-тайм конфигов не грузит — самодостаточен, всё через
`--config`/`--providers-config`.

**Разрешение:** на проде оставить **симлинк** `Build_Search_index/firmware/src/providers.yaml` →
`/root/RAG/config/providers.yaml` (один источник, не копия); веб всё равно передаёт явные
`--providers_config`/`providers_path` (переопределяют дефолт в `main()`).

### 4. Манифест файлов «чистого» прода

- **interface_RAG**: `config.yaml`, `firmware/src/*.py`, `firmware/src/static/`,
  `firmware/src/requirements.txt`.
- **Create_Markdown_YA**: `firmware/src/create_markdown.py` (конфиги — только CLI-ключи).
- **Build_Search_index**: `create_index.py`, `qa_graph.py`, `search.py`, `llm_providers.py`,
  `telegram_bot/asset_helpers.py`, `providers.yaml` (симлинк на общий конфиг).
- Исключить: `.git`, `docs`, `workflows`, `.hermes`, `tests`, `.pytest_cache`, `README*`,
  `__pycache__`, `*.pyc`, `tmp/`, `bot.log*`, `request_bot.py`, `.env` в каталогах пайплайнов.

### 5. Токенизатор

`create_markdown.py --rag` → `transformers.AutoTokenizer.from_pretrained("Qwen/Qwen3-Embedding-8B",
local_files_only=False)`; при `allow_degraded_fallback: false` отсутствие токенизатора/доступа к HF —
жёсткая ошибка. Кэш — стандартный HF-кэш (`~/.cache/huggingface` или `$HF_HOME`). Prerequisite деплоя.

### 6. Текущее состояние кода (для Coder)

- `app.py` уже имеет `_combined_providers()`/`_write_combined()` — **удалить**, заменить на один
  `providers.yaml` из `config_dir`.
- `index_document()` (фаза 2 индексации) **не передаёт** `--providers_config` — добавить.
- `chat_api.ChatSession` указывает `providers_path`/`search_config_path` на `build_search_index_dir/...`
  — переключить на `config_dir`.
- `deploy_config.DeployConfig` не имеет `config_dir` — добавить + производные свойства.
- UI: `index.html` содержит raw-дампы (`providers-view`/`env-view`), 5 кнопок «Сохранить», fallback без
  чекбокса; `settings.html` — отдельный фрагмент (устаревает). Реворк по §4.8.

## Ограничения соблюдены

- Пайплайны не изменялись (только чтение исходников).
- `docs/product/*` не редактировались.
- Production-код не писался — только архитектурные документы.
