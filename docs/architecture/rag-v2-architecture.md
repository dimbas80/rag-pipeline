# RAG v2 Architecture: Семантические активы и токен-ориентированное чанкирование

**Статус:** Предложено (архитектура, исправлено)
**Дата:** 2026-08-05
**ADR:** [ADR-010](decision-records/adr-010-rag-semantic-assets-and-token-chunking.md)
**На основе:** ADR-009, `firmware/src/pipeline.py` (секция 12)

> **Исправления (2026-08-05):**
> - Токенизатор заменён с `tiktoken cl100k_base` на `Qwen/Qwen3-Embedding-8B` через `transformers.AutoTokenizer`
> - Запрещён молчаливый chars/token fallback — только явный деградированный режим
> - Уточнена семантика `--rag`: перезаписывает `rag_chunks.jsonl` + `rag_assets.json`, не трогает `.md`/`image/`
> - Таблицы всегда вырезаются в `image/` на этапе 1 (без `--ai`); `--ai` управляет только vision/AI-коррекцией

---

## 1. Обзор

RAG v2 расширяет существующий RAG-контур (ADR-009, секция 12 pipeline.py) следующими возможностями:

1. **Токен-ориентированное чанкирование** (Qwen3-Embedding-8B native tokenizer, `max_chunk_tokens: 7000`)
2. **Статус документа** (`active`/`inactive`) в `rag_config.yaml` и каждой JSONL-строке
3. **Удаление номеров страниц** из публичного JSONL (сохранение как `_source_page`)
4. **Реестр активов** `rag_assets.json` — таблицы и изображения с привязкой к чанкам
5. **Трёхэтапный CLI** с гарантией безопасности `--rag` (без повторного OCR, перезаписывает только производные файлы)
6. **Стабильные chunk_id** не зависящие от страниц PDF

---

## 2. Целевая архитектура

### 2.1 Декомпозиция модулей

```
pipeline.py
├── ...
├── 11. CLI & Main                         (существующий, обновлён)
├── 12. RAG JSONL Converter v1             (существующий, обновлён)
│   ├── load_rag_config()                  (обновлён: валидация status)
│   ├── _extract_heading_number()
│   ├── _build_ancestors()                 (обновлён: heading_texts)
│   ├── parse_md_structure()
│   ├── extract_clause_text()
│   ├── extract_references()
│   ├── _get_page_for_heading()            (обновлён: возвращает _source_page)
│   ├── _split_oversized_clause()          (обновлён: token-based)
│   └── build_rag_jsonl()                  (обновлён: новый контракт)
│
├── 13. Tokenizer Chunking                 (НОВЫЙ, ~70 строк)
│   ├── _init_tokenizer(config) → Callable   # загрузка Qwen3 tokenizer через transformers
│   ├── _load_qwen_tokenizer(model_id, revision, offline) → AutoTokenizer
│   └── _degraded_tokenizer(ratio) → Callable  # явный деградированный fallback
│
├── 14. Asset Registry                     (НОВЫЙ, ~80 строк)
│   ├── _extract_tables_from_md(md_text) → list[dict]
│   ├── _extract_images_from_md(md_text) → list[dict]
│   ├── _build_asset_registry(md_text, doc_slug, img_dir) → dict
│   ├── _link_assets_to_chunks(assets, chunks) → None
│   └── write_rag_assets(assets, output_path) → None
│
├── 15. RAG Pipeline Orchestrator          (НОВЫЙ, ~80 строк)
│   └── run_rag_pipeline(md_text, rag_config, doc_key, img_dir, out_dir) → bool
│       ├── _init_tokenizer(config)
│       ├── build_rag_jsonl_v2(md_text, None, rag_config, doc_key, tokenizer) → str
│       ├── _build_asset_registry(md_text, doc_key, img_dir) → dict
│       ├── _link_assets_to_chunks(assets, chunks)
│       └── write_rag_assets(assets, output_path)
```

### 2.2 Обновлённый JSONL-контракт

**Было (v1, ADR-009):**
```jsonl
{"document_id":"СО 153-34.21.122-2003","title":"...","chapter":"3","section":"3.2","clause":"3.2.1","text":"...","source":{"file":"...","page":7},"references":["п. 3.2.1"]}
```

**Стало (v2, ADR-010):**
```jsonl
{"document_id":"СО 153-34.21.122-2003","title":"...","status":"active","chunk_id":"so153/3.2.1","chapter":"3","section":"3.2","clause":"3.2.1","section_path":"3 → 3.2 → 3.2.1","heading_texts":{"chapter":"3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ","section":"3.2. Внешняя молниезащитная система","clause":"3.2.1. Молниеприемники"},"text":"...","embedding_text":"Заголовок главы: 3. ...\nЗаголовок раздела: 3.2. ...\nЗаголовок пункта: 3.2.1. ...\n\n...","embedding_tokens":1560,"source":{"file":"..."},"_source_page":7,"assets":["so153/table/1"],"references":["п. 3.2.1"],"chunk_tokens":1450,"chunking_method":"qwen3"}
```

