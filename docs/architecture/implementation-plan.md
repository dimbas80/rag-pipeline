# Implementation Plan: pipeline.py

Порядок реализации модулей файла `firmware/src/pipeline.py` от фундамента к интеграции.

## Принцип

Каждый юнит реализуется как атомарный блок с тестированием в `tests/`. Юниты 1–4 могут разрабатываться параллельно (независимые утилиты). Юниты 5–6 зависят от 1. Юниты 7–11 — последовательная цепочка.

---

## Unit 0: Структура файла и импорты

**Цель:** Создать каркас файла с импортами, константами, docstring.

**Строки:** ~20

**Содержание:**
- Shebang `#!/usr/bin/env python3`
- Docstring модуля
- Все импорты: `argparse, json, logging, os, re, shutil, sys, time, io, base64` + `pathlib.Path` + `httpx, yaml, fitz` + `dotenv` (условный импорт)
- Константы: `YANDEX_OCR_URL`, `YANDEX_POLL_URL`, `PROVOD_BASE_URL`, `AI_MAX_CHARS=24000`

**Зависимости:** Нет

**Приёмка:**
- `python3 -c "import sys; sys.path.insert(0, 'firmware/src'); import pipeline"` проходит без ошибок

---

## Unit 1: Утилиты

**Цель:** Реализовать служебные функции, не зависящие от Yandex/постобработки.

**Строки:** ~80

**Функции:**
1. `setup_logging(log_path)` — скопировать из Create_Markdown_mineru.py:97-111
2. `load_env(env_path)` — скопировать из Create_Markdown_mineru.py:114-125
3. `load_config(config_path)` — скопировать из Create_Markdown_mineru.py:128-136, адаптировать default под Yandex
4. `ensure_dir(path)` — скопировать из Create_Markdown_mineru.py:139-142
5. `find_input_files(input_path)` — скопировать, убрать pptx/xlsx
6. `safe_write(path, content)` — скопировать из Create_Markdown_mineru.py:162-165

**Зависимости:** Нет

**Приёмка:**
- `load_env()` читает .env, возвращает dict
- `find_input_files("file.pdf")` → `[Path("file.pdf")]`
- `find_input_files("dir/")` → все .pdf/.docx/.doc

---

## Unit 2: Yandex OCR API

**Цель:** Отправка PDF в Yandex Vision OCR, получение результата.

**Строки:** ~120

**Функции:**
1. `convert_docx_to_pdf(input_path, tmp_dir)` — вызов LibreOffice headless
2. `send_to_yandex_ocr(pdf_path, api_key, folder_id, model, timeout)` — async отправка + опрос
3. `_poll_yandex_operation(operation_id, api_key, folder_id, timeout, poll_interval)` — GET-цикл опроса

**Алгоритм `send_to_yandex_ocr()`:**
```
1. Прочитать PDF → base64
2. POST /ocr/v1/recognizeTextAsync:
   body = {mimeType: "application/pdf", languageCodes: ["ru","en"],
           model: "math-markdown", content: base64_pdf}
3. Извлечь operationId из ответа
4. Вызвать _poll_yandex_operation()
5. Сохранить результат как tmp/<file>/yandex_result.json
6. Вернуть pages (list[dict])
```

**Обработка ошибок:**
- HTTP 429: ретрай через 5с (rate limit)
- HTTP 4xx/5xx: 3 ретрая с exponential backoff (1с, 4с, 16с)
- Таймаут операции: `log.error`, raise `TimeoutError`

**Зависимости:** Unit 0 (импорты), Unit 1 (ensure_dir)

**Приёмка:**
- Вызов с тестовым PDF возвращает list[dict] длиной = количеству страниц
- Каждый элемент имеет `result.textAnnotation`
- Промежуточный JSON сохраняется в tmp/

---

## Unit 3: JSON → Markdown Parser

**Цель:** Разбор Yandex JSON в Markdown-текст + список изображений.

**Строки:** ~200

**Функции:**
1. `parse_yandex_json_to_md(json_path, pages=None)` — главная (читает JSON или принимает pages)
2. `_extract_markdown_field(pages)` — извлечь поле `markdown`
3. `_parse_tables_from_json(pages)` — структурированные таблицы → MD
4. `_parse_pictures_from_json(pages)` — bounding box'ы картинок
5. `_parse_blocks_to_text(pages)` — FALLBACK: блоки (layoutType) → текст

