# Implementation report — t_9dabf220

## Summary

Реализован RAG v2 (ADR-010) в `firmware/src/pipeline.py`:
- токен-ориентированное чанкирование через Qwen3-native tokenizer
  (`Qwen/Qwen3-Embedding-8B`, `transformers.AutoTokenizer`, `max_chunk_tokens: 7000`);
- JSONL v2 контракт: `status`/`status_reason`/`replaced_by_document_id`,
  стабильный `chunk_id`, `section_path`, `heading_texts`, `chunk_tokens`,
  `chunking_method`, `assets`, публичный `source` без `page` (+ технический `_source_page`);
- реестр активов `rag_assets.json` (таблицы и изображения из итогового Markdown,
  относительные пути к `image/`, связь с реальными `chunk_id`);
- трёхэтапный CLI: `.md` внутри `Markdown/` + `--rag` = только индексация
  (без OCR/AI/извлечения); таблицы ВСЕГДА вырезаются в `image/` для PDF/DOCX
  (без `--ai`), `--ai` добавляет только vision/AI-коррекцию;
- атомарная запись `safe_write()` (temp-file + `os.replace()`) и атомарная
  перезапись `rag_chunks.jsonl` + `rag_assets.json` (`.md`/`image/` не трогаются);
- устранён риск вложенного `Markdown/<file>/Markdown/<file>`.

## Файлы изменены

| Файл | Действие | Описание |
|------|----------|----------|
| `firmware/src/pipeline.py` | Изменён | Секции 12 (обновлена), 13 (новая: tokenizer), 14 (новая: asset registry), 15 (новая: orchestrator); обновлён `process_file()`/`parse_args()`/`safe_write()` |
| `firmware/src/rag_config.yaml` | Изменён | `max_chunk_tokens: 7000`, `tokenizer: Qwen/Qwen3-Embedding-8B`, `allow_degraded_fallback`, `status`-поля, `replaced_by_document_id` (официальный номер), `replaced_by_doc_key`; добавлены документы `sp89_kotelnye`, `old_snip` |
| `firmware/src/test_rag_jsonl.py` | Изменён | Конфиг-тесты v2, `.md --rag` без `source.page`, mock `_init_tokenizer` |
| `firmware/src/test_rag_v2.py` | **Новый** | 57 тестов: tokenizer, degraded, JSONL v2, assets, classify, атомарность, безопасный `--rag` |
| `firmware/src/requirements.txt` | **Новый** | Зависимости + `transformers>=4.51.0` |
| `README.md` | Изменён | RAG v2 документация, трёхэтапный CLI, формат выхода |

## Реализация

### 1. Токенизатор (секция 13)

`_init_tokenizer(config) -> tuple[Callable, str]`:
- Основной путь: `transformers.AutoTokenizer.from_pretrained("Qwen/Qwen3-Embedding-8B", revision="main", local_files_only=False, trust_remote_code=False)` → `(lambda text: len(tokenizer.encode(text)), "qwen3")`
- `ImportError`/ошибка загрузки: `log.error`, затем
  - `allow_degraded_fallback: true` → `(chars/ratio, "degraded_chars_per_token")` + `log.warning`
  - иначе → `RuntimeError` (RAG отказывается, без молчаливого fallback)
- Никакого tiktoken/cl100k_base в коде.

`_split_oversized_clause_tokens(text, max_tokens, tokenize, overhead=200)`:
- лимит в токенах (`max_tokens - overhead`), атомарные блоки (таблицы/код),
  гигантский блок публикуется как есть с `log.warning` (ADR-010g).

### 2. JSONL v2 (секция 12, `build_rag_jsonl_v2`)

