# Implementation report — t_37246615

## Задача

Сквозная ID-маркировка таблиц `<!-- t_pN_M -->` на всём пути пайплайна
(вырезка → vision-распознавание → скриптовая постобработка → AI), чтобы AI
надёжно сопоставлял OCR-таблицы с vision-эталонами по ID, а не по содержимому.

## Что сделано

### 1. `extract_table_images()` (firmware/src/pipeline.py, ~стр. 1486)

В возвращаемый dict каждой таблицы добавлено поле `id`:

```python
table_images.append({
    "page": pi,
    "table_idx": table_counter,
    "path": fname,
    "id": f"t_p{pi + 1}_{ti}",   # ← новое
})
```

`pi` — 0-based страница, `ti` — 0-based индекс таблицы на странице
(`enumerate(tables)`). Docstring обновлён.

### 2. `recognize_tables_vision()` (~стр. 1657)

При записи `table_N.md` первой строкой добавляется ID-маркер:

```python
safe_write(out_path, f"<!-- {ti['id']} -->\n{result}")
```

### 3. `run_script_postprocess()` (~стр. 2417)

Сигнатура расширена (оба параметра опциональны → обратная совместимость):

```python
def run_script_postprocess(
    md_text: str,
    img_dir: str | Path,
    page_boundaries: list[tuple[int, int]] | None = None,
    table_images: list[dict] | None = None,
) -> str:
```

Вставка ID выполняется после шага 2 («объединение таблиц»), перед шагом 3:

```python
if page_boundaries and table_images:
    md_text = _inject_table_ids(md_text, page_boundaries, table_images)
```

### 4. Новая функция `_inject_table_ids()` (~стр. 2421)

- Разбивает md_text на строки, для каждой строки определяет страницу через
  `page_boundaries` (`[(start, end), ...]`, 0-based, end exclusive).
- Таблица = группа строк `|...|`, перед которой (пропуская пустые строки) идёт
  строка-название (не `|`-строка). Несколько `|`-строк подряд — одна таблица.
- Индекс таблицы на странице сбрасывается на новой странице.
- Маркер `<!-- t_p{page+1}_{idx} -->` вставляется отдельной строкой ПЕРЕД
  строкой-названием.
- Реализована двухпроходно (поиск таблиц → сборка результата), что исключает
  ошибки смещения индексов при вставке.
- `table_images` принимается как признак «включить маркировку» (проверяется в
  `run_script_postprocess`).

### 5. `process_file()` (~стр. 3497)

`table_images` инициализируется `[]` до блока 4b (чтобы быть доступной при
`use_ai=False`), и в вызов постобработки передаются новые параметры:

```python
md_text = run_script_postprocess(
    md_text, img_dir,
    page_boundaries=page_boundaries,
    table_images=table_images if use_ai else None,
)
```

### 6. `config_ai.yaml`

В секции `ai_postprocess.prompt`, в блоке «СВЕРКА ТАБЛИЦ С VISION-распознаванием»,
ПЕРЕД правилом 14 добавлено правило ID-сопоставления:

```yaml
    Таблицы имеют ID-маркеры вида <!-- t_pN_M --> перед названием таблицы
    в обоих блоках (Markdown-файл и Эталонные таблицы). Сверяй таблицы
    с одинаковыми ID: найди в «Эталонных таблицах» таблицу с тем же ID,
    сравни с таблицей в Markdown-файле, исправь/дополни данные.
    НЕ сверяй таблицы с разными ID. УДАЛИ все <!-- t_p... --> маркеры
    из итогового Markdown.
```

Нумерация правил 14–20 не менялась, `_chunk_text`, `SECTION_BOUNDARY_RE`,
`AI_MAX_CHARS`, этапы 6+7 и настройки провайдеров не трогались.

## Изменённые файлы

- `firmware/src/pipeline.py` — пункты 1–5 выше.
- `firmware/src/config_ai.yaml` — пункт 6.
- `firmware/src/test_ai_table.py` — +2 теста (id в extract_table_images,
  маркер в table_N.md).
