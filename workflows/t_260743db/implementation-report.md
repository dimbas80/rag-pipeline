# Implementation report — t_260743db

## Summary

Реализован модуль `md_to_rag_jsonl` (секция 12, ADR-9) в
`firmware/src/pipeline.py` — конвертация структурированного Markdown
(`##`–`#####` заголовки) в JSONL для RAG-индексации. Добавлены 9 функций
по ADR-9 + 2 вспомогательные (`_find_doc_key`, `_write_rag_jsonl`), новый
конфиг `firmware/src/rag_config.yaml` (документ `so153_molniezashita`),
интеграция в CLI через флаг `--rag`.

## Файлы изменены

- `firmware/src/pipeline.py` — секция «12. RAG JSONL Converter (ADR-9)»
  (~450 строк), интеграция в `parse_args()`/`process_file()`/`main()`.
- `firmware/src/rag_config.yaml` — новый конфиг по
  `docs/software/rag_config_spec.yaml` с одним документом
  `so153_molniezashita` (`ignore_sections: ["Содержание"]`).
- `firmware/src/test_rag_jsonl.py` — новый файл тестов (52 теста).
- `firmware/src/pipeline.py` — защитный guard в `rename_images()`
  (см. «Безопасность» ниже).

## Реализация

### Секция 12 — 9 функций по ADR-9

1. `load_rag_config(path)` — YAML → dict; `FileNotFoundError` /
   `yaml.YAMLError`.
2. `_extract_heading_number(text)` — regex `^(\d+(?:\.\d+)*)\.\s`
   («3.2.1. Молниеприемники» → «3.2.1»).
3. `_build_ancestors(headings, idx)` — {chapter, section, clause} по
   ближайшим предшествующим heading нужного уровня (таблица ADR-9a):
   `## 3.` → {3, null, null}; `### 3.2.` → {3, 3.2, null};
   `#### 3.2.1.` → {3, 3.2, 3.2.1}; `##### 3.2.1.1.` → {3, 3.2, 3.2.1.1}.
   Ненумерованный подпункт наследует clause предыдущего `####`/`#####`.
4. `parse_md_structure(md_text)` — regex `^(#{2,5})\s+(.+)$` → список
   `{level, number, heading_text, line_num, next_line_num}`.
5. `extract_clause_text(md_text, line, next_line)` — строки
   `[heading_line+1 : next_heading_line]`, join, strip.
6. `extract_references(text, patterns)` — re.findall + каноническая форма
   «п. 3.2.1» / «табл. 3.1» (префикс выводится из ключевых слов
   паттерна: пункт→п., табл→табл., разд→разд., гл→гл.), дедупликация,
   сортировка. Приёмка плана: `extract_references("см. п. 3.2.1, табл.
   3.1")` → `["п. 3.2.1", "табл. 3.1"]` ✓.
7. `_get_page_for_heading(number, json_headings)` — первое вхождение
   `number` в результате `_extract_headings_from_json()` (ADR-9c).
