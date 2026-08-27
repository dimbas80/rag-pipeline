# interface_RAG — План реализации (для Coder)

> Сопутствующий документ: `docs/architecture/architecture.md` (обязательно прочитать перед началом).
> Эта версия плана описывает **реворк** (единый общий конфиг + чистый прод + новый UI) поверх уже
> работающего web-ui. Фаза 1 (конфиг + деплой) и Фаза 2 (UI) — отдельные задачи Coder'а; порядок
> этапов ниже даёт проверяемый результат на каждом шаге.

---

## Конвенции (обновлены под реворк)

- Код — `firmware/src/`, тесты — `firmware/tests/`, статика — `firmware/src/static/`.
- Python ≥ 3.11, venv в корне. Зависимости — `firmware/src/requirements.txt` (список §2.7 архитектуры).
- **Все пути конфигов/пайплайнов — только из `deploy_config.DeployConfig`**; новый источник —
  `config_dir` (общий каталог конфигов). Никаких хардкодов.
- Каждый запуск пайплайна — `subprocess.Popen(argv: list[str], shell=False)`; env — инъекция
  `os.environ | read_env_raw(config_dir/".env")` (§8.2 архитектуры).
- Конфиги пишутся атомарно (temp + `os.replace`) + `.bak`; ключи `.env` маскируются в UI.
- Логи/комментарии — русский; type hints; `snake_case`.

---

## Фаза 1 (зад. t_7ee33160) — единый общий конфиг + deploy-скрипт

### Этап 1.1 — `config_dir` в конфиге и `deploy_config`

**Файлы:**
- `config.yaml` — добавить `config_dir` в секции `dev:` (`/root/projects/interface_RAG/config`) и
  `prod:` (`/root/RAG/config`); `env_file` переключить на `<config_dir>/.env` (§8.1 архитектуры).
- `firmware/src/deploy_config.py` — добавить поле `config_dir: Path` в `DeployConfig` и производные
  свойства `providers_path` / `create_markdown_config_path` / `search_config_path`; в `load()` добавить
  `config_dir` в список обязательных.
- `firmware/tests/test_deploy_config.py` — дополнить: `config_dir` обязателен; производные пути
  указывают на `<config_dir>/<имя>`; `INTERFACE_RAG_ENV=prod` даёт prod-секцию.

**Критерии:** `deploy_config.load()` возвращает `config_dir`; `env_file == config_dir/".env"`;
тесты зелёные.

### Этап 1.2 — консолидация чтения/записи в один `providers.yaml`

**Файлы:**
- `firmware/src/app.py` — **удалить** `_providers_path()`/`_search_providers_path()`/
  `_combined_providers()`/`_write_combined()`; заменить на единый файл `cfg.providers_path`
  (= `<config_dir>/providers.yaml`):
  - `GET/PUT /api/settings/providers` — читать/писать один файл (валидация `validate_providers`).
  - `PUT /api/settings/providers/roles` — `sync_role_models` по одному файлу.
  - `POST /api/settings/providers/add` — `add_provider(cfg.providers_path, ...)` + запись
    `api_key_env` в `.env`.
  - `_config_path(kind)` → `cfg.search_config_path` / `cfg.create_markdown_config_path`.
  - `registration.vision_prefill` — `providers_path=cfg.providers_path`.
  - `GET /api/settings/env` / `PUT` — `cfg.env_file` (уже так, путь сменился на `<config_dir>/.env`).
- `firmware/src/config_ui.py` — без изменений API (уже атомарно + `.bak` + маски); убедиться, что
  `write_yaml`/`write_env` корректно пишут в несуществующий `config_dir` (создаёт родителя).

**Критерии:** настройки читают/пишут один `providers.yaml` в `config_dir`; при сохранении роли
(`sync_role_models`) в файле появляется только одна секция `roles` с обеими ветками
`create_markdown` и `build_search_index`.

### Этап 1.3 — argv пайплайнов на общий конфиг + явный `--providers_config` в индексации

**Файлы:**
- `firmware/src/app.py`:
  - `convert()`: `--config <config_dir>/create_markdown_config.yaml`,
    `--providers-config <config_dir>/providers.yaml` (§8.4).
  - `index_document()`: фаза 1 (`--rag`) с теми же двумя ключами; фаза 2 (`create_index.py`) —
    **добавить** `--providers_config <config_dir>/providers.yaml` (underscore) к текущим
    `--qdrant-path/--collection/--strict` (§8.4). Сейчас этот флаг в индексации отсутствует —
    обязателен для единого конфига.
- `firmware/src/chat_api.py` — `QAGraphConfig(providers_path=cfg.providers_path,
  search_config_path=cfg.search_config_path, ...)` (сейчас указывает на `build_search_index_dir/...`).

