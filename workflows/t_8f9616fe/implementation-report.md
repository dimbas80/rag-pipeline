# Implementation Report — t_8f9616fe

## Summary

Созданы два production-скрипта в `firmware/src/` на основе `example/ingest.py`
и `example/query_rag.py`:

- `firmware/src/create_index.py` — индексация JSONL-чанков (+assets.json) в Qdrant:
  dense-эмбеддинги через SiliconFlow API (`Qwen/Qwen3-Embedding-8B`),
  sparse — локальный `fastembed` (`Qdrant/bm25`).
- `firmware/src/search.py` — гибридный поиск (dense+sparse, RRF-fusion) с реранком
  через SiliconFlow Rerank API (`Qwen/Qwen3-Reranker-8B`).
- `firmware/src/requirements.txt` — зависимости.
- `firmware/tests/test_search_index.py` — 22 pytest-теста (все проходят).

## Files changed

| File | Status |
|---|---|
| `firmware/src/create_index.py` | added |
| `firmware/src/search.py` | added |
| `firmware/src/requirements.txt` | added |
| `firmware/tests/test_search_index.py` | added |

`example/ingest.py` и `example/query_rag.py` **не изменялись**.

## create_index.py

- `load_assets`, `validate_data`, `build_embed_text`, `stable_point_id`,
  структура payload — идентичны `example/ingest.py` (включая `section_path`,
  `row_index` из примера).
- Dense: `POST {SILICONFLOW_BASE_URL}/v1/embeddings`
  `{"model": "Qwen/Qwen3-Embedding-8B", "input": [...], "encoding_format": "float"}`,
  размерность берётся из первого эмбеддинга (probe), коллекция создаётся с
  `dense` (COSINE) + `sparse` векторами.
- Sparse: `SparseTextEmbedding(model_name="Qdrant/bm25")` без изменений.
- Аргументы: `--chunks` (обяз.), `--assets` (обяз.), `--collection`
  (default `technical_standard`), `--qdrant-url`, `--batch-size` (default 16),
  `--strict`, `--api-key`.
- Batch-вставка с прогрессом `tqdm`.
- `--strict` → exit 1 при ошибках валидации; без него — предупреждение и продолжение.
- Retry (3 попытки с backoff) на API-вызовы.

## search.py

- Dense-эмбеддинг запроса через SiliconFlow с инструкцией Qwen3-Embedding
  (`QUERY_INSTRUCTION` — как в `example/query_rag.py`).
- Sparse: локальный `fastembed`.
- Реранк: `POST {SILICONFLOW_BASE_URL}/v1/rerank`
  `{"model": "Qwen/Qwen3-Reranker-8B", "query": ..., "documents": [...], "top_n": ...}`;
  скоры выравниваются по исходному порядку документов.
- Distance-фильтр перед реранком: отсев кандидатов с RRF score < 0.15,
  fallback на сырые данные, если отсеялись все (проверено: фильтр срабатывает,
  в лог пишется `[FILTER] отсеяно N кандидатов`).
- Фильтры по payload: `--domain`, `--document_type`, `--document_id`
  (`models.Filter(must=[...])`, применяется к обоим prefetch).
- Аргументы: `--collection` (default `technical_standard`), `--query`,
  `--retrieve-k` (30), `--final-k` (6), `--qdrant-url`, `--api-key`.
- Режимы вывода (из ChromaDB-скриптов): `--json`, `--sources` (уникальные
  имена документов, без реранка), `--list-collections` (имя: N чанков).
- Relevance-бар (`█` * score) и метки качества `точное`/`хорошее`/`среднее`.

## API-ключ

- Порядок: `--api-key` > `SILICONFLOW_API_KEY` (env) > `.env` через
  `python-dotenv`. Ключи не хардкожены. При отсутствии — exit 2 с сообщением.

## Отклонения от примера/ТЗ (задокументированные решения)

1. **`SILICONFLOW_BASE_URL` env-override.** Базовый URL API читается из env
   (default `https://api.siliconflow.cn`). Нужен для прокси/тестового стенда;
   поведение по умолчанию соответствует ТЗ.
2. **`--list-collections` использует `client.get_collections()`**, т.к. в
   `qdrant-client` 1.18 метода `list_collections()` нет (проверено на реальном
   Qdrant 1.19).
3. **Метки качества** адаптированы: в ChromaDB-скриптах пороги заданы для
   distance (меньше = лучше: 0.22/0.28). Здесь показывается rerank-score
   (больше = лучше), пороги инвертированы: >= 0.78 `точное`, >= 0.72 `хорошее`,
   иначе `среднее`.
4. **RRF-score scale**: Qdrant 1.19 возвращает RRF score = Σ 1/(1+rank) по
   спискам (проверено контрольным экспериментом: top в обоих списках = 1.0,
   rank1 в одном списке = 0.5, rank2 = 0.33...). Порог 0.15 отсекает кандидатов
   глубже ~5-го места в одном списке — осмысленный фильтр слабых кандидатов.

## Validation performed

### Автотесты
`python -m pytest firmware/tests/test_search_index.py -v` → **22 passed**.
Покрытие: validate_data (5 кейсов), build_embed_text, stable_point_id,
build_payload, embed_texts_siliconflow (формат запроса/парсинг/retry),
rerank_siliconflow (формат запроса/выравнивание скоров), embed_query
(инструкция), filter_by_rrf_score (отсев + fallback), build_payload_filter,
quality_label, result_to_dict, --help обоих скриптов.

### End-to-end (mock SiliconFlow + реальный Qdrant)
Так как `SILICONFLOW_API_KEY` из окружения и .env профилей отклоняется
API (`401 "Api key is invalid"` — ключ просрочен/отозван), живой вызов
SiliconFlow невозможен. Вместо этого:

- скачан и запущен реальный Qdrant 1.19.0 (localhost:6333);
- поднят локальный mock SiliconFlow (`/v1/embeddings`, `/v1/rerank`),
  повторяющий документированный контракт API;
- синтетические данные: 2 документа, 7 чанков, таблицы/картинки в assets.json.

Проверено:
1. `create_index.py` индексирует 7 чанков в коллекцию `technical_standard`
   (dense dim=32 из probe, sparse-вектора, payload со всеми полями).
2. `create_index.py --strict` с битыми данными → exit 1; без `--strict` → exit 0;
   без ключа → exit 2.
3. `search.py` по запросу «высота молниеотвода для зоны Б» возвращает
   6 результатов с корректным реранком (doc1-002 на первом месте), метками
   качества и барами.
4. Distance-фильтр: запрос «поливинилхлоридный пластикат...» → отсеяно
   2 кандидата (RRF < 0.15), реранк на оставшихся 5.
5. `--json`, `--sources` (2 уникальных документа), `--list-collections`
   (technical_standard: 7 чанков).
6. Фильтры `--domain=cable`, `--document_id=std-001`, `--document_type=standard`
   возвращают только точки нужного документа.

## Known limitations

- Живой вызов SiliconFlow API не проверен: имеющийся ключ отклоняется
  (`401 Api key is invalid`). Контракт запроса/ответа реализован по
  документации из ТЗ и проверен на mock, повторяющем этот контракт.
- Спасибо `fastembed`-модель `Qdrant/bm25` скачивается с HuggingFace при
  первом запуске (18 файлов) — нужен доступ к HF.

## Unresolved issues

- Нет. Замечаний к архитектуре не возникло.
