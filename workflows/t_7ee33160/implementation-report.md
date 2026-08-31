# Implementation report — t_7ee33160

## Фаза 1 (coder): единый общий конфиг + deploy-скрипт (чистый прод)

Дата: 2026-08-27. Источники истины: `docs/architecture/architecture.md` (§4.9, §4.10, §8),
`docs/architecture/implementation-plan.md` (этапы 1.1–1.5), `docs/product/requirements.md` §8,
решения 18–19 в `docs/product/decision-log.md`.

## Что сделано

### 1. Единый общий каталог конфигов
- `config.yaml` — в секции `dev:`/`prod:` добавлен `config_dir`
  (dev `/root/projects/interface_RAG/config`, prod `/root/RAG/config`);
  `env_file` переключён на `<config_dir>/.env`. Незакоммиченные правки пользователя
  (prompts/model_tags) сохранены, `git restore/checkout/stash` не выполнялись.
- `firmware/src/deploy_config.py` — поле `config_dir: Path` в `DeployConfig` + производные
  свойства `providers_path`, `create_markdown_config_path`, `search_config_path`;
  `config_dir` добавлен в список обязательных; `env_file` по умолчанию = `<config_dir>/.env`
  (инвариант §8.1).
- `firmware/src/config_ui.py` — `read_yaml` для отсутствующего файла возвращает `{}`
  (не 500 на первом старте); `write_env` создаёт родительский каталог
  (атомарность + `.bak` + маскирование сохранены).
- Создан dev-каталог `/root/projects/interface_RAG/config/`:
  `providers.yaml` (эталон «полный», с ролями `build_search_index`, sha
  `fe4ec2cd…` — совпадает с LXC), `create_markdown_config.yaml` (из Create_Markdown_YA),
  `search_config.yaml` (из Build_Search_index), `.env` (8 ключей, объединение
  dev-источников CMY → BSI, права 600).

### 2. Передача конфигов пайплайнам (только CLI-ключи + инъекция .env)
- `firmware/src/jobs.py` — добавлен `build_env(env_file)`:
  `os.environ | config_ui.read_env_raw(env_file)` — единая точка инъекции `.env`
  в окружение subprocess (§8.2).
- `firmware/src/app.py`:
  - удалены `_providers_path()`/`_search_providers_path()`/`_combined_providers()`/
    `_write_combined()`; настройки читают/пишут **один** `cfg.providers_path`
    (`GET/PUT /api/settings/providers`, `PUT …/providers/roles`, `POST …/providers/add`);
  - `convert()`: argv = `--config <config_dir>/create_markdown_config.yaml`,
    `--providers-config <config_dir>/providers.yaml` (§8.4);
  - `index_document()`: фаза 1 (`--rag`) — те же два ключа; фаза 2 — добавлен
    **`--providers_config <config_dir>/providers.yaml`** (underscore) к
    `--qdrant-path/--collection/--strict` (§8.4);
  - `registration_prefill()` — `providers_path=cfg.providers_path`;
  - `_config_path(kind)` → `cfg.search_config_path` / `cfg.create_markdown_config_path`;
  - `settings_status()` — по одному `providers.yaml`;
  - `POST /api/settings/providers/refresh` (этап 1.4) → `providers_api.refresh_all_models`;
  - env везде через `build_env(cfg.env_file)`.
- `firmware/src/chat_api.py` — `QAGraphConfig(providers_path=cfg.providers_path,
  search_config_path=cfg.search_config_path, …)` (каталог пайплайна не трогается).
- `firmware/src/providers_api.py` — `refresh_all_models(providers_path, env)`:
  `/v1/models` для каждого провайдера с ключом; ошибки по одному провайдеру не роняют
  остальных; возврат `{provider: {ok, models|error}}` (решение №11).

