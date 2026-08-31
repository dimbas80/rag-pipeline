# Implementation report — t_842616ab

Фаза 2 (UI) второй итерации `interface_RAG`: решения 20–29, frontend-часть.
Изменены только `firmware/src/static/*`. Backend-логика фазы 1 не трогалась.
Отчёт по архитектуре: `workflows/t_8b227daf/architecture-report.md` (§10.2 — план фазы 2).

---

## 1. Что изменено (файлы)

| Файл | Изменение (решение) |
|---|---|
| `firmware/src/static/index.html` | №20 — убран дубликат `id="reg-status"`: `<span>` переименован в `id="reg-feedback"`; опции селекта — русские подписи «активный»/«не активный» при значениях `active`/`inactive`. №21 — единая точка сообщений регистрации `#reg-feedback` рядом с кнопкой. №23 — поле `reg-ignore_sections` предзаполнено `value="Предисловие, Содержание"`. №25 — чат: окно истории `#chat-messages` + единственная строка ввода `#chat-input`/`#chat-send` ВНИЗУ; удалены `#query` (textarea сверху), `#answer`, `#chat-actions`, `#reply-row`/`#reply-input`/`#reply-send`; кнопка «Показать документы в базе» перенесена в боковую карточку |
| `firmware/src/static/style.css` | №26 — `.site-header { position: sticky; top: 0; z-index: 50; }`. №25 — стили `.chat-messages` (окно истории, автоскролл-контейнер), `.chat-bubble.user/.assistant`, `.chat-bubble-images`, `.chat-bubble-sources`, `.chat-input-row`; удалены стили `#query`/`#answer`/`.reply-row`. №27 — `.settings-layout` (грид 220px + контент), `.settings-nav` (sticky-меню), `.settings-content`, `.settings-section[hidden]`; media-query для узких экранов |
| `firmware/src/static/app.js` | №25 — чат переписан: `state.chat = {awaitingClarification, history}`, рендер «пузырей» (`addBubble`), один ввод в двух режимах (query / reply при `awaitingClarification`), Enter = отправить, автоскролл; `node`/`clarification`/`answer`/`error` обрабатываются как раньше. №28 — `prefillRegistration`: при `source="reg_yaml"` заполняются ВСЕ поля формы (`fillAllRegistrationFields` + маппинг `REG_FIELD_MAP`), включая даты (datetime → ГГГГ-ММ-ДД), `ignore_sections` (list → ", "), `status` (fillSelect); при `source="vision"` — поведение как раньше. №21 — все `setStatus("reg-status", …)` → `setStatus("reg-feedback", …)`; из обработчика регистрации убран дубль в `doc-status` (сообщение успеха/ошибки — только возле кнопки) |
| `firmware/src/static/settings.js` | №27 — сайдбар: `NAV_SECTIONS` (провайдеры и роли / Yandex OCR / ключи API провайдеров / прочие ключи / параметры графа / папка Qdrant), слева меню, справа активный раздел; все секции рендерятся, видима одна (`state.activeSection`, `applySectionVisibility`); `renderEnvCard` разбита на `renderYandexCard`/`renderProviderKeysCard`/`renderOtherKeysCard`. №29 — секция «Папка Qdrant»: `GET /api/settings/qdrant` в `loadSettings` (Promise.all), поле пути, кнопка «Сбросить к дефолту» (PUT `{path: ""}`), сохранение в единой `saveSettings` (PUT только при изменении/сбросе) |

Изменений вне `static/*` нет (git diff подтверждает: только 4 файла статики).

---

## 2. Как проверено (живой рендер + тесты)

Dev-сервер: `python3 -m uvicorn firmware.src.app:app --host 127.0.0.1 --port 8091`
(порт 8081 занят SearXNG; конфиг dev из `config.yaml`). Проверка — браузером по
http://127.0.0.1:8091/ (DOM-проверки через console + скриншоты).