8. `_split_oversized_clause(text, max_chars)` — разбиение по параграфам
   (`\n\n`), таблицы (`|`) и кодовые блоки (`````) атомарны; одиночный
   блок > max_chars публикуется как есть с `log.warning` (ADR-9b).
9. `build_rag_jsonl(md_text, json_headings, rag_config, doc_key)` — сборка
   JSONL: doc_meta + defaults + patterns + ignore_sections (skip_until_level),
   пустые clause пропускаются, oversized → подчанки с суффиксом «(ч. N)»
   и повторением метаданных. Поля строки: document_id, document_id_alt,
   title, edition, date_enacted, date_amended, amended_by, chapter,
   section, clause, text, source{file, page}, references.

### Вспомогательные функции

- `_find_doc_key(input_path, rag_config)` — сопоставление source_file →
  doc_key с нормализацией (без учёта регистра и разделителей `-`/`_`,
  поэтому «СО153-34_21_122» == «со153-34-21-122»).
- `_write_rag_jsonl(md_text, json_headings, rag_config, input_path,
  out_dir, file_stem)` — общий вывод `Markdown/<file>/rag_chunks.jsonl`
  (используется в PDF-пути, .md+--ai и .md+--rag).

### Интеграция в CLI

- `parse_args()`: `--rag` (flag), `--rag-config` (default
  `./rag_config.yaml`), примеры в epilog.
- `process_file()`: параметры `use_rag: bool = False, rag_config: dict |
  None = None` добавлены **в конец** сигнатуры (существующие тесты,
  вызывающие функцию позиционно, не ломаются). После сохранения
  итогового .md — Этап 8b: `_write_rag_jsonl(md_text, headings, ...)`.
  Режим `.md` теперь разрешён при `--ai` **или** `--rag` (ADR-9, режим
  `.md + --rag` без OCR: `source.page = null`).
- `main()`: при `--rag` загружает rag_config (ошибка → `log.error`,
  RAG-генерация пропускается, файлы обрабатываются как обычно —
  error handling по architecture.md §7), передаёт в process_file.

## Тесты

Новый `firmware/src/test_rag_jsonl.py` — 52 теста: все 9 функций +
`_find_doc_key`, `_write_rag_jsonl` через process_file (mocked OCR),
интеграцию `.md + --rag`, `parse_args`, реальный JSON
`/mnt/sdb/!База_ГОСТ/tmp/СО153-34_21_122-2003 Молниезащита/yandex_result.json`
(приёмка ADR-9c: 79 строк, source.page у всех, ignore_sections работает).

## Валидация

- `python3 -m pytest test_*.py` — **143 passed** (91 существующих + 52 новых).
- CLI end-to-end на реальных данных (без OCR):
  `python3 pipeline.py -i <so153>.md --rag --rag-config ./rag_config.yaml`
  → `Markdown/СО153-34_21_122-2003 Молниезащита/rag_chunks.jsonl`
  (79 строк, 193 КБ): корректные document_id/title/source.file, главы
  1–4, 42 строки с references, 43 строки с «(ч. N)», 0 пустых text.
- PDF-путь с реальным JSON: 79/79 строк имеют `source.page` из
  `_extract_headings_from_json()`.
- Живой OCR по PDF не запускался (внешний API Yandex, стоимость/время);
  PDF-путь покрыт mocked-тестами `process_file` + реальным JSON.

## Безопасность (важно)

Во время подготовки тестовых данных был вызван
`run_script_postprocess(md, '')` — `rename_images()` с пустым `img_dir`
трактует `Path('')` как `Path('.')` и **переименовал все файлы текущей
папки** в `fig_N.*` (включая `pipeline.py`, конфиги, `.env` с API-ключами).
Всё восстановлено (чистые `mv` — данные не пострадали; `.env` возвращён).
Добавлен защитный guard: `rename_images()` при пустом `img_dir` или
`Path('.')` — no-op с `log.warning`. Рекомендуется в будущем не вызывать
скриптовую постобработку с пустым путём к image-папке.

## Известные ограничения / отклонения

- `defaults.include_tables` / `include_images` из конфига читаются, но не
  фильтруют текст (таблицы/изображения уже встроены в MD как Markdown;
  architecture.md §5.5b не предусматривает фильтрацию).
- `_build_ancestors` опирается на реальные заголовки-предки (по ADR-9),
  а не на разбор номера: если в документе нет явного `###` перед `####`,
  `section` будет null (для ГОСТ-документов иерархия всегда полная).
- Oversized chapter/section без номера clause: суффикс «(ч. N)» не
  добавляется (ADR-9b применяет суффикс только к полю clause).
- `process_file()`: сигнатура расширена в конец (use_rag/rag_config) —
  локальное решение, чтобы не ломать существующие вызовы; план Unit 11
  предполагал другой порядок параметров.