**Алгоритм `parse_yandex_json_to_md()`:**
```
1. Загрузить JSON (если pages не передан)
2. _extract_markdown_field() → md_text
3. Если markdown пуст — _parse_blocks_to_text() как fallback
4. _parse_tables_from_json() → таблицы (вставить в md_text ИЛИ добавить)
5. _parse_pictures_from_json() → pictures_list
6. Вернуть (md_text, pictures_list)
```

**Алгоритм `_parse_tables_from_json()`:**
```
Для каждой страницы:
  Для каждой таблицы в textAnnotation.tables:
    Извлечь cells (rowIndex, columnIndex, text, columnSpan)
    Построить матрицу ячеек (аналогично _parse_table_html)
    Конвертировать в Markdown (_matrix_to_markdown)
    Добавить перед таблицей её координаты (page, y_start)
```

**Алгоритм `_parse_blocks_to_text()` (fallback):**
```
Для каждой страницы:
  Сортировать блоки по y, затем по x
  Для каждого блока:
    layoutType TEXT/UNSPECIFIED → параграф (join lines через пробел)
    layoutType LIST → маркированный список (префикс "- ")
    Разделять блоки двойным переносом строки
    Если y-разрыв между блоками > порога → дополнительный \n (новый параграф)
```

**Важно:** Поле `markdown` из `math-markdown` модели уже содержит Markdown с заголовками, списками, формулами и таблицами. Если оно есть — `_parse_blocks_to_text()` не вызывается.

**Зависимости:** Unit 0

**Приёмка:**
- Тестовый JSON (7 стр) → валидный Markdown
- Формулы `$...$` сохранены как есть
- Таблицы из поля `tables` сконвертированы в MD
- `pictures_list` содержит bounding box'ы

---

## Unit 4: Image Extractor

**Цель:** Вырезать изображения из PDF по координатам из Yandex `pictures`.

**Строки:** ~80

**Функции:**
1. `extract_images_from_pdf(pdf_path, pictures, output_img_dir)` — главная
2. `_match_images_to_pictures(doc, pictures, page_dims)` — сопоставление
3. `_crop_and_save_image(page, bbox, output_path, scale_x, scale_y)` — вырезание

**Алгоритм `extract_images_from_pdf()`:**
```
1. Открыть PDF через fitz.open(pdf_path)
2. Для каждой страницы вычислить scale_x, scale_y
3. _match_images_to_pictures() — для каждого picture найти ближайшее изображение в PDF
4. Для каждого сопоставленного:
   - _crop_and_save_image() — вырезать область с высоким DPI (matrix=fitz.Matrix(3,3))
   - Сохранить как image/fig_N.png
5. Вернуть список {"fig_num": N, "page": P, "filename": "fig_N.png", "bbox": {...}}
```

**Алгоритм `_match_images_to_pictures()`:**
```
Для каждого picture в pictures:
  Для каждого встроенного изображения на соответствующей странице:
    Получить bbox изображения через page.get_image_bbox()
    Вычислить IoU (Intersection over Union)
    Если IoU > 0.5 → считать соответствием
  Если соответствие не найдено → пометить как неразрешённое
```

**Зависимости:** Unit 0, Unit 3 (формат pictures)

**Приёмка:**
- Тестовый PDF → изображения вырезаны в image/
- Имена: fig_1.png, fig_2.jpg, ...
- Размеры соответствуют координатам

---

## Unit 5: Postprocessing — LaTeX

**Цель:** Очистка LaTeX-формул (скопировать из Create_Markdown_mineru.py).

**Строки:** ~80

**Функции (копируются как есть):**
1. `_clean_spaces_in_numbers(text)` — из Create_Markdown_mineru.py:855-859
2. `_simplify_math_commands(text)` — из Create_Markdown_mineru.py:862-875
3. `_fix_latex_ocr_artifacts(text)` — из Create_Markdown_mineru.py:942-948
4. `_clean_extra_braces(text)` — из Create_Markdown_mineru.py:878-939
5. `cleanup_latex(md_text)` — из Create_Markdown_mineru.py:961-970

**ВАЖНО:** `wrap_equations()` **не копировать** — в Yandex формулы уже в `$...$`.

**Зависимости:** Unit 0 (re)

**Приёмка:**
- `_clean_spaces_in_numbers("0 , 4 2 9")` → `"0,429"`
- `_simplify_math_commands("\\mathsf{X}")` → `"X"`
- `cleanup_latex()` на тестовом Markdown — формулы очищены

---

## Unit 6: Postprocessing — Tables

**Цель:** Конвертация HTML-таблиц и объединение смежных таблиц.

**Строки:** ~180

