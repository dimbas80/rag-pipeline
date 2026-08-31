# Review — Rework 3 (финал, закрытие CHANGES_REQUESTED из t_8de157f7)

## Verdict

**CHANGES_REQUESTED**

`assign_rework_to=coder`.

Rework 3 закрыл три из пяти пунктов прошлого вердикта корректно и проверено вживую:
разнос ролей по `providers.yaml` СВОЕГО пайплайна (пункт 1), серверный convert-gating по
ролям/ключам (пункт 3), `caption` в отдаче картинок (пункт 5). Однако **провайдерный
«Добавить»-флоу сломан** (пункт 2): модалка и scan работают, но `/add` возвращает 422,
провайдера добавить нельзя. Дополнительно: Required Change #4 (тесты) закрыт частично —
gating индекса и мок `qa_graph` не покрыты; найден регресс `settings_status`.

## Scope

Проверялись фактические файлы и реальный запуск/запись (не отчёт coder'а):
- `firmware/src/{app,config_ui,providers_api,chat_api,deploy_config}.py`,
  `firmware/src/static/{index.html,app.js}`, `firmware/tests/*`.
- `docs/product/decision-log.md` (№5, №6, №9), `docs/architecture/architecture.md` (§4.1–4.4, §6, §7, §8).
- `workflows/t_8de157f7/review.md` (исходный вердикт), `workflows/t_96b0be8e/implementation-report.md`.
- Пайплайны-соседи `Create_Markdown_YA/` и `Build_Search_index/` (только чтение providers.yaml).
- Эмпирическая проверка записи ролей в temp-каталоги (monkeypatch `app.cfg`) и
  HTTP-прогон `/api/settings/providers/add`, `/api/documents/{sid}/convert`, `/api/settings/status`.

## Проверяемые пункты (check-list задачи)

| # | Пункт | Статус | Доказательство |
|---|---|---|---|
| 1 | Роли пишутся в providers.yaml СВОЕГО пайплайна | **PASS** | `_write_combined` (app.py:234-240) пишет `create_markdown.*` → `create_markdown_dir/providers.yaml`, `build_search_index.*` → `build_search_index_dir/providers.yaml`. Эмпирически: после `update_provider_roles(kind="chat")` CM-файл имеет `roles.create_markdown.{table_vision,ai_postprocess}`, BSI-файл — `roles.build_search_index.{query_processing,embedding,rerank}`; CM-файл НЕ содержит `build_search_index`, BSI-файл НЕ содержит `create_markdown`. `sync_role_models` (config_ui.py:106-121): chat→`ai_postprocess`+`query_processing`, vision→`table_vision`+`registration_vision` (одна секция, без отдельного селектора — решение №9), embedding/rerank→BSI. `GET /api/settings/providers` отдаёт `_combined_providers()` — объединённые роли обоих пайплайнов. |
| 2 | Модалка провайдера + scan/add | **FAIL (add)** | Модалка есть (`<dialog id="provider-dialog">` в index.html, поля `provider-name`/`provider-key`/`provider-url`, кнопки `provider-test`/`provider-add`/`provider-cancel`); обработчики в app.js:2-7 реально вызывают `/api/settings/providers/scan` (работает) и `/api/settings/providers/add`. НО `provider-add` шлёт `models: scan.models` = `[{name, tag}, ...]` (список dict'ов), тогда как `ProviderAdd.models: dict[str,str] \| list[str]` → **422**. Empirically: `POST /add` c этим payload → 422 (pydantic). Провайдера через UI добавить нельзя. |
| 3 | Convert gating по ролям/ключам | **PASS** | `convert()` (app.py:123-138) проверяет роли `table_vision`/`ai_postprocess` (`provider`+`model`) и непустой `api_key_env` в `.env`. Эмпирически: без ключей в `.env` → `400 "Настройте vision и чат-модель и добавьте API-ключи"`; при пустой секции `roles.create_markdown` → тот же `400`. Обе ветки подтверждены. |
| 4 | Тесты: gating индекса / traversal / маскирование; мок qa_graph | **PARTIAL** | `pytest firmware/tests -q` → **16 passed** (зелёный). traversal (`test_traversal_routes_reject_unsafe_paths`) и маскирование env (`test_settings_response_does_not_expose_env_values`) — есть. НО **gating индекса не покрыт** (нет теста, бьющего в `/api/documents/{sid}/index` и проверяющего 400), **`test_chat_api` не мокает `qa_graph`** (только `select_images`). |
| 5 | `select_images` → `{url, caption, asset_type}` | **PASS** | chat_api.py:84-112 возвращает `{"url", "caption", "asset_type": "image"}`; `caption` пробрасывается из второго элемента `resolve_images_to_send` (chat_api.py:99,106). Покрыто `test_select_images_returns_urls`. |

## Findings

### HIGH

**#1. Провайдерный «Добавить» флоу сломан — 422 на `/add`.**
   Location: `static/app.js:7` (обработчик `provider-add` шлёт `models: scan.models` = `[{name, tag}, ...]`),
   `app.py:48-52` (`ProviderAdd.models: dict[str,str] | list[str]`), `providers_api.py:25-31` (`add_provider`
   делает `{m: tag_model(m) for m in models}`), `providers_api.py:9-13` (`tag_model` вызывает `name.lower()`).
   Проблема: `/api/settings/providers/scan` возвращает `{models: [{name, tag}, ...]}`; UI пересылает эти
   dict'ы в `models` как есть. Бэкенд ждёт список строк или `{name: tag}`. Итог: pydantic отдаёт **422**
   до вызова хэндлера. Даже если бы payload прошёл, `add_provider` упал бы с `AttributeError` на
   `dict.lower()`. Провайдера через модалку добавить невозможно — пункт 2 не выполнен.
   Expected: UI должен передавать `models` как список имён (строк) или `{name: tag}`; либо бэкенд
   принимает список `{name, tag}` и маппит их. Обе стороны должны сойтись по контракту.
   Reason: блокирует функциональность «Добавить провайдера» (решение №5).

### MEDIUM

**#2. Required Change #4 прошлого ревью закрыт частично — gating индекса и мок qa_graph не покрыты.**
   Location: `firmware/tests/test_app.py` (3 теста: существование маршрутов, traversal, маскирование —
   нет теста gating'а `/api/documents/{sid}/index`), `firmware/tests/test_chat_api.py` (1 тест
   `select_images`, нет мока `qa_graph.run/resume` и interrupt→clarification).
   Expected: `test_app` — тест, создающий сессию без `.md`/`_reg.yaml`/`write_enabled` и проверяющий
   `400` на `/index`; `test_chat_api` — мок `QAGraph` (run/stream/resume + `__interrupt__` → clarification).
   Reason: сценарии безопасности/gating из прошлого вердикта остались непроверенными.

**#3. Регресс `settings_status`: build_search_index-роли отдаются как не заданные.**
   Location: `app.py:308-313` (`settings_status` читает `config_ui.read_yaml(_providers_path())` — только
   CM-файл). После Rework 3 роли `build_search_index.*` живут в BSI-файле, поэтому
   `roles.build_search_index.query_processing/embedding/rerank` всегда `False`, хотя заданы.
   Эмпирически подтверждено: `settings_status()["roles"]["build_search_index.query_processing"] == False`,
   при том что `_combined_providers()` его содержит.
   Expected: читать `_combined_providers()` (или оба файла). Точка `/api/settings/status` сейчас не
   используется фронтом (grep по static — 0 вхождений), но контракт §7 нарушен.
   Reason: «сводка готовности» врёт про готовность индексации.

### LOW (не блокирует)

**#4. `_write_combined` стирает комментарии и переструктурирует BSI-файл.**
   `write_yaml(..., comments_preserving=False)` (config_ui.py:14) дропает все комментарии (в т.ч.
   «build_search_index добавит собственный pipeline позднее»). `_write_combined` заменяет `roles` BSI-файла
   на только `build_search_index`, удаляя оттуда `create_markdown` (BSI его не читает — безвредно), и
   дублирует объединённый реестр `providers` в оба файла. Косметика/структура, функционально не блокирует.

## Закрытие Required Changes из t_8de157f7

| # | Required Change | Статус |
|---|---|---|
| 1 | Модалка «добавить провайдера» + scan | **PARTIAL** — модалка/scan есть, add сломан (Finding #1) |
| 2 | Гейтить convert по ролям/ключам | **PASS** — обе ветки 400 подтверждены |
| 3 | Целевой файл для `build_search_index.*` | **PASS** — split-write в свой файл |
| 4 | Усилить тесты (gating индекса + traversal + маскирование; мок qa_graph) | **PARTIAL** — traversal/маскирование есть; gating индекса и мок qa_graph нет (Finding #2) |
| 5 | Вернуть `caption` | **PASS** — `{url, caption, asset_type}` |

## Required Changes (до PASS)

1. **[HIGH] Починить «Добавить провайдера»** — согласовать контракт `models` между UI и бэкендом
   (app.js:7 ↔ app.py:48-52 ↔ providers_api.py:25-31). Минимально: UI передаёт `models` как список имён
   строк или `{name: tag}`; либо `ProviderAdd`/`add_provider` принимают `[{name, tag}]` и маппят теги.
   После исправления проверить реальный `POST /api/settings/providers/add` → 200 и появление провайдера
   в `GET /api/settings/providers` (и в `.env` через `api_key_env`).
2. **[MEDIUM] Добавить тест gating'а индекса** в `test_app.py`: сессия без `.md`/`_reg.yaml`/`write_enabled`
   → `400` на `POST /api/documents/{sid}/index`.
3. **[MEDIUM] Добавить мок `qa_graph`** в `test_chat_api.py`: `run/stream` с `__interrupt__` →
   `clarification`, `resume` → `answer`; без реального импорта графа.
4. **[MEDIUM] Исправить `settings_status`** (app.py:308-313): читать роли через `_combined_providers()`,
   чтобы `build_search_index.*` отдавались корректно.
5. **[LOW] (опц.) Сохранять комментарии / не дублировать providers-реестр** в `_write_combined` при
   записи обоих файлов.

## Risks

- Объём: 1 HIGH (контракт add) + 2 MEDIUM теста + 1 MEDIUM status + опц. LOW. Точечные правки.
- Интеграция с реальными пайплайнами на dev не воспроизводится: нет `.env`, `uploads/`, `qdrant_data`;
  «живой» чат/конвертация/индексация и реальный `/models`-scan не проверяются — тесты на моках/заглушках.
- `BASE_MARKDOWN` захардкожен в `qa_graph.py` (`/mnt/sdb/!База_ГОСТ/Markdown`) — на dev картинки в чате
  не резолвятся (арх. §10, не блокирует).

## Notes

- Все 6 модулей на try/except-импорте; запуск `from firmware.src import app` даёт 33 маршрута,
  `python3 -m pytest firmware/tests -q` → **16 passed** (расхождений с отчётом coder'а нет).
- Convert-gating, split-write ролей и caption — проверены вживую (не по отчёту), PASS.
- `validate_providers` не валидирует секцию `roles` (только `providers`) — `registration_vision`/fallback
  `{}` проходят без проблем; ок для текущей схемы.
