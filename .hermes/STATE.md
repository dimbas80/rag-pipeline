<!--
  STATE.md — current snapshot of this project's state.
  Git-tracked. History lives in `git log -- .hermes/STATE.md`, not inline.
-->

# Project State — interface_RAG

_Last updated: 2026-08-27 — by: orchestrator — task: деплой на LXC_

## Working functionality

- Веб-оркестратор (FastAPI + vanilla JS) поверх Create_Markdown_YA и
  Build_Search_index: wizard добавления документа (upload → регистрация →
  convert → index, SSE-логи, Stop), WS-чат с цитатами и картинками,
  настройки провайдеров/ролей/.env.
- 19 тестов passed; PASS получен дважды независимым ревью.
- Развёрнут на LXC `192.0.2.21:80` (systemd `interface-rag.service`,
  prod, write_enabled). Smoke-тест на реальных данных: 33 маршрута,
  10 документов из Qdrant, WS-чат с ответом, картинки/.md отдаются.

## Known issues

- `create_markdown.registration_vision` не настроена в providers.yaml на LXC
  (роль создаётся через UI настроек при сохранении; convert/chat работают и без неё).

## In progress

- (нет)

## Planned / backlog

- Полный e2e через UI/API (upload → convert → index) на реальном PDF.
- Проверить `registration_vision` после первого сохранения ролей в настройках.

## Recent decisions

- Деплой на LXC: пайплайны и web-ui — `/root/RAG/{Create_Markdown_YA,
  Build_Search_index, interface_RAG}`, единый venv `/root/RAG/venv`,
  systemd-юнит `interface-rag.service` (INTERFACE_RAG_ENV=prod, порт 80).
- База знаний `/mnt/sdb/!База_ГОСТ` (Markdown + qdrant_data) подключена
  как есть — ничего не пересоздавалось.
- `providers.yaml` на LXC — полный вариант из Build_Search_index
  (с `build_search_index` ролями), синхронизирован в оба пайплайна.
- `.env` на LXC собран из `Create_Markdown_YA/firmware/src/.env` +
  `SILICONFLOW_API_KEY` из Build_Search_index (7 ключей, права 600).

Полные требования — `docs/product/requirements.md`; решения — `docs/product/decision-log.md`.
