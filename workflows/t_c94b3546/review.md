# Review: t_c94b3546 — проверка SECTION_BOUNDARY_RE и промпта OCR-диаграмм

## Verdict

**PASS**

## Scope

Проверка реализации задачи t_7056b7bb:
1. `SECTION_BOUNDARY_RE` в `firmware/src/pipeline.py` (~2474)
2. Поведение regex на тестовых строках
3. Промпт правила 4 в `firmware/src/config_ai.yaml`
4. Синтаксис Python и YAML
5. Тесты

## Requirements Compliance

| Requirement | Status | Evidence |
|---|---|---|
| `SECTION_BOUNDARY_RE.match("### Примеры классификации объектов")` → not None | PASS | `<re.Match span=(0,5), match='### П'>` |
| `SECTION_BOUNDARY_RE.match("**СОДЕРЖАНИЕ**")` → not None | PASS | `<re.Match span=(0,14), match='**СОДЕРЖАНИЕ**'>` |
| `SECTION_BOUNDARY_RE.match("Средние расстояния...")` → None | PASS | `None` |
| Синтаксис Python без ошибок | PASS | `ast.parse()` OK |
| Синтаксис YAML без ошибок | PASS | `yaml.safe_load()` OK |
| `pipeline.py` импортируется без ошибок | PASS | Python AST + тесты проходят |
| Правило 4 дополнено очисткой подписей с диаграмм | PASS | Строка 62 содержит уточнение про `![fig_N]` и `*Рис.*` |
| Нумерация 5–20 не сдвинута | PASS | Правила 5–20 на месте, нумерация не изменилась |

## Architecture Compliance

N/A — задача не затрагивает архитектурные изменения. Изменения локализованы:
- `pipeline.py`: только `SECTION_BOUNDARY_RE` (строка 2474–2475) + комментарий (строки 2467–2473)
- `config_ai.yaml`: только правило 4 (строка 62)

Логика `_split_oversized_section`, `_chunk_text`, `AI_MAX_CHARS` не изменялась.

## Implementation Review

### 1. SECTION_BOUNDARY_RE (pipeline.py:2474–2475)

**Фактический код:**
```python
SECTION_BOUNDARY_RE = re.compile(
    r"^(?:#{1,5}\s+\S|\*\*[^*]+\*\*$)"
)
```

**Отклонение от буквального текста задачи:** в задаче указан regex с `$` после закрывающей скобки группы:
```
r"^(?:#{1,5}\s+\S|\*\*[^*]+\*\*)$"
```

Coder корректно идентифицировал, что `$` после группы анкерит ВЕСЬ regex к концу строки, из-за чего заголовки типа `### 2.1. Термины` не матчатся (требуется, чтобы после `\S` шёл конец строки). Это баг в спецификации задачи.

**Исправление:** `$` перемещён внутрь группы, где он анкерит только bold-ветку. Заголовочная ветка матчит префикс строки (без требования конца строки). Это соответствует заявленному поведению: «строка начинается с заголовка ИЛИ строка целиком жирная».

**Вердикт:** корректное и необходимое исправление.

### 2. Поведение regex

Все тесты пройдены:

**Должны матчиться:**
- `### 2.1. Термины` → PASS
- `### Примеры классификации объектов` → PASS
- `**СОДЕРЖАНИЕ**` → PASS
- `# Заголовок` → PASS
- `## 2.1. Термины` → PASS
- `#### 4.7.1. Меры защиты` → PASS
- `##### 4.7.1.1. Дополнительные меры` → PASS
- `**Примеры классификации объектов**` → PASS

**Не должны матчиться:**
- `Средние расстояния...` → PASS (None)
- `**жирный ** текст**` → PASS (None — не вся строка жирная)
- `Обычный текст без заголовка.` → PASS (None)
- `*Таблица 4.3*` → PASS (None — одна звёздочка)
- `| **200 кА** |` → PASS (None — ячейка таблицы)

### 3. Промпт config_ai.yaml

Строка 62 содержит:
```
4. OCR-артефакты: удалить мусор. В том числе одиночные строки из 1–3 слов ЗАГЛАВНЫМИ БУКВАМИ, расположенные между ![fig_N] и *Рис.* — это подписи с диаграмм, их нужно удалить.
```

Соответствует спецификации. Нумерация правил 5–20 не сдвинута.

### 4. Синтаксис

- `pipeline.py`: `ast.parse()` — OK
- `config_ai.yaml`: `yaml.safe_load()` — OK

## Tests

- `test_chunk_overlap_table_bbox.py`: 11 passed
- Полный прогон (по данным coder): 137 passed / 7 failed (все 7 — предсуществующие падения в `test_gap_filling.py`, не связанные с данной задачей)

## Findings

### Finding 1 — Task spec regex bug (RESOLVED by coder)

**Severity:** HIGH (if unfixed — would break acceptance criterion #1)

**Location:** Task body specifies `r"^(?:#{1,5}\s+\S|\*\*[^*]+\*\*)$"` with `$` after group, anchoring entire regex to end-of-string. This fails on multi-word headings like `### 2.1. Термины`.

**Resolution:** Coder moved `$` inside the group — `r"^(?:#{1,5}\s+\S|\*\*[^*]+\*\*$)"` — anchoring only the bold branch. This is the correct semantic: headings match by prefix, bold lines match entire line.

### Finding 2 — Task body test script uses wrong regex (NOTE)

**Severity:** N/A (task documentation issue, not code issue)

**Location:** Task body `t_c94b3546` section 2 test script.

The test script in the task body uses `$` after the group:
```python
R = re.compile(r'^(?:#{1,5}\s+\S|\*\*[^*]+\*\*)$')
```
Running this verbatim would fail on `### 2.1. Термины`. The actual implementation is correct — it's the test script in the task description that's buggy. The review was performed against the actual implementation (with `$` inside the group).

## Required Changes

None.

## Risks

- **Low:** `\S` в заголовочной ветке матчит ЛЮБОЙ непробельный символ после `#...` + пробел. Это может дать ложное срабатывание на строки вида `### *italic heading*` (матчнет `*`). Однако такие строки в реальных документах маловероятны, и последствия — всего лишь дополнительная граница секции (не потеря данных).
- **Low:** Bold-ветка `\*\*[^*]+\*\*$` требует хотя бы одного символа между звёздочками. Пустая bold-строка `****` не матчится — корректно.

## Notes

- Кодер задокументировал отклонение от буквального текста задачи в комментарии к implement-отчёту — практика заслуживает одобрения.
- Комментарий над `SECTION_BOUNDARY_RE` (строки 2467–2473) поясняет семантику regex и причину размещения `$` — полезно для будущих читателей кода.
- Focused diff подтверждает, что изменения ограничены только указанными областями.
