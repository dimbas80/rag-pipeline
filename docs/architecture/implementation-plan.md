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

## Unit 3b: Heading Extractor

**Цель:** Извлечь заголовки разделов из Yandex JSON и заменить `**жирные**` заголовки в MD на правильные `##`/`###`/`####`.

**Строки:** ~60

**Функции:**
1. `_extract_headings_from_json(pages)` — извлечение заголовков по 5 правилам
2. `_apply_headings_to_md(md_text, headings)` — замена `**text**` на `#{level+1} text` в MD

**Алгоритм `_extract_headings_from_json()`:**
```
1. Построить per-page индекс Y-координат всех блоков (для правила 4).
2. Для каждой страницы, для каждого блока:
   a. Извлечь text, y_top, x_left, width
   b. Вычислить rel_x = x_left / width
   c. Правило 1: text.match(r'^\d+\.(\d+\.)*\s')
   d. Правило 2: len(text) < 100 AND len(text.split()) < 10
   e. Правило 3: rel_x < 0.50
   f. Правило 4: count_blocks_at_y(page, y_top, tolerance=15) == 1
   g. Правило 5: level = text.match(r'^(\d+(?:\.\d+)*)\.')[1].count('.')
3. Вернуть список {page, y, level, number, text, full_text}
```

**Коррекция regex:** `^\d+\.(\d+\.)*\s` (не `^\d+(\.\d+)*\s`) — номер всегда заканчивается точкой перед пробелом: «3. ЗАЩИТА», «3.2.1. Молниеприемники».

**Алгоритм `_apply_headings_to_md()`:**
```
Для каждого heading в headings:
  1. Построить паттерн: r'\*\*' + re.escape(full_text) + r'\*\*'
  2. Заменить на: '#' * (level + 1) + ' ' + full_text
  3. Использовать re.sub(count=1) — заменять только первое вхождение
     (дубликаты в оглавлении останутся **жирными**, что корректно)
Edge cases:
  - headings пуст → вернуть md_text без изменений
  - full_text содержит спецсимволы → re.escape()
  - Заголовок уже `# ...` → не заменять (проверка: строка начинается с **)
```

**Интеграция в `process_file()`:**
```python
# После Этапа 3 (parse_yandex_json_to_md), перед Этапом 5 (run_script_postprocess)
headings = _extract_headings_from_json(pages)
if headings:
    md_text = _apply_headings_to_md(md_text, headings)
    log.info(f"  Заголовков заменено: {len(headings)}")
else:
    log.info("  Заголовки не найдены — MD без изменений")
```

**Зависимости:** Unit 0 (re, collections.defaultdict), Unit 3 (формат pages из parse_yandex_json_to_md)

**Приёмка:**
- Тестовый JSON (29 стр, СО153-34.21.122-2003) → 68 заголовков извлечено
- `**3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**` → `## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ`
- `**3.2.1. Молниеприемники**` → `#### 3.2.1. Молниеприемники`
- `**200 кА**` (табличное значение) → НЕ заменяется (нет в headings)
- `**1. ВВЕДЕНИЕ**` → `## 1. ВВЕДЕНИЕ` (rel_x=0.455 < 0.50)
- Заголовки в оглавлении (дубликаты) → НЕ заменяются (count=1)
- JSON без blocks[] → `_extract_headings_from_json()` → `[]`, MD без изменений

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

## Unit 10b: Fix — Section-aware chunking + prompt table immobility

**Цель:** Устранить дублирование таблиц в конце документа при AI-постобработке больших файлов.

**Спецификация:** [ADR-007](decision-records/adr-007-section-aware-chunking.md)

**Строки:** ~50 новых, ~30 изменённых

### 10b.1 `_chunk_text()` — section-aware chunking

**Алгоритм (двухфазный):**

```
ФАЗА 1: Партиционирование на логические секции

SECTION_BOUNDARY_RE = r'^(?:#{1,4}\s+\d+(?:\.\d+)*\s|\*\*\d+(?:\.\d+)*\s+[^*]+?\*\*)'

1. Разбить текст на строки
2. Найти индексы строк, совпадающих с SECTION_BOUNDARY_RE
3. Если границ не найдено → fallback на старую логику
4. Сформировать секции:
   - preamble = строки 0..first_boundary
   - sections[i] = строки boundary[i]..boundary[i+1]
5. Каждая секция — строка (join lines через "\n")

ФАЗА 2: Сборка чанков из целых секций

chunks = []
current = ""
for section in sections:
    if не умещается в current:
        if section сама > max_chars:
            flush current в chunks
            sub = _split_oversized_section(section, max_chars)
            добавить sub в chunks
        else:
            chunks.append(current)
            current = section
    else:
        добавить section к current
flush current в chunks
```