### 3. Deploy-скрипт `scripts/deploy.sh` (dry-run по умолчанию)
- `scripts/deploy.sh` (bash): режимы `--dry-run` (по умолчанию) и `--apply`.
  Константы: `LXC=192.0.2.21`, `LXC_ROOT=/root/RAG`, `CONFIG_DIR=/root/RAG/config`,
  `SERVICE=interface-rag.service`.
  Шаги:
  1. Проверка доступности LXC (dry-run терпит недоступность; `--apply` — нет).
  2. Бэкап: `cp -a` конфигов в `/root/RAG/config.bak.<ts>/{config,interface_RAG,CMY,BSI}`
     (включая `providers.yaml`/`.env` из каталогов пайплайнов, если существуют).
  3. Консолидация: `scripts/consolidate_config.py` — эталон `providers.yaml`
     (вариант с LXC, с ролями `build_search_index`) + долив отсутствующих
     провайдеров/моделей из варианта CMY; `.env` — объединение всех источников,
     приоритет значения LXC > dev, ключи не удаляются; `create_markdown_config.yaml`
     из CMY; `search_config.yaml` из BSI. В `--apply` — push в `/root/RAG/config/`.
  4. rsync-манифест (§8.3): interface_RAG (config.yaml + firmware/src/ + static/ +
     requirements.txt), Create_Markdown_YA (create_markdown.py), Build_Search_index
     (create_index/qa_graph/search/llm_providers/telegram_bot/asset_helpers.py);
     исключения `.git/docs/workflows/.hermes/tests/.pytest_cache/README/__pycache__/
     tmp/bot.log/request_bot.py/.env`.
  5. Симлинк `Build_Search_index/firmware/src/providers.yaml` → `/root/RAG/config/providers.yaml`
     (импорт-тайм BSI) + очистка устаревших копий конфигов и dev-артефактов (после бэкапа).
  6. Рестарт `systemctl restart interface-rag.service` + `is-active`.
  7. Smoke: `curl -sf http://127.0.0.1/` → ожидается HTTP 200 (в `--apply` проверяется).
- Требования на машине запуска: `sshpass`, `rsync`, `python3` + PyYAML
  (rsync был установлен на dev-машине).

## Файлы изменены

- `config.yaml` (M)
- `firmware/src/deploy_config.py` (M)
- `firmware/src/config_ui.py` (M)
- `firmware/src/jobs.py` (M)
- `firmware/src/providers_api.py` (M)
- `firmware/src/app.py` (M)
- `firmware/src/chat_api.py` (M)
- `firmware/tests/test_deploy_config.py` (M), `test_app.py` (M), `test_config_ui.py` (M),
  `test_jobs.py` (M), `test_providers_api.py` (M)
- `config/` (новое; dev-каталог конфигов: providers.yaml, create_markdown_config.yaml,
  search_config.yaml, .env)
- `scripts/deploy.sh`, `scripts/consolidate_config.py` (новые)
- `workflows/t_7ee33160/implementation-report.md` (этот отчёт)

Пайплайны (`Create_Markdown_YA`, `Build_Search_index`) не изменялись (только чтение).
`git restore/checkout/stash` не выполнялись.

## Тесты

`python3 -m pytest firmware/tests -q` → **30 passed** (было 19).

Добавлены/обновлены:
- `test_deploy_config.py` — `config_dir` обязателен; производные пути указывают на
  `<config_dir>/<имя>`; `env_file == config_dir/.env` (и уважает явное значение);
  `INTERFACE_RAG_ENV=prod`.
- `test_app.py` — argv convert/index содержат пути из `config_dir`
  (включая `--providers_config`), единый `providers.yaml` в settings,
  маршрут `/api/settings/providers/refresh`.
- `test_providers_api.py` — `refresh_all_models`: обновление моделей, изоляция ошибок,
  пропуск провайдера без ключа.
- `test_config_ui.py` — `read_yaml` отсутствующего файла; `write_env` создаёт родителя.
- `test_jobs.py` — `build_env`: файл имеет приоритет над окружением, остальное сохраняется.

## Валидация

- `python3 -m pytest firmware/tests -q` — зелёные (30 passed).
- Импорт приложения: `from firmware.src import app` — OK (34 маршрута);
  `deploy_config.load()` возвращает `config_dir`/`env_file`/производные пути.