**Изменения полей:**

| Поле | v1 | v2 | Примечание |
|------|-----|-----|------------|
| `status` | — | `"active" \| "inactive"` | Из rag_config |
| `chunk_id` | — | `"so153/3.2.1"` | Стабильный ID; повторы → `.../occurrence_{N}` |
| `section_path` | — | `"3 → 3.2 → 3.2.1"` | Человекочитаемый путь |
| `heading_texts` | — | `{chapter, section, clause}` | Названия (не номера) |
| `embedding_text` | — | `"Заголовок главы: ...\n...\n\n{text}"` | Заголовки + text для embedding-модели |
| `embedding_tokens` | — | `1560` | Токены фактического `embedding_text` |
| `source.page` | `7` | **Удалено** | Заменено на `_source_page` |
| `_source_page` | — | `7` | Приватное, для отладки |
| `assets` | — | `["so153/table/1"]` | Ссылки на активы |
| `chunk_tokens` | — | `1450` | Токены только `text` (не `embedding_text`) |
| `chunking_method` | — | `"qwen3" \| "degraded_chars_per_token"` | Метод подсчёта токенов |
| `max_chunk_chars` (конфиг) | `1500` | **Удалено** | Заменено на `max_chunk_tokens` |
| `max_chunk_tokens` (конфиг) | — | `7000` | Новый лимит |

**`embedding_text` — контракт:**
- Поле добавляется в каждую запись v2, публичное поле `text` не изменяется.
- Формат детерминированный: непустые заголовки из `heading_texts` в порядке
  `chapter → section → clause` (префиксы `Заголовок главы/раздела/пункта:`),
  затем пустая строка и исходный `text`. Пустые/`None` заголовки пропускаются.
- Для oversized чанков строится отдельно для каждой части `/part_{N}`:
  те же заголовки + текст конкретной части.
- `embedding_tokens` — токены `embedding_text` (лимит embedding-модели);
  `chunk_tokens` остаётся количеством токенов только `text`.