**`_split_oversized_section()`** — текущая реализация `_chunk_text()` (потоковая разбивка по пустым строкам с сохранением таблиц и кодовых блоков).

**Сигнатура (не меняется):**
```python
def _chunk_text(text: str, max_chars: int = AI_MAX_CHARS) -> list[str]:
    """Разбить текст на части для AI по границам разделов.
    Если разделов нет — fallback на разбивку по параграфам."""
```

### 10b.2 `config_ai.yaml` — фикс промпта

**Удалить из `ai_postprocess.prompt` секции «ЧТО МОЖНО ДЕЛАТЬ»:**

- Пункт 13: «Объединить таблицы по следующим признакам...» — **УДАЛИТЬ**
- Пункт 14: «Примечания и сноски под таблицами...» — **УДАЛИТЬ**
- Пункт 15: «Форматирование подписей Таблица N» — **УДАЛИТЬ**

Удалить потому что: скриптовая постобработка (`merge_tables()`, `fix_notes()`, `fix_table_fig_labels()`) уже выполняет эти операции.

**Добавить в секцию «ЧТО НЕЛЬЗЯ»:**

```yaml
    - НЕ перемещай таблицы. Каждая таблица должны остаться на своём исходном
      месте в тексте. НЕ собирай таблицы в конце документа. НЕ меняй
      порядок следования таблиц. Таблицы НЕЛЬЗЯ выносить из раздела,
      к которому они относятся.
```

### 10b.3 Тесты

**Обновить `test_chunk_overlap_table_bbox.py`:**

1. **test_section_aware_chunking:** Подать Markdown с bold-заголовками (`**1. Title**`, `**1.1. Sub**`) и `###` — проверить что каждый чанк начинается с границы раздела
2. **test_no_boundaries_fallback:** Подать текст без заголовков — проверить что отрабатывает старая логика
3. **test_oversized_section:** Подать секцию > max_chars — проверить что разбивается по параграфам, таблицы не разорваны
4. **test_preamble:** Подать текст с преамбулой перед первым заголовком — преамбула в первом чанке

**Зависимости:** Unit 10 (существующий `_chunk_text`)

**Приёмка:**
- `_chunk_text()` на тестовом документе СО153-34.21.122-2003 (1465 строк) создаёт чанки, начинающиеся с границ разделов
- Промпт `ai_postprocess` не содержит пп. 13-15, содержит запрет на перемещение таблиц
- Повторный `ai_postprocess()` на тестовом документе не дублирует таблицы
- Все существующие тесты проходят
- Fallback-логика работает для текстов без разделов

---

## Unit 12: RAG JSONL Converter

**Цель:** Конвертировать структурированный Markdown (c `##`/`###`/`####`/`#####` заголовками) в JSONL для RAG-индексации.

**Строки:** ~150

