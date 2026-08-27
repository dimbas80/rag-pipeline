<!--
  STATE.md — current snapshot of this project's state.
  Git-tracked. History lives in `git log -- .hermes/STATE.md`, not inline.
-->

# Project State — interface_RAG

_Last updated: 2026-08-27 — by: orchestrator — task: — (requirements stage)_

## Working functionality

- (ничего — проект на этапе требований, реализация не начата)

## Known issues

- (нет)

## In progress

- (нет)

## Planned / backlog

- Discovery/architecture: схема `<stem>_reg.yaml`, публичный интерфейс `qa_graph.py`,
  определение capabilities моделей при «добавить провайдера».
- Architect → Coder → Reviewer (web-ui поверх CLI обоих пайплайнов).
- Деплой на LXC `192.0.2.21` (порт 80, systemd, smoke-тест).

## Recent decisions

- Имя проекта `interface_RAG`; пайплайны не сливаем — тонкий веб-оркестратор.
- Прод LXC `192.0.2.21:80`, без авторизации; запись в Qdrant — только прод.
- «Добавить документ» — wizard по шагам; «Добавить в базу» только при `.md` +
  `<stem>_reg.yaml`.
- Добавление провайдера → сканирование моделей + теги в `providers.yaml`.
- Основная чат-модель = дефолт для `ai_postprocess` и `query_processing` синхронно.
- «Показать документы в базе» — distinct-метаданные из Qdrant.

Полные требования — `docs/product/requirements.md`; решения — `docs/product/decision-log.md`.