Каждая строка:
```json
{"document_id": "...", "title": "...", "status": "active",
 "chunk_id": "so153_molniezashita/3.2.1",
 "chapter": "3", "section": "3.2", "clause": "3.2.1",
 "section_path": "3 → 3.2 → 3.2.1",
 "heading_texts": {"chapter": "...", "section": "...", "clause": "..."},
 "text": "...", "source": {"file": "..."}, "_source_page": 7,
 "assets": ["so153_molniezashita/table/1"], "references": [...],
 "chunk_tokens": 1450, "chunking_method": "qwen3"}
```
- `status` из `documents.<slug>.status` или `default_status` (warning при отсутствии);
- `chunk_id`: `{doc_key}/{clause_number}`; ненумерованный → `{doc_key}/_h{ordinal}`;
  разбитый → `/part_{N}` + суффикс «(ч. N)»;
- `source.page` удалён, `_source_page` остаётся (int|null);
- таблицы остаются в `text` из итогового Markdown.

`validate_rag_config()`: max_chunk_tokens int 100..100000; status active/inactive;
`status=active` требует null для `status_reason`/`replaced_by_document_id`/`replaced_by_doc_key`.
`load_rag_config()` логирует warnings валидации.

### 3. Asset Registry (секция 14)

- `_extract_tables_from_md()`: детект `|...|` + `|---|`, caption («Таблица N» до/после),
  `md_lines` [start, end), `row_count`, `image_path: image/table_N.png`;
- `_extract_images_from_md()`: `![caption](image/...)`;
- `_build_asset_registry()`: asset_id `{doc_slug}/table/{N}` / `{doc_slug}/fig/{N}`,
  пути относительные к `image/`, проверка существования файлов (нет файла → пропуск с warning;
  нет `image/` → пустой реестр);
- `_link_assets_to_chunks()`: таблица связывается по Markdown-блоку/подписи в тексте чанка,
  изображение — по `image_path`/ссылке; двунаправленно: `asset.chunk_ids` и `chunk.assets`;
- `write_rag_assets()`: атомарная запись, приватное поле `_md_block` не попадает в JSON.

### 4. RAG Pipeline Orchestrator (секция 15)

- `_classify_input()`: pdf/docx/md_standalone/md_rag/unknown;
- `_run_rag_only(md_path, rag_config)`: `.md` внутри `Markdown/` — только чтение MD,
  `_find_doc_key`, `run_rag_pipeline`, ничего не пересоздаёт (OCR/AI/вырезка не вызываются);
- `run_rag_pipeline()`: tokenizer → `build_rag_jsonl_v2` → assets → `_link_assets_to_chunks`
  → атомарная перезапись `rag_chunks.jsonl` + `rag_assets.json`;
- `_write_rag_jsonl()`: обёртка для PDF/DOCX-пути (вызывает `run_rag_pipeline`).

### 5. CLI / process_file

- `md_rag` (`.md` внутри `Markdown/`): без `--rag` → ошибка; с `--rag` → `_run_rag_only()`
  (пишет производные файлы рядом с MD, не создаёт `Markdown/<file>/Markdown/...`);
- PDF/DOCX: таблицы ВСЕГДА вырезаются через `extract_table_images()` (PyMuPDF, локально),
  vision/AI — только с `--ai`;
- `safe_write()`: temp-file + `os.replace()` (атомарно, fsync, cleanup tmp при ошибке).

### 6. Устранение вложенного Markdown/<file>/Markdown/<file>

Раньше `.md` внутри `Markdown/` попадал в общий путь OCR → создавал
`Markdown/<file>/Markdown/<file>`. Теперь `_classify_input()` распознаёт такой вход как
`md_rag` и направляет в `_run_rag_only()`, который пишет `rag_chunks.jsonl`/`rag_assets.json`
в директорию рядом с `.md` (без создания новых подпапок). Подтверждено E2E и тестом
`test_md_inside_markdown_with_rag_no_nesting`.

## Тесты

### Новые (test_rag_v2.py, 57 тестов)
- `TestInitTokenizer`: qwen3 available/unavailable, degraded fallback, import error;
- `TestSplitOversizedClauseTokens`: токен-лимит, таблицы/код атомарны, гигантский блок;
- `TestValidateRagConfig`: max_tokens, status, `replaced_by_document_id` (официальный номер);
- `TestBuildRagJsonlV2`: схема (нет source.page, есть status/chunk_id/section_path/heading_texts/
  chunk_tokens/chunking_method), oversized `/part_N`, degraded method, unnumbered fallback;
