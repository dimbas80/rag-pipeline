# Review — Rework 2 (закрытие CHANGES_REQUESTED из t_1e689276)

## Verdict

**CHANGES_REQUESTED**

Запуск, синхронная запись ролей, URL картинок и единый двухфазный indexing job —
закрыты корректно и проверены вживую. Но **два пункта проверяемого списка не выполнены**:
(1) модалка «добавить провайдера» + scan в UI отсутствуют (пункт 4);
(2) convert не гейтится по ролям/ключам на сервере (пункт 7).
Дополнительно найден функциональный дефект: синхронизация ролей `build_search_index.*`
(`query_processing`/`embedding`/`rerank`) пишется в **не тот** `providers.yaml` — чат и
`create_index.py` читают эти роли из `Build_Search_index/.../providers.yaml`, а UI пишет их в
`Create_Markdown_YA/.../providers.yaml`. `assign_rework_to=coder`.

## Scope

Проверялись фактические файлы и реальный запуск (не отчёт coder'а):
- `firmware/src/{app,deploy_config,jobs,config_ui,providers_api,llm_client,registration,qdrant_api,chat_api}.py`,
  `firmware/src/static/{index.html,app.js,settings.html,style.css}`,
  `firmware/tests/*`, `config.yaml`.
- `docs/product/requirements.md`, `docs/product/decision-log.md` (№5, №6, №9),
  `docs/architecture/architecture.md` (§4.1, §4.3, §4.4, §5, §6, §7, §8).
- `workflows/t_1e689276/review.md` (исходный вердикт) и `workflows/t_b29db84d/review.md`.
- Оба пайплайна-соседа: `Create_Markdown_YA/` и `Build_Search_index/` (только чтение).

## Проверяемые пункты (check-list задачи)

| # | Пункт | Статус | Доказательство |
|---|---|---|---|
| 1 | Запуск обоими способами без ImportError | **PASS** | `from firmware.src import app` → 33 маршрута; `cd firmware/src && python3 -c 'import app'` → 33 маршрута; `python3 -m uvicorn firmware.src.app:app` → «Application startup complete» (bind-ошибка `[Errno 98]` — уже занят порт 8099, к импорту не относится). Все модули на try/except-паттерне: `app.py:18-29`, `providers_api.py:4-7`, `chat_api.py:9-12`, `registration.py:39-42`. Голых `from .X import` без fallback не осталось. |
| 2 | Тесты 6 файлов, зелёные, не пустые | **PASS (с оговоркой)** | Все 6 файлов есть (`test_jobs`, `test_providers_api`, `test_llm_client`, `test_registration`, `test_app`, `test_chat_api`), `python3 -m pytest firmware/tests -q` → **14 passed** за 1.37s. Файлы непустые и реально покрывают функции модулей. Оговорка → Finding #4 (покрытие тоньше, чем требовал Required Change #2 прошлого ревью). |
| 3 | Фронтенд «Настройки»: select'ы + save → PUT roles | **PASS** | `loadSettings()` (app.js) тянет `/api/settings/providers`, заполняет `chat/vision/embedding/rerank`-select'ы; фильтр по тегу: `x.tag===kind` для vision/embedding/rerank. `saveRole(kind)` → `PUT /api/settings/providers/roles` `{kind, spec}`. `config_ui.sync_role_models` (config_ui.py:106-121): chat→`ai_postprocess`+`query_processing`, vision→`table_vision`+`registration_vision` (одна секция, без селектора «регистрация»), embedding→`build_search_index.embedding`, rerank→`build_search_index.rerank`. Оговорка → Finding #3 (целевой файл для build_search_index-ролей неверен). |
| 4 | Модалка провайдера + scan в UI | **FAIL** | Кнопка `add-provider` есть в index.html, но в app.js **нет** обработчика; модалки (имя/api_key/base_url + «Тест»/«Добавить»/«Отмена») нет; вызовы `/api/settings/providers/scan` и `.../add` из UI отсутствуют. Бэкенд-маршруты (app.py:226-239) рабочие, но из UI недостижимы. = Required Change #4 прошлого ревью, не закрыт. |
| 5 | Картинки чата: URL `/api/images/...` + рендер | **PASS** | `select_images` (chat_api.py:84-111) маппит fs-путь → `relative_to(base_markdown)` и отдаёт `{"url": "/api/images/<doc_dir>/<rel_path>", "asset_type": "image"}`. Фронтенд рендерит `<img src=i.url>` (app.js, обработчик `ws.onmessage`). Оговорка → Finding #5 (`caption` отсутствует, отбрасывается). |
| 6 | Индексация одной job, лог обеих фаз, gating | **PASS** | `index_document` (app.py:133-142) → `runner.start_sequence([rag, index])`; `jobs.start_sequence` (jobs.py:40-81) эмитит `[phase 1/2] started/done` + строки обеих фаз в один `log_buffer` → один SSE (`/api/jobs/{id}/events`). Gating сохранён (app.py:136-137: `.md`+`_reg.yaml`+`write_enabled`). |
| 7 | Convert gating по ролям/ключам | **FAIL** | `convert()` (app.py:123-131) не проверяет роли `create_markdown.table_vision`/`ai_postprocess` и непустые `api_key_env` в `.env` перед запуском. Нет 4xx с сообщением. = Required Change #7 прошлого ревью, не закрыт. |

## Findings

### HIGH

**#3. Синхронизация ролей `build_search_index.*` пишется в неверный `providers.yaml`.**
   Location: `app.py:204-205` (`_providers_path()` = `cfg.create_markdown_dir / "providers.yaml"`),
   `config_ui.py:106-121` (`sync_role_models` пишет `build_search_index.query_processing/embedding/rerank`
   в тот же файл), `chat_api.py:37` (`providers_path = cfg.build_search_index_dir / "providers.yaml"`).
   Проблема: чат читает роль `query_processing` из `Build_Search_index/firmware/src/providers.yaml`
   (`qa_graph.py:251-252`: `_get_providers(cfg.providers_path)` → `resolve_subrole(..., "build_search_index", "query_processing")`),
   `create_index.py` читает embedding/rerank из `providers.yaml` рядом со своим `__file__` (BSI). UI же пишет
   эти роли в `Create_Markdown_YA/firmware/src/providers.yaml`. Файлы — разные (проверено: разные inode;
   в CM-файле секция `roles.build_search_index` отсутствует, есть комментарий «build_search_index добавит
   собственный pipeline позднее»). Итог: выбор чат-модели в UI не влияет на `query_processing` чата,
   выбор embedding/rerank не влияет на индексацию. Решение №6 функционально нарушено для чат-половины;
   vision-роли (№9) корректны, т.к. обе `create_markdown.*`.
   Expected: `sync_role_models`/`update_provider_roles` должны писать `build_search_index.*` роли в
   `build_search_index_dir/providers.yaml`, а `create_markdown.*` — в `create_markdown_dir/providers.yaml`
   (либо зафиксировать в архитектуре единый общий файл и обновить потребителей).

### MEDIUM

**#4. Покрытие тестов тоньше Required Change #2 прошлого ревью.**
   `test_app.py` — 3 assert только на существование маршрутов (`/index`, `/images`, `/roles`), не покрывает
   gating индекса, path traversal (markdown/image), маскирование env. `test_chat_api.py` — только
   `select_images` (нет мока `qa_graph.run/resume` + interrupt→clarification). `test_registration.py`/`test_jobs.py`/
   `test_llm_client.py` — по 1 тесту (make_slug, sequence, chat_completion auth). Функции покрыты на smoke-уровне,
   но конкретные сценарии безопасности/gating из прошлого вердикта не проверены.

### LOW (не блокирует)

**#5. `select_images` отбрасывает caption.** Location: `chat_api.py:99` (`paths, _ = resolve_images_to_send(...)`).
   Возвращается `{url, asset_type}` без `caption`; архитектура §6 требует `{url, caption, asset_type}`.
   Фронтенд рендерит без caption — визуально не блокирует, но контракт недовыполнен.

**#6. Единый cwd для обеих фаз indexing.** `jobs.start_sequence` запускает `create_index.py` (BSI) с
   `cwd=create_markdown_dir`. Не блокер: `input_dir` и `--qdrant-path` передаются абсолютными, а
   `providers.yaml` для `create_index.py` берётся от `__file__` (BSI). Низкий риск, зафиксирован.

## Закрытие Required Changes из t_1e689276

| # | Required Change | Статус |
|---|---|---|
| 1 | Починить запуск (providers_api.py относительный импорт) | **PASS** — все модули на try/except; оба запуска 33 маршрута |
| 2 | Тесты этапов 2–7 (6 файлов) | **PARTIAL** — файлы есть, 14 passed, но покрытие тоньше (Finding #4) |
| 3 | Подключить фронтенд «Настройки» | **PASS** — select'ы + save → PUT roles, одна vision-секция |
| 4 | Модалка «добавить провайдера» + scan | **FAIL** — не реализовано (Finding: пункт 4) |
| 5 | Картинки → `{url, caption, asset_type}` | **PARTIAL** — url/asset_type есть, caption нет (Finding #5) |
| 6 | Индексация одной job с двумя фазами | **PASS** — `start_sequence`, лог обеих фаз в один SSE |
| 7 | Гейтить convert по ролям/ключам | **FAIL** — не реализовано (Finding: пункт 7) |

## Required Changes (до PASS)

1. **[HIGH] Реализовать модалку «добавить провайдера»** в `static/index.html` + `static/app.js`:
   поля имя / api key / base_url, кнопки «Тест» (→ `POST /api/settings/providers/scan`, показать модели с
   тегами) / «Добавить» (→ `POST /api/settings/providers/add`) / «Отмена»; привязать к существующей кнопке
   `add-provider`. (Пункт 4, решение №5.)
2. **[HIGH] Гейтить convert на сервере** (`app.py:123-131`): перед запуском проверить, что роли
   `create_markdown.table_vision` и `create_markdown.ai_postprocess` заданы в providers.yaml и их
   `api_key_env` имеют непустые значения в `.env`; иначе `HTTPException(4xx)` с понятным сообщением
   «настройте провайдеры/ключи» (арх. §4.4). (Пункт 7.)
3. **[HIGH] Исправить целевой файл для ролей `build_search_index.*`** (`query_processing`/`embedding`/`rerank`):
   писать их в `build_search_index_dir/providers.yaml`, а не в `create_markdown_dir/providers.yaml`
   (Finding #3). Иначе решение №6 и выбор embedding/rerank не доходят до чата/индексации.
4. **[MEDIUM] Усилить тесты** до уровня Required Change #2: `test_app` — gating индекса + traversal
   (markdown/image) + маскирование env; `test_chat_api` — мок `qa_graph.run/resume` + interrupt→clarification.
5. **[LOW] Вернуть `caption`** в отдачу картинок (арх. §6): `{url, caption, asset_type}`, пробрасывая второй
   элемент `resolve_images_to_send` (Finding #5).

## Risks

- Объём: модалка + серверный gating + правка целевого файла ролей + 2 файла тестов. Рекомендуется разбить
  на под-задачи.
- Интеграция с реальными пайплайнами (PDF→OCR→RAG→Qdrant→чат) на dev не проверяется: нет `.env`, `uploads/`
  (каталог не создан), `qdrant_data`. «Живой» чат/конвертация/индексация невоспроизводимы — тесты на моках.
- `BASE_MARKDOWN` в `qa_graph.py` захардкожен (`/mnt/sdb/!База_ГОСТ/Markdown`) — на dev картинки не резолвятся
  (арх. §10, не блокирует).

## Notes

- Все 6 модулей приведены к try/except-паттерну импорта — CRITICAL прошлого ревью закрыт, оба способа запуска
  дают 33 маршрута.
- `read_env_raw` + инъекция raw-значений в subprocess (convert/index/prefill) — закрыто.
- `sync_role_models` атомарно пишет обе роли одной записью (решения №6/№9) — корректно по структуре, проблема
  только в целевом файле для build_search_index-ролей (Finding #3).
- Отчёт coder'а (`implementation-report.md`) заявляет «15 passed»; фактически `pytest` даёт **14 passed** —
  расхождение в отчёте, не в коде.
