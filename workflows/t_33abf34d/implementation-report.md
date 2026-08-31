# Implementation Report — t_33abf34d

## Summary

Rework: Qdrant переведён в локальный режим (как ChromaDB PersistentClient).
`--qdrant-url` / `QdrantClient(url=...)` заменены на `--qdrant-path`
(по умолчанию `./qdrant_data`) / `QdrantClient(path=...)` в обоих скриптах.
Сервер Qdrant больше не нужен — клиент сам управляет файлами в `./qdrant_data/`.

## Files changed

| File | Change |
|---|---|
| `firmware/src/create_index.py` | `--qdrant-url` → `--qdrant-path` (default `./qdrant_data`); `QdrantClient(path=args.qdrant_path)`; docstring: убрана инструкция «поднять Qdrant: docker run …» |
| `firmware/src/search.py` | `--qdrant-url` → `--qdrant-path` (default `./qdrant_data`); `QdrantClient(path=args.qdrant_path)` |

Больше нигде в `firmware/` нет упоминаний `qdrant-url` / `qdrant_url` /
`http://localhost:6333` (проверено grep'ом).

## Что НЕ менялось

- Вся остальная логика (embedding через SiliconFlow, sparse через fastembed,
  валидация, payload, остальные аргументы) — без изменений.
- `firmware/src/requirements.txt` — без изменений: локальный режим
  поддерживается базовым `qdrant-client` (в 1.18 встроенный `QdrantLocal`,
  проверено).
- `example/ingest.py`, `example/query_rag.py` — вне рамок задачи, не трогались.

## Validation performed

### Автотесты
`python -m pytest firmware/tests/test_search_index.py -q` → **22 passed**.

### End-to-end (полностью локальный режим, без Qdrant-сервера)
- Поднят mock SiliconFlow (`/v1/embeddings`, `/v1/rerank`), т.к. реальный
  `SILICONFLOW_API_KEY` отклоняется API (`401 Api key is invalid`).
- Синтетические данные: 4 чанка, assets.json с таблицей/картинкой.
- `create_index.py --qdrant-path ./qdrant_data` → коллекция `technical_standard`
  создана, 4 чанка проиндексированы, exit 0. В `./qdrant_data/` появились
  `meta.json`, `collection/technical_standard/storage.sqlite`, `.lock` — без сервера.
- `search.py --qdrant-path ./qdrant_data --list-collections` →
  `technical_standard: 4 чанков`.
- `search.py --query "высота молниеотвода"` → реранк работает, релевантный чанк
  (c2, молниезащита) на первом месте, метки качества/бары на месте.
- `--json`, `--sources` — корректный вывод.
- `--help` обоих скриптов: присутствует только `--qdrant-path`, старого
  `--qdrant-url` нет.

## Known limitations

- Живой вызов SiliconFlow не проверялся (ключ невалиден) — контракт проверен
  на mock, как и в предыдущей итерации (t_8f9616fe).

## Unresolved issues

- Нет. Архитектурных замечаний нет: изменение укладывается в ТЗ.
