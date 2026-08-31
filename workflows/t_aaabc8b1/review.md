# Review — coder-фикс QDRANT_PATH: убрать process-env, нормализация пути (t_aaabc8b1)

Задача: `t_aaabc8b1` · Вход: архитектура `t_12a1b3fd/architecture-report.md`,
реализация `t_91760d99/implementation-report.md`, контекст бага
`t_842616ab/implementation-report.md` §3 и `t_219236ed/review.md` §7.

## Вердикт

**PASS**

Все 6 пунктов приёмки проверены **по коду и живому запуску** (не по отчёту кодера):
собственный pytest-прогон + собственный live-скрипт, воспроизводящий цепочку бага
через **реальный** `llm_providers.get_api_key()` пайплайна. Блокирующих дефектов нет.

---

## 1. Как проверялось (не по отчёту)

- `python3 -m pytest firmware/tests -q` → **65 passed, 1 warning** (StarletteDeprecation,
  к коду не относится). База 62 → 65.
- `git diff` прочитан целиком: изменены ровно 4 файла (`app.py` +4, `deploy_config.py`
  +6/-7, `test_app.py` +12, `test_deploy_config.py` +47/-4). Границы не нарушены.
- Live-скрипт `/tmp/qdrant_review_live.py`: временный конфиг + интерфейс `.env` без
  `QDRANT_PATH`, затем **настоящий** `import llm_providers` из
  `/root/projects/Build_Search_index` и вызов `get_api_key()` (реальный голый
  `load_dotenv()` нашёл `/root/projects/Build_Search_index/.env` и положил прод-путь в
  `os.environ`), далее проверка `cfg.qdrant_path` + все переходы через `TestClient`.

---

## 2. Соответствие требованиям (по пунктам)

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 1 | `qdrant_path_override` больше НЕ читает `os.environ["QDRANT_PATH"]` — только `read_env_raw(env_file).get("QDRANT_PATH")` | PASS | `deploy_config.py:43` — `return read_env_raw(self.env_file).get("QDRANT_PATH") or None`. Ветка `os.environ.get` удалена (git diff подтверждает). |
| 2 | Заражение `os.environ["QDRANT_PATH"]` не меняет `cfg.qdrant_path`; `GET /api/settings/qdrant` остаётся `overridden:false` без ключа в `.env` интерфейса | PASS | Live: `get_api_key()` реально положил `/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data` в `os.environ`; `override` после заражения = `None`, путь = дефолт; `GET` → `overridden:false` и до, и после. |
| 3 | Обратная связь: `PUT /api/settings/qdrant` реально пишет `QDRANT_PATH` в `.env` (`overridden:true`, `.env`=0600); сброс пустой строкой работает | PASS | Live: PUT абсолютного пути → 200, `overridden:true`, `.env` `QDRANT_PATH=/custom/qdrant/path`, `stat` = `0o600`. PUT `{path:""}` → `overridden:false`, ключ удалён, прочие ключи `.env` сохранены. |
| 4 | `app.py::update_qdrant` нормализует путь (`/tmp/../etc` → `/etc`); штатный абсолютный путь не сломан | PASS | Live: PUT `/tmp/../etc` → 200, в `.env` и в ответе `path = /etc` (после `Path(path).resolve()`). Обычный абсолютный путь проходит без изменений; относительный → 400 (регрессия не сломана). |
| 5 | Регрессия: `INTERFACE_RAG_ENV` в `load()` по-прежнему из process-env; `import os` легитимен | PASS | `deploy_config.py:72` — `os.environ.get("INTERFACE_RAG_ENV", …)` без изменений; `import os` (строка 3) используется. Покрыто `test_load_dev_and_env_override` (зелёный). |
| 6 | `pytest firmware/tests -q` — все зелёные; новые тесты осмысленные | PASS | 65 passed. Новые тесты ассертят поведение, не снапшоты: `test_qdrant_path_ignores_process_env_pollution` (ровно сценарий бага), `test_qdrant_path_survives_load_dotenv` (настоящий `load_dotenv(dotenv_path=…)` + `delenv`), `test_put_qdrant_settings_normalizes_dotdot_path` (нормализация). Переписанный `test_qdrant_path_override_from_process_env` и инвертированный `test_qdrant_path_env_file_beats_process_env` корректны. |

---

## 3. Архитектура и границы

- Порядок резолва после фикса: `.env` интерфейса → `qdrant_path_default`; process-env не
  читается — ровно рекомендация A из архитектуры §2.1. ✅
- Границы соблюдены: `chat_api.py`, `qdrant_api.py`, `config_ui.py` — не изменены;
  пайплайны (`Build_Search_index/**`, `Create_Markdown_YA/**`) — не изменены;
  `.hermes/STATE.md` не тронут (mtime от коммита 046c4fb, не от фикса).
- **Отклонение (обоснованное, не блокер):** архитектура §4.3 перечисляла `app.py`
  среди «менять не нужно», однако пункт 4 приёмки этой задачи явно требует
  нормализацию пути в `update_qdrant`, а prior-review `t_219236ed` §6.2 зафиксировал это
  как LOW. Правка аддитивная (4 строки: `path = str(Path(path).resolve())` после
  `is_absolute()`), резолв-приоритет и `.env`-обработку не меняет. Отклонение
  оправдано приёмкой задачи; обновление архитектуры не требуется (раздел §5.4 о
  «существующих» тестах не нарушен — новый тест аддитивный).

## 4. Безопасность

- Секретов в diff нет. Единственные совпадения по маске: тестовая заглушка
  `DEEPSEEK_API_KEY=sk-x` (заведомо фейковый плейсхолдер) и строка-метка
  `TELEGRAM_BOT_TOKEN` в `settings.js` (UI-лейбл, не значение). Реальные ключи в
  отчётах/коде отсутствуют.
- Нормализация пути через `Path(path).resolve()` (strict=False) не падает на
  несуществующем каталоге и не выводит путь за пределы ФС; это конфигурационный
  параметр админ-поля, не join с пользовательским вводом — валидации достаточно.

## 5. Замечания (не блокирующие)

- Латентный риск §3.3 архитектуры (резолв API-ключей in-process чата не из
  `config/.env`) остаётся — вне объёма, корректно зафлажен на отдельную задачу.
- Смоук проводился на dev-окружении (TestClient), не на живом prod-сервере — допустимо:
  фикс не зависит от окружения (чистая логика резолва), покрыт unit + live.

---

## Файлы ревью

- Отчёт: `workflows/t_aaabc8b1/review.md` (этот файл).
- Live-скрипт: `/tmp/qdrant_review_live.py` (временный конфиг, реальное окружение не
  изменялось; `config/.env` и пайплайны не тронуты).
