# Review — вторая итерация interface_RAG (решения 20–29)

Задача: `t_219236ed` · Вход: decision-log 20–29, архитектура `t_8b227daf`,
реализация фазы 1 `t_6046ae9e` + фазы 2 `t_842616ab`.

## Вердикт

**PASS**

Все 10 пунктов приёмки (№20–№29) реализованы и проверены **вживую** по коду и
запущенному dev-серверу (не по отчётам кодеров). Блокирующих дефектов нет.
Единственная найденная проблема (загрязнение `QDRANT_PATH` голым `load_dotenv()`)
— кросс-фазная, уже на отдельном architect-треке (см. §7), не блокирует этот
ревью (решение №29 в части UI/API-контракта работает корректно).

---

## 1. Как проверялось

- `python3 -m pytest firmware/tests -q` → **62 passed** (1 warning, не относится к коду).
- `node --check app.js settings.js` → OK.
- Свежий dev-сервер `INTERFACE_RAG_ENV=dev uvicorn app:app :8094` (старый сервер на
  :8099 крутился на коде ДО второй итерации — не использовался).
- Живой прогон backend-контрактов скриптом `workflows/t_219236ed/live_test.py`
  (upload → register → prefill → re-register → qdrant PUT/сброс/валидация).
- DOM-проверки браузером (console JS) на http://127.0.0.1:8094/.
- Фактические `stat`/`ls -l` прав файлов после реальной записи.

---

## 2. Соответствие требованиям (по пунктам)

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 20 | Статус-селект раскрывается; «активный»/«не активный»; в `_reg.yaml` `active`/`inactive` | PASS | Дубль `id="reg-status"` устранён (count=1). Опции `[{active,«активный»},{inactive,«не активный»}]`. Значения в `_reg.yaml`: `status: active` (live-запись). Корень замечания (двойной id) закрыт. |
| 21 | Сообщение о регистрации возле кнопки | PASS | `#reg-feedback` (span) в `.form-actions` — тот же родитель, что и `#register`. `setStatus("reg-feedback", …)` в prefill и обработчике регистрации; `#doc-status` остался только для событий загрузки. |
| 22 | Права: каталоги 0777, файлы 0666; `.env`=0600; без глобального umask | PASS | Live-`stat`: загруженный файл 666, `uploads/` 777, каталог `<stem>/` 777, `_reg.yaml` 666, `.bak` 666, `.env` 600 (после записи). `config_ui.write_env/write_yaml` chmod 0600 (+`.bak`). `preexec_fn=_child_umask_zero` только в subprocess. Глобального `umask 000` нет. |
| 23 | `ignore_sections` предзаполнен «Предисловие, Содержание» | PASS | `value="Предисловие, Содержание"` в разметке; live DOM `value` = «Предисловие, Содержание». |
| 24 | Одна запись на документ; повторная регистрация не плодит slug | PASS | Live: slug1==slug2==`GOST_9999_kabeli`, `documents` содержит ровно 1 ключ, title обновился. Схема совпадает с `_REG_FIELD_ORDER` пайплайна (15 полей), `source_file` сохранён (сопоставление `_find_doc_key` не сломано). |
| 25 | Чат одним окном, ввод внизу, уточнение через тот же ввод | PASS | `#chat-messages` + единственные `#chat-input`/`#chat-send` внизу. `#query`/`#answer`/`#reply-row` удалены. `awaitingClarification` роутит query vs reply. |
| 26 | Шапка sticky | PASS | `.site-header{position:sticky;top:0;z-index:50}`; live computed `position:sticky, top:0px`. |
| 27 | Настройки — боковое меню | PASS | 6 пунктов `NAV_SECTIONS` слева, контент справа (grid `220px + 1fr`). Клик переключает видимую секцию (проверено providers→qdrant: visible только qdrant, active-nav=qdrant). |
| 28 | Prefill из `_reg.yaml` без OCR | PASS | Live: `source="reg_yaml"`, `exists=true`, возвращены все 16 ключей (15 полей + slug), `read_reg_record` читает до `extract_first_page` (OCR не вызывается). Повторная загрузка того же файла → `reg_yaml`. |
| 29 | Папка Qdrant персистентна + `--qdrant-path` | PASS (API/UI) | `GET` → `{path,default,overridden}`. `PUT {path}` → `overridden:true`, `QDRANT_PATH` в `.env` (600). `PUT {path:""}` → сброс. `index_document` передаёт `--qdrant-path str(cfg.qdrant_path.resolve())`. Относительный/`../` путь → 400. |

**См. §7** про единственную оговорку по №29 (загрязнение process-env).

---

## 3. Безопасность

- **Path-traversal в поле папки Qdrant**: `PUT /api/settings/qdrant` требует абсолютный
  путь (`is_absolute()`), иначе 400. Проверено live: `relative/path` → 400,
  `../escape` → 400. Значение — конфигурационный параметр (куда читать/писать базу),
  не join с пользовательским вводом поверх песочницы; валидации достаточно для поля
  админ-интерфейса без авторизации (решение №8). PASS.
