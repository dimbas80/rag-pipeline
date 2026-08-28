<!--
  STATE.md — current snapshot of this project's state.
  Git-tracked. History lives in `git log -- .hermes/STATE.md`, not inline.
-->

# Project State — interface_RAG

_Last updated: 2026-08-28 — by: orchestrator — task: итерация 5 (решения 40–46) задеплоена на прод; хвосты прав и orphan-uvicorn закрыты_

## Working functionality

- Веб-оркестратор (FastAPI + vanilla JS) поверх Create_Markdown_YA и
  Build_Search_index: upload → регистрация → convert → index (SSE-логи, Stop),
  WS-чат с цитатами и картинками, настройки провайдеров/ролей/.env.
- Шапка «База данных нормативно-технических документов» (через дефис) + короткий
  подзаголовок; фавикон; MD-вьювер; настройки — единая «Сохранить» +
  «Обновить модели» (реальный опрос провайдеров), fallback по чекбоксу,
  пояснения ролей, параметры графа, ключи маскированы, без дампов.
- Чат: живой стриминг узлов графа по WS + индикатор «Думаю…» (сброс на
  answer/clarification/error); дедуп источников по document_id→title;
  fallback пустого ответа (LLM вернул пустое → честное сообщение, не пустой
  пузырь); max_tokens 4096 (не обрезает длинные ответы deepseek-v4-pro).
- Регистрация: статичная форма, prefill из `_reg.yaml` (без OCR); статус
  active/inactive хранится в `_reg.yaml` как есть, русские подписи только в UI;
  одна запись `{slug: record}` (upsert); тип документа — `<datalist>`
  (пополняемый, свободный ввод); права на файлы в `!База_ГОСТ` 0777/0666.
- Телеграм-бот `interface-rag-bot.service` (systemd): `ExecStart` через
  `/root/RAG/venv`, `EnvironmentFile=/root/RAG/config/.env`, токен и допустимые
  чаты (`TELEGRAM_ALLOWED_USERS`) из единого конфига; поллинг активен.
- Единый общий конфиг: dev `/root/projects/interface_RAG/config`, prod
  `/root/RAG/config` (`providers.yaml`, `create_markdown_config.yaml`,
  `search_config.yaml`, `.env`). Пишет только интерфейс; пайплайнам передаётся
  CLI-ключами (`--providers-config` CMY, `--providers_config` BSI) + инжект
  `.env` в subprocess. Симлинки на проде: BSI `providers.yaml` и
  `search_config.yaml` → `/root/RAG/config/` (импорт-тайм BSI).
- Чат инжектит ключи первой строкой `ChatSession.__post_init__` из
  `config/.env` — чат и subprocess резолвят ключи из одного источника.
- Прод: чистая структура `/root/RAG` — только скрипты, конфиги и симлинки
  (без tests/docs/.worktrees/include/lib/README/example/snapshots, корневых
  `.env` пайплайнов). Развёрнут на LXC `192.0.2.21:80` (systemd
  `interface-rag.service`, prod, write_enabled). Доступ: `sshpass -p root ssh
  root@192.0.2.21`.
- Итерация 5 (решения 40–46): кнопка «Удалить результат» рядом со «Стоп»
  (удаляет `tmp/<stem>/`, `image/`, `table_images.json`; сохраняет `.md`,
  `_reg.yaml`, `_assets.json`, `_chunks.jsonl`); настройки разделены на
  «Провайдеры» (карточки: имя редактируемо с атомарным переименованием
  ссылок ролей, base_url, ключ-маска — значение только в `config/.env`,
  модели; «Удалить» с защитой 409 для занятого ролью; «Обновить модели»
  у каждого провайдера — только его модели) и «Роли» (каскадные селекты
  «Провайдер» → «Модель» с фильтром по тегу роли); «Ключи API провайдеров»
  переехали в карточки провайдеров; индикатор «Думаю…» сбрасывается при
  обрыве/закрытии WS. Тесты: 95/95 (+16 новых).

## Known issues

- Бот отвечает всем в Telegram, пока `TELEGRAM_ALLOWED_USERS` пуст — заполнить
  список допустимых чатов в настройках, затем перезапустить бота.
- LOW (ревью итерации 5, неблокирующие): (а) имя файла с символами вне
  стандартного набора (напр. «ГОСТ (ред.1).pdf») загружается, но «Удалить
  результат»/вьювер .md его не обработают; (б) «Обновить модели»/«Удалить»
  провайдера после переименования до «Сохранить» работают по старому имени.

## In progress

- (нет активных задач)

## Planned / backlog

- Полный e2e на проде на реальном PDF (upload → convert → index → вопрос в чате)
  — выполняет пользователь; пишет в боевой Qdrant.
- Заполнить `TELEGRAM_ALLOWED_USERS` и перезапустить бота (см. Known issues).
- Возможная мини-итерация по двум LOW-замечаниям ревью итерации 5 — по запросу.

## Recent decisions

- Решения 10–19 (1-я итер.), 20–29 (2-я), 30–32 (3-я), 33–39 (4-я),
  40–46 (5-я) — в `docs/product/decision-log.md`.
- Ключевые: 20–29 — статус active/inactive в `_reg.yaml`, права 0777/0666,
  ignore_sections «Предисловие, Содержание», одна запись `{slug: record}`, чат
  одним окном, sticky-шапка, сайдбар (6 разделов), prefill без OCR, папка
  Qdrant через UI; 30–32 — стриминг WS + «Думаю…», дедуп источников, шапка
  «нормативно-технических»; 33–39 — fallback пустого ответа, max_tokens 4096,
  чистый прод (runtime-манифест + cleanup), бот-сервис, раздел «Телеграм» +
  `TELEGRAM_ALLOWED_USERS`, `<datalist>` типа документа, md-переиндексация без
  пересохранения существующего `.md` (фикс на стороне интерфейса).
- 40–46 — «Удалить результат» (`tmp/<stem>/` + `image/` + `table_images.json`;
  `.md`/`_reg`/`_chunks`/`_assets` сохраняются), разделение настроек
  «Провайдеры»/«Роли» с каскадными селектами, ключи только в `.env` (карточка —
  маска+редактирование), атомарное переименование провайдера с переписыванием
  ссылок ролей, «Удалить» провайдера с защитой 409, per-provider
  «Обновить модели», onclose/onerror-сброс «Думаю…».
- Инфраструктура (итерация 5): разовая нормализация прав `!База_ГОСТ` на проде
  выполнена (1234 файла → 0666, каталоги 0777); «хвост» про orphan-uvicorn
  закрыт без действий: порт 8081 — searxng, 8099 — единственный dev-инстанс.
- Инфраструктура: QDRANT_PATH фикс — `deploy_config.py` читает только `.env`
  интерфейса (не `load_dotenv` пайплайнов); унификация ключей чата —
  `_inject_env_file` первой строкой `__post_init__`; консолидация env — 2
  источника (remote `config/.env` → dev `config/.env`), `merge_env` неразрушающий
  (setdefault, первый выигрывает); эталон `providers.yaml` — remote BSI.
- Секреты в git: `.gitignore` = `config/.env`, `*.bak`, `uploads/`; перед
  коммитом контролируется отсутствие `.env`/`.bak`.

Полные требования — `docs/product/requirements.md`; решения — `docs/product/decision-log.md`.
