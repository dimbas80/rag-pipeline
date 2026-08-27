<!--
  STATE.md — current snapshot of this project's state.
  Git-tracked. History lives in `git log -- .hermes/STATE.md`, not inline.
-->

# Project State — interface_RAG

_Last updated: 2026-08-27 — by: orchestrator — task: реворк после приёмки (единый конфиг + UI)_

## Working functionality

- Веб-оркестратор (FastAPI + vanilla JS) поверх Create_Markdown_YA и
  Build_Search_index: upload → регистрация → convert → index (SSE-логи, Stop),
  WS-чат с цитатами и картинками, настройки провайдеров/ролей/.env.
- Развёрнут на LXC `192.0.2.21:80` (systemd `interface-rag.service`, prod,
  write_enabled). Доступ к проду: `sshpass -p root ssh root@192.0.2.21`.

## Known issues (устранение — текущий реворк)

- Прод содержит полную структуру проекта (`docs/`, `tests/`, `.pytest_cache/`) —
  нужны только пайплайны + веб + общие конфиги.
- `providers.yaml` продублирован в обоих пайплайнах, `.env` — в 4 местах; нужен
  единый общий конфиг, передаваемый пайплайнам CLI-ключами.
- UI: нет фавиконки/названия/подзаголовка; регистрация — disabled-кнопки без
  формы с комментариями; нет MD-вьювера; настройки показывают сырые дампы
  конфигов, 5 кнопок «Сохранить», fallback без чекбокса, нет пояснений ролей,
  нет параметров графа, «Обновить» не опрашивает провайдеров.

## In progress

- Реворк: architect (`t_1247387a`) → coder фаза 1 конфиг/деплой (`t_7ee33160`)
  → coder фаза 2 UI (`t_14fb20d5`) → reviewer (`t_a88ac826`).

## Planned / backlog

- Передеплой на LXC после PASS (реальный деплой выполняет оркестратор).
- Smoke-тест на реальных данных после передеплоя.

## Recent decisions

- Решения 10–19 в `docs/product/decision-log.md`: регистрация областью (не
  wizard), единый «Сохранить» + «Обновить», fallback по чекбоксу, пояснения
  ролей, параметры графа, без дампов конфигов, фавикон/шапка, MD-вьювер,
  чистая структура прода, единый общий конфиг через CLI-ключи.

Полные требования — `docs/product/requirements.md`; решения — `docs/product/decision-log.md`.
