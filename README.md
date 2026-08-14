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
SILICONFLOW_API_KEY=sk-...    # для embedding/rerank (search.py, create_index.py)
DEEPSEEK_API_KEY=sk-...       # для LLM-чата (qa_graph.py, bot.py)
TELEGRAM_BOT_TOKEN=123:abc    # только для bot.py
QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data  # только для bot.py
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

```bash
python create_index.py \
  --chunks "СО153_Молниезащита_chunks.jsonl" \
  --assets "СО153_Молниезащита_assets.json" \
  --qdrant-path "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data"
```

| Параметр | По умолчанию | Описание |
|---|---|---|
| `--chunks` (обязательный) | — | JSONL с чанками |
| `--assets` (обязательный) | — | JSON с assets.json |
| `--collection` | `technical_standard` | Имя коллекции |
| `--qdrant-path` | `./qdrant_data` | Путь к Qdrant |
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

**Импорт (как в bot.py):**

```python
from qa_graph import QAGraph, QAGraphConfig

qa = QAGraph(QAGraphConfig(
    qdrant_path="/mnt/sdb/.../qdrant_data",
    llm_config_path="firmware/src/llm_config.yaml",
))
result = qa.run("запрос")
# result = {"final_answer": "...", "search_results": [...], "cited_chunk_ids": [...]}
```

| Параметр CLI | По умолчанию | Описание |
|---|---|---|
| `--query` | — | Один запрос |
| `--interactive` | — | Диалоговый режим |
| `--qdrant-path` | `./qdrant_data` | Путь к Qdrant |
| `--llm-config` | `firmware/src/llm_config.yaml` | Конфиг LLM-провайдеров |
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
| `llm_config_path` | `firmware/src/llm_config.yaml` | Путь к YAML-конфигу LLM |
| `llm_provider` | `None` | Переопределение провайдера |
| `llm_model` | `None` | Переопределение модели |
| `llm_api_key` | `None` | Переопределение ключа |
| `score_good_threshold` | `0.7` | Порог «хорошего» результата |
| `score_medium_threshold` | `0.4` | Порог «среднего» |
| `max_reformulate_attempts` | `2` | Макс. попыток переформулировки |

---

### 4. `telegram_bot/bot.py` — Telegram-бот

Принимает сообщения, вызывает `qa_graph.run()`, возвращает ответ с цитатами и прикрепляет картинки/таблицы **только из процитированных LLM пунктов** (фильтр по `cited_chunk_ids` + интент: «рисунок» → `fig_*`, «таблица» → `table_*`).

```bash
cd telegram_bot && python bot.py
```

Требуется `TELEGRAM_BOT_TOKEN` и `QDRANT_PATH` в `.env`. Автоматически перезапускается при сетевых сбоях.

---

## Конфигурация LLM: `llm_config.yaml`

```yaml
default_provider: deepseek       # провайдер по умолчанию
default_model: deepseek-v4-flash # модель по умолчанию

providers:
  deepseek:
    base_url: https://api.deepseek.com/v1/chat/completions
    api_key_env: DEEPSEEK_API_KEY  # имя переменной в .env
    models:
      - deepseek-v4-flash
      - deepseek-v4-pro
  siliconflow:
    base_url: https://api.siliconflow.com/v1/chat/completions
    api_key_env: SILICONFLOW_API_KEY
    models:
      - Qwen/Qwen3.5-35B-A3B
      - Qwen/Qwen3-32B

nodes:                            # параметры узлов графа
  analyze_query:    { temperature: 0.0, max_tokens: 256 }
  reformulate_query: { temperature: 0.3, max_tokens: 256 }
  ask_clarification: { temperature: 0.3, max_tokens: 512 }
  generate_answer:  { temperature: 0.0, max_tokens: 2048 }
```

Можно добавить любого OpenAI-совместимого провайдера, указав `base_url`, `api_key_env` и список `models`.

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