- `firmware/src/test_table_id_markers.py` — НОВЫЙ: 10 тестов `_inject_table_ids`
  и интеграции `run_script_postprocess`.
- `firmware/src/test_chunk_overlap_table_bbox.py` — обновлено ожидание dict
  extract_table_images (добавлен `id`).
- `firmware/src/test_headings.py`, `test_rag_jsonl.py`, `test_gap_filling.py` —
  моки `run_script_postprocess` обновлены на приём новых kwargs.

## Проверка (критерии приёмки)

1. `extract_table_images()` возвращает `"id": "t_p1_0"` для первой таблицы
   первой страницы — ✅ (`test_extract_table_images_ids`, реальный PDF через fitz).
2. `table_1.md` начинается с `<!-- t_p1_0 -->` — ✅
   (`test_recognize_tables_vision_writes_id_marker`).
3. После `run_script_postprocess()` первая md-таблица имеет
   `<!-- t_p{N}_{M} -->` перед названием — ✅
   (`test_run_script_postprocess_injects_ids`, `test_inject_ids_*`).
4. В `config_ai.yaml` есть правило «сверяй таблицы с одинаковыми ID» — ✅.
5. Существующие тесты: 147 passed (полный набор, кроме устаревшего
   `test_gap_filling.py` — см. «Известные ограничения»).
6. YAML и Python синтаксис без ошибок — ✅ (`yaml.safe_load`, `ast.parse`).
7. `pipeline.py` импортируется без ошибок — ✅ (`import pipeline`).

## Валидация

- `python3 -c "import ast; ast.parse(open('firmware/src/pipeline.py'))"` — OK.
- `python3 -c "import yaml; yaml.safe_load(open('firmware/src/config_ai.yaml'))"` — OK.
- `python3 -c "import pipeline"` — OK.
- `python3 -m pytest -q --ignore=test_gap_filling.py` → **147 passed**.
- `python3 -m pytest test_ai_table.py test_table_id_markers.py` → **22 passed**.
- Промпт-правило присутствует и читается из YAML (assert по ключевым строкам).

## Известные ограничения

1. **`test_gap_filling.py` (7 из 9 тестов падают) — предсуществующая проблема,
   НЕ связана с этой задачей.** Файл не закоммичен и написан под удалённую
   функцию `ai_table`/gap-filling (отдельная секция конфига, `gap_input`,
   флаг `--ai-table`). В рабочем дереве `pipeline.py` эта функция уже удалена
   предыдущими задачами (см. `test_ai_table.py::test_cli_flag_ai_table_removed`,
   который подтверждает удаление флага). `grep` по рабочему дереву: 0 вхождений
   `ai_table`/`gap_input`. Падения этих тестов были и до данной задачи.
   Решение (переписать тесты под текущий поток 6+7 или удалить файл) —
   за ревьюером/оркестратором.

2. **Продолжения таблиц между страницами («Окончание/Продолжение таблицы N»).**
   Индексы в `_inject_table_ids` считаются по итоговому md_text (после склейки
   продолжений в `parse_yandex_json_to_md` и `merge_tables`), а id в
   `extract_table_images` — по `tables[]` в JSON (до склейки). При наличии
   продолжения на странице таблицы ПОСЛЕ него могут получить индекс, не
   совпадающий с vision-id (md-таблица N на странице P ↔ vision t_pP_N±1).
   Следствие безвредное: AI просто не найдёт эталон с тем же ID и не станет
   сверять такую таблицу («НЕ сверяй таблицы с разными ID»). Точное выравнивание
   потребовало бы передачи информации о склейке из `_stitch_continuation_tables`
   — вне рамок утверждённой архитектуры (спека предполагает совпадение
   порядка таблиц md ↔ JSON).

3. `page_boundaries` считаются до вставок изображений/заголовков и шагов 1–2
   постобработки; определение страницы по этим границам — аппроксимация
   (та же, что уже используется в `_insert_images_into_md` и
   `_stitch_continuation_tables`).

## Отклонений от архитектуры

Нет. Реализовано строго по ТЗ: сигнатуры, формат ID и точка вставки
(после шага 2, перед шагом 3) соответствуют утверждённому описанию.