- `TestAssetRegistry`: tables/images, caption, md_lines, linkage, missing image skip, no-image-dir;
- `TestClassifyInput`;
- `TestRunRagOnly`: `.md --rag` не меняет hash MD/image, идемпотентность, атомарность (нет .tmp),
  no-doc-key, no-image-dir;
- `TestRunRagPipeline`: JSONL+assets записаны, linkage, missing tokenizer → RuntimeError;
- `TestProcessFileTableExtraction`: PDF без `--ai` вырезает таблицы, `--ai` добавляет vision,
  `.md` внутри Markdown/ требует `--rag`, без вложенного Markdown/;
- `TestSafeWriteAtomic`: атомарная запись.

### Обновлены (test_rag_jsonl.py, 54 теста)
- `test_load_rag_config_ok`: v2 поля (max_chunk_tokens, tokenizer, status);
- `test_load_rag_config_inactive_document`: `replaced_by_document_id` = официальный номер;
- `.md --rag` интеграция: проверка v2-контракта без `source.page`; mock `_init_tokenizer`
  для детерминированности.

### Прогон
```
python3 -m pytest -q --ignore=test_gap_filling.py
208 passed in ~15s
```
(`test_gap_filling.py` — пре-существующий сломанный файл, тестирует несуществующую функцию;
не связан с этой задачей.)

## E2E (реальный документ)

Команда:
```bash
python3 pipeline.py -i "/mnt/sdb/!База_ГОСТ/Markdown/СО153-34_21_122-2003 Молниезащита/СО153-34_21_122-2003 Молниезащита.md" --rag --rag-config rag_config.yaml
```

Результат (фактический):
- `rag_chunks.jsonl`: 30 строк, все валидные JSON; `source` без `page`;
  `chunking_method: qwen3` (реальный Qwen3-токенизатор), `status: active`;
  max `chunk_tokens`: 3468 (≤ 7000); пример: `chunk_id so153_molniezashita/2.3.3`,
  `section_path 2 → 2.3 → 2.3.3`;
- `rag_assets.json`: 14 изображений, все `image_path` существуют, все связаны
  с `chunk_id` (referenced ⊆ registered, missing = NONE);
- таблицы в этом старом MD не вырезаны (нет `image/table_N.png` — документ собран
  до v2) → пропущены с `log.warning` по архитектуре §9; на новых PDF/DOCX таблицы
  вырезаются всегда;
- `.md` и `image/` не модифицированы (md5 до/после совпадают);
- повторный запуск идемпотентен (30 строк, те же chunk_id).

PDF-путь с реальным Yandex JSON (tmp/yandex_result.json):
- `_source_page` проставлен из headings (30/30), публичный `source` чист.

## Ограничения / известные особенности

1. `test_gap_filling.py` — пре-существующие 8 fail (не относится к задаче).
2. Таблицы в `rag_assets.json` регистрируются только если файл `image/table_N.png`
   существует (архитектура §9: пропуск с warning). Для старых документов, собранных
   до v2, табличные активы будут пусты; повторный прогон PDF/DOCX создаст их.
3. `build_rag_jsonl_v1()` сохранён под псевдонимом `build_rag_jsonl` для обратной
   совместимости (ADR-010 §7.5); новый код использует `build_rag_jsonl_v2()`.
4. Токенизатор скачивается с HuggingFace Hub при первом запуске (~5 МБ);
   для офлайн-режима после первой загрузки можно использовать `local_files_only=True`
   (задокументировано в ADR-010, в коде — параметр `tokenizer_revision`).

## Отклонения от архитектуры

Отклонений нет. Реализация следует `docs/architecture/rag-v2-architecture.md`
(секции 12–15) и `docs/architecture/decision-records/adr-010-*.md`, включая
уточнение `replaced_by_document_id` = официальный номер документа-преемника
(не slug) из t_8e1044ee.