- **Права не раскрывают секреты**: `.env` и `*.yaml` в `config_dir` = 0600 (проверено
  `stat`), `.bak` = 0600. Раскрытие прав 0666/0777 ограничено каталогами базы
  (`!База_ГОСТ`, `uploads/`), куда конфиги не пишутся. PASS.
- **Маскирование ключей сохранено**: `GET /api/settings/env` возвращает `••••xxxx`
  (live-проверка: все 8 ключей замаскированы). Фильтр `value.startswith("••••")` в
  `PUT /api/settings/env` не затирает реальный ключ маской. PASS.

---

## 4. Регрессии предыдущего реворка (решения 10–19)

Не сломаны:
- №15 (маскирование ключей) — live подтверждено (§3).
- №11 (единая кнопка «Сохранить») — `#settings-save` одна; `saveSettings` собирает все секции.
- №17 (MD-вьювер) — `md-view`/`md-download`/`md-preview` на месте.
- №19 (единый конфиг) — чтение/запись только из `cfg.config_dir`; пайплайнам конфиги через CLI-ключи.
- №10–14, 16, 18 — области/форма/роли/шапка/чистый прод не затронуты изменениями второй итерации (diff затрагивает только `firmware/src/static/*` + backend-точки, перечисленные в отчётах).

---

## 5. Архитектура и тесты

- Реализация соответствует `architecture-report.md` §2–§9: `fs_perms` (§3.1),
  `_child_umask_zero` (§3.2), `config_ui` chmod 0600 (§3.3), upsert `write_reg_yaml`
  (§2), `read_reg_record`/`read_reg_slug` (§4), `qdrant_path`-свойство + endpoints
  (§5.2), интеграция деплоя `config/.env` (§5.4), чат/сайдбар/prefill (§6–§9).
- Тесты осмысленные (ассертят поведение, не снапшоты): `test_perms` (реальные режимы
  через `stat`), `test_registration` (upsert, одна запись, права), `test_app`
  (prefill reg_yaml vs vision, qdrant PUT/валидация), `test_deploy_config` (приоритет
  override), `test_jobs` (preexec_fn + реальный дочерний umask), `test_config_ui`
  (0600 после записи). 62 passed.

---

## 6. Замечания (не блокирующие)

1. **MEDIUM/инфо** — `settings.html` (standalone) продолжает работать: `#settings-root`
   на месте, `window.initSettings = loadSettings`. Регрессии нет.
2. **LOW** — `update_qdrant` принимает абсолютный, но не нормализованный путь
   (`/tmp/../etc` пройдёт). Для LAN-админ-поля без авторизации допустимо; при желании
   — `Path(path).resolve()`.
3. **LOW** — `write_env` (прежнее поведение) отбрасывает строки-комментарии `.env` при
   перезаписи. Не введено этой итерацией.

---

## 7. Загрязнение QDRANT_PATH (кросс-фазный баг, НЕ блокер)

Подтверждено по коду + окружению (не только по отчёту):

- `Build_Search_index/firmware/src/llm_providers.py:93` в `get_api_key()` вызывает
  голый `load_dotenv()`. `find_dotenv()` резолвится в
  `/root/projects/Build_Search_index/.env`, где лежит
  `QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data` (прод-путь).
  `load_dotenv(override=False)` кладёт его в `os.environ` процесса интерфейса.
- `DeployConfig.qdrant_path_override` (фаза 1) приоритизирует process-env над `.env`
  интерфейса → после первого чат-запроса `cfg.qdrant_path` на dev молча становится
  прод-путём; `GET /api/settings/qdrant` отдаёт `overridden:true` без реального
  override в `.env`.

**Дополнительные детали/риски резолвинга для architect-задачи:**

- Фикс «в лоб» со стороны интерфейса (`os.environ.pop("QDRANT_PATH")` после
  ChatSession) **недостаточен**: `load_dotenv()` вызывается в `get_api_key()` на
  каждый резолв ключа в течение той же и последующих чат-сессий и снова установит
  ключ. Потребуется либо поп в самом потоке `qa_graph` (вне границы правок
  интерфейса), либо инверсия приоритета.
- Чистейший интерфейс-вариант — приоритет **`.env` интерфейса над process-env** в
  `qdrant_path_override` (файл интерфейса — источник истины по решению №19), с
  сохранением явного process-env от оператора на старте. Это противоречит текущему
  «process-env > .env» из фазы 1 — потому и отдано архитектору.
- `create_index.py:199` тоже вызывает `load_dotenv()`, но исполняется в **дочернем**
  subprocess (jobs.py), поэтому процесс интерфейса не загрязняет — важен только
  in-process импорт `qa_graph`/`llm_providers`.

Вывод по пункту: UI/API-контракт №29 корректен и работает; баг относится к
кросс-фазному резолвингу `QDRANT_PATH` и уже на отдельном треке. Не дублирую как
блокер этого ревью.

---

## 8. Файлы ревью

- Отчёт: `workflows/t_219236ed/review.md` (этот файл).
- Скрипт live-проверки: `workflows/t_219236ed/live_test.py`.
- Тест-артефакты (uploads/`GOST_9999_test*`, временный `QDRANT_PATH`) удалены;
  `config/.env` возвращён в исходное состояние (0600, без `QDRANT_PATH`).