**Спецификация:** [ADR-009](decision-records/adr-009-md-to-rag-jsonl.md), [architecture.md §5.5b](../architecture.md#55b-rag-jsonl-converter)

**Функции:**

1. `load_rag_config(config_path)` — загрузить `rag_config.yaml`
2. `_extract_heading_number(heading_text)` — извлечь номер `"3.2.1"` из текста заголовка; regex `^(\d+(?:\.\d+)*)\.\s`
3. `_build_ancestors(headings, idx)` — построить `{chapter, section, clause}` для heading по ближайшим предкам меньшего уровня
4. `parse_md_structure(md_text)` — разобрать MD на список clause; regex `^(#{2,5})\s+(.+)$`; вернуть `[{level, number, heading_text, line_num, next_line_num}]`
5. `extract_clause_text(md_text, heading_line, next_heading_line)` — извлечь текст между заголовками; строки `[heading_line+1 : next_heading_line]`
6. `extract_references(text, patterns)` — regexp-извлечение кросс-ссылок с дедупликацией
7. `_get_page_for_heading(number, json_headings)` — индекс `{number: page}` из `_extract_headings_from_json()` результата
8. `_split_oversized_clause(text, max_chars)` — разбить по параграфам, не разрывая таблицы/код-блоки
9. `build_rag_jsonl(md_text, json_headings, rag_config, doc_key)` — собрать JSONL

**Алгоритм `build_rag_jsonl()`:**
```
1. doc_meta = rag_config['documents'][doc_key]
2. max_chars = defaults.get('max_chunk_chars', 1500)
3. patterns = rag_config['references']['patterns'] (если extract_references)
4. ignore = doc_meta.get('ignore_sections', [])
5. headings = parse_md_structure(md_text)
6. Для каждого heading:
   a. Пропустить если в ignore_sections (с подразделами: skip_until_level)
   b. text = extract_clause_text(); пропустить если пустой
   c. ancestors = _build_ancestors(headings, i)
   d. page = _get_page_for_heading(number, json_headings)
   e. refs = extract_references(text, patterns)
   f. Подчанки = _split_oversized_clause(text, max_chars)
      Для каждого: JSON record с суффиксом «(ч. N)» в clause
7. Вернуть "\n".join(json_lines) + "\n"
```

**Зависимости:** Unit 0 (json, re, yaml, log), Unit 3b (`_extract_headings_from_json`)

**Приёмка:**
- `parse_md_structure()` на тестовом MD (СО153-34, 1465 строк) → ~50-80 heading
- `_build_ancestors()` для `#### 3.2.1.` → `{chapter: "3", section: "3.2", clause: "3.2.1"}`
- `extract_references("см. п. 3.2.1, табл. 3.1")` → `["п. 3.2.1", "табл. 3.1"]`
- `_split_oversized_clause(2500 chars)` → 2 чанка, таблицы не разорваны
- `_get_page_for_heading("3.2.1", json_headings)` → 7
- `build_rag_jsonl()` → валидный JSONL, каждая строка `json.loads()`
- `ignore_sections: ["Содержание"]` → heading + подразделы пропущены
- Oversized clause → подчанки с «(ч. 1)», «(ч. 2)»
- `source.page = null` когда `json_headings` пуст


## Unit 11 (updated): CLI & Main — RAG Integration

---


## Unit 11: CLI & Main — Интеграция

**Цель:** Собрать все юниты в рабочий пайплайн.

**Строки:** ~120

**Функции:**
1. `parse_args(argv)` — argparse (адаптировать из Create_Markdown_mineru.py:1891-1925)
   - `-i/--input` (required)
   - `--ai` (flag)
   - `--rag` (flag) — **новый: генерация JSONL**
   - `--rag-config` (default: `./rag_config.yaml`) — **новый: путь к rag_config.yaml**
   - `--config` (default: `./config_ai.yaml`)
   - **Убрать** `--backend` (в Yandex один backend)

2. `process_file(input_path, use_ai, use_rag, config, rag_config, api_key, folder_id, output_base, tmp_base)` — обновлённый оркестратор

3. `main()` — обновлённая точка входа

**Алгоритм `process_file()`:**
```
1. Определить file_stem из input_path
2. Если DOCX/DOC → convert_docx_to_pdf() → pdf_path
   Иначе pdf_path = input_path
3. send_to_yandex_ocr(pdf_path, ...) → pages
   Сохранить pages как tmp/<file>/yandex_result.json
4. parse_yandex_json_to_md(json_path, pages) → (md_text, pictures, page_boundaries)
   Сохранить md_text как tmp/<file>/raw.md
4b. _extract_headings_from_json(pages) → headings
    Если headings не пуст: _apply_headings_to_md(md_text, headings) → md_text
5. Если есть pictures → extract_images_from_pdf(pdf_path, pictures, image_dir)
   Вставить ссылки ![Рис. N](image/fig_N.ext) в md_text
6. run_script_postprocess(md_text, image_dir) → md_text
7. Если --ai → ai_postprocess(md_text, config, file_stem) → md_text
8. safe_write(Markdown/<file>/<file>.md, md_text)
9. Если --rag:                                                      ← НОВОЕ
   a. doc_key = _find_doc_key(input_path, rag_config)
   b. Если doc_key найден:
      jsonl = build_rag_jsonl(md_text, headings, rag_config, doc_key)
      safe_write(Markdown/<file>/rag_chunks.jsonl, jsonl)
   c. Иначе: log.warning(f"Документ не найден в rag_config: {file_stem}")
10. Переместить промежуточные файлы в tmp/<file>/
```

**Алгоритм `main()`:**
```
1. parse_args()
2. load_env() → YANDEX_API_KEY, YANDEX_FOLDER_ID, PROVOD_API_KEY
3. load_config() → ai_config (если --ai)
4. load_rag_config() → rag_config (если --rag)                        ← НОВОЕ
5. setup_logging(tmp/Create_Markdown_VisionOCR.log)
6. find_input_files() → список файлов
7. Для каждого файла:
     try: process_file()
     except Exception: log.error(), failed++
8. Вывести статистику: успешно N, ошибок M
9. exit(1) если были ошибки
```

**Зависимости:** Все юниты 1–12

**Приёмка:**
- `python3 pipeline.py -i test.pdf` → создаёт `Markdown/test/test.md` + `image/`
- `python3 pipeline.py -i test.docx` → конвертирует, обрабатывает
- `python3 pipeline.py -i dir/` → обрабатывает все файлы в папке
- `python3 pipeline.py -i test.pdf --ai` → включает AI-постобработку
- `python3 pipeline.py -i test.pdf --rag` → создаёт `Markdown/test/rag_chunks.jsonl`
- `python3 pipeline.py -i test.pdf --ai --rag` → AI + RAG вместе
- Лог пишется в `tmp/Create_Markdown_VisionOCR.log`

---

## Порядок реализации

```
Фаза 1 (фундамент):  Unit 0 → Unit 1
Фаза 2 (OCR):        Unit 2 → Unit 3 → Unit 3b → Unit 4
                      (2 и 3+3b+4 можно параллельно)
Фаза 3 (постобр):    Unit 5 → Unit 6 → Unit 7 → Unit 8 → Unit 9
                      (5,6,7,8 могут параллельно после прототипа Unit 9)
Фаза 4 (AI):         Unit 10 → Unit 10b
Фаза 5 (сборка):     Unit 11
Фаза 6 (RAG):        Unit 12
```

### Приоритеты

| Приоритет | Юнит | Причина |
|-----------|------|---------|
| P0 | Unit 2 | Ключевая интеграция с Yandex — без неё ничего не работает |
| P0 | Unit 3 | Парсинг JSON — определяет качество выходного Markdown |
| P1 | Unit 3b | Заголовки → RAG-индексация, структурная целостность |
| P0 | Unit 11 | Интеграция — нужна для E2E-тестирования |
| P1 | Unit 4 | Извлечение изображений — критично для ГОСТ-документов |
| P1 | Unit 5,6 | LaTeX и таблицы — основные артефакты |
| P2 | Unit 1 | Утилиты — нужны всем, но тривиальны |
| P2 | Unit 7,8 | Подписи и примечания — улучшение читаемости |
| P3 | Unit 10 | AI — опциональный флаг |
| P1 | Unit 10b | Фикс дублирования таблиц — критический баг |
| P2 | Unit 12 | RAG JSONL — новый функционал, зависит от Unit 3b |

---

## Оценка трудозатрат

| Юнит | Строки | Сложность | Часов |
|------|--------|-----------|-------|
| 0 | 20 | низкая | 0.3 |
| 1 | 80 | низкая (копирование) | 0.5 |
| 2 | 120 | средняя (API, ретраи) | 2.0 |
| 3 | 200 | высокая (парсинг, несколько источников) | 3.0 |
| **3b** | 60 | средняя (алгоритм + regex) | 1.0 |
| 4 | 80 | высокая (PyMuPDF, сопоставление) | 2.5 |
| 5 | 80 | низкая (копирование) | 0.5 |
| 6 | 180 | низкая (копирование) | 0.5 |
| 7 | 60 | низкая (копирование + замена путей) | 0.5 |
| 8 | 60 | низкая (копирование) | 0.3 |
| 9 | 30 | низкая (сборка) | 0.3 |
| 10 | 100 | низкая (копирование) | 0.5 |
| **10b** | 80 | средняя (алгоритм + тесты) | 1.5 |
| **12** | 150 | средняя (алгоритм, regex, JSONL) | 2.5 |
| 11 | 120 | средняя (интеграция) | 2.0 |
| **Всего** | **~1420** | | **~17.9 ч** |

> Оценка выше 800 строк из-за копирования вспомогательных функций таблиц (Unit 6 — 180 строк вспомогательных). Целевой размер основного кода (без вспомогательных) — ~800 строк.