**Функции (скопировать из Create_Markdown_mineru.py):**
1. `_parse_table_html(html)` — стр. 521-571
2. `_matrix_to_markdown(matrix)` — стр. 574-598
3. `_merge_header_rows(matrix)` — стр. 601-627
4. `_clean_table_html(html)` — стр. 630-664
5. `convert_html_tables(md_text)` — стр. 667-693
6. `_find_table_boundaries(lines)` — стр. 697-711
7. `_table_header(table_lines)` — стр. 714-716
8. `_is_continuation(text)` — стр. 719-722
9. `_extract_table_number(text)` — стр. 815-818
10. `_same_table_caption(lines, t1_start, t2_start)` — стр. 821-848
11. `merge_tables(md_text)` — стр. 725-812

**Адаптация:** Никакой — функции копируются дословно (работают с чистым Markdown).

**Зависимости:** Unit 0 (re, BeautifulSoup)

**Приёмка:**
- HTML-таблица → Markdown-таблица
- Две таблицы с «Продолжение» → объединены
- Примечание из colspan строки → `> Примечание`

---

## Unit 7: Postprocessing — Images & Captions

**Цель:** Переименование изображений, форматирование подписей.

**Строки:** ~60

**Функции (скопировать из Create_Markdown_mineru.py):**
1. `rename_images(md_text, img_dir)` — стр. 1010-1043
2. `fix_image_captions(md_text)` — стр. 1046-1063
3. `fix_table_fig_labels(md_text)` — стр. 1066-1080

**Адаптация:** Путь `image/` вместо `images/`. В `rename_images()` заменить `images/` на `image/`.

**Зависимости:** Unit 0 (re, shutil, Path)