| Пункт приёмки | Результат |
|---|---|
| №20 статус-селект раскрывается, русские подписи, значения `active`/`inactive` | ✅ `reg-status` ровно 1 элемент, опции `[{active, «активный»}, {inactive, «не активный»}]`, дубликат id устранён (корень №1 — дубль id, см. архитектуру §1.1) |
| №21 сообщение регистрации возле кнопки | ✅ после регистрации `#reg-feedback` = «Документ зарегистрирован: GOST_18410_test_doc ✓» (ok); `#doc-status` (область загрузки) остаётся только для событий загрузки — сообщение регистрации туда не пишется |
| №23 `ignore_sections` предзаполнен | ✅ значение по умолчанию «Предисловие, Содержание» |
| №25 чат одним окном, ввод внизу, уточняющий вопрос через тот же ввод | ✅ `#query`/`#answer`/`#reply-row` отсутствуют в DOM; запрос → user-bubble + WS → `node` (статус) → `clarification` (assistant-bubble); ответ на уточнение через тот же `#chat-input` → снова `node` (граф продолжил обработку). Ввод очищается, история накапливается, автоскролл работает |
| №26 sticky-шапка | ✅ `position: sticky`, при `scrollY=600` (макс. 856) шапка `top: 0` |
| №27 настройки — боковое меню | ✅ 6 пунктов меню слева, справа — активный раздел; клик переключает (проверено providers → qdrant), остальные секции скрыты |
| №28 prefill из `_reg.yaml` без OCR | ✅ E2E: загружен файл при существующем `<stem>_reg.yaml` → `source="reg_yaml"`, все 15 полей восстановлены (document_id, title, domain, edition=1973, date_enacted=1973-01-01, status=active, ignore_sections), feedback «Поля восстановлены из существующей регистрации (_reg.yaml)». Повторная регистрация → ровно одна запись, slug стабилен (`GOST_18410_test_doc`) |
| №29 папка Qdrant в настройках | ✅ секция с путём и подписью «дефолт из config.yaml»; смена пути → «Сохранить» → `QDRANT_PATH` записан в `config/.env`, `GET /api/settings/qdrant` → `overridden:true`, права `.env` = 0600; «Сбросить к дефолту» → «Сохранить» → `QDRANT_PATH` удалён из `.env`, `overridden:false`, путь = дефолт |

Тесты: `python3 -m pytest firmware/tests -q` → **62 passed** (backend фазы 1 зелёный;
frontend-тестов в проекте нет). Синтаксис JS: `node --check app.js / settings.js` → OK.

Скриншоты (в этой папке): `chat-single-window.png`, `documents-tab.png`,
`settings-sidebar.png`, `settings-qdrant.png`. (Визуальный анализ картинок
aux-vision-моделью в этой среде недоступен — использована DOM-верификация.)

---

## 3. Найденная проблема (вне объёма фазы 2 — флаг оркестратору)

**`load_dotenv()` из пайплайна «загрязняет» `QDRANT_PATH` в процессе интерфейса.**

Воспроизведено: на чистом dev-сервере `GET /api/settings/qdrant` → `overridden:false`,
путь = дефолт. После **первого же запроса в чате** (WS `/ws/chat`) тот же endpoint →
`overridden:true`, путь = `/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data` (прод-путь),
хотя `.env` интерфейса не менялся.

Причина: `Build_Search_index/firmware/src/llm_providers.py:93` вызывает голый
`load_dotenv()`, который поднимается по каталогам и находит
`/root/projects/Build_Search_index/.env` с `QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data`,
и кладёт его в `os.environ` **процесса интерфейса**. `DeployConfig.qdrant_path_override`
(фаза 1) имеет приоритет process-env > .env — после первого чат-запроса эффективный
путь Qdrant на dev молча становится прод-путём (documents_in_base / collections / chat
читают прод-базу; index на dev всё равно не пишет — `write_enabled: false`).

Это пересечение backend (фаза 1) и пайплайна (Build_Search_index, менять нельзя по
контракту). В объём «только static/*» не входит; предлагаемое направление для
архитектора/следующей итерации:
- в `deploy_config.py` не доверять `QDRANT_PATH` из process-env, если он совпадает с
  дефолтом пайплайна, ИЛИ
- в интерфейсе после создания ChatSession убирать ключ из `os.environ`
  (`os.environ.pop("QDRANT_PATH", None)`), либо
- в `llm_providers.py` пайплайна заменить голый `load_dotenv()` на явный путь
  (но пайплайн — вне зоны правок интерфейса).

UI-часть №29 при этом работает корректно: она честно показывает и сохраняет то, что
возвращает backend. Проблема проявляется только после первого чат-запроса и связана
с приоритетом process-env в фазе 1.

## 4. Известные ограничения / замечания

- `source_file` в prefill из `_reg.yaml` не восстанавливается: поле readonly и всегда
  отражает фактически загруженный файл (backend при регистрации всё равно
  перезаписывает его `s["name"]`). Намеренное отличие от таблицы маппинга §10.2 —
  сохранение инварианта сопоставления файла с записью (`_find_doc_key`).
- Если WS закрыт во время ожидания уточняющего ответа, reply не ставится в очередь
  (в очередь попадает только `query`) — поведение сохранено с прежней версии.
- `edition`/`date_enacted` в `_reg.yaml` после регистрации из формы — строки
  (`'1973'`, `'1973-01-01'`): так было и раньше (форма отдаёт строки), пайплайн
  читает их без изменений.
- Тестовая папка Qdrant в `.env` не осталась: после проверок `QDRANT_PATH` удалён,
  `.env`/`.env.bak` = 0600 без этого ключа; тестовые файлы uploads удалены.

## 5. Файлы

- Изменено: `firmware/src/static/index.html`, `firmware/src/static/style.css`,
  `firmware/src/static/app.js`, `firmware/src/static/settings.js`.
- Отчёт: `workflows/t_842616ab/implementation-report.md` + 4 скриншота.