**Критерии:** argv содержит точные пути из §8.4; `chat_api` не трогает каталог пайплайна для
конфигов. Тест: мок `runner.start`/`start_sequence`, assert на состав argv (пути из `config_dir`).

### Этап 1.4 — кнопка «Обновить» (refresh моделей)

**Файлы:**
- `firmware/src/providers_api.py` — `refresh_all_models(providers_path, env) -> dict`: для каждого
  провайдера с ключом из `env` вызвать `scan_models`; обновить `models` (теги по имени); ошибки по
  одному провайдеру не роняют остальные (возврат `{provider: {ok, models|error}}`).
- `firmware/src/app.py` — `POST /api/settings/providers/refresh` (вызывает `refresh_all_models` с
  `read_env_raw(cfg.env_file)`).
- `firmware/tests/test_providers_api.py` — мок `scan_models`; проверка обновления моделей и изоляции
  ошибок.

**Критерии:** refresh обновляет список моделей без потери провайдеров/ролей; частичная ошибка не
прерывает остальных.

### Этап 1.5 — deploy-скрипт `scripts/deploy.sh` (dry-run обязателен)

**Файлы:** `scripts/deploy.sh` (bash). Флаг `--dry-run` (по умолчанию). Константы: `LXC=192.0.2.21`,
`LXC_ROOT=/root/RAG`, `CONFIG_DIR=/root/RAG/config`.

Порядок (см. §4.10 архитектуры):
1. `--dry-run` → печатать план без изменений (что будет забекаплено/скопировано/симлинкнуто).
2. Бэкап: `ssh root@$LXC "cp -a $CONFIG_DIR $CONFIG_DIR.bak.$(date +%s)"` (и копии `providers.yaml`/
   `.env` из каталогов пайплайнов, если ещё существуют).
3. Консолидация в `$CONFIG_DIR/`: эталон `providers.yaml` — «полный» вариант (Build_Search_index, с
   ролями `build_search_index`); долить провайдеров/модели из варианта Create_Markdown_YA; `.env` —
   собрать все 8 ключей БЕЗ затирания реальных значений (порядок приоритета: значение с LXC >
   значение с dev, ключи не удаляются).
4. rsync манифеста (§8.3) → `/root/RAG/{interface_RAG,Create_Markdown_YA,Build_Search_index}/`
   (исключая `.git docs workflows .hermes tests .pytest_cache README* __pycache__ tmp bot.log* request_bot.py .env`).
5. Симлинк: `ssh root@$LXC "ln -sfn $CONFIG_DIR/providers.yaml $LXC_ROOT/Build_Search_index/firmware/src/providers.yaml"`.
6. Рестарт: `ssh root@$LXC "systemctl restart interface-rag.service"`.
7. Smoke: `ssh root@$LXC "curl -sf http://127.0.0.1/ -o /dev/null -w '%{http_code}'"` → ожидаем `200`.

**Критерии приёмки фазы 1:**
- `python3 -m pytest firmware/tests -q` — зелёные (обновлены/добавлены под `config_dir`).
- `scripts/deploy.sh --dry-run` отрабатывает без ошибок и печатает корректный план (бэкап +
  консолидация + rsync-манифест + симлинк + рестарт + smoke). Реальный деплой НЕ выполняется
  (его запустит оркестратор после ревью).
- НЕ выполнять `git restore/checkout/stash`; не трогать пайплайны.

---

## Фаза 2 (зад. t_14fb20d5) — реворк UI

### Этап 2.1 — шапка и оформление

**Файлы:** `firmware/src/static/index.html`, `static/style.css`, `static/favicon.svg`.
- `<title>` с понятным названием (напр. «Оркестратор ГОСТ-базы: OCR→Markdown и семантический поиск»);
  `<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">`; заголовок + подзаголовок на
  странице (§4.8). Единый стиль трёх вкладок (шрифты/отступы/цвета).

### Этап 2.2 — «Добавить документ» одной страницей (3 области)

**Файлы:** `static/index.html`, `static/app.js`, `static/style.css`, `firmware/src/registration.py`
(если нужен экспорт `FIELDS` для рендера формы).
- Три визуальные области: **Загрузка / Регистрация (форма) / Лог и прогресс** (НЕ пошаговые модалки).
- Форма регистрации — статичная, ВСЕ поля `documents.<slug>` (`registration.FIELDS`, §2.3):
  русское название + комментарий (назначение + пример, tooltip), обязательные помечены `*`
  (`_REG_COMPLETENESS_FIELDS`), placeholder = имя поля. Vision-prefill заполняет определённые поля
  (редактируемы). Кнопка «Зарегистрировать документ» внизу области.
