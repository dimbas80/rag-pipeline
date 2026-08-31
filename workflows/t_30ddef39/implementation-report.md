# Отчёт реализации t_30ddef39

## Сделано
- Добавлен deploy-конфиг dev/prod и загрузчик `DeployConfig` с выбором через `INTERFACE_RAG_ENV`.
- Добавлены базовые модули jobs (argv-only subprocess, лог, SSE queue, stop), config_ui (YAML/.env mask + backup), providers_api (scan/tag/add), llm_client, registration.
- Добавлен FastAPI-каркас с загрузкой документов, регистрацией, конвертацией, job status/events/stop и безопасной отдачей Markdown.
- Добавлена статическая vanilla JS страница с тремя вкладками и wizard-кнопками.

## Проверка
- `python3 -m pytest firmware/tests -q` — 2 passed.
- `python3 -m compileall -q firmware/src` — успешно.

## Ограничения
- Полная интеграция QAGraph, Qdrant API, индексации и settings/chat WebSocket маршрутов требует следующего этапа реализации; текущий каркас не изменяет соседние пайплайны.
