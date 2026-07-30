# Implementation Report

## Task: t_dddea607
## Реализация `firmware/src/pipeline.py` — конвертация PDF/DOCX → Markdown через Yandex Vision OCR

---

## Summary

Реализован файл `firmware/src/pipeline.py` (1792 строки) — пайплайн конвертации PDF/DOCX в Markdown через Yandex Vision OCR (модель `math-markdown`). Файл содержит все 11 секций, описанных в архитектуре.

---

## Files Changed

### Created
- `/root/projects/Create_Markdown_YA/firmware/src/pipeline.py` — основной модуль пайплайна

### Read-only (existing)
- `/root/projects/Create_Markdown_YA/docs/architecture/architecture.md` — архитектура
- `/root/projects/Create_Markdown_YA/docs/architecture/implementation-plan.md` — план реализации

---

## Structure (11 секций)

| # | Секция | Строки | Статус |
|---|--------|--------|--------|
| 0 | Imports & Constants | ~20 | Реализовано |
| 1 | Utilities | ~80 | Реализовано (копия из Create_Markdown_mineru.py) |
| 2 | Yandex OCR API | ~150 | Реализовано (новая разработка: async-запрос + NDJSON polling) |
| 3 | JSON → Markdown Parser | ~180 | Реализовано (новая разработка: markdown, tables, pictures, blocks) |
| 4 | Image Extractor | ~80 | Реализовано (PyMuPDF, crop по bounding box) |
| 5 | LaTeX Postprocessing | ~80 | Реализовано (копия из Create_Markdown_mineru.py) |
| 6 | Tables Postprocessing | ~150 | Реализовано (копия из Create_Markdown_mineru.py) |
| 7 | Images & Captions | ~60 | Реализовано (копия из Create_Markdown_mineru.py) |
| 8 | Notes & OCR Fixes | ~60 | Реализовано (копия из Create_Markdown_mineru.py) |
| 9 | Full Script Postprocess | ~30 | Реализовано (адаптировано: без wrap_equations) |
| 10 | AI Postprocess | ~130 | Реализовано (копия из Create_Markdown_mineru.py) |
| 11 | CLI & Main | ~120 | Реализовано (адаптировано под Yandex: без --backend) |

---

## Validation Performed

| Тест | Результат |
|------|-----------|
| `python3 -c "import pipeline"` | ✅ Модуль импортируется без ошибок |
| CLI парсинг (`-i file.pdf`, `--ai`, `--config`) | ✅ Все аргументы корректно распознаются |
| `--help` | ✅ Выводит справку |
| `_extract_markdown_field()` на реальном JSON (7 стр, 21Кб markdown) | ✅ Корректно извлекает текст |
| `_parse_tables_from_json()` на реальном JSON (структурированные таблицы) | ✅ Конвертирует ячейки → Markdown-таблицы (12Кб таблиц) |
| `_parse_pictures_from_json()` на реальном JSON | ✅ Найдено 1 изображение |
| `parse_yandex_json_to_md()` | ✅ 33 991 символ markdown, 1 картинка |
| `_clean_spaces_in_numbers()` | ✅ |
| `_simplify_math_commands()` → `\mathsf{X}` → `X` | ✅ |
| `cleanup_latex()` → `\mathsf, \mathbf` очищены | ✅ |
| `convert_html_tables()` | ✅ HTML → Markdown |
| `fix_notes()` → `> Примечание` | ✅ |
| `fix_ocr_artifacts()` → разбитые слова склеены | ✅ |
| `fix_table_fig_labels()` → `# Таблица N` → `Таблица N` | ✅ |
| `fix_image_captions()` → `*Рис. N*` | ✅ |
| `merge_tables()` → `Продолжение` | ✅ |
| `run_script_postprocess()` (интеграция) | ✅ |
| `_chunk_text()` (3 чанка) | ✅ |

---

## Key Design Decisions

1. **Параметр json_path → pages**: Изменён порядок параметров в `parse_yandex_json_to_md()`, чтобы `pages` был первым (опциональным) — удобнее передавать результат `send_to_yandex_ocr()` напрямую.

2. **Yandex NDJSON polling**: Реализована поддержка NDJSON-формата ответа Yandex OCR (каждая строка — JSON страницы). Опрос с 2-секундным интервалом и таймаутом 600с.

3. **Без `wrap_equations()`**: Согласно ADR-6, в Yandex формулы уже встроены в текст как `$LaTeX$`/`$$LaTeX$$`.

4. **Извлечение изображений**: Через PyMuPDF (fitz) по bounding box'ам из поля `pictures`. Координаты в пикселях → points.

5. **Санитайзинг пути `!База_ГОСТ`**: Спецсимвол `!` в имени папки требует экранирования в Python.

---

## Test Infrastructure

Временные скрипты для тестирования:
- `/tmp/inspect_yandex_json.py` — инспекция структуры Yandex JSON
- `/tmp/inspect_yandex_structure.py` — детальная структура полей markdown/tables/pictures
- `/tmp/test_parser.py` — тест парсера JSON → Markdown
- `/tmp/test_postprocess.py` — тест всех функций постобработки

---

## Known Limitations

1. **DOCX изображения**: При конвертации DOCX → PDF через LibreOffice координаты изображений могут сместиться. Для DOCX с изображениями требуется дополнительная логика (python-docx + Pillow).

2. **Масштаб координат**: В `extract_images_from_pdf()` отсутствует точный пересчёт пикселей Yandex → points PyMuPDF, так как width/height textAnnotation не передаются в функцию. Используется масштаб 2.0 (DPI).

3. **Вставка изображений по координатам**: Пока только логирование. Требуется сложная логика вставки `![Рис. N](image/fig_N.png)` в точную позицию Markdown по Y-координате bounding box'а.

4. **Большие PDF (>200 страниц)**: Yandex лимит — 200 страниц. Для PDF >50 стр нужно разбиение на части (аналогично `page_splitter.py` из Create_markdown).

5. **Разрыв inline-формул**: `$...$` без нового окружения `$$\n...\n$$` может неправильно обрабатываться при очень коротких формулах на одной строке.

---

## Dependencies

### Python packages
- `httpx>=0.24` — HTTP-клиент (Yandex OCR API, Provod API)
- `PyYAML>=6.0` — config_ai.yaml
- `PyMuPDF>=1.23.0` — fitz, извлечение изображений
- `beautifulsoup4>=4.0` — парсинг HTML-таблиц
- `python-dotenv>=1.0` — .env файлы (опционально, встроен `load_env`)

### System dependencies
- `libreoffice` — headless конвертация DOCX/DOC → PDF

### External APIs
- Yandex Vision OCR: `https://ai.api.cloud.yandex.net/ocr/v1/`
- Provod AI: `https://api.provod.ai/v1/chat/completions`

### Environment variables
- `YANDEX_API_KEY` — API-ключ Yandex
- `YANDEX_FOLDER_ID` — Folder ID Yandex
- `PROVOD_API_KEY` — ключ Provod (только для `--ai`)
