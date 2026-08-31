# Review — t_3927ea7d (Фаза 1: единый конфиг + deploy-скрипт)

Дата: 2026-08-27. Ревьюер: reviewer (независимая проверка, не по отчёту coder'а).
Объект ревью: реализация coder t_7ee33160.

## Verdict

**PASS**

Все 10 пунктов проверки удовлетворены с фактическими доказательствами (код, прогоны,
read-only инвентаризация LXC). Блокирующих дефектов нет.

## Scope

Проверены: `config.yaml`, `firmware/src/{deploy_config,config_ui,jobs,providers_api,
app,chat_api}.py`, `config/` (dev-каталог), `scripts/{deploy.sh,consolidate_config.py}`,
`firmware/tests/*` (5 файлов). Сверка с `docs/architecture/architecture.md` (§4.9, §4.10,
§8) и `docs/architecture/implementation-plan.md` (этапы 1.1–1.5), решения 18–19 decision-log.

## Requirements / checklist

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 1 | Единый каталог конфигов (dev `.../interface_RAG/config`, prod `/root/RAG/config`) | PASS | config.yaml: `dev.config_dir`/`prod.config_dir`; `env_file` → `<config_dir>/.env` в обеих секциях. `config/` содержит providers.yaml, create_markdown_config.yaml, search_config.yaml, .env (8 канонических ключей, права 600) |
| 2 | deploy_config: `config_dir` + производные пути; env_file дефолт; config_dir обязателен | PASS | `DeployConfig.config_dir`, `providers_path`/`create_markdown_config_path`/`search_config_path` (property); `required` включает `config_dir`; `env_file = section.get("env_file") or config_dir/".env"` |
| 3 | app.py: единый providers.yaml; удалены `_combined_providers`/`_write_combined`; argv convert/index из config_dir; POST refresh; settings_status по одному файлу; .env через build_env | PASS | git diff подтверждает удаление обеих функций; convert → `--config`/`--providers-config` из `cfg.*_config_path`; index фаза 2 → `--providers_config` (underscore); `POST /api/settings/providers/refresh` → `refresh_all_models`; `settings_status` читает `cfg.providers_path`; env через `build_env(cfg.env_file)` |
| 4 | chat_api: QAGraphConfig из config_dir | PASS | `providers_path=str(cfg.providers_path)`, `search_config_path=str(cfg.search_config_path)` |
| 5 | jobs.build_env: os.environ \| read_env_raw, файл приоритет | PASS | `env = dict(os.environ); env.update(read_env_raw(env_file))`; test_jobs подтверждает приоритет файла над окружением |
| 6 | config_ui: read_yaml отсутствующего → {}; write_env создаёт родителя; атомарность+.bak+маски | PASS | `read_yaml` вернул `{}` при отсутствии; `write_yaml`/`write_env` делают `parent.mkdir(parents=True)`; `write_env` сохранил mkstemp+`os.replace`+.bak |
| 7 | providers_api.refresh_all_models: изоляция ошибок, `{provider:{ok,models\|error}}` | PASS | try/except на провайдера; возврат `{ok, models|error}`; test_providers_api покрывает изоляцию и пропуск без ключа |
| 8 | deploy.sh: dry-run по умолчанию, --apply; шаги бэкап→консолидация→rsync→симлинк→очистка→рестарт→smoke; consolidate без вывода секретов | PASS | dry-run прогон (см. Tests) печатает корректный план; consolidate выводит только имена ключей и счётчики провайдеров, значений нет |
| 9 | Тесты 30 passed | PASS | `python3 -m pytest firmware/tests -q` → 30 passed (1.45s) |
| 10 | Ограничения: пайплайны не тронуты; git restore/checkout/stash не выполнялись; реальный деплой не выполнялся | PASS | `git diff --stat` — только файлы interface_RAG (пайплайны не в списке); `git status` — незакоммиченные правки config.yaml сохранены, нет признаков restore; deploy только dry-run |

## Architecture compliance

- Единый каталог конфигов и передача пайплайнам только CLI-ключами + инъекция `.env` (§4.9, §8.4) — соблюдено.
- argv точные (§8.4): convert/`--rag` → `--providers-config` (дефис, CMY); `create_index.py` → `--providers_config` (underscore). Сверено с фактическими argparse пайплайнов:
  - `create_markdown.py:6377` — `--providers-config` (дефис) ✓
  - `create_index.py:425` — `--providers_config` (underscore) ✓
  - `qa_graph.py:1719` — `--providers_config` (underscore) ✓
- rsync-манифест (§8.3) — состав файлов совпадает (interface_RAG + create_markdown.py + create_index/qa_graph/search/llm_providers/asset_helpers), исключения корректны (`.git docs workflows .hermes tests .pytest_cache README* __pycache__ *.pyc tmp bot.log* request_bot.py .env`).
- Импорт-тайм нюанс (§4.9) — симлинк `Build_Search_index/firmware/src/providers.yaml → /root/RAG/config/providers.yaml` создаётся в шаге 4. `DEFAULT_PROVIDERS_PATH` в `llm_providers.py:13` = `Path(__file__).parent/"providers.yaml"` — подтверждён симлинк-зависимый импорт-тайм `_get_providers()`.

## Tests executed (реально выполнены ревьюером)

- `python3 -m pytest firmware/tests -q` → **30 passed**, 1 warning (StarletteDeprecation, не релевантно).
- `bash -n scripts/deploy.sh` → OK.
- `python3 -m py_compile scripts/consolidate_config.py` → OK.
- `python3 -c "from firmware.src import app; print(len(app.app.routes))"` → **34 маршрута**.
- `./scripts/deploy.sh --dry-run` → OK, план корректен: бэкап → консолидация (5 providers, роли `[build_search_index, create_markdown]`, эталон `remote/BSI/providers.yaml`, 10 env-ключей, источники перечислены, значений нет) → rsync-манифест (5 команд) → симлинк + очистка → рестарт → smoke 200.
- sha256 dev `config/providers.yaml` == LXC `Build_Search_index/firmware/src/providers.yaml` == `fe4ec2cd…` (эталон совпадает с продом).
- Read-only grep LXC: `interface_RAG/.env` = 7 ключей (без TELEGRAM_BOT_TOKEN, как заявлено); `QDRANT_PATH`/`TELEGRAM_ALLOWED_USERS` читаются только `request_bot.py`, который исключён из манифеста.

## Findings (неблокирующие)

1. **LOW — `start_sequence` использует один `cwd` для обеих фаз.** `index_document()` передаёт `cwd=create_markdown_dir` на обе фазы; фаза 2 (`create_index.py`) по §8.4 должна выполняться с `cwd=build_search_index_dir`. Функционально безвредно: все пути абсолютные (`--qdrant-path` через `.resolve()`, `--providers_config` абсолютный, `input_dir` абсолютный), а импорт-тайм `DEFAULT_PROVIDERS_PATH` резолвится от `__file__`, не от cwd. Поведение предсуществующее (не введено Фазой 1). Рекомендация (опционально, Фаза 2+): расширить `start_sequence` до per-phase cwd.
2. **LOW — прод `.env` будет содержать 10 ключей, а не ровно 8.** Консолидация объединяет ключи всех источников: 8 канонических + `QDRANT_PATH` + `TELEGRAM_ALLOWED_USERS` (из dev `Build_Search_index/.env`). Оба ключа читает только `request_bot.py` (исключён из деплоя) — инертны для веба и пайплайнов. Соответствует принципу «ключи не удаляются» (§4.10); задокументировано в отчёте.
3. **LOW — `add_provider` не пишет значение ключа в `.env`.** `ProviderAdd` содержит `api_key_env` (имя переменной), но не значение ключа; фактический ключ добавляется через настройки env. Соответствует согласованному контракту API (ранее ревью веб-ui); ввод ключа — часть модалки «Добавить провайдера» Фазы 2.

## Required changes

Нет (PASS). Три находки — неблокирующие наблюдения; см. Risks.

## Risks

- Реальный деплой (`--apply`) НЕ выполнялся (по ограничению задачи) — план dry-run корректен, но фактический rsync/симлинк/рестарт на LXC не проверялись вживую. Оркестратор запускает `--apply` после ревью.
- `QDRANT_PATH` в консолидированном `.env` несёт dev-значение; безвредно, т.к. читается только исключённым `request_bot.py`, но при будущем деплое бота на прод его стоит переопределить.
- `registration_vision` отсутствует в providers.yaml (аддитивная роль Фазы 2) — `/api/settings/status` честно показывает её False.

## Notes

- Пайплайны (`Create_Markdown_YA`, `Build_Search_index`) не изменялись — только чтение (подтверждено `git diff --stat`).
- `git restore/checkout/stash` не выполнялись; незакоммиченные правки `config.yaml` (prompts/model_tags) сохранены.
- Ревьюер не редактировал производственный код.