**`chunk_id` — occurrence-суффикс (уникальность):**
- Базовый ID `{doc_slug}/{clause_number}` (или `/_h{ordinal}`) сохраняется
  для первого вхождения. Повторный numbered top-level блок (напр. «## 1/2/3»
  в разделе рекомендаций после «## 1. ВВЕДЕНИЕ») получает суффикс
  `/occurrence_{N}` (N = 2, 3, …) ДО `/part_{N}` для oversized чанков.
- ID уникальны в пределах одного результата `build_rag_jsonl_v2()`,
  детерминированы (зависят только от порядка заголовков в документе),
  случайные UUID не используются.

### 2.3 rag_assets.json — контракт

```json
{
  "document_id": "СО 153-34.21.122-2003",
  "doc_slug": "so153_molniezashita",
  "generated_at": "2026-08-05T12:00:00Z",
  "image_dir": "image",
  "assets": {
    "tables": [
      {
        "asset_id": "so153/table/1",
        "asset_type": "table",
        "caption": "Таблица 3.1 — Значения сопротивления заземлителей",
        "md_lines": [245, 278],
        "image_path": "image/table_1.png",
        "row_count": 12,
        "chunk_ids": ["so153/3.1"]
      }
    ],
    "images": [
      {
        "asset_id": "so153/fig/1",
        "asset_type": "image",
        "caption": "Рис. 1 — Зона защиты одиночного стержневого молниеотвода",
        "image_path": "image/fig_1.png",
        "md_line": 312,
        "chunk_ids": ["so153/3.2.1"]
      }
    ]
  }
}
```

**Поля asset (общие):**
| Поле | Тип | Описание |
|------|------|----------|
| `asset_id` | string | Уникальный ID: `"{doc_slug}/{type}/{N}"` |
| `asset_type` | string | `"table"` или `"image"` |
| `caption` | string | Подпись из Markdown (после таблицы/изображения) |
| `image_path` | string | Относительный путь к файлу изображения |
| `chunk_ids` | [string] | Список chunk_id, в которых упоминается актив |

**Специфичные поля для таблиц:**
| Поле | Тип | Описание |
|------|------|----------|
| `md_lines` | [int, int] | Диапазон строк таблицы в .md файле [start, end) |
| `row_count` | int | Количество строк данных (без заголовка) |

**Специфичные поля для изображений:**
| Поле | Тип | Описание |
|------|------|----------|
| `md_line` | int | Номер строки ссылки на изображение в .md |

**Разрешение путей:**
- Все `image_path` — относительно директории `rag_assets.json`
- Приложение читает `image_dir` из реестра и разрешает пути: `Path(assets_dir) / asset["image_path"]`

---

## 3. Алгоритм токен-ориентированного чанкирования

### 3.1 Инициализация токенизатора

Токенизатор загружается через `transformers.AutoTokenizer` — загружаются **только файлы
токенизатора** (~5 МБ), без весов модели (8B параметров, ~16 ГБ).

```python
def _init_tokenizer(config: dict) -> tuple[Callable[[str], int], str]:
    """Фабрика токенизатора.

    config['defaults'] должен содержать:
      - tokenizer: str            (HF model id, "Qwen/Qwen3-Embedding-8B")
      - tokenizer_revision: str   (опционально, "main")
      - allow_degraded_fallback: bool (default False)
      - tokenizer_fallback_ratio: float (default 3.5, только при allow_degraded_fallback)

    Returns:
        (tokenize_fn, chunking_method)
        tokenize_fn: text → token_count
        chunking_method: "qwen3" | "degraded_chars_per_token"
    """
    model_id = config.get("defaults", {}).get("tokenizer", "Qwen/Qwen3-Embedding-8B")
    revision = config.get("defaults", {}).get("tokenizer_revision", "main")

    # Попытка 1: transformers + HuggingFace
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            local_files_only=False,   # первый раз: скачать
            trust_remote_code=False,
        )
        log.info(f"Токенизатор: transformers/{model_id} (загружен)")
        return (lambda text: len(tokenizer.encode(text)), "qwen3")
    except ImportError:
        log.error(
            "transformers не установлен — RAG-индексация невозможна. "
            "Установите: pip install transformers>=4.51.0"
        )
    except Exception as e:
        log.error(f"Не удалось загрузить токенизатор {model_id}: {e}")

    # Попытка 2: явный деградированный fallback (только если разрешён)
    allow_fallback = config.get("defaults", {}).get("allow_degraded_fallback", False)
    if allow_fallback:
        ratio = float(config.get("defaults", {}).get("tokenizer_fallback_ratio", 3.5))
        log.warning(
            f"Работаю в деградированном режиме: chars/token={ratio}. "
            f"Точность чанкирования снижена."
        )
        return (lambda text: max(1, int(len(text) / ratio)), "degraded_chars_per_token")

    # Без fallback: жёсткая ошибка
    raise RuntimeError(
        "RAG-индексация невозможна: токенизатор Qwen3 недоступен. "
        "Установите transformers>=4.51.0 и убедитесь в доступе к HuggingFace Hub, "
        "либо включите allow_degraded_fallback: true в rag_config.yaml (с потерей точности)."
    )
```

### 3.2 Стратегия чанкирования

Алгоритм `build_rag_jsonl()` обновлён:

```
Для каждого clause (как в ADR-009, ADR-9a):
  1. text = extract_clause_text()
  2. tokens = tokenizer(text + заголовок + метаданные)
  3. Если tokens <= max_chunk_tokens:
       → Один чанк, как в v1
  4. Если tokens > max_chunk_tokens:
       a. Разбить text на атомарные блоки (таблицы, код, параграфы)
          через _split_oversized_clause_tokens() — аналог _split_oversized_clause(),
          но с лимитом в токенах
       b. Каждый подчанок получает суффикс «(ч. N)» в clause и chunk_id
       c. Метаданные повторяются в каждом подчанке (как в ADR-9b)
```

### 3.3 _split_oversized_clause_tokens()

Аналог существующего `_split_oversized_clause()`, но с токен-ориентированным лимитом:

```python
def _split_oversized_clause_tokens(
    text: str,
    max_tokens: int,
    tokenize: Callable[[str], int],
    overhead: int = 200,  # резерв на метаданные
) -> list[str]:
    """Разбить текст clause на подчанки ≤ max_tokens - overhead токенов."""
    effective_limit = max_tokens - overhead
    if tokenize(text) <= effective_limit:
        return [text]

    # Разбить на блоки (как в _split_oversized_clause)
    blocks = _tokenize_blocks(text, tokenize)

    chunks = []
    current_blocks = []
    current_tokens = 0

    for block, block_tokens in blocks:
        if block_tokens > effective_limit:
            # Гигантский блок (таблица/код) — публикуем как есть
            if current_blocks:
                chunks.append("\n\n".join(current_blocks))
                current_blocks, current_tokens = [], 0
            log.warning(f"Блок {block_tokens} токенов > лимита {effective_limit} — как есть")
            chunks.append(block)
            continue

        sep_tokens = tokenize("\n\n") if current_blocks else 0
        if current_tokens + sep_tokens + block_tokens > effective_limit:
            chunks.append("\n\n".join(current_blocks))
            current_blocks, current_tokens = [], 0

        current_blocks.append(block)
        current_tokens += block_tokens + (sep_tokens if len(current_blocks) > 1 else 0)

    if current_blocks:
        chunks.append("\n\n".join(current_blocks))

    return chunks if chunks else [text]
```

---

## 4. rag_assets.json — алгоритм построения

### 4.1 Извлечение таблиц

```python
def _extract_tables_from_md(md_text: str) -> list[dict]:
    """Найти все Markdown-таблицы в тексте.

    Алгоритм:
      1. Разбить md_text на строки
      2. Детектить таблицы: строка содержит '|', следующая строка — '|---|'
      3. Для каждой таблицы:
         - caption: ищем подпись до или после таблицы:
           «Таблица N» или «Таблица N.M — ...»
         - md_lines: [start_line, end_line)
         - image_path: "image/table_{N}.png" (N — порядковый номер)
    """
    lines = md_text.splitlines()
    tables = []
    i = 0
    n = 0  # счётчик таблиц
    while i < len(lines):
        stripped = lines[i].strip()
        # Детектим начало таблицы: строка с '|' И следующая с '|---'
        if stripped.startswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r"^\|[\s\-:|]+\|$", next_line):
                n += 1
                start = i
                # Ищем конец таблицы (строки с '|')
                while i < len(lines) and lines[i].strip().startswith("|"):
                    i += 1
                end = i
                # Ищем caption (строка до или после)
                caption = _find_table_caption(lines, start, end, n)
                tables.append({
                    "asset_id": f"table_{n}",  # doc_slug добавляется позже
                    "asset_type": "table",
                    "caption": caption,
                    "md_lines": [start, end],
                    "image_path": f"image/table_{n}.png",
                    "row_count": (end - start - 2),  # минус заголовок и разделитель
                    "chunk_ids": [],
                })
                continue
        i += 1
    return tables
```

### 4.2 Извлечение изображений

```python
def _extract_images_from_md(md_text: str) -> list[dict]:
    """Найти все изображения в Markdown.

    Паттерн: ![caption](image/fig_N.ext)
    """
    images = []
    for m in re.finditer(r'!\[(.*?)\]\((image/.*?)\)', md_text):
        line_num = md_text[:m.start()].count('\n')
        images.append({
            "asset_id": None,  # заполняется позже
            "asset_type": "image",
            "caption": m.group(1),
            "image_path": m.group(2),
            "md_line": line_num,
            "chunk_ids": [],
        })
    return images
```

### 4.3 Связывание активов с чанками

```python
def _link_assets_to_chunks(
    assets: dict,
    chunks: list[dict],  # уже построенные чанки (JSONL-строки как dict)
) -> None:
    """Для каждого актива найти чанки, в которых он упоминается.

    Мутирует assets in-place, заполняя chunk_ids.
    """
    for table in assets["assets"]["tables"]:
        # Таблица принадлежит чанку, если текст чанка содержит таблицу
        # или caption таблицы
        for chunk in chunks:
            if _chunk_contains_table(chunk, table):
                table["chunk_ids"].append(chunk["chunk_id"])

    for img in assets["assets"]["images"]:
        img_ref = f"![]({img['image_path']})"
        for chunk in chunks:
            if img["image_path"] in chunk["text"] or img_ref in chunk["text"]:
                img["chunk_ids"].append(chunk["chunk_id"])
```

---

## 5. Трёхэтапный CLI

### 5.1 Состояния входного файла

```python
def _classify_input(input_path: Path) -> str:
    """Классифицировать входной файл.

    Returns:
        "pdf": PDF, требует OCR (таблицы ВСЕГДА вырезаются в image/)
        "docx": DOCX/DOC, требует конвертации + OCR
        "md_standalone": .md вне Markdown/ → AI-постобработка
        "md_rag": .md внутри Markdown/ → RAG-индексация (только --rag)
    """
    suffix = input_path.suffix.lower()
    if suffix in (".pdf",):
        return "pdf"
    if suffix in (".docx", ".doc"):
        return "docx"
    if suffix == ".md":
        if "Markdown" in input_path.parts:
            return "md_rag"
        return "md_standalone"
    return "unknown"
```

### 5.2 Режим вырезания таблиц (всегда на этапе 1)

На этапе 1 (PDF/DOCX → Markdown) таблицы **всегда** вырезаются в `image/table_N.png`,
независимо от флага `--ai`. Это локальная операция через PyMuPDF (`fitz`), не требующая
AI/API-вызовов.

```python
# Внутри process_file() для pdf/docx:
# ... OCR, парсинг, постобработка ...

# Вырезание таблиц — ВСЕГДА (без --ai):
table_images = _extract_table_images_from_pdf(pdf_path, tables, output_img_dir)
# Сохраняет image/table_1.png, image/table_2.png, ...

# AI-постобработка — только с --ai:
if use_ai:
    md_text = ai_postprocess(md_text, config, file_label)
```

**Отличие `--ai` от вырезания таблиц:**
| Операция | Нужен `--ai`? | Где выполняется |
|----------|---------------|-----------------|
| Вырезание таблиц в image/table_N.png | Нет (всегда) | PyMuPDF, локально |
| Вырезание изображений в image/fig_N.png | Нет (всегда) | PyMuPDF, локально |
| Vision/AI-коррекция OCR-ошибок | Да | Через Provod API (LLM) |
| LaTeX-чистка, таблицы, подписи | Нет (всегда) | Скриптовая постобработка |

### 5.3 process_file() — обновлённая логика

```python
def process_file(
    input_path: str,
    use_ai: bool,
    config: dict,
    api_key: str,
    folder_id: str,
    output_base: str,
    tmp_base: str,
    use_rag: bool = False,
    rag_config: dict | None = None,
) -> bool:
    input_path = Path(input_path).resolve()
    file_type = _classify_input(input_path)

    # ── Режим .md + --rag: ТОЛЬКО индексация ──
    if file_type == "md_rag":
        if not use_rag:
            log.error(".md в Markdown/: требуется флаг --rag")
            return False
        return _run_rag_only(input_path, rag_config)

    # ... (существующая логика для pdf/docx/md_standalone) ...

    # ── После сохранения .md + image/ ──
    if use_rag and file_type != "md_rag":
        _run_rag_on_final_md(
            md_text, headings, rag_config, input_path, out_dir, file_stem
        )
```

### 5.4 _run_rag_only() — безопасный режим

```python
def _run_rag_only(md_path: Path, rag_config: dict | None) -> bool:
    """RAG-индексация существующего Markdown (без OCR/постобработки).

    Вход: Markdown/<file>/<file>.md
    Действия:
      1. Читает .md файл (read-only)
      2. Проверяет наличие image/ рядом
      3. Инициализирует токенизатор (Qwen3 или degraded fallback)
      4. build_rag_jsonl() → ПЕРЕЗАПИСЫВАЕТ rag_chunks.jsonl
      5. _build_asset_registry() → ПЕРЕЗАПИСЫВАЕТ rag_assets.json
    НЕ модифицирует .md, НЕ запускает OCR, НЕ извлекает изображения.
    """
    md_text = md_path.read_text(encoding="utf-8")
    doc_dir = md_path.parent
    img_dir = doc_dir / "image"

    if not img_dir.is_dir():
        log.warning(f"  image/ не найден: {img_dir} — реестр активов будет пустым")

    doc_key = _find_doc_key(md_path.name, rag_config)
    if doc_key is None:
        log.warning(f"Документ не найден в rag_config: {md_path.name}")
        return False

    tokenize, chunking_method = _init_tokenizer(rag_config)
    jsonl = build_rag_jsonl_v2(md_text, None, rag_config, doc_key, tokenize, chunking_method)

    # Атомарная перезапись производных файлов
    rag_path = doc_dir / "rag_chunks.jsonl"
    safe_write(rag_path, jsonl)
    log.info(f"  RAG JSONL перезаписан: {rag_path} ({len(jsonl.splitlines())} строк)")

    # Реестр активов
    assets = _build_asset_registry(md_text, doc_key, str(img_dir) if img_dir.is_dir() else None)
    assets_path = doc_dir / "rag_assets.json"
    with open(assets_path, "w", encoding="utf-8") as f:
        json.dump(assets, f, ensure_ascii=False, indent=2)
    log.info(f"  RAG Assets перезаписан: {assets_path}")

    return True
```

### 5.5 CLI — примеры использования

```bash
# Этап 1: Генерация Markdown из PDF (таблицы всегда в image/)
python3 pipeline.py -i file.pdf                  # без AI
python3 pipeline.py -i file.pdf --ai             # с AI (сложный документ)

# Этап 2: Проверка человеком
# (открыть Markdown/file/file.md, сверить с PDF, при необходимости поправить)

# Этап 3: RAG-индексация проверенного Markdown
# (перезаписывает rag_chunks.jsonl и rag_assets.json, не трогает .md и image/)
python3 pipeline.py -i Markdown/file/file.md --rag

# Всё вместе (быстрый путь, без проверки):
python3 pipeline.py -i file.pdf --ai --rag
```

**Контракт перезаписи:**
| Файл | Модифицируется `--rag`? | Примечание |
|------|--------------------------|------------|
| `<file>.md` | **НЕТ** | Read-only, источник истины |
| `image/*.png` | **НЕТ** | Изображения не пересоздаются |
| `rag_chunks.jsonl` | **ДА (перезапись)** | Производный от .md + image/ |
| `rag_assets.json` | **ДА (перезапись)** | Производный от .md + image/ |

---

## 6. rag_config.yaml — обновлённая схема

```yaml
# ═══════════════════════════════════════════════════════════════
# Конфигурация RAG-индексации нормативных документов v2
# ═══════════════════════════════════════════════════════════════

defaults:
  output_format: "jsonl"
  max_chunk_tokens: 7000                  # БЫЛО: max_chunk_chars: 1500
  tokenizer: "Qwen/Qwen3-Embedding-8B"    # НОВОЕ: HF model id для AutoTokenizer
  tokenizer_revision: "main"              # НОВОЕ: фиксированная ревизия
  allow_degraded_fallback: false          # НОВОЕ: запретить молчаливый fallback
  tokenizer_fallback_ratio: 3.5           # chars/token (только при allow_degraded_fallback: true)
  include_tables: true
  include_images: true
  extract_references: true
  default_status: "active"                # НОВОЕ: статус по умолчанию если не указан

references:
  patterns:
    - 'см\.\s*(?:п\.|пункт)\s*(\d+(?:\.\d+)*)'
    - '(?:согласно|по)\s+(?:п\.|пункту)\s*(\d+(?:\.\d+)*)'
    - '(?:табл\.|таблица)\s*(\d+(?:\.\d+)*)'
    - 'разд\.\s*(\d+(?:\.\d+)*)'
    - '(?:гл\.|глава)\s*(\d+(?:\.\d+)*)'

documents:
  so153_molniezashita:
    document_id: "СО 153-34.21.122-2003"
    document_id_alt: null
    title: "Инструкция по устройству молниезащиты зданий..."
    edition: "2003"
    date_enacted: "2003-06-30"
    date_amended: null
    amended_by: null
    source_file: "СО153-34_21_122-2003 Молниезащита.pdf"
    status: active                     # НОВОЕ
    status_reason: null                # НОВОЕ
    replaced_by_document_id: null      # НОВОЕ
    replaced_by_doc_key: null          # НОВОЕ
    ignore_sections:
      - "Содержание"

  sp89_kotelnye:
    document_id: "СП 89.13330.2016"
    document_id_alt: "СНиП II-35-76"
    title: "Котельные установки"
    edition: "2016"
    date_enacted: "2017-06-17"
    date_amended: "2021-05-18"
    amended_by: "Приказ Минстроя РФ № 295/пр от 18.05.2021"
    source_file: "СП 89.13330.2016 Котельные установки.pdf"
    status: active
    status_reason: null
    replaced_by_document_id: null
    replaced_by_doc_key: null
    ignore_sections:
      - "Предисловие"
      - "Содержание"

  # Пример недействующего документа
  old_snip:
    document_id: "СНиП 2.04.05-86"
    document_id_alt: null
    title: "Отопление, вентиляция и кондиционирование"
    edition: "1986"
    date_enacted: "1987-01-01"
    date_amended: null
    amended_by: null
    source_file: "СНиП_2.04.05-86.pdf"
    status: inactive                   # НОВОЕ
    status_reason: "Заменён на СП 60.13330.2012"  # НОВОЕ
    replaced_by_document_id: "СП 60.13330.2012"   # НОВОЕ: официальный номер заменившего документа
    replaced_by_doc_key: "sp60_otoplenie"         # НОВОЕ: ключ каталога (slug) при необходимости
    ignore_sections:
      - "Содержание"
```

### Валидация rag_config.yaml

```python
def validate_rag_config(config: dict) -> list[str]:
    """Проверить rag_config на корректность. Возвращает список ошибок."""
    errors = []

    # defaults
    defaults = config.get("defaults", {})
    mt = defaults.get("max_chunk_tokens", 0)
    if not isinstance(mt, int) or mt < 100 or mt > 100000:
        errors.append(f"defaults.max_chunk_tokens должен быть int 100..100000, получено {mt}")

    # documents
    for slug, doc in config.get("documents", {}).items():
        status = doc.get("status")
        if status is None:
            errors.append(f"documents.{slug}: status не указан (будет '{config.get('defaults', {}).get('default_status', 'active')}')")
        elif status not in ("active", "inactive"):
            errors.append(f"documents.{slug}: status='{status}' — допустимы только active/inactive")

        if status == "active":
            if doc.get("replaced_by_document_id") is not None:
                errors.append(f"documents.{slug}: status=active, но replaced_by_document_id не null")
            if doc.get("replaced_by_doc_key") is not None:
                errors.append(f"documents.{slug}: status=active, но replaced_by_doc_key не null")
            if doc.get("status_reason") is not None:
                errors.append(f"documents.{slug}: status=active, но status_reason не null")

    return errors
```

---

## 7. Миграция с v1

### 7.1 Что меняется в pipeline.py

| Функция | Изменение |
|---------|-----------|
| `load_rag_config()` | Добавить вызов `validate_rag_config()`, warning при ошибках валидации |
| `_split_oversized_clause()` | **Заменяется** на `_split_oversized_clause_tokens()` с параметром `tokenize` |
| `build_rag_jsonl()` | **Переименовать** в `build_rag_jsonl_v1()` (оставить для совместимости). **Новая** `build_rag_jsonl_v2()` с обновлённым контрактом |
| `_write_rag_jsonl()` | Обновить: вызывать v2, писать `rag_assets.json` |
| `parse_args()` | Без изменений (флаги те же) |
| `process_file()` | Добавить логику `_classify_input()`, режим `md_rag`, таблицы всегда вырезаются |
| `main()` | Без изменений |
| **Новые функции** | `_init_tokenizer()`, `_split_oversized_clause_tokens()`, `_extract_tables_from_md()`, `_extract_images_from_md()`, `_build_asset_registry()`, `_link_assets_to_chunks()`, `_classify_input()`, `_run_rag_only()`, `validate_rag_config()`, `build_rag_jsonl_v2()` |

### 7.2 Что меняется в rag_config.yaml

| Поле | v1 | v2 | Действие |
|------|-----|-----|----------|
| `defaults.max_chunk_chars` | `1500` | **Удалено** | Заменено на `max_chunk_tokens` |
| `defaults.max_chunk_tokens` | — | `7000` | **Добавить** |
| `defaults.tokenizer` | — | `"Qwen/Qwen3-Embedding-8B"` | **Добавить** |
| `defaults.tokenizer_revision` | — | `"main"` | **Добавить** |
| `defaults.allow_degraded_fallback` | — | `false` | **Добавить** |
| `defaults.tokenizer_fallback_ratio` | — | `3.5` | **Добавить** |
| `defaults.default_status` | — | `"active"` | **Добавить** |
| `documents.<slug>.status` | — | `"active"` | **Добавить** для каждого документа |
| `documents.<slug>.status_reason` | — | `null` | **Добавить** |
| `documents.<slug>.replaced_by_document_id` | — | `null` | **Добавить** |
| `documents.<slug>.replaced_by_doc_key` | — | `null` | **Добавить** (опционально) |

### 7.3 Что меняется в тестах

| Тест | Изменение |
|------|-----------|
| `test_load_rag_config_ok` | Добавить проверку новых полей |
| `test_build_rag_jsonl_basic` | Обновить ожидаемый JSONL-контракт (нет source.page, есть chunk_id, status, section_path, chunking_method, etc.) |
| `test_build_rag_jsonl_oversized_splits` | Заменить на `test_build_rag_jsonl_v2_oversized_token_splits` |
| `test_build_rag_jsonl_every_line_valid` | Обновить набор обязательных полей |
| `test_build_rag_jsonl_source_page_null` | Заменить: теперь `source.page` не должно быть, `_source_page` должно быть |
| **Новые тесты** | `test_tokenizer_init_qwen3`, `test_tokenizer_init_degraded_fallback`, `test_tokenizer_unavailable_raises`, `test_split_oversized_clause_tokens`, `test_extract_tables_from_md`, `test_extract_images_from_md`, `test_build_asset_registry`, `test_link_assets_to_chunks`, `test_classify_input`, `test_validate_rag_config`, `test_rag_output_structure`, `test_md_plus_rag_does_not_modify_md`, `test_rag_overwrites_jsonl_and_assets` |

### 7.4 Порядок миграции

```
1. Обновить rag_config.yaml (добавить поля, заменить max_chunk_chars → max_chunk_tokens, tokenizer → Qwen)
2. Добавить секцию 13: Tokenizer Chunking (_init_tokenizer с AutoTokenizer)
3. Добавить секцию 14: Asset Registry (все _extract_*, _build_*, _link_*)
4. Обновить секцию 12: build_rag_jsonl_v2() с новым контрактом + chunking_method
5. Обновить секцию 11: process_file() — режим md_rag, вызов v2, таблицы всегда вырезаются
6. Обновить тесты: test_rag_jsonl.py → test_rag_jsonl_v2.py
7. Добавить тесты для новых секций
8. Обновить ADR-009: пометить поля source.page и max_chunk_chars как deprecated
```

### 7.5 Обратная совместимость

- `build_rag_jsonl_v1()` сохраняется (deprecated) — можно вызвать при необходимости
- `rag_config.yaml` без новых полей: `validate_rag_config()` даст warning, статус = `default_status`
- Старые JSONL-файлы: не затрагиваются, v2 пишет новые файлы
- Флаги CLI: `--rag` и `--rag-config` без изменений

---

## 8. Выходная структура (обновлённая)

```
<input_dir>/
├── Markdown/
│   └── <file_stem>/
│       ├── <file_stem>.md            # Итоговый Markdown (НЕ модифицируется --rag)
│       ├── image/                     # fig_1.png, table_1.png, ...
│       ├── rag_chunks.jsonl          # RAG JSONL v2 (ПЕРЕЗАПИСЫВАЕТСЯ --rag)
│       └── rag_assets.json           # Реестр активов (ПЕРЕЗАПИСЫВАЕТСЯ --rag)
├── tmp/
│   └── <file_stem>/
│       ├── yandex_result.json
│       ├── raw.md
│       └── *.pdf
```

---

## 9. Error Handling (расширенный)

| Ситуация | Поведение |
|----------|-----------|
| `transformers` не установлен | `log.error`, RAG отказывается запускаться (если не `allow_degraded_fallback`) |
| Токенизатор не загружен (сеть/кеш) | `log.error`, RAG отказывается запускаться (если не `allow_degraded_fallback`) |
| `allow_degraded_fallback: true` + нет transformers | `log.warning`, работает chars/token, `chunking_method = "degraded_chars_per_token"` |
| `rag_config.yaml` без поля `status` | `log.warning`, использовать `default_status` |
| `rag_config.yaml`: active, но есть status_reason | `log.warning`, проигнорировать status_reason |
| `.md + --rag`, нет `image/` | `log.warning`, `rag_assets.json` без таблиц/изображений |
| `.md + --rag`, документ не в rag_config | `log.error`, пропустить |
| Таблица > max_chunk_tokens | `log.warning`, опубликовать как отдельный чанк |
| Таблица > 2× max_chunk_tokens | Разбить по строкам с повторением заголовка |
| Clause текст > max_chunk_tokens, неразбиваем | `log.warning`, опубликовать как есть |
| `rag_assets.json`: image_path не существует | `log.warning`, пропустить asset |

---

## 10. Зависимости

### Python packages (новые)

```
transformers>=4.51.0     # AutoTokenizer для Qwen3-Embedding-8B (~5 МБ токенизатор)
```

### Существующие (без изменений)

```
httpx, PyYAML, PyMuPDF, python-dotenv
```

### Удалённые зависимости

```
tiktoken  # удалён — заменён на transformers.AutoTokenizer (Qwen3-native)
```

---

## 11. Нерешённые риски

1. **Доступность HuggingFace Hub при первой загрузке:** При первом запуске `_init_tokenizer()`
   требуется доступ к huggingface.co для скачивания файлов токенизатора (~5 МБ). В офлайн-окружении
   необходимо предварительно скачать токенизатор. Решение: документировать процедуру
   `HF_HUB_OFFLINE=1` после первого запуска, или использовать `local_files_only=True`.

2. **Связывание таблиц с чанками при разбиении:** Если таблица находится в clause, который
   разбит на подчанки, `_link_assets_to_chunks()` должен определить, в какой именно подчанок
   попала таблица. Это требует сохранения границ подчанков при построении JSONL.

3. **Стабильность chunk_id при переименовании slug:** Если администратор изменит `doc_slug`
   в `rag_config.yaml`, все chunk_id изменятся. Это задокументированное ограничение —
   slug считается стабильным идентификатором.

4. **Извлечение caption таблиц:** Текущая эвристика ищет «Таблица N» в соседних строках.
   Для сложных документов с нетипичным форматированием caption может быть не найден.
   Решение: `caption = ""` если не найден, ручная правка в `rag_assets.json`.

5. **Версионирование токенизатора:** При обновлении ревизии токенизатора (`tokenizer_revision`)
   может измениться токенизация (маловероятно для стабильного Qwen2Tokenizer). Рекомендуется
   фиксировать ревизию в конфиге и пересчитывать JSONL при смене ревизии.
