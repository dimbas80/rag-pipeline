<!--
  STATE.md — current snapshot of this project's state.
  Git-tracked. History lives in `git log -- .hermes/STATE.md`, not inline.
-->

# Project State — interface_RAG

_Last updated: 2026-08-27 — by: orchestrator — task: реворк после приёмки завершён и задеплоен_

## Working functionality

- Веб-оркестратор (FastAPI + vanilla JS) поверх Create_Markdown_YA и
  Build_Search_index: upload → регистрация → convert → index (SSE-логи, Stop),
  WS-чат с цитатами и картинками, настройки провайдеров/ролей/.env.
- Реворк после приёмки (решения 10–19) реализован: два независимых PASS-ревью,
  33 теста passed, задеплоен на LXC. Шапка/фавикон/подзаголовок; регистрация —
  статичная форма со всеми полями и комментариями; MD-вьювер; настройки —
  единая «Сохранить» + «Обновить модели» (реальный опрос провайдеров), fallback
  по чекбоксу, пояснения ролей, параметры графа, ключи маскированы, без дампов.
- Единый общий конфиг: dev `/root/projects/interface_RAG/config`, prod
  `/root/RAG/config` (`providers.yaml`, `create_markdown_config.yaml`,
  `search_config.yaml`, `.env`). Пишет только интерфейс; пайплайнам передаётся
  CLI-ключами (`--providers-config` CMY, `--providers_config` BSI) + инжект
  `.env` в subprocess. На проде симлинк BSI `providers.yaml → /root/RAG/config/`
  (импорт-тайм загрузка в Build_Search_index).
- Прод: чистая структура `/root/RAG` (пайплайны + веб + config/, без docs/tests/
  .git/.hermes/.env в папках). Развёрнут на LXC `192.0.2.21:80` (systemd
  `interface-rag.service`, prod, write_enabled). Доступ: `sshpass -p root ssh
  root@192.0.2.21`.

## Known issues

- (нет — реворк закрыл предыдущие)

## In progress

- (нет активных задач)

## Planned / backlog

- Полный e2e на проде на реальном PDF (upload → convert → index → вопрос в чате)
  — пишет в боевой Qdrant; выполняется по явному запросу пользователя.
- Прибрать orphan uvicorn на dev (порты 8099/8081) при следующем деплое.

## Recent decisions

- Решения 10–19 в `docs/product/decision-log.md`: регистрация областью (не
  wizard), единый «Сохранить» + «Обновить», fallback по чекбоксу, пояснения
  ролей, параметры графа, без дампов конфигов, фавикон/шапка, MD-вьювер,
  чистая структура прода, единый общий конфиг через CLI-ключи.
- Единый конфиг — один каталог, пишет только интерфейс; эталон `providers.yaml`
  — remote BSI (чтобы 222 модели из теста «Обновить» не утекли на прод).
- Секреты в git: `.gitignore` = `config/.env`, `*.bak`, `uploads/`; перед
  коммитом контролируется отсутствие `.env`/`.bak`.

Полные требования — `docs/product/requirements.md`; решения — `docs/product/decision-log.md`.
