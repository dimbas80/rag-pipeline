# Review — interface_RAG web-ui (полный rework, фазы 1–3)

## Verdict

**CHANGES_REQUESTED**

Сдача фаз 1–3 неполная. Бэкенд-часть в основном корректна (маршруты, безопасность,
env-инъекция, синхронная запись ролей), но (1) документированная команда запуска
по-прежнему падает с тем же `ImportError: relative import`, что ловило первое ревью;
(2) фронтенд «Настройки» не подключён к бэкенду (решения №6/№9 недостижимы из UI);
(3) обязательный набор тестов этапов 2–7 не создан. `assign_rework_to=coder`.

## Scope

Проверялись фактические артефакты (не отчёты):
- `docs/product/requirements.md`, `docs/product/decision-log.md` (решения №6, №9),
  `docs/architecture/architecture.md` (вкл. обновлённую §4.1), `docs/architecture/implementation-plan.md`.
- `firmware/src/{app,deploy_config,jobs,config_ui,providers_api,llm_client,registration,qdrant_api,chat_api}.py`,
  `firmware/src/static/{index.html,app.js,settings.html,style.css}`, `config.yaml`,
  `firmware/src/requirements.txt`, `firmware/tests/`.
- Предыдущее ревью `workflows/t_b29db84d/review.md` (10 Required Changes).
- Оба пайплайна-соседа проверены `git status`.

## Что проверялось вживую

- `python3 -m pytest firmware/tests -q` → **7 passed** (3 файла: test_deploy_config=2,
  test_config_ui=3, test_phase2=2).
