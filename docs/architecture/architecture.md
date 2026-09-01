# Architecture: Create_Markdown_YA Pipeline

PDF/DOCX → Markdown pipeline via Yandex Vision OCR (`math-markdown` model).

> **Исторический документ (архитектура v1).** Описывает исходный дизайн пайплайна
> и местами устарел. Актуальные контракты: RAG v2 — `rag-v2-architecture.md` + ADR-010,
> реестр провайдеров — `providers-registry.md`, флаг `--reg` — `rag-register-flag.md`,
> ID-маркеры таблиц — `table-id-marker-contract.md`. Секция «8. Configuration»
> ниже приведена к текущей схеме конфигов (`providers.yaml` + `create_markdown_config.yaml`).

## 1. Overview

Целевой файл: **`firmware/src/create_markdown.py`** (~700–800 строк), один Python-модуль.

Pipeline заменяет MinerU на Yandex Vision OCR. От MinerU (`Create_markdown`) берётся только логика постобработки: LaTeX-чистка, таблицы, изображения, подписи, примечания, AI-постобработка.

---

## 2. Architecture Decisions (ADR)

### ADR-1: Модель Yandex OCR — `math-markdown`

**Проблема:** Yandex Vision OCR предлагает 6 моделей. Базовая `page` возвращает только текстовые блоки с layoutType. Модель `math-markdown` возвращает:
- `markdown` — готовый Markdown с заголовками, списками, формулами, таблицами
- `tables` — структурированные ячейки (rowIndex, columnIndex, text)
- `pictures` — boundingBox'ы изображений
- `blocks` — текстовые блоки с LaTeX-формулами в тексте

**Решение:** Использовать `math-markdown` как основную модель. Парсить все три источника: `markdown` (базовый каркас), `tables` (точные таблицы), `pictures` (координаты изображений).

**Rationale:** `math-markdown` даёт наиболее богатый вывод. Если поле отсутствует (старая модель) — fallback на парсинг блоков.

### ADR-2: Извлечение изображений из PDF через PyMuPDF

**Проблема:** Yandex OCR возвращает только bounding box'ы изображений (поле `pictures`), но не сами изображения. Нужно вырезать изображения из исходного PDF.

**Решение:** Использовать PyMuPDF (fitz) для:
1. Извлечения всех встроенных изображений из PDF (`page.get_images()`)
2. Сопоставления их bounding box'ов с координатами из `pictures`
3. Вырезания изображений с сохранением в `image/`

**Rationale:** PyMuPDF — стандарт для работы с PDF в Python. Алгоритм сопоставления координат уже описан в yandex-ocr skill.

**Масштабирование координат:** Yandex возвращает координаты в пикселях (ширина/высота страницы из textAnnotation). PyMuPDF использует points (1/72 дюйма). Формула:
```
scale_x = float(ta["width"]) / page.rect.width
scale_y = float(ta["height"]) / page.rect.height
```

### ADR-3: DOCX/DOC → PDF через LibreOffice

**Проблема:** Yandex OCR принимает только PDF и изображения (PNG/JPEG). DOCX/DOC должны быть сконвертированы.

**Решение:** Использовать LibreOffice headless для конвертации:
```bash
libreoffice --headless --convert-to pdf input.docx --outdir tmp/
```

**Rationale:** Бесплатно, доступно на сервере, поддерживает все форматы MS Office.

### ADR-4: Один файл, секционная структура

**Проблема:** Требование — один файл `create_markdown.py`. Но код объёмный (~800 строк).

**Решение:** Организовать файл секциями с чёткими разделителями-комментариями:
```
# === 0. Imports & Constants ===
# === 1. Utilities ===
# === 2. Yandex OCR API ===
# === 3. JSON → Markdown Parser ===
# === 4. Image Extractor ===
# === 5. Postprocessing: LaTeX ===
# === 6. Postprocessing: Tables ===
# === 7. Postprocessing: Images & Captions ===
# === 8. Postprocessing: Notes & OCR Fixes ===
# === 9. Full Script Postprocess ===
# === 10. AI Postprocess ===
# === 11. CLI & Main ===
```

### ADR-5: Копирование функций постобработки из Create_markdown

**Проблема:** Функции постобработки из `Create_Markdown_mineru.py` отлажены и проверены. Переписывать их не нужно.

**Решение:** Скопировать следующие функции как есть (с адаптацией сигнатур где необходимо):
- `convert_html_tables()`, `merge_tables()` + все вспомогательные
- `cleanup_latex()`, `_clean_spaces_in_numbers()`, `_simplify_math_commands()`, `_fix_latex_ocr_artifacts()`, `_clean_extra_braces()`
- `rename_images()`, `fix_image_captions()`, `fix_table_fig_labels()`, `fix_notes()`, `fix_ocr_artifacts()`
- `_chunk_text()`, `_call_ai_api()`, `ai_postprocess()`
- `setup_logging()`, `load_env()`, `load_config()`, `ensure_dir()`, `find_input_files()`, `safe_write()`

**НЕ копировать:** Функции MinerU API (`run_pipeline_local`, `run_vlm_cloud`, `_poll_vlm_batch`, `_extract_vlm_zip`), сборку Markdown из content_list_v2 (`build_markdown_from_content_list`, `_extract_text_content`), `organize_output()` — всё это заменяется Yandex-логикой.

### ADR-6: Удаление `wrap_equations()`

