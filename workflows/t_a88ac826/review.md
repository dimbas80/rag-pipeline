# Review — реворк interface_RAG (единый конфиг + UI)

## Verdict

**PASS**

## Scope

Независимое ревью полного реворка interface_RAG (Фаза 1 — единый конфиг + deploy-скрипт,
Фаза 2 — реворк UI) против источников истины:
- `docs/product/requirements.md` (§5.0, §5.2, §5.3, §8)
- `docs/product/decision-log.md` (решения 10–19)
- `docs/architecture/architecture.md` и `docs/architecture/implementation-plan.md`

Проверка выполнялась по коду + живому запуску (НЕ по отчётам coder'а). Сервер запущен
`INTERFACE_RAG_ENV=dev` на порту 8099 (8081 занят SearXNG — зафиксировано в handoff родителя
t_14fb20d5; config.yaml не менялся).

## Requirements (decision-log 10–19)

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 10 | Регистрация — отдельная область, статичная форма, комментарии, кнопка | PASS | index.html: 3 области (`Загрузка`/`Регистрация`/`Конвертация`); 15 полей `documents.<slug>`, у каждого русское название + tooltip-комментарий + placeholder=имя поля; обязательные помечены `*`; кнопка «Зарегистрировать документ». Проверено в браузере + end-to-end upload/register (slug `GOST_7777_test`, `_reg.yaml` с 15 полями) |
| 11 | Единая «Сохранить» + «Обновить» | PASS | settings.js: одна кнопка «Сохранить» (батч roles+search-config+env), «Обновить модели» → `POST /api/settings/providers/refresh`. Живой refresh сделал реальные запросы к 5 провайдерам: deepseek(3), provod(46), anymodel(82), siliconflow(76) ok; zai-custom timeout — изолирован, остальных не уронил |
| 12 | Fallback по чекбоксу | PASS | 4 чекбокса «Fallback» (по секции), поля fallback `hidden` до включения чекбокса. Проверено в DOM |
| 13 | Пояснения ролей | PASS | У каждой секции chat/vision/embedding/rerank комментарий назначения + tooltip `?` |
| 14 | Параметры узлов графа + комментарии | PASS | 4 узла (analyze_query, reformulate_query, ask_clarification, generate_answer) с комментариями; правятся через `PUT /api/settings/search-config` |
| 15 | Без сырых конфигов, ключи маскированы | PASS | Нет `providers-view`/`env-view`; `GET /api/settings/env` отдаёт только `••••<last4>`; PUT игнорирует значения-маски |
| 16 | Шапка: favicon + title + подзаголовок | PASS | `<title>Оркестратор ГОСТ-базы…`, `<link rel="icon">` на favicon.svg (HTTP 200), h1 + subtitle |
| 17 | MD-вьювер | PASS | «Просмотр .md» + «Скачать .md»; `GET /api/files/markdown/<stem>` → 200 text/markdown; inline-рендер заголовков/таблиц/цитат/изображений |
| 18 | Прод — чистая структура | PASS | deploy.sh rsync-манифест исключает `.git/docs/workflows/.hermes/tests/.pytest_cache/README/__pycache__/bot.log/request_bot.py/.env`; очистка старых копий после бэкапа |
| 19 | Единый общий конфиг через CLI-ключи | PASS | Интерфейс пишет только в `config_dir`; convert/index передают `--config`/`--providers-config`/`--providers_config`; `.env` инжектится через `build_env`; на проде симлинк BSI providers.yaml |

## Unified config (проверка №2)

- `deploy_config.py`: производные `providers_path`/`create_markdown_config_path`/`search_config_path` = `<config_dir>/…`; `env_file` всегда `<config_dir>/.env` (инвариант в `load()`).
- `app.py`: удалены `_providers_path`/`_combined_providers`/`_write_combined` — только `cfg.providers_path`. Проверено grep — следов старых комбинированных путей нет.
- **CLI-флаги сверены с исходниками пайплайнов**:
  - `create_markdown.py:6371-6380` — `--config`, `--providers-config` (дефис). Веб передаёт корректно.
  - `create_index.py:425` — `--providers_config` (underscore). Веб передаёт корректно (+ `--qdrant-path`/`--collection`/`--strict`).
- Пайплайны НЕ изменены: `git status` в `Create_Markdown_YA` и `Build_Search_index` чистый.

## Security (проверка №3)

- Ключи `.env` в UI/API только маска `••••<last4>` — живьём подтверждено (все 8 ключей маскированы).
- Защита от затирания маской: `PUT /api/settings/env` отбрасывает значения `••••…` (app.py:294); тест `test_put_env_ignores_masked_values` это покрывает.
- deploy.sh: бэкап `cp -a` конфигов прода перед изменением; консолидация `.env` объединяет ключи без удаления, приоритет LXC > dev (consolidate_config.py `merge_env` — `setdefault`, первый источник выигрывает). Dry-run подтвердил: 10 ключей сохранены, ничего не затёрто.

## Tests (проверка №4)

`python3 -m pytest firmware/tests -q` → **33 passed** (1 warning Starlette deprecation, не релевантно).
Тесты осмысленные (assert на поведение, не снапшоты): argv-состав конвертации/индексации с путями из `config_dir`, env-инъекция с приоритетом файла, маска-гейт при записи env, изоляция ошибок refresh, sync-роли chat/vision, slug-транслит/коллизии, traversal-reject.

## UI live (проверка №5)

Браузер (http://127.0.0.1:8099/):
- Три вкладки: Чат / Добавить документ / Настройки — открываются.
- Шапка: title + favicon + h1 + подзаголовок.
- «Добавить документ»: 3 области; форма регистрации из 15 полей с комментариями и `*`; «Зарегистрировать документ» (disabled до загрузки — корректный gating); convert/index disabled; MD-ссылки hidden до появления `.md`.
- Настройки: единая «Сохранить», «Обновить модели», «Добавить провайдера» (модалка: имя/base_url/key + Тест/Добавить/Отмена); 4 секции ролей с «Основная модель» + чекбокс «Fallback» + пояснение; vision/embedding/rerank — только модели с нужным тегом (vision=10, embedding=3, rerank=2); «Параметры графа» — 4 узла с комментариями; env-ключи маскированы; бейджи env/write_enabled.
- End-to-end: upload `.md` → register → slug `GOST_7777_test` → `_reg.yaml` (15 полей, source_file авто) → MD-вьювер 200.
- Path traversal: `/api/files/markdown/..%2F..%2Fetc%2Fpasswd` → 404; `/api/images/..%2F..%2F…` → 404.

## Deploy

`bash -n scripts/deploy.sh` OK. `scripts/deploy.sh` (dry-run) отрабатывает без ошибок:
проверка LXC → бэкап → консолидация (5 providers, roles обеих веток, 10 env-ключей без удаления)
→ rsync-манифест → симлинк BSI providers.yaml → очистка → systemd restart → smoke (ожидаем 200).
Реального деплоя не было (правильно — его запускает оркестратор после ревью).

## Findings

Нет дефектов уровня CRITICAL/HIGH/MEDIUM.

Наблюдения (не блокирующие):
- `config_ui.write_yaml` не сохраняет YAML-комментарии (параметр `comments_preserving` не используется).
  UI не пишет `create_markdown_config.yaml` (только roles + search-config + env), поэтому риск минимален;
  зафиксировано как известное поведение, не требующее правок в рамках этого реворка.
- Консолидация `.env` на проде даёт 10 ключей (8 канонических + `QDRANT_PATH` + `TELEGRAM_ALLOWED_USERS`
  из прод-`.env`) — корректное сохранение существующих прод-ключей, не удаление.

## Risks

- Импорт-тайм `providers.yaml` в BSI требует симлинк на проде — покрыт deploy.sh (шаг 4).
- Токенизатор HF при первом `--rag` — prerequisite деплоя (зафиксировано в архитектуре §10).
- Dev-порт 8081 занят SearXNG — запуск на 8099; `config.yaml` не менялся.

## Notes

Сервер для ревью оставлен запущенным на 8099 (proc pid 44065/44070). Тестовые загрузки
(`GOST_7777_test.*`) удалены после проверки.