- API smoke (TestClient): `GET /api/settings/providers` → 200, 5 провайдеров, обе ветки
  ролей; `GET /api/settings/env` → 200, ключи замаскированы; `GET /api/settings/search-config`
  и `create-markdown-config` → 200; `GET /api/settings/status` → роли заданы;
  `GET /` → 200.
- `bash -n scripts/deploy.sh`, `python3 -m py_compile scripts/consolidate_config.py`.
- rsync-строка проверена против LXC в режиме `-n` (read-only, ничего не записано).
- Read-only инвентаризация LXC (192.0.2.21): `/root/RAG/{interface_RAG,
  Create_Markdown_YA, Build_Search_index, venv}`; `/root/RAG/config` отсутствует;
  systemd `interface-rag.service` активен (WorkingDirectory=/root/RAG/interface_RAG,
  INTERFACE_RAG_ENV=prod); эталонный `providers.yaml` на LXC совпадает с dev BSI
  (sha `fe4ec2cd…`); `.env` LXC содержит 7 канонических ключей.

### Deploy dry-run

Команда: `cd /root/projects/interface_RAG && ./scripts/deploy.sh --dry-run`

Вывод (кратко):
```
Deploy interface_RAG → LXC 192.0.2.21 (dry-run)
== 0. Проверка доступности LXC ==   → LXC доступен
== 1. Бэкап конфигов прода ==       → [план] бэкап в /root/RAG/config.bak.<ts>
   (config/ + interface_RAG/.env + CMY/.env+providers.yaml + BSI/.env+providers.yaml)
== 2. Консолидация ==
   providers: 5 шт, roles: [build_search_index, create_markdown],
   источник эталона: remote/BSI/providers.yaml
   env keys (10): YANDEX_API_KEY, YANDEX_FOLDER_ID, DEEPSEEK_API_KEY, PROVOD_API_KEY,
   ANYMODEL_API_KEY, Z_AI_API_KEY, SILICONFLOW_API_KEY, TELEGRAM_BOT_TOKEN,
   QDRANT_PATH, TELEGRAM_ALLOWED_USERS
   env sources: remote/interface_RAG/.env, remote/CMY/.env, remote/BSI/.env,
   dev CMY/.env, dev BSI/.env
== 3. rsync-манифест ==             → 5 команд (интерфейс + оба пайплайна)
== 4. Симлинк + очистка ==          → [план] симлинк providers.yaml; очистка dev-артефактов
== 5. Рестарт systemd ==            → [план] systemctl restart interface-rag.service
== 6. Smoke ==                      → [план] curl http://127.0.0.1/ → ожидается 200
== Готово (dry-run) ==
   Для реального деплоя (после ревью): scripts/deploy.sh --apply
```
Реальный деплой НЕ выполнялся (его запускает оркестратор после ревью).

## Известные ограничения и решения

- Прод `.env` консолидируется как **объединение всех ключей** источников (10):
  8 канонических (§8.1) + `QDRANT_PATH` и `TELEGRAM_ALLOWED_USERS` из dev
  `Build_Search_index/.env`. Это соответствует «ключи не удаляются / без потери
  реальных ключей»; лишние ключи инертны для веба и пайплайнов (Qdrant-путь и
  параметры чата передаются CLI-ключами/QAGraphConfig).
- Роль `create_markdown.registration_vision` отсутствует в эталонном `providers.yaml`
  (новая аддитивная роль из Фазы 2) — `/api/settings/status` показывает её False.
- Deploy-скрипт использует sshpass (root/root, LAN, решение №8); пароль переопределяется
  переменной `LXC_PASS`. rsync установлен на dev-машине (был необходим).
- На LXC `/root/RAG/config` ещё не существует — скрипт создаёт его в `--apply`.

## Отклонения от архитектуры

Явных отклонений нет. Уточнения реализации:
- argv пайплайнов строится в `app.py` (этап 1.3 плана), инъекция env — единая функция
  `jobs.build_env` (§8.2); оба документа соблюдены.
- `env_file` в `config.yaml` задан явно как `<config_dir>/.env`; `deploy_config`
  дополнительно дефолтит его на `<config_dir>/.env`, если поле отсутствует.