**Проблема:** В MinerU формулы приходили как отдельные блоки `equation.text`. В Yandex формулы уже встроены в текст как `$LaTeX$` (inline) или `$$LaTeX$$` (display).

**Решение:** Функция `wrap_equations()` **не нужна**. LaTeX-формулы в Yandex-ответе уже обёрнуты в `$...$`. Исключена из пайплайна.

### ADR-7: Section-aware chunking для устранения дублирования таблиц

**Проблема:** `_chunk_text()` разбивает документ по лимиту 24000 символов, игнорируя границы разделов. AI в каждом чанке с неполным разделом выносит таблицы в конец вывода, создавая дубликаты при сборке.

**Решение (2 компонента):**
1. **`_chunk_text()` — двухфазный алгоритм:** Фаза 1 — партиционирование текста по границам разделов (regex: `**N. Title**`, `### N. Title`, `#### N.N. Title`). Фаза 2 — сборка чанков из целых секций. Секции > max_chars разбиваются по параграфам (старая логика).
2. **Промпт — удаление табличных инструкций:** Убрать пункты 13-15 (объединение таблиц, вынос примечаний, форматирование подписей) — скриптовая постобработка уже делает это. Добавить запрет на перемещение таблиц в «ЧТО НЕЛЬЗЯ».

**Rationale:** Устранение первопричины (разрыв разделов) + устранение инструкций, провоцирующих перемещение таблиц. Минимальные изменения: только `_chunk_text()` и `config_ai.yaml`.

Полный текст: [ADR-007](decision-records/adr-007-section-aware-chunking.md)

### ADR-8: Извлечение заголовков разделов из Yandex JSON → Markdown headings

**Проблема:** `_block_to_md()` рендерит блоки `LAYOUT_TYPE_SECTION_HEADER` с номером раздела (например, «3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ») как `**жирный текст**`. Это неправильно — для Markdown-заголовков должны использоваться `##`, `###`, `####`. Для последующей RAG-индексации нужна корректная иерархия разделов.

**Почему через Yandex JSON, а не постфактум-парсинг MD:** Парсинг готового Markdown ненадёжен — `**200 кА**` (табличное значение) и `**3.2.1. Молниеприемники**` (заголовок) визуально идентичны. В Yandex JSON заголовки разделов идентифицируются по комбинации layoutType + bounding box + текстового паттерна, что надёжно отличает их от табличных ячеек и обычного текста.

**Решение:** Добавить две функции в пайплайн, работающие после `parse_yandex_json_to_md()` и до `run_script_postprocess()`:

1. **`_extract_headings_from_json(pages)`** — извлекает заголовки из сырых JSON-страниц по 5 правилам:
   - **Правило 1 (текст):** текст начинается с номера раздела: `^\d+\.(\d+\.)*\s` (напр. «3.», «3.2.1.»)
   - **Правило 2 (длина):** len(text) < 100 символов И слов < 10
   - **Правило 3 (позиция):** rel_x < 0.50 (левый край страницы, не таблица)
   - **Правило 4 (одна строка):** на той же странице и Y-координате (±15px) нет других блоков — это ключевой фильтр, отсекающий ячейки таблиц
   - **Правило 5 (уровень):** глубина нумерации: «3» → level 1 (##), «3.2» → level 2 (###), «3.2.1» → level 3 (####), «3.2.1.1» → level 4 (#####)
   
   Возвращает: `[{"page": N, "y": Y, "level": L, "number": "3.2.1", "text": "Молниеприемники", "full_text": "3.2.1. Молниеприемники"}, ...]`

2. **`_apply_headings_to_md(md_text, headings)`** — находит в MD-тексте строки `**full_text**` и заменяет их на `#{level+1} full_text`. Использует `re.escape()` для безопасной обработки спецсимволов в тексте заголовка.

**Rationale:** Геометрические признаки из JSON (rel_x, Y-изоляция) надёжнее текстовых эвристик на MD. Валидировано на ГОСТ СО153-34.21.122-2003 (29 страниц): найдено 68 заголовков, включая глубокую вложенность до 4 уровней. «Один блок на строке» надёжно отсекает false positives — ячейки таблиц («1 Входящие линии», «2 Антенны») всегда находятся на Y-линии с другими блоками.

**Fallback:** Если Yandex JSON недоступен (старая модель без blocks[]), `_extract_headings_from_json()` возвращает пустой список, `_apply_headings_to_md()` пропускает MD без изменений.

**Коррекция regex:** В задании указан паттерн `^\d+(\.\d+)*\s`, но он не матчит «1. ВВЕДЕНИЕ» — финальная точка после группы цифр не сопровождается цифрой. Правильный паттерн: `^\d+\.(\d+\.)*\s` — номер всегда заканчивается точкой перед пробелом.

**Параметр rel_x:** В задании указан порог 0.45, но на тестовых данных заголовки «1. ВВЕДЕНИЕ» (x=0.455), «4.4. Соединения» (x=0.450) и «4.5. Заземление» (x=0.452) превышают этот порог. Рекомендованный порог: 0.50. Фильтр «один блок на строке» выполняет основную работу по отсеву false positives, поэтому небольшое ослабление rel_x безопасно.

**Интеграция в pipeline:**
```
parse_yandex_json_to_md(pages)    → md_text, pictures, page_boundaries
_extract_headings_from_json(pages) → headings
_apply_headings_to_md(md_text, headings) → md_text (с ##/###/####)
run_script_postprocess(md_text)
```

---

### ADR-9: md_to_rag_jsonl — конвертация MD в JSONL для RAG-индексации

**Проблема:** После Этапа 1 MD-файлы имеют корректную иерархию заголовков `##`/`###`/`####`/`#####`. Нужно преобразовать их в структурированный JSONL для RAG-индексации, где каждая строка — clause с полным контекстом (метаданные документа, иерархия разделов, номер страницы, кросс-ссылки).

**Решение:** Добавить секцию 12 в `create_markdown.py` — `md_to_rag_jsonl` (~150 строк). Активируется флагом `--rag`.

Ключевые решения:
- **ADR-9a:** Clause = каждый заголовок от `##` до `#####`. Текст ограничен следующим заголовком любого уровня.
- **ADR-9b:** Oversized clause (> max_chunk_chars=1500) разбиваются по границам параграфов с повторением метаданных.
- **ADR-9c:** Номера страниц берутся из `_extract_headings_from_json()` (ADR-8) — без повторного парсинга JSON.
- **ADR-9d:** Конфигурация в `rag_config.yaml` с секциями `defaults`, `references.patterns`, `documents.<slug>`.
- **ADR-9e:** Интеграция как секция 12 в create_markdown.py, флаг `--rag`, позиция после всей постобработки.

Полный текст: [ADR-009](decision-records/adr-009-md-to-rag-jsonl.md)

---

## 3. Module Decomposition

```
create_markdown.py (~800 lines)
├── 0. Imports & Constants          (~15 lines)
├── 1. Utilities                    (~80 lines)
│   ├── setup_logging(log_path)
│   ├── load_env(env_path)
│   ├── load_config(config_path)
│   ├── ensure_dir(path)
│   ├── find_input_files(input_path)
│   └── safe_write(path, content)
│
├── 2. Yandex OCR API              (~120 lines)
│   ├── convert_docx_to_pdf(docx_path, tmp_dir)
│   ├── send_to_yandex_ocr(pdf_path, api_key, folder_id, model)
│   └── _poll_yandex_operation(operation_id, api_key, folder_id)
│
├── 3. JSON → Markdown Parser      (~200 lines)
│   ├── parse_yandex_json_to_md(json_path, pages=None)
│   ├── _block_to_md(block)
│   ├── _table_to_md(table)
│   ├── _find_table_caption_for(blocks, table, all_tables)
│   └── _stitch_continuation_tables(md_text, ...)
│
├── 3b. Heading Extractor           (~60 lines)
│   ├── _extract_headings_from_json(pages)   → list[dict]
│   └── _apply_headings_to_md(md_text, headings) → str
│
├── 4. Image Extractor             (~80 lines)
│   ├── extract_images_from_pdf(pdf_path, pictures, page_dims, output_img_dir)
│   ├── _match_images_to_pictures(doc, pictures, page_dims)
│   └── _crop_and_save_image(doc, img_info, bbox, output_path)
│
├── 5. Postprocessing: LaTeX       (~80 lines)
│   ├── _clean_spaces_in_numbers(text)
│   ├── _simplify_math_commands(text)
│   ├── _fix_latex_ocr_artifacts(text)
│   ├── _clean_extra_braces(text)
│   └── cleanup_latex(md_text)
│
├── 6. Postprocessing: Tables      (~180 lines)
│   ├── convert_html_tables(md_text)
│   ├── merge_tables(md_text)
│   └── [вспомогательные: _parse_table_html, _matrix_to_markdown, ...]
│
├── 7. Postprocessing: Images      (~60 lines)
│   ├── rename_images(md_text, img_dir)
│   ├── fix_image_captions(md_text)
│   └── fix_table_fig_labels(md_text)
│
├── 8. Postprocessing: Notes       (~60 lines)
│   ├── fix_notes(md_text)
│   └── fix_ocr_artifacts(md_text)
│
├── 9. Full Script Postprocess     (~30 lines)
│   └── run_script_postprocess(md_text, img_dir)
│
├── 10. AI Postprocess             (~100 lines)
│   ├── _chunk_text(text, max_chars)
│   ├── _call_ai_api(text, config, context)
│   └── ai_postprocess(md_text, config, file_label)
│
├── 11. CLI & Main                 (~120 lines)
│   ├── parse_args(argv)
│   ├── process_file(file_path, use_ai, use_rag, config, rag_config, api_key, folder_id, output_base, tmp_base)
│   └── main()
│
└── 12. RAG JSONL Converter        (~150 lines)
    ├── load_rag_config(config_path) → dict
    ├── _extract_heading_number(heading_text) → str | None
    ├── _build_ancestors(headings_tree, idx) → dict
    ├── parse_md_structure(md_text) → list[dict]
    ├── extract_clause_text(md_text, heading_line, next_heading_line) → str
    ├── extract_references(text, patterns) → list[str]
    ├── _get_page_for_heading(number, json_headings) → int | None
    ├── _split_oversized_clause(text, max_chars) → list[str]
    └── build_rag_jsonl(md_text, json_headings, rag_config, doc_key) → str
```

---

## 4. Data Flow

```
Вход: PDF/DOCX/DOC (файл или папка)
│
├─ DOCX/DOC? ──→ convert_docx_to_pdf() ──→ PDF
│
├─ send_to_yandex_ocr()
│   ├─ POST /ocr/v1/recognizeTextAsync (model=math-markdown)
│   ├─ _poll_yandex_operation() (опрос статуса, до ~10 мин)
│   └─ Сохраняет JSON → tmp/<file>/yandex_result.json
│
├─ parse_yandex_json_to_md()
│   ├─ Сборка: блоки (Y-сортировка) + таблицы → единый .md
│   ├─ _block_to_md(): LAYOUT_TYPE_SECTION_HEADER → **жирный** (временно)
│   └─ Возвращает (md_text, pictures, page_boundaries)
│   Сохраняет → tmp/<file>/raw.md
│
├─ _extract_headings_from_json(pages)
│   ├─ 5 правил: текст ^\d+\. → len<100 → rel_x<0.50 → Y-изоляция → уровень
│   └─ Возвращает [{"page", "level", "full_text", ...}, ...]
│
├─ _apply_headings_to_md(md_text, headings)
│   ├─ Поиск **full_text** в md_text → замена на #{level+1} full_text
│   └─ Если headings пуст → md_text без изменений
│
├─ extract_images_from_pdf()
│   ├─ PyMuPDF: page.get_images() → список встроенных изображений
│   ├─ _match_images_to_pictures() → сопоставление координат
│   ├─ Вырезание/сохранение → image/fig_N.ext
│   └─ Вставка ссылок ![fig_N](image/fig_N.ext) в .md на позиции по координатам
│
├─ run_script_postprocess()
│   ├─ convert_html_tables()     (если есть HTML)
│   ├─ merge_tables()            (Продолжение/Окончание/одинаковый заголовок)
│   ├─ cleanup_latex()           (пробелы, \\mathsf и т.д.)
│   ├─ rename_images()           (хеши → fig_N)
│   ├─ fix_image_captions()      (*Рис. N* курсив)
│   ├─ fix_notes()               (> Примечание)
│   ├─ fix_table_fig_labels()    (Таблица N, Рис. N)
│   └─ fix_ocr_artifacts()       (разбитые слова, пробелы)
│
├─ [--ai] ai_postprocess()
│   ├─ _chunk_text()             (чанки по ~24000 симв)
│   ├─ _call_ai_api()            (Provod API: gemini-3.5-flash → claude-sonnet-5)
│   └─ Сборка результата
│
├─ [--rag] build_rag_jsonl()
│   ├─ parse_md_structure(md_text) → список clause
│   ├─ extract_clause_text() для каждого clause
│   ├─ _get_page_for_heading() → source.page из _extract_headings_from_json()
│   ├─ extract_references() → кросс-ссылки
│   ├─ _split_oversized_clause() для clause > max_chunk_chars
│   └─ JSONL → Markdown/<документ>/rag_chunks.jsonl
│
└─ Сохранение: Markdown/<файл>/<файл>.md + image/
   Промежуточные → tmp/<file>/
   Лог → tmp/Create_Markdown_VisionOCR.log
```

---

## 5. Function Signatures

### 5.1 Yandex OCR API

```python
def convert_docx_to_pdf(input_path: str | Path, tmp_dir: str | Path) -> Path:
    """Конвертировать DOCX/DOC в PDF через LibreOffice headless.
    Returns: путь к сконвертированному PDF."""

def send_to_yandex_ocr(
    pdf_path: str | Path,
    api_key: str,
    folder_id: str,
    model: str = "math-markdown",
    timeout: int = 600,
) -> list[dict]:
    """Отправить PDF в Yandex Vision OCR (async), дождаться результата.
    Returns: список page-словарей (распарсенный JSON).
    Raises: RuntimeError при ошибке API/таймауте."""

def _poll_yandex_operation(
    operation_id: str,
    api_key: str,
    folder_id: str,
    timeout: int = 600,
    poll_interval: int = 2,
) -> list[dict]:
    """Опросить статус асинхронной операции Yandex OCR.
    Returns: список page-словарей.
    Raises: TimeoutError при превышении timeout."""
```

### 5.2 JSON → Markdown Parser

```python
def parse_yandex_json_to_md(
    json_path: str | Path,
    pages: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Разобрать Yandex OCR JSON в Markdown + список картинок.
    Приоритет: markdown field → tables → blocks (fallback).
    Returns: (markdown_text, pictures_list).
    pictures_list: [{"page": int, "bbox": {"vertices": [...]}}, ...]"""

def _extract_markdown_field(pages: list[dict]) -> str:
    """Извлечь поле 'markdown' из textAnnotation всех страниц.
    Если поле отсутствует — возвращает пустую строку."""

def _parse_tables_from_json(pages: list[dict]) -> str:
    """Извлечь структурированные таблицы из поля 'tables'.
    Конвертирует cells (rowIndex, columnIndex, text) в Markdown-таблицы."""

def _parse_pictures_from_json(pages: list[dict]) -> list[dict]:
    """Извлечь bounding box'ы изображений из поля 'pictures'.
    Returns: [{"page": N, "bbox": {...}}, ...]"""

def _parse_blocks_to_text(pages: list[dict]) -> str:
    """Fallback: собрать текст из блоков (layoutType: TEXT, LIST, UNSPECIFIED).
    Группирует строки по bounding box'ам, определяет абзацы и списки."""

### 5.2b Heading Extractor

```python
def _extract_headings_from_json(pages: list[dict]) -> list[dict]:
    """Извлечь заголовки разделов из Yandex OCR JSON.

    Алгоритм (5 правил):
      1. Собрать все блоки со всех страниц с page_number, y, x_left, rel_x, text
      2. Отфильтровать кандидатов:
         - text matches ^\\d+\\.(\\d+\\.)*\\s  (начинается с номера раздела)
         - len(text) < 100
         - len(text.split()) < 10
         - rel_x < 0.50  (x_left / page_width)
      3. Проверка «один блок на строке»: на той же странице и
         Y-координате (±15 px) нет других блоков
      4. Определить уровень по глубине номера:
         - "3"       → level 1 → ##
         - "3.2"     → level 2 → ###
         - "3.2.1"   → level 3 → ####
         - "3.2.1.1" → level 4 → #####

    Returns:
        [{"page": N, "y": Y, "level": L, "number": "3.2.1",
          "text": "Молниеприемники", "full_text": "3.2.1. Молниеприемники"}, ...]
    """

def _apply_headings_to_md(md_text: str, headings: list[dict]) -> str:
    """Заменить **жирные** заголовки на правильные Markdown-заголовки.

    Для каждого heading в headings:
      1. Построить поисковый паттерн: \\*\\*{re.escape(full_text)}\\*\\*
      2. Заменить на: {"#" * (level + 1)} {full_text}

    Если headings пуст — вернуть md_text без изменений (fallback).

    Edge cases:
      - Дублирующиеся заголовки (например, в оглавлении): re.sub() с count=1
        заменяет только первое вхождение
      - Спецсимволы в тексте (*, [, etc.): re.escape()
      - Заголовок уже может быть обработан ранее: проверка, что строка
        начинается с **, а не с #
    """
```

### 5.3 Image Extractor

```python
def extract_images_from_pdf(
    pdf_path: str | Path,
    pictures: list[dict],
    output_img_dir: str | Path,
) -> list[dict]:
    """Вырезать изображения из PDF по координатам из pictures.
    Сохраняет в output_img_dir как fig_N.ext.
    Returns: [{"fig_num": N, "page": P, "filename": "fig_N.png", "bbox": {...}}, ...]"""

def _match_images_to_pictures(
    doc: fitz.Document,
    pictures: list[dict],
    page_dims: list[tuple[float, float]],
) -> list[dict]:
    """Сопоставить встроенные изображения PDF с pictures от Yandex OCR.
    Использует пересечение bounding box'ов (IoU)."""

def _crop_and_save_image(
    page: fitz.Page,
    bbox: dict,
    output_path: Path,
    scale_x: float,
    scale_y: float,
) -> bool:
    """Вырезать область страницы по bbox и сохранить как изображение."""
```

### 5.4 Postprocessing Pipeline

```python
def run_script_postprocess(md_text: str, img_dir: str | Path) -> str:
    """Выполнить полную скриптовую постобработку Markdown.
    Порядок: HTML-таблицы → merge_tables → LaTeX → изображения → подписи → примечания → OCR-фиксы."""

def cleanup_latex(md_text: str) -> str:
    """Очистить все LaTeX-формулы: пробелы в числах, \\mathsf→text, скобки, артефакты."""

def convert_html_tables(md_text: str) -> tuple[str, int]:
    """Конвертировать HTML-таблицы в Markdown. Returns: (текст, количество)."""

def merge_tables(md_text: str) -> str:
    """Объединить смежные таблицы по признакам продолжения."""

def rename_images(md_text: str, img_dir: str | Path) -> tuple[str, int]:
    """Переименовать изображения в fig_N, обновить ссылки в Markdown."""

def fix_image_captions(md_text: str) -> str:
    """Подписи «Рис. N» после изображений → курсив."""

def fix_table_fig_labels(md_text: str) -> str:
    """Убрать заголовочный # перед «Таблица N», «Рис. N»."""

def fix_notes(md_text: str) -> str:
    """Примечания/сноски → цитаты (> ...)."""

def fix_ocr_artifacts(md_text: str) -> str:
    """Склеить разбитые слова, убрать двойные пробелы."""
```

### 5.5 AI Postprocess

```python
def ai_postprocess(md_text: str, config: dict, file_label: str = "") -> str:
    """AI-постобработка через Provod API с чекпойнтингом.
    config: словарь из config_ai.yaml (primary/fallback/prompt)."""

def _call_ai_api(text: str, config: dict, context: str = "") -> str | None:
    """Вызвать Provod API. Основная модель → fallback при ошибке.
    Returns: обработанный текст или None при ошибке."""

def _chunk_text(text: str, max_chars: int = 24000) -> list[str]:
    """Разбить Markdown на чанки, сохраняя целостность таблиц и кодовых блоков."""
```

### 5.5b RAG JSONL Converter

```python
def load_rag_config(config_path: str | Path) -> dict:
    """Загрузить rag_config.yaml.
    Returns: полный словарь конфига с defaults, references, documents.
    Raises: FileNotFoundError, yaml.YAMLError."""

def _extract_heading_number(heading_text: str) -> str | None:
    """Извлечь номер раздела из текста заголовка.
    Примеры: '3.2.1. Молниеприемники' → '3.2.1'
             '1.1. Общие положения' → '1.1'
    Regex: ^(\\d+(?:\\.\\d+)*)\\.\\s
    Returns: номер (строка) или None."""

def _build_ancestors(
    headings: list[dict],
    idx: int,
) -> dict[str, str | None]:
    """Построить chapter/section/clause для данного heading по индексу.
    Ищет ближайшие предшествующие heading меньшего уровня.
    Returns: {'chapter': '3', 'section': '3.2', 'clause': '3.2.1'}.
    Поля, для которых предок не найден, равны None."""

def parse_md_structure(md_text: str) -> list[dict]:
    """Разобрать Markdown на иерархию clause.

    Алгоритм:
      1. Разбить текст на строки (splitlines)
      2. Найти все строки-заголовки: ^(#{2,5})\\s+(.+)$
      3. Для каждого заголовка:
         - level = len(match[1]) (## → 2, ### → 3, #### → 4, ##### → 5)
         - heading_text = match[2]
         - number = _extract_heading_number(heading_text)
         - line_num = индекс строки
      4. Для каждого heading определить next_heading_line_num:
         - Индекс строки следующего heading в списке (любого уровня)
         - Для последнего heading — len(lines)
      5. Построить список clause, каждый с полями:
         - level, number, heading_text, line_num, next_line_num

    Returns:
        [{
            'level': 2,          # 2=##, 3=###, 4=####, 5=#####
            'number': '3',       # извлечённый номер
            'heading_text': '3. Защита от прямых ударов молнии',
            'line_num': 42,      # 0-based индекс строки заголовка
            'next_line_num': 78, # 0-based индекс строки след. заголовка (эксклюзив)
                                 # или len(lines) для последнего
        }, ...]

    Corner cases:
      - Заголовки без номера (напр. '## Введение') → number=None,
        пропускаются? Нет, clause всё равно создаётся
        (chapter/section/clause = null)
      - Оглавление (Content) — если heading_text содержит 'Содержание'
        И doc_meta.ignore_sections содержит 'Содержание' →
        пропускается вся секция (см. build_rag_jsonl)
      - Дублирующиеся заголовки (оглавление + основной текст) —
        обрабатываются оба (оглавление фильтруется через ignore_sections)
    """

def extract_clause_text(
    md_text: str,
    heading_line: int,
    next_heading_line: int,
) -> str:
    """Извлечь текст clause между двумя заголовками.

    Алгоритм:
      1. lines = md_text.splitlines()
      2. Извлечь lines[heading_line + 1 : next_heading_line]
      3. Join через \\n
      4. strip() ведущих/завершающих пробелов и пустых строк

    Таблицы и изображения сохраняются как есть (Markdown).
    Не модифицирует текст.

    Returns: текст clause (может быть пустой строкой).
    """

def extract_references(
    text: str,
    patterns: list[str],
) -> list[str]:
    """Извлечь кросс-ссылки из текста clause.

    Алгоритм:
      1. Для каждого regexp-паттерна из patterns:
         - re.findall(pattern, text)
         - Для каждого совпадения: привести к канонической форме
           (напр. 'табл. 3.1' → 'табл. 3.1')
      2. Дедупликация (set → sorted list)
      3. Вернуть список (может быть пустым)

    Returns: list[str] уникальных ссылок в порядке возрастания.
    """

def _get_page_for_heading(
    number: str,
    json_headings: list[dict] | None,
) -> int | None:
    """Найти номер страницы для clause по номеру раздела.

    Использует результат _extract_headings_from_json() (ADR-8):
      json_headings = [{'page': N, 'number': '3.2.1', ...}, ...]

    Алгоритм:
      1. Если json_headings пуст или None → вернуть None
      2. Построить индекс: {heading['number']: heading['page']}
         (первое вхождение для каждого номера)
      3. Найти json_headings[i] где heading['number'] == number
      4. Вернуть page (0-based), или None

    Edge cases:
      - Номер не найден в JSON (нетипичный формат) → None
      - Несколько headings с одинаковым номером (оглавление + текст)
        → берётся первое (из оглавления — page > 0, корректно)
    """

def _split_oversized_clause(
    text: str,
    max_chars: int,
) -> list[str]:
    """Разбить текст clause на подчанки по границам параграфов.

    Алгоритм:
      1. Если len(text) <= max_chars → [text]
      2. Разбить text по \\n\\n (параграфы)
      3. Объединить параграфы в группы ≤ max_chars:
         - Таблицы (строка начинается с '|') не разрывать:
           собрать все смежные строки таблицы в один блок
         - Кодовые блоки (```...```) не разрывать
      4. Каждая группа → подчанок
      5. Вернуть list[str]

    Гарантия: если один параграф/таблица > max_chars — он публикуется
    как есть (log.warning).

    Returns: список подчанков (всегда минимум 1 элемент).
    """

def build_rag_jsonl(
    md_text: str,
    json_headings: list[dict] | None,
    rag_config: dict,
    doc_key: str,
) -> str:
    """Построить JSONL-строку для RAG-индексации.

    Алгоритм:
      1. Загрузить doc_meta = rag_config['documents'][doc_key]
         Если doc_key не найден → raise ValueError
      2. defaults = rag_config['defaults']
      3. patterns = rag_config['references']['patterns']
         (если defaults.extract_references = true)
      4. ignore_sections = doc_meta.get('ignore_sections', [])
      5. clauses = parse_md_structure(md_text)

      6. Для каждого clause:
         a. Проверить ignore_sections: если heading_text содержит
            имя игнорируемой секции → пропустить ВСЕ clause до
            следующего heading того же или выше уровня
            (реализация: флаг skip_until_level)
         b. Извлечь текст: extract_clause_text(md_text, line_num, next_line_num)
         c. Пропустить если текст пустой (article heading без контента)
         d. Определить chapter/section/clause через _build_ancestors()
         e. Определить source.page через _get_page_for_heading(number, json_headings)
         f. Извлечь references через extract_references(text, patterns)
         g. Если len(text) > max_chunk_chars:
            - Разбить через _split_oversized_clause(text, max_chunk_chars)
            - Для каждого подчанка создать отдельную JSONL-строку
            - Добавить суффикс «(ч. N)» к clause
         h. Собрать JSON-объект и записать как строку в JSONL

      7. Вернуть полную JSONL-строку (разделитель \\n)

    Returns:
        JSONL-строка, каждая строка — валидный JSON-объект.
        Пример строки:
        {"document_id":"СО 153-34.21.122-2003","document_id_alt":null,
         "title":"Инструкция...","edition":"2003","date_enacted":"2003-06-30",
         "date_amended":null,"amended_by":null,"chapter":"3","section":"3.2",
         "clause":"3.2.1","text":"Внешняя МЗС ...",
         "source":{"file":"СО153-34_21_122-2003.pdf","page":7},
         "references":["табл. 3.1"]}
    """
```

### 5.6 CLI & Main

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разобрать аргументы командной строки.
    -i/--input (required): входной файл или папка
    --ai (flag): включить AI-постобработку
    --rag (flag): сгенерировать JSONL для RAG-индексации
    --config (default: ./create_markdown_config.yaml): промпты + RAG
    --providers-config (default: ./providers.yaml): реестр провайдеров"""

def process_file(
    input_path: str,
    use_ai: bool,
    use_rag: bool,
    config: dict,
    rag_config: dict | None,
    api_key: str,
    folder_id: str,
    output_base: str,
    tmp_base: str,
) -> bool:
    """Обработать один файл: Yandex OCR → парсинг → изображения → постобработка → [RAG] → сохранение.
    Returns: True при успехе.
    НОВОЕ: use_rag + rag_config для RAG JSONL генерации."""

def main() -> None:
    """Точка входа. Парсинг аргументов, итерация по файлам, process_file()."""
```

---

## 6. Dependencies

### Python packages

```
httpx>=0.24          # HTTP-клиент (Yandex OCR API, Provod API)
PyYAML>=6.0          # config_ai.yaml
PyMuPDF>=1.23.0      # fitz — работа с PDF, извлечение изображений
python-dotenv>=1.0   # .env файлы
```

### System dependencies

```
libreoffice          # headless конвертация DOCX/DOC → PDF
```

### External APIs

| API | URL | Переменные окружения |
|-----|-----|---------------------|
| Yandex Vision OCR | `https://ai.api.cloud.yandex.net/ocr/v1/` | `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` |
| Provod AI | `https://api.provod.ai/v1/chat/completions` | `PROVOD_API_KEY` |

---

## 7. Error Handling Strategy

| Ситуация | Поведение |
|----------|-----------|
| DOCX → PDF: LibreOffice не найден | `log.error`, пропустить файл |
| Yandex OCR: HTTP 4xx/5xx | 3 ретрая с экспоненциальной задержкой (1с, 4с, 16с) |
| Yandex OCR: таймаут операции | `log.error`, пропустить файл |
| Yandex OCR: пустой результат | `log.warning`, пропустить файл |
| PyMuPDF: не удалось открыть PDF | `log.error`, пропустить файл |
| Изображения: не найдены в PDF | `log.warning`, продолжить без изображений |
| AI: все модели недоступны | `log.error`, вернуть исходный Markdown |
| AI: ошибка сети | 3 ретрая на основную модель, затем fallback |
| Heading extraction: не найдено заголовков | `log.info("Заголовки не найдены")`, MD без изменений |
| Heading extraction: JSON без blocks[] | `_extract_headings_from_json()` → `[]`, fallback на MD как есть |
| Heading extraction: ошибка regex | `log.warning`, пропустить конкретный heading |
| RAG: create_markdown_config.yaml не найден | `log.error`, пропустить RAG-генерацию |
| RAG: doc_key не найден в конфиге | `log.error`, пропустить файл (нельзя определить метаданные) |
| RAG: clause текст > max_chunk_tokens и неразбиваем | `log.warning`, опубликовать как есть |
| RAG: JSON-сериализация ошибка | `log.error`, пропустить конкретный clause |

**Принцип:** ошибка в одном файле не останавливает обработку остальных. `process_file()` возвращает `False`, `main()` считает статистику.

---

## 8. Configuration

Три источника конфигурации (полный контракт реестра провайдеров — `providers-registry.md`):

### 8.1 .env — ключи API

```bash
YANDEX_API_KEY=<key>         # Yandex Vision OCR
YANDEX_FOLDER_ID=<folder_id>
DEEPSEEK_API_KEY=<key>       # ai_postprocess (primary)
PROVOD_API_KEY=<key>         # table_vision (primary) + ai_postprocess (fallback)
ANYMODEL_API_KEY=<key>       # table_vision (fallback)
Z_AI_API_KEY=<key>           # zai-custom (опционально)
SILICONFLOW_API_KEY=<key>    # embedding/rerank (build_search_index)
```

### 8.2 providers.yaml — реестр провайдеров + роли

```yaml
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models:
      deepseek-v4-flash: chat
      deepseek-v4-pro: chat
      deepseek-v4-flash-vision-exp: vision
  # ... provod, anymodel, zai-custom, siliconflow (полный список — в providers.yaml)

roles:
  create_markdown:
    table_vision:
      provider: provod
      model: glm-4.5v
      fallback: {provider: anymodel, model: glm/glm-4.6v}
    ai_postprocess:
      provider: deepseek
      model: deepseek-v4-pro
      fallback: {provider: provod, model: deepseek-v4-pro}
```

### 8.3 create_markdown_config.yaml — промпты + RAG-секции

```yaml
table_vision:
  prompt: | ...

ai_postprocess:
  prompt: | ...

reg_extract:
  prompt: | ...

defaults:
  output_format: "jsonl"
  max_chunk_tokens: 7000          # v2 (ADR-010)
  tokenizer: "Qwen/Qwen3-Embedding-8B"
  allow_degraded_fallback: false
  include_tables: true
  include_images: true
  extract_references: true
  default_status: "active"

references:
  patterns:
    - 'см\\.\\s*(?:п\\.|пункт)\\s*(\\d+(?:\\.\\d+)*)'
    # ... (полный список — в create_markdown_config.yaml)
```

Каталог `documents` удалён из конфига — записи живут в per-document `<stem>_reg.yaml`
(см. `rag-register-flag.md`).

## 9. Output Structure

```
<входная_директория>/
├── Markdown/
│   └── <имя_файла>/
│       ├── <имя_файла>.md       # Итоговый Markdown
│       ├── rag_chunks.jsonl      # RAG JSONL (если --rag)
│       └── image/                # fig_1.png, fig_2.jpg, ...
├── tmp/
│   ├── Create_Markdown_VisionOCR.log  # Общий лог
│   └── <имя_файла>/              # Промежуточные файлы
│       ├── yandex_result.json    # Сырой ответ Yandex OCR
│       ├── raw.md                # Markdown до постобработки
│       └── *.pdf                 # Сконвертированный PDF (если вход был DOCX)
```

---

## 10. Key Differences from Create_Markdown_mineru.py

| Аспект | MinerU (старый) | Yandex OCR (новый) |
|--------|-----------------|-------------------|
| OCR engine | MinerU pipeline/VLM | Yandex math-markdown |
| Вход DOCX | MinerU сам парсит | Конвертация через LibreOffice |
| Таблицы | HTML из MinerU | MD-таблицы из поля `tables` + `markdown` |
| Формулы | Отдельные блоки `equation.text` | Инлайн `$LaTeX$` в тексте |
| Изображения | MinerU вырезает сам | PyMuPDF по координатам из `pictures` |
| `wrap_equations()` | Нужна | **Не нужна** |
| Сборка Markdown | `content_list_v2.json` | Поле `markdown` + `tables` |
| Разбиение PDF | `page_splitter.py` (200 стр/200 МБ) | Yandex лимит: 200 стр/10 МБ — та же логика |
| RAG-индексация | Отсутствует | **Новая:** `--rag`, JSONL из MD-заголовков |

---

## 11. Unresolved Risks

1. **DOCX с изображениями:** Yandex OCR принимает только PDF. DOCX → PDF через LibreOffice может изменить координаты изображений. Решение: если вход DOCX, извлекать изображения из DOCX напрямую (python-docx + Pillow), а не из сконвертированного PDF.

2. **Модель `math-markdown` — доступность поля `markdown`:** Не все версии API возвращают это поле. Нужен fallback на парсинг `blocks` + `tables`. Реализован в `_parse_blocks_to_text()`.

3. **Сопоставление изображений по IoU:** Алгоритм `_match_images_to_pictures()` может не найти соответствие для некоторых изображений (разные DPI, сжатие). Решение: если изображение не сопоставлено — вставить его в конец документа с комментарием.

4. **Таймаут Yandex OCR для больших PDF:** Лимит 10 минут на операцию может не хватить для 200-страничного PDF. Решение: если PDF > 50 страниц — разбивать на части по 30 страниц (аналогично `page_splitter.py`).

5. **Heading-to-page mapping без OCR:** Если `--rag` используется с `.md` файлом (без Yandex OCR), `_extract_headings_from_json()` вернёт `[]`, и все `source.page` будут `null`. Это приемлемо — RAG всё равно работает, но без привязки к страницам.

6. **Нестандартная нумерация разделов:** Некоторые ГОСТ используют римские цифры (I, II, IV) или буквы (а, б, в) для нумерации приложений. Regex `^\\d+(\\.\\d+)*\\.\\s` не сматчит такие номера — `_extract_heading_number()` вернёт `None`. Решение: для приложений номер определяется позиционно (первый heading после «Приложение» → appendix «А»). Не реализовано в v1 — приложения без номеров получают `chapter/section/clause = null`.

7. **Матчинг doc_key по имени файла:** Если источник — DOCX (конвертируется в PDF), имя файла во временной папке отличается от имени в конфиге. Решение: матчить `source_file` из конфига по `input_path.name` (не `pdf_path.name`). Для `.md` файлов — по `stem`. Реализовать матчинг в `main()` до вызова `process_file()`.