- Gating шагов сохраняется (кнопки следующих шагов активны после предыдущего, §4.4).
- MD-вьювер: после конвертации — кнопка/ссылка `GET /api/files/markdown/<stem>` (просмотр/скачивание).

### Этап 2.3 — Настройки (без сырых дампов, единая «Сохранить»)

**Файлы:** `static/index.html`, `static/app.js`, `static/style.css`; backend — из фазы 1.
- Убрать `providers-view`/`env-view` (raw `<pre>` дампы) — только структурированные поля (ключи
  маскированы).
- Вверху — единая **«Сохранить»** (собирает все секции в один батч) и **«Обновить»**
  (`POST /api/settings/providers/refresh`).
- Секции chat/vision/embedding/rerank: подпись «Основная модель» + чекбокс «Fallback» (настройки
  fallback видны только при включённом чекбоксе) + пояснение роли (tooltip/подпись); в списках
  vision/embedding/rerank — только модели с соответствующим тегом.
- Секция «Параметры графа» (`search_config.yaml`: analyze_query, reformulate_query,
  ask_clarification, generate_answer) с комментариями назначения.
- Yandex OCR — отдельная секция (ключи). «Добавить провайдера» — модалка (имя/api key/base_url;
  «Тест»/«Добавить»/«Отмена»).
- Синхронные роли сохраняются (§4.1/§4.3).

**Критерии приёмки фазы 2:**
- `python3 -m pytest firmware/tests -q` — зелёные.
- Локальный запуск `INTERFACE_RAG_ENV=dev` (порт 8081): три вкладки открываются; настройки
  сохраняются одной кнопкой; fallback по чекбоксу; параметры графа редактируются; MD-вьювер отдаёт `.md`.
- В интерфейсе нет raw-дампов конфигов; ключи маскированы.

---

## Итоговые критерии приёмки (весь реворк)

- [ ] Один общий каталог конфигов (dev `.../interface_RAG/config`, prod `/root/RAG/config`):
  `providers.yaml`, `create_markdown_config.yaml`, `search_config.yaml`, `.env`.
- [ ] Пайплайнам конфиг передаётся только CLI-ключами (`--config`, `--providers-config`,
  `--providers_config`); `.env` — инъекция в `env` subprocess.
- [ ] `create_index.py` получает явный `--providers_config <config_dir>/providers.yaml`.
- [ ] `chat_api` использует `providers_path`/`search_config_path` из `config_dir`.
- [ ] Пайплайны не изменены (`git status` чистый относительно старта); симлинк на проде создаётся
  deploy-скриптом, не вручную.
- [ ] Deploy-скрипт имеет `--dry-run`; бэкап + консолидация + rsync-манифест + симлинк + рестарт + smoke.
- [ ] UI: шапка (title+favicon+подзаголовок), «Добавить документ» одной страницей (3 области, статичная
  форма), MD-вьювер, настройки без raw-дампов с единой «Сохранить» + «Обновить», fallback по чекбоксу,
  пояснения ролей, параметры графа.
- [ ] `python3 -m pytest firmware/tests -q` — зелёный (интеграционные — отдельным прогоном).

---

## Замечания и ловушки для Coder

1. **Имя RAG-файлов**: пайплайн пишет `<stem>_chunks.jsonl` / `<stem>_assets.json` (НЕ `rag_chunks.jsonl`).
2. **`--reg` (TTY) не использовать** — веб пишет `<stem>_reg.yaml` сам (`registration.py`).
3. **`--collection`/`--qdrant-path`/`--providers_config` для `create_index.py` передавать явно**;
   `--providers_config` — через underscore (в `create_markdown.py` — дефис: `--providers-config`).
4. **Импорт-тайм `providers.yaml`**: `create_index.py:53` и `search.py:38` грузят `firmware/src/providers.yaml`
   при импорте → на проде нужен симлинк на `<config_dir>/providers.yaml` (иначе падение при старте).
5. **Qdrant-замок**: не держать `QdrantClient` открытым между запросами.
6. **QAGraph на WebSocket-сессию**, не глобальный синглтон.
7. **`BASE_MARKDOWN`** в `qa_graph.py` захардкожен — не менять.
8. **Токенизатор HF** скачивается при первом `--rag`; на LXC нужен доступ к HF или предзагруженный кэш
   (`HF_HOME`).
9. **Vision-роль** `create_markdown.registration_vision` — новая, аддитивная; пайплайны её игнорируют.
10. **Не `git restore/checkout/stash` конфигов** — на dev есть незакоммиченные правки пользователя
    (`config.yaml` модифицирован).
11. **Дефис vs underscore в флагах**: `create_markdown.py` — `--providers-config`; `create_index.py` и
    `qa_graph.py` — `--providers_config`.
