# interface_RAG

Веб-приложение (FastAPI + веб-UI) — лицевая часть RAG-конвейера базы
нормативно-технических документов. Объединяет два соседних пайплайна из
монорепозитория — `create_markdown` (PDF → Markdown) и `build_search_index`
(Markdown → Qdrant + RAG-граф) — под единым интерфейсом: приёмка и
регистрация документов, конвертация, индексация, семантический поиск и чат
по базе.

## Возможности

### Приёмка и регистрация документов
- Загрузка PDF (лимит размера настраивается).
- Авторегистрация: vision-LLM извлекает с титульного листа метаданные
  (`document_id`, `title`, `domain_hint`, `document_type`) — с возможностью
  ручной правки перед записью.

### Конвертация и индексация
- Конвертация PDF → Markdown пайплайном `create_markdown`.
- Индексация Markdown в Qdrant пайплайном `build_search_index`.
- Фоновое выполнение задач: очередь, прогресс, поток событий (SSE),
  остановка выполняющейся задачи.

### Библиотека
- Просмотр сконвертированного Markdown, включая иллюстрации из документов.
- Список документов, уже присутствующих в базе.

### RAG-чат
- Ответы по базе с потоковой отдачей (WebSocket `/ws/chat`).
- Уточняющие вопросы при неоднозначном запросе (interrupt-механизм QAGraph).
- Ответ с цитатами (ID чанков), списком источников и релевантными
  иллюстрациями из документов.

### Настройки через веб-панель
- LLM-провайдеры: список, роли (chat / embedding / rerank / vision),
  проверка доступности, обновление, добавление и удаление.
- Параметры поиска (`search_config.yaml`), конфигурация `create_markdown`,
  переменные окружения (`.env`).
- Qdrant: путь к данным, список коллекций.
- Статус и перезапуск Telegram-бота.

## Состав

| Модуль (`firmware/src/`) | Назначение |
|---|---|
| `app.py` | FastAPI-приложение: REST API, WebSocket чата, раздача веб-UI |
| `chat_api.py` | RAG-сессии поверх `QAGraph` (пайплайн `build_search_index`) |
| `jobs.py` | Фоновые задачи (JobRunner, события задач) |
| `registration.py` | Извлечение метаданных документа vision-LLM |
| `providers_api.py` | Управление LLM-провайдерами и ролями |
| `qdrant_api.py` | Коллекции и документы в Qdrant |
| `config_ui.py` | Настройки (search/create-markdown/env/qdrant) |
| `llm_client.py` | Вызовы LLM |
| `bot_service.py` | Статус/перезапуск Telegram-бота (systemd-юнит) |
| `deploy_config.py` | Профили dev/prod из `config.yaml` |
| `fs_perms.py` | Права доступа к файлам |
| `static/` | Веб-интерфейс (главная страница + панель настроек) |

## Конфигурация

- `config.yaml` — профили **dev** (`127.0.0.1:8081`) и **prod** (`0.0.0.0:80`):
  каталоги конфигурации и загрузок, база Markdown, параметры Qdrant,
  пути к пайплайнам, `.env`.
- `config/providers.yaml` — LLM-провайдеры (общий с пайплайнами).
- `config/search_config.yaml`, `config/create_markdown_config.yaml` —
  параметры поиска и конвертации.
- `scripts/consolidate_config.py` — консолидация общих конфигов из
  соседних проектов монорепозитория.
- `scripts/deploy.sh` — деплой на LXC-хост; параметры подключения — в
  `scripts/deploy.conf` (в git не входит, шаблон: `deploy.conf.example`).

## Запуск (dev)

```bash
pip install -r firmware/src/requirements.txt
python -m uvicorn firmware.src.app:app --host 127.0.0.1 --port 8081
```

Веб-интерфейс: `http://127.0.0.1:8081`.

Тесты:

```bash
pytest firmware/tests
```
