# Build_Search_index

QA-система по нормативным документам (ГОСТ, СП, СНиП, СанПиН): гибридный поиск (dense + sparse), реранк, LangGraph-граф с LLM-генерацией ответов и Telegram-бот.

## Установка

```bash
pip install -r firmware/src/requirements.txt
```

Требуется Python ≥ 3.11.

### Переменные окружения

Создать `.env` в корне проекта:

```env
SILICONFLOW_API_KEY=sk-...    # embedding/rerank (согласно providers.yaml)
DEEPSEEK_API_KEY=sk-...       # query_processing (согласно providers.yaml)
TELEGRAM_BOT_TOKEN=123:abc    # только для request_bot.py
QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data  # только для request_bot.py
```

---

## Скрипты

Все скрипты находятся в `firmware/src/`.

### 1. `search.py` — гибридный поиск по Qdrant

Консольный поиск: dense-вектор (SiliconFlow) + sparse-вектор (BM25), реранк (Qwen3-Reranker-8B).

```
python search.py --query "тросовый молниеприемник" --json
```

| Параметр | По умолчанию | Описание |
|---|---|---|
| `--query` | — | Поисковый запрос |
| `--collection` | `technical_standard` | Имя коллекции Qdrant |
| `--retrieve-k` | `30` | Кандидатов из Qdrant |
| `--final-k` | `6` | Результатов после реранка |
| `--qdrant-path` | `./qdrant_data` | Путь к хранилищу Qdrant |
| `--api-key` | из `.env` | API-ключ SiliconFlow |
| `--domain` | — | Фильтр по domain |
| `--document_type` | — | Фильтр по типу документа |
| `--document_id` | — | Фильтр по ID документа |
| `--json` | — | Вывод в JSON |
| `--sources` | — | Только имена документов |
| `--list-collections` | — | Список коллекций |

---

### 2. `create_index.py` — индексация документа в Qdrant

Принимает `_chunks.jsonl` и `_assets.json` (выход пайплайна `Create_Markdown_YA`), создаёт dense + sparse векторы и загружает в Qdrant.

Перед индексацией **удаляет старые точки** документа с тем же `document_id` — переиндексация безопасна.

Основной способ — передать папку документа позиционным аргументом (в ней автоматически
найдутся `*_chunks.jsonl` и `*_assets.json`):

```bash
python3 create_index.py '/mnt/sdb/!База_ГОСТ/Markdown/ГОСТ18410-73_Кабели_с_бумажной_изоляцией' \
    --qdrant-path '/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data' --strict
```

> **Важно:** `--qdrant-path` по умолчанию — относительный `./qdrant_data`. Без явного
> абсолютного пути индекс уходит в dev-копию рядом с cwd, а не в рабочую базу
> (`/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data`, которую читает бот). Переменная
> `QDRANT_PATH` из `.env` этим скриптом НЕ читается (её берёт только `request_bot.py`) —
> всегда передавайте `--qdrant-path` явно.

| Параметр | По умолчанию | Описание |
|---|---|---|
| `input_dir` (позиционный) | — | Папка документа: авто-поиск `*_chunks.jsonl` + `*_assets.json` |
| `--chunks` / `--assets` | — | Альтернатива `input_dir`: явные файлы (взаимоисключающе) |
| `--collection` | авто-выбор | Имя коллекции (единственная в базе подхватится сама) |
| `--qdrant-path` | `./qdrant_data` | Путь к Qdrant — **задавайте абсолютный** (см. выше) |
| `--batch-size` | `16` | Размер батча индексации |
| `--api-key` | из `.env` | API-ключ SiliconFlow |
| `--strict` | — | Прервать при ошибках валидации |

---

### 3. `qa_graph.py` — QA-система (LangGraph)

Граф из 6 узлов: `analyze_query → search → evaluate → generate_answer`, с уточнениями и переформулировкой. Можно использовать как CLI или импортировать `QAGraph` в другие модули.

**CLI-режим:**

```bash
python qa_graph.py --query "Допустимый ток ВВГ 4×120 в земле"
python qa_graph.py --interactive  # диалоговый режим
```

**Импорт (как в request_bot.py):**

