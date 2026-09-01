# Implementation report — t_28d9833e

## Задача

Исправить RAG v2 в `Create_Markdown_YA`:
1. Уникальность `chunk_id` (повторные top-level `## 1/2/3` перезаписывали друг друга).
2. Наследование `section`/`heading_texts.section` от предыдущей главы (у повторов был чужой `section: 4.7`).
3. Поле `embedding_text`/`embedding_tokens` с контекстом заголовков для embedding-модели (без изменения публичного `text`).

## Изменения

### `firmware/src/pipeline.py`

- **`_build_ancestors()`** — новый top-level `##` теперь сбрасывает `section`/`clause` в `None`
  (раньше искал предыдущие `###`/`####` из старой главы). Поиск `section` для `####`/`#####`
  ограничен текущей главой (останавливается на `##`).
- **`_build_heading_texts()`** — аналогично: для нового `##` `section`/`clause` тексты = `None`,
  поиск `section` ограничен главой.
- **`_build_embedding_text()`** (новый хелпер) — детерминированный формат
  `Заголовок главы/раздела/пункта: ...` + `\n\n{text}`; пустые/`None` заголовки пропускаются.
- **`build_rag_jsonl_v2()`**:
  - счётчик повторов базового `chunk_id`; первый сохраняет ID, повторы получают
    `/occurrence_{N}` (N = 2, 3, …) **до** `/part_{N}` для oversized чанков;
  - новые поля каждой записи: `embedding_text`, `embedding_tokens`;
  - `chunk_tokens` остаётся количеством токенов только `text`;
  - для oversized частей `embedding_text`/`embedding_tokens` строятся отдельно для каждой части.

### `firmware/tests/test_rag_jsonl.py`

- Регрессии иерархии: новый `##` не наследует `section`/`clause` (сценарий СО153: `### 4.7` → `## 1`);
  `####` в новой главе без `###` не наследует section предыдущей главы;
  `heading_texts` для нового `##` сброшены.
- Unit-тесты `_build_embedding_text`: полный путь, короткий section-only, chapter-only, без заголовков.

### `firmware/tests/test_rag_v2.py`

- `test_chunk_id_unique_with_repeated_headings` — повторные `## 1/2` дают
  `len(ids) == len(set(ids))`, первый ID базовый, повтор — `/occurrence_2`, детерминированность.
- `test_chunk_id_occurrence_three` — третий повтор `/occurrence_3`.
- `test_embedding_text_short_section_only` — короткий section-only чанк: глава + раздел + исходный текст.
- `test_embedding_text_oversized_parts` — каждая часть со своими embedding-полями.
- `test_embedding_text_with_occurrence_id` — повторный top-level: occurrence-суффикс, без чужого section.
- `test_every_line_valid_required_fields` дополнен `embedding_text`/`embedding_tokens`.

### Документация

- `docs/architecture/rag-v2-architecture.md` — JSONL-контракт v2: `embedding_text`/`embedding_tokens`,
  occurrence-суффикс `chunk_id`.
- `docs/architecture/decision-records/adr-010-rag-semantic-assets-and-token-chunking.md` —
  дополнены ADR-010 (поле-локаторы) и ADR-010f (шаблон `...[/occurrence_{N}][/part_{N}]`, уникальность).

## Тесты

```
cd firmware && python3 -m pytest tests/test_rag_jsonl.py -q      → 62 passed
cd firmware && python3 -m pytest tests/test_rag_v2.py tests/test_rag_jsonl.py -q → 124 passed
cd firmware && python3 -m pytest tests/ -q --ignore=tests/test_gap_filling.py → 221 passed
```

`test_gap_filling.py` пропущен (pre-existing broken, тестирует несуществующую фичу).

## Проверка на реальном документе

```
python3 firmware/src/pipeline.py -i "/mnt/sdb/!База_ГОСТ/Markdown/СО153-34_21_122-2003 Молниезащита/СО153-34_21_122-2003 Молниезащита.md" \
  --rag --rag-config firmware/src/rag_config.yaml
```

- Исходный `.md` не изменён (mtime не тронут), OCR/AI не запускались.
- JSONL: 30 строк, **30 уникальных `chunk_id`** (было 29 — дубликат `/1`).
- Дубликат `/1` (повторный `## 1. Разработка...`) → `so153_molniezashita/1/occurrence_2`;
  все остальные ID сохранены без изменений.
- У повторов `## 1/2/3` `section: None` (наследованный `4.7` устранён).
- `so153_molniezashita/2.3` (text = 158 символов): `embedding_text` содержит
  «Заголовок главы: 2. ОБЩИЕ ПОЛОЖЕНИЯ» + «Заголовок раздела: 2.3. Параметры токов молнии» + текст;
  `embedding_tokens: 90`, `chunk_tokens: 45`.
- `rag_assets.json`: все ссылки asset → chunk указывают на существующие ID (16 ссылок, пропущенных нет).

## Ограничения

- Повторные `## 2`/`## 3` в рекомендациях не получили occurrence-суффикс, т.к. первые
  `## 2`/`## 3` (основная часть) имеют пустой текст и пропускаются — ID и так уникальны.
