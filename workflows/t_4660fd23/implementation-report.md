# Implementation report — t_4660fd23

## Summary
Два изменения в `firmware/src/pipeline.py`:

### 1. Overlap 3 строки между чанками AI-постобработки
В `ai_postprocess()` (строка ~2546) при обработке каждого чанка, кроме
первого, в начало добавляется контекст из последних 3 строк предыдущего
чанка, обёрнутый в маркеры `[Контекст из предыдущего чанка]` /
`[Конец контекста]`:

```python
chunk = chunks[i]
if i > 0:
    # Добавляем последние 3 строки предыдущего чанка как контекст
    prev_lines = chunks[i - 1].strip().split("\n")
    context = "\n".join(prev_lines[-3:]) if len(prev_lines) >= 3 else chunks[i - 1].strip()
    chunk = f"[Контекст из предыдущего чанка]\n{context}\n[Конец контекста]\n\n{chunk}"
result = _call_ai_api(chunk, config, f"{file_label} [ч.{i + 1}]")
```

- Один чанк → overlap не добавляется: `ai_postprocess` выходит раньше
  (`if len(chunks) == 1`), в цикле `i > 0` никогда не срабатывает.
- Если предыдущий чанк короче 3 строк — контекстом становится весь
  предыдущий чанк (fallback из задания).
- Fallback при ошибке AI (`results[i] = result if result else chunks[i]`)
  не менялся: в итоговый документ попадает исходный чанк без маркеров.

### 2. boundingBox таблиц вверх на 30px
В `extract_table_images()` (строка ~1417) область вырезки таблицы
поднимается вверх на 30px, чтобы захватить заголовок, с защитой от
выхода за границу страницы:

```python
x0 = min(xs) * sx - 2
y0 = max(0, min(ys) * sy - 32)  # +30px вверх для заголовка
x1, y1 = max(xs) * sx + 2, max(ys) * sy + 2
```

`y0` не опускается ниже 0 (`max(0, ...)`). `x0/x1/y1` не менялись.

## Files changed
- `firmware/src/pipeline.py` — два хунка (строки ~1417 и ~2546)
- `firmware/src/test_chunk_overlap_table_bbox.py` — новый файл тестов (5 тестов)

## Tests added
`test_chunk_overlap_table_bbox.py`:
- `test_ai_postprocess_single_chunk_no_overlap` — 1 чанк: маркеров контекста нет
- `test_ai_postprocess_multi_chunk_overlap_last_3_lines` — 2 чанка: во 2-й
  добавлены ровно последние 3 строки 1-го в обёртке `[Контекст...]...[Конец контекста]`
- `test_ai_postprocess_multi_chunk_overlap_short_prev` — предыдущий чанк < 3 строк:
  контекст = весь предыдущий чанк
- `test_extract_table_images_bbox_extended_up_30px` — `y0 = min(ys)*sy - 32`,
  `x0/x1/y1` не изменены, PNG сохраняется
- `test_extract_table_images_bbox_clamped_at_page_top` — таблица у верхнего края:
  `y0 == 0` (clamp)

## Validation
- `python3 -m pytest test_chunk_overlap_table_bbox.py -v` → 5 passed
- Полный набор `python3 -m pytest -q` (firmware/src) → **65 passed**
  (60 существующих + 5 новых)

## Known limitations
- Если модель проигнорирует маркеры и вернёт контекстный блок как есть,
  он попадёт в итоговый документ — по заданию это ожидаемое поведение
  («AI должен понимать, что ... справочная информация»), без
  дополнительной обрезки в коде.
- Чекпойнт (tmp/.ai_checkpoints) не версионируется: если после обновления
  кода остался чекпойнт от старой версии с тем же числом чанков, готовые
  чанки будут взяты из него (без overlap), а новые — с overlap. Чекпойнт
  удаляется после успешного завершения, поэтому проблема возможна только
  при прерванном предыдущем запуске.

## Deviations from architecture
Нет. `config_ai.yaml` не менялся; логика чанкинга (`_chunk_text`) и вырезки
не переписывалась — только добавлен overlap и подъём области вырезки.