**Приёмка:**
- `fig_1.png` в image/ → `![Рисунок 1](image/fig_1.png)` в Markdown
- Строка «Рис. 1 Схема» после изображения → `*Рис. 1 Схема*`
- `# Таблица 3.4` → `Таблица 3.4` (без #)

---

## Unit 8: Postprocessing — Notes & OCR Fixes

**Цель:** Примечания → цитаты, склейка разбитых слов.

**Строки:** ~60

**Функции (скопировать из Create_Markdown_mineru.py):**
1. `fix_notes(md_text)` — стр. 1083-1114
2. `fix_ocr_artifacts(md_text)` — стр. 1117-1140

**Зависимости:** Unit 0 (re)

**Приёмка:**
- «Примечание — ...» → `> Примечание — ...`
- «п р и м е ч а н и е» → `> п р и м е ч а н и е`
- «раз-\\nбитое» → «разбитое»

---

## Unit 9: Full Script Postprocess

**Цель:** Собрать все функции постобработки в один пайплайн.

**Строки:** ~30

**Функция:**
```python
def run_script_postprocess(md_text: str, img_dir: str | Path) -> str:
```
Копируется из Create_Markdown_mineru.py:1147-1178, НО:
- **Убрать** вызов `wrap_equations()` (стр. 1157)
- Сохранить порядок: HTML-таблицы → merge → LaTeX → изображения → подписи → примечания → OCR

**Зависимости:** Units 5, 6, 7, 8

**Приёмка:**
- Интеграционный тест: сырой Markdown → обработанный Markdown

---

## Unit 10: AI Postprocess

**Цель:** AI-постобработка через Provod API (опционально, флаг `--ai`).

**Строки:** ~100

**Функции (скопировать из Create_Markdown_mineru.py):**
1. `AI_CLEANUP_DEFAULT_PROMPT` — константа (стр. 1635-1655)
2. `_chunk_text(text, max_chars)` — стр. 1658-1746
3. `_call_ai_api(text, config, context)` — стр. 1749-1828
4. `ai_postprocess(md_text, config, file_label)` — стр. 1831-1888

**Адаптация:**
- Чекпойнты в `tmp/.ai_checkpoints/` (как в оригинале)
- `_call_ai_api()`: PROVOD_API_KEY из os.environ
- Ретраи и fallback без изменений

**Зависимости:** Unit 0, Unit 1 (config loading)

**Приёмка:**
- `_chunk_text()`: Markdown с таблицами разбит на чанки, таблицы не разорваны
- `_call_ai_api()`: API отвечает → обработанный текст (нужен реальный ключ)
- `ai_postprocess()`: чекпойнт создаётся/читается/удаляется

---

## Unit 11: CLI & Main — Интеграция

**Цель:** Собрать все юниты в рабочий пайплайн.

**Строки:** ~120

**Функции:**
1. `parse_args(argv)` — argparse (адаптировать из Create_Markdown_mineru.py:1891-1925)
   - `-i/--input` (required)
   - `--ai` (flag)
   - `--config` (default: `./config_ai.yaml`)
   - **Убрать** `--backend` (в Yandex один backend)

2. `process_file(input_path, use_ai, config, api_key, folder_id, output_base, tmp_base)` — главный оркестратор

3. `main()` — точка входа

**Алгоритм `process_file()`:**
```
1. Определить file_stem из input_path
2. Если DOCX/DOC → convert_docx_to_pdf() → pdf_path
   Иначе pdf_path = input_path
3. send_to_yandex_ocr(pdf_path, ...) → pages
   Сохранить pages как tmp/<file>/yandex_result.json
4. parse_yandex_json_to_md(json_path, pages) → (md_text, pictures)
   Сохранить md_text как tmp/<file>/raw.md
5. Если есть pictures → extract_images_from_pdf(pdf_path, pictures, image_dir)
   Вставить ссылки ![Рис. N](image/fig_N.ext) в md_text
6. run_script_postprocess(md_text, image_dir) → md_text
7. Если --ai → ai_postprocess(md_text, config, file_stem) → md_text
8. safe_write(Markdown/<file>/<file>.md, md_text)
9. Переместить промежуточные файлы в tmp/<file>/
```

**Алгоритм `main()`:**
```
1. parse_args()
2. load_env() → YANDEX_API_KEY, YANDEX_FOLDER_ID, PROVOD_API_KEY
3. load_config() → ai_config (если --ai)
4. setup_logging(tmp/Create_Markdown_VisionOCR.log)
5. find_input_files() → список файлов
6. Для каждого файла:
     try: process_file()
     except Exception: log.error(), failed++
7. Вывести статистику: успешно N, ошибок M
8. exit(1) если были ошибки
```

**Зависимости:** Все юниты 1–10

**Приёмка:**
- `python3 pipeline.py -i test.pdf` → создаёт `Markdown/test/test.md` + `image/`
- `python3 pipeline.py -i test.docx` → конвертирует, обрабатывает
- `python3 pipeline.py -i dir/` → обрабатывает все файлы в папке
- `python3 pipeline.py -i test.pdf --ai` → включает AI-постобработку
- Лог пишется в `tmp/Create_Markdown_VisionOCR.log`

---

## Порядок реализации

```
Фаза 1 (фундамент):  Unit 0 → Unit 1
Фаза 2 (OCR):        Unit 2 → Unit 3 → Unit 4
                      (2 и 3+4 можно параллельно)
Фаза 3 (постобр):    Unit 5 → Unit 6 → Unit 7 → Unit 8 → Unit 9
                      (5,6,7,8 могут параллельно после прототипа Unit 9)
Фаза 4 (AI):         Unit 10
Фаза 5 (сборка):     Unit 11
```

### Приоритеты

| Приоритет | Юнит | Причина |
|-----------|------|---------|
| P0 | Unit 2 | Ключевая интеграция с Yandex — без неё ничего не работает |
| P0 | Unit 3 | Парсинг JSON — определяет качество выходного Markdown |
| P0 | Unit 11 | Интеграция — нужна для E2E-тестирования |
| P1 | Unit 4 | Извлечение изображений — критично для ГОСТ-документов |
| P1 | Unit 5,6 | LaTeX и таблицы — основные артефакты |
| P2 | Unit 1 | Утилиты — нужны всем, но тривиальны |
| P2 | Unit 7,8 | Подписи и примечания — улучшение читаемости |
| P3 | Unit 10 | AI — опциональный флаг |

---

## Оценка трудозатрат

| Юнит | Строки | Сложность | Часов |
|------|--------|-----------|-------|
| 0 | 20 | низкая | 0.3 |
| 1 | 80 | низкая (копирование) | 0.5 |
| 2 | 120 | средняя (API, ретраи) | 2.0 |
| 3 | 200 | высокая (парсинг, несколько источников) | 3.0 |
| 4 | 80 | высокая (PyMuPDF, сопоставление) | 2.5 |
| 5 | 80 | низкая (копирование) | 0.5 |
| 6 | 180 | низкая (копирование) | 0.5 |
| 7 | 60 | низкая (копирование + замена путей) | 0.5 |
| 8 | 60 | низкая (копирование) | 0.3 |
| 9 | 30 | низкая (сборка) | 0.3 |
| 10 | 100 | низкая (копирование) | 0.5 |
| 11 | 120 | средняя (интеграция) | 2.0 |
| **Всего** | **~1130** | | **~12.9 ч** |

> Оценка выше 800 строк из-за копирования вспомогательных функций таблиц (Unit 6 — 180 строк вспомогательных). Целевой размер основного кода (без вспомогательных) — ~800 строк.