```python
from qa_graph import QAGraph, QAGraphConfig

qa = QAGraph(QAGraphConfig(
    qdrant_path="/mnt/sdb/.../qdrant_data",
    search_config_path="firmware/src/search_config.yaml",
    providers_path="firmware/src/providers.yaml",
))
result = qa.run("запрос")
# result = {"final_answer": "...", "search_results": [...], "cited_chunk_ids": [...]}
```

| Параметр CLI | По умолчанию | Описание |
|---|---|---|
| `--query` | — | Один запрос |
| `--interactive` | — | Диалоговый режим |
| `--qdrant-path` | `./qdrant_data` | Путь к Qdrant |
| `--config` | `firmware/src/search_config.yaml` | Параметры узлов search_config.yaml |
| `--providers_config` | `firmware/src/providers.yaml` | Реестр провайдеров и роли |
| `--llm-provider` | из конфига | Провайдер (deepseek, siliconflow) |
| `--llm-model` | из конфига | Модель |
| `--api-key` | из `.env` | API-ключ |
| `--verbose` | — | Подробный вывод шагов |

#### `QAGraphConfig` (dataclass)

| Поле | По умолчанию | Описание |
|---|---|---|
| `qdrant_path` | `./qdrant_data` | Путь к Qdrant |
| `collection` | `technical_standard` | Коллекция |
| `retrieve_k` | `30` | Кандидатов |
| `final_k` | `6` | Результатов после реранка |
| `rrf_threshold` | `0.15` | Порог RRF-фильтра |
| `search_config_path` | `firmware/src/search_config.yaml` | Путь к параметрам узлов |
| `providers_path` | `firmware/src/providers.yaml` | Путь к реестру провайдеров |
| `llm_provider` | `None` | Переопределение провайдера |
| `llm_model` | `None` | Переопределение модели |
| `llm_api_key` | `None` | Переопределение ключа |
| `score_good_threshold` | `0.7` | Порог «хорошего» результата |
| `score_medium_threshold` | `0.4` | Порог «среднего» |
| `max_reformulate_attempts` | `2` | Макс. попыток переформулировки |

---

### 4. `telegram_bot/request_bot.py` — Telegram-бот

Принимает сообщения, вызывает `qa_graph.run()`, возвращает ответ с цитатами и прикрепляет картинки/таблицы **только из процитированных LLM пунктов** (фильтр по `cited_chunk_ids` + интент: «рисунок» → `fig_*`, «таблица» → `table_*`).

```bash
cd telegram_bot && python request_bot.py
```

Требуется `TELEGRAM_BOT_TOKEN` и `QDRANT_PATH` в `.env`. Автоматически перезапускается при сетевых сбоях.

---

## Конфигурация LLM

`search_config.yaml` содержит только параметры узлов графа:

```yaml
nodes:
  analyze_query:    { temperature: 0.0, max_tokens: 256 }
  reformulate_query: { temperature: 0.3, max_tokens: 256 }
  ask_clarification: { temperature: 0.3, max_tokens: 512 }
  generate_answer:  { temperature: 0.0, max_tokens: 2048 }
```

`providers.yaml` содержит реестр провайдеров и назначения ролей. Для поиска используются `roles.build_search_index.query_processing`, `.embedding` и `.rerank`; каждая подроль задаёт `provider` и `model`, а также необязательный `fallback`. `base_url` хранится без endpoint: система добавляет `/chat/completions`, `/embeddings` или `/rerank`.

При недоступности основной модели вызывается fallback. Пустой или отсутствующий fallback означает, что продолжить нельзя и выбрасывается `ProviderUnavailableError`. Ключ разрешается в порядке `--api-key` → переменная из `api_key_env` → `.env`.

---

## Архитектура поиска

```
Qdrant (dense: SiliconFlow embedding, sparse: BM25 fastembed)
   ↓ hybrid_search (RRF-слияние)
   ↓ filter_by_rrf_score (порог 0.15)
   ↓ rerank_siliconflow (Qwen3-Reranker-8B)
   ↓ top-N результатов
```

Индексация: `create_index.py` принимает чанки (JSONL) + ассеты (JSON) от пайплайна `Create_Markdown_YA`, строит dense + sparse векторы, загружает в Qdrant.
