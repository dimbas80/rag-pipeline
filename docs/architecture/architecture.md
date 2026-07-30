# Architecture: Create_Markdown_YA Pipeline

PDF/DOCX → Markdown pipeline via Yandex Vision OCR (`math-markdown` model).

## 1. Overview

Целевой файл: **`firmware/src/pipeline.py`** (~700–800 строк), один Python-модуль.

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

**Проблема:** Требование — один файл `pipeline.py`. Но код объёмный (~800 строк).

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

---

## 3. Module Decomposition

```
pipeline.py (~800 lines)
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
│   ├── parse_yandex_json_to_md(json_path)
│   ├── _extract_markdown_field(pages)
│   ├── _parse_tables_from_json(pages)
│   ├── _parse_pictures_from_json(pages)
│   └── _parse_blocks_to_text(pages)      # Fallback
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
└── 11. CLI & Main                 (~120 lines)
    ├── parse_args(argv)
    ├── process_file(file_path, use_ai, config, api_key, folder_id, output_base, tmp_base)
    └── main()
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
│   ├─ _extract_markdown_field()      → базовый Markdown
│   ├─ _parse_tables_from_json()      → Markdown-таблицы (из структурированных ячеек)
│   ├─ _parse_pictures_from_json()    → список {page, bbox}
│   └─ Сборка: markdown_field + таблицы → единый .md
│   Сохраняет → tmp/<file>/raw.md
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
└─ Сохранение: Markdown/<file>/<file>.md + image/
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

### 5.6 CLI & Main

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разобрать аргументы командной строки.
    -i/--input (required): входной файл или папка
    --ai (flag): включить AI-постобработку
    --config (default: ./config_ai.yaml): путь к конфигу AI"""

def process_file(
    input_path: str,
    use_ai: bool,
    config: dict,
    api_key: str,
    folder_id: str,
    output_base: str,
    tmp_base: str,
) -> bool:
    """Обработать один файл: Yandex OCR → парсинг → изображения → постобработка → сохранение.
    Returns: True при успехе."""

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

**Принцип:** ошибка в одном файле не останавливает обработку остальных. `process_file()` возвращает `False`, `main()` считает статистику.

---

## 8. Configuration

### .env

```bash
YANDEX_API_KEY=<key>
YANDEX_FOLDER_ID=<folder_id>
PROVOD_API_KEY=<key>      # только для --ai
```

### config_ai.yaml

```yaml
postprocess:
  primary:
    provider: provod
    base_url: https://api.provod.ai/v1
    model: google/gemini-3.5-flash
  fallback:
    provider: provod
    base_url: https://api.provod.ai/v1
    model: anthropic/claude-sonnet-5
  prompt: |
    Ты — редактор технических текстов...
```

---

## 9. Output Structure

```
<входная_директория>/
├── Markdown/
│   └── <имя_файла>/
│       ├── <имя_файла>.md       # Итоговый Markdown
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

---

## 11. Unresolved Risks

1. **DOCX с изображениями:** Yandex OCR принимает только PDF. DOCX → PDF через LibreOffice может изменить координаты изображений. Решение: если вход DOCX, извлекать изображения из DOCX напрямую (python-docx + Pillow), а не из сконвертированного PDF.

2. **Модель `math-markdown` — доступность поля `markdown`:** Не все версии API возвращают это поле. Нужен fallback на парсинг `blocks` + `tables`. Реализован в `_parse_blocks_to_text()`.

3. **Сопоставление изображений по IoU:** Алгоритм `_match_images_to_pictures()` может не найти соответствие для некоторых изображений (разные DPI, сжатие). Решение: если изображение не сопоставлено — вставить его в конец документа с комментарием.

4. **Таймаут Yandex OCR для больших PDF:** Лимит 10 минут на операцию может не хватить для 200-страничного PDF. Решение: если PDF > 50 страниц — разбивать на части по 30 страниц (аналогично `page_splitter.py`).