- Импорт пакетом `from firmware.src import app` из корня → **OK, 33 маршрута**.
- Импорт плоским `import app` из `firmware/src/` (симуляция документированного
  `uvicorn app:app`) → **FAIL**: `ImportError: attempted relative import with no known
  parent package` (см. Findings #1).
- `git status` в `Create_Markdown_YA` и `Build_Search_index` → **чистый**.

## Requirements / критерии compliance

| Критерий | Статус | Комментарий |
|---|---|---|
| 1. Три раздела UI + wizard gating + «Добавить в базу» только при `.md`+`_reg.yaml`+`write_enabled` | PASS | Три вкладки (index.html nav); `index` гейтится на сервере (`app.py:136-137`, 400 «Завершите предыдущие шаги») и в UI (`app.js:4` по `state.can_index`). |
| 2. Решение №9: ОДНА секция «vision», атомарно в `table_vision`+`registration_vision`, без селектора «регистрация» | FAIL (UI) | Бэкенд корректен (`config_ui.sync_role_models` kind=vision, `app.py:222-229`). В UI секции «vision» нет: select'ы живут только в мёртвом фрагменте `static/settings.html`, не подключённом к `index.html`/`app.js`. |
| 3. Решение №6: одна чат-модель синхронно в `ai_postprocess`+`query_processing` | FAIL (UI) | Бэкенд корректен (`sync_role_models` kind=chat). Из UI недостижим — нет привязки select'ов и обработчиков сохранения. |
| 4. Чат: `WS /ws/chat`, цитаты+картинки, `GET /api/documents-in-base` (distinct Qdrant) | PARTIAL | WS и documents-in-base есть; цитаты возвращаются (`cited_chunk_ids`). Картинки отдаются сырыми fs-путями без `url`/`caption` (Findings #5). |
| 5. Индексация: `POST /api/documents/{sid}/index` двухфазная, явные `--qdrant-path`/`--collection`/`--strict`, gating | PARTIAL | Аргументы и gating корректны (`app.py:133-147`), но реализовано двумя job'ами + фон. поток, а не «одна job с двумя фазами» (Findings #6). |
| 6. Настройки: все `/api/settings/*`, теги по имени, модалка провайдера, маскирование, удаление ключа в env | PARTIAL | Все маршруты бэкенда есть; `tag_model` ок; маскирование (`read_env`) и удаление ключа (EnvUpdate.delete → `write_env`) ок. **Модалка «добавить провайдера» + scan в UI отсутствуют** (Findings #4). |
| 7. Безопасность: argv без shell; traversal (upload/images/markdown); `upload.max_mb`; безопасная отдача `image/` | PASS | `jobs.py:43` shell=False + проверка argv; upload `app.py:73-81` (basename+ext+limit+1); markdown `app.py:182-187` (regex+resolve+префикс); image `app.py:199-206` (basename+abs-проверка+whitelist+префикс). |
| 8. Инъекция `.env` в subprocess (convert + index) RAW-значениями | PASS | `read_env_raw` заведён (`config_ui.py:35-44`) и применён в `convert` (`app.py:130`), `index` (`app.py:140`), `prefill` (`app.py:117`). Комментарий №1 закрыт. |
| 9. Документированная команда `uvicorn ...` реально стартует | FAIL (CRITICAL) | См. Findings #1. |
| 10. Тесты этапов 2–7 зелёные; `requirements.txt` полон | FAIL (тесты) | `requirements.txt` полон. Тесты: только 3 файла из требуемых 7 (Findings #2). |
| 11. Пайплайны не изменены | PASS | `git status` обоих репозиториев чистый. |

## Закрытие Required Changes предыдущего ревью (t_b29db84d)

| # | Пункт | Статус |
|---|---|---|
| 1 | qdrant_api.py + chat_api.py + `/ws/chat` + documents-in-base + `/api/images` | PASS |
| 2 | `POST .../index` (2 фазы, явные флаги, gating) | PASS (с оговоркой Findings #6) |
| 3 | все `/api/settings/*` + синхронная запись №6/№9 | PARTIAL — бэкенд есть, UI не подключён |
| 4 | `registration.vision_prefill` + `.../register/prefill` + fallback | PASS |
| 5 | инъекция `.env` в convert/index | PASS (raw) |
| 6 | enforce лимит загрузки | PASS |
| 7 | «Стоп» + SSE в UI; gating кнопок по состоянию | PARTIAL — «Стоп»/SSE есть; gating convert по провайдерам отсутствует (Findings #7) |
| 8 | `make_slug` полная транслитерация; `write_env` удаление ключей | PASS |
| 9 | исправить команду запуска | **FAIL — не закрыт** (Findings #1) |
| 10 | дополнить requirements.txt + тесты этапов 2–7 | PARTIAL — requirements.txt да, тесты нет (Findings #2) |

## Findings

### CRITICAL

1. **Документированный запуск по-прежнему падает — тот же `ImportError: relative import`.**
   Location: `firmware/src/providers_api.py:4` (`from .config_ui import read_yaml, write_yaml`).
   `app.py:24-29` добавил try/except-фолбэк для `uvicorn app:app`, но в except-ветке делает
   плоский `import config_ui, registration, providers_api`, а `providers_api.py` остался на
   относительном импорте. Воспроизведение: `cd firmware/src && python3 -c "import app"` →
   `ImportError: attempted relative import with no known parent package` (цепочка
   app.py:27 → providers_api.py:4). Работает только `uvicorn firmware.src.app:app` из корня.
   Expected: привести `providers_api.py` к тому же try/except-паттерну, что остальные модули
   (`chat_api.py:9-12`, `registration.py:39-42`), либо зафиксировать в архитектуре §8 команду
   `uvicorn firmware.src.app:app ...` из корня. Это ровно Required Change №9 прошлого ревью —
   не закрыт.

### HIGH

2. **Набор тестов этапов 2–7 не создан.** Присутствуют только `test_deploy_config.py`,
   `test_config_ui.py`, `test_phase2.py` (итого 7 passed). Отсутствуют требуемые impl-plan
   (этапы 2–7) и Required Change №10: `test_jobs`, `test_providers_api`, `test_llm_client`,
   `test_registration`, `test_app` (gating/traversal/masking), `test_chat_api` (мок qa_graph).
   «7 passed» — это 3+2+2, а не заявленный набор.

3. **Фронтенд «Настройки» не подключён к бэкенду (решения №6/№9 недостижимы из UI).**
   Location: `firmware/src/static/app.js` (`loadSettings()` только читает providers/env и
   кладёт JSON в `<pre>`), `firmware/src/static/index.html` (в секции настроек нет селекторов
   моделей), `firmware/src/static/settings.html` (мёртвый фрагмент, ниоткуда не подключается).
   Select'ы `chat-model`/`vision-model`/`embedding-model`/`rerank-model` + fallback не
   заполняются моделями; `save-chat-model`/`save-vision-model` без обработчиков;
   `PUT /api/settings/providers/roles` (синхронная запись №6/№9) из UI не вызывается; у
   embedding/rerank нет кнопок сохранения. Бэкенд при этом корректен
   (`config_ui.sync_role_models` chat→`ai_postprocess`+`query_processing`, vision→
   `table_vision`+`registration_vision`, одной атомарной записью, без отдельного селектора
   регистрации) — но из UI он недостижим.

4. **Модалка «добавить провайдера» и scan отсутствуют в UI.** Location: `static/app.js`,
   `static/index.html`. Бэкенд-маршруты `POST /api/settings/providers/scan` и `.../add`
   существуют и рабочие, но в интерфейсе нет окна (имя/api key/base_url + «Тест»/«Добавить»/
   «Отмена») и нет вызова scan. Из настроек реально работает только редактирование `.env`.

### MEDIUM

5. **Картинки чата не привязаны к безопасному маршруту.** Location: `firmware/src/chat_api.py:84-101`
   (`select_images` возвращает `[{"path": <fs-путь>, "asset_type": "image"}]`). Архитектура §6
   требует `{url, caption, asset_type}` с `url` на `GET /api/images/...`. Клиент получает
   абсолютные пути ФС (напр. `/mnt/sdb/.../image/table_1.png`) без `caption` и без URL — их
   нельзя отрисовать в браузере. Сам маршрут `GET /api/images/...` существует и traversal-safe
   (`app.py:198-206`). Сигнатура `resolve_images_to_send(search_results, answer, query, prefer)`
   сверена с `telegram_bot/asset_helpers.py:297` — корректна.

6. **Индексация реализована двумя job'ами, а не «одной job с двумя фазами».**
   Location: `firmware/src/app.py:141-147`. `--rag` job возвращается клиенту, а `create_index.py`
   запускается фоновым потоком отдельной job'ой `index-qdrant`; клиентский SSE следит только за
   первой job'ой — лог/прогресс фазы Qdrant невидим, id второй job'ы не отдаётся наружу. Арх. §5
   шаг 4: «две под-задачи последовательно (одна job с двумя фазами)». Функционально индексация
   выполняется, контракт стриминга фазы 2 нарушен.

7. **Convert не гейтится по наличию провайдеров/ключей.** Location: `app.js:4`
   (`convert.disabled = !state.source`), `app.py:123-131` (нет проверки ролей/ключей). Арх. §4.4
   требует активность шага 3 только при заданных ролях `providers.yaml` + ключах `.env`. Сейчас
   convert активен сразу после загрузки; `--ai` упадёт в рантайме без ключей вместо гейта.

### LOW (не блокирует)

8. `settings_status` (`app.py:276-281`) включает `registration_vision` в список «required» ролей,
   хотя до первой настройки vision эта роль легитимно отсутствует — статус просто покажет false.

## Required Changes (до PASS)

1. Починить запуск по документированной команде: перевести `providers_api.py:4` на
   try/except-импорт (как `chat_api.py`/`registration.py`) ИЛИ согласовать/зафиксировать команду
   `uvicorn firmware.src.app:app ...` из корня в архитектуре §8. Прогнать оба варианта запуска.
2. Создать отсутствующие тесты этапов 2–7: `test_jobs`, `test_providers_api`, `test_llm_client`,
   `test_registration`, `test_app` (gating/traversal/masking), `test_chat_api` (мок `qa_graph`
   run/resume + interrupt→clarification). `pytest firmware/tests -q` зелёный по полному набору.
3. Подключить фронтенд «Настройки»: заполнять select'ы `chat-model`/`vision-model`/`embedding-model`/
   `rerank-model` (+fallback) моделями из `providers.yaml` по тегам; обработчики
   `save-chat-model`/`save-vision-model` → `PUT /api/settings/providers/roles` (kind chat/vision);
   кнопки сохранения для embedding/rerank; **одна** секция «vision» без отдельного селектора
   «регистрация» (решения №6/№9).
4. Реализовать в UI модалку «добавить провайдера» (имя/api/base_url + «Тест»→scan/«Добавить»/
   «Отмена») с вызовом `/api/settings/providers/scan` и `.../add`.
5. Привести отдачу картинок чата к `{url, caption, asset_type}` с `url` на `GET /api/images/...`
   (маппинг fs-пути → относительный `doc_dir/rel_path`), вместо сырых fs-путей.
6. Реализовать индексацию как одну job с двумя фазами (или, как минимум, транслировать лог и
   status обеих фаз и отдавать финальный job_id клиенту), согласно арх. §5 шаг 4.
7. Гейтить шаг конвертации на сервере по наличию ролей `providers.yaml` + ключей `.env`
   (арх. §4.4), с сообщением «настройте провайдеры/ключи».

## Risks

- Объём доработок — фронтенд настроек (селекторы+модалка+обработчики) + 6 тестовых файлов +
  исправление запуска. Рекомендуется выделить отдельными под-задачами.
- Интеграция с реальными пайплайнами (PDF→OCR→RAG→Qdrant→чат) на dev не проверялась: нет
  `.env`, `uploads/`, `qdrant_data`. Без ключей и данных «живой» чат/конвертация невоспроизводимы —
  тесты чата должны быть на моках.
- `BASE_MARKDOWN` в `qa_graph.py` захардкожен (`/mnt/sdb/!База_ГОСТ/Markdown`) — на dev картинки
  не резолвятся (арх. §10, не блокирует).

## Notes

- Бэкенд синхронной записи ролей (`config_ui.sync_role_models`) корректен: chat → `ai_postprocess`+
  `query_processing`; vision → `table_vision`+`registration_vision`, одной атомарной записью, без
  отдельного селектора регистрации. Проблема только в отсутствии UI-привязки.
- `read_env_raw` (комментарий №1 воркера) заведён и применён — закрыто.
- Механика WS-чата (накопление `state.update(values)` из `stream_mode="updates"`) сверена с
  `qa_graph.py`: `search`-узел возвращает `search_results`, `generate_answer` возвращает
  `final_answer`+`cited_chunk_ids` (строки 1314, 960 и др.); `resume()` после interrupt работает
  через `Command(resume=...)` в тот же `thread_id`. Комментарий №2 воркера — не подтверждён как баг.
- `make_slug` полная транслитерация и `write_env` (удаление ключа) — исправлены и покрыты тестами.
- SSE-события теперь кодируются JSON (`app.py:172` `json.dumps`) — LOW предыдущего ревью закрыт.
