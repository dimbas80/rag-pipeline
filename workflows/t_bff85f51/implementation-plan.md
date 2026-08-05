# Implementation Plan: Исправления RAG v2 — токенизатор Qwen3, overwrite и режим извлечения таблиц

**Task:** t_bff85f51 (follow-up к t_c16e1de8)
**Parent architecture:** [rag-v2-architecture.md](../../docs/architecture/rag-v2-architecture.md)
**Parent ADR:** [ADR-010](../../docs/architecture/decision-records/adr-010-rag-semantic-assets-and-token-chunking.md)
**Parent plan:** [t_c16e1de8/implementation-plan.md](../t_c16e1de8/implementation-plan.md)
**Type:** Архитектурная коррекция (delta к родительскому плану)

---

## Обзор исправлений

Родительская архитектура выбрала `tiktoken cl100k_base` как токенизатор, но фактическая
embedding-модель проекта — `Qwen3-Embedding-8B` (через SiliconFlow API). Требуется заменить
токенизатор на Qwen3-native и уточнить семантику нескольких архитектурных решений.

**7 обязательных исправлений:**

| # | Исправление | Затрагивает |
|---|-------------|-------------|
| 1 | Токенизатор: `tiktoken cl100k_base` → `Qwen/Qwen3-Embedding-8B` через `transformers.AutoTokenizer` | `_init_tokenizer()`, `rag_config.yaml` |
| 2 | Убрать `tiktoken` из ADR/архитектуры/зависимостей | ADR-010a, architecture.md, rag_config.yaml |
| 3 | Запретить молчаливый chars/token fallback; degraded-режим только явно | `_init_tokenizer()`, error handling |
| 4 | Уточнить `--rag`: перезаписывает JSONL + assets, НЕ трогает .md/image/ | `_run_rag_only()`, документация |
| 5 | Таблицы всегда вырезаются в image/ на этапе 1 (без `--ai`); `--ai` = vision-коррекция | `process_file()` |
| 6 | Проверить Qwen3 tokenizer по официальным источникам, добавить ссылки | ADR-010a |
| 7 | Сохранить ранее принятые решения (status, _source_page, chunk_id, max_chunk_tokens: 7000, etc.) | Все файлы |

---

## Файлы, затрагиваемые изменениями

| Файл | Действие | Описание |
|------|----------|----------|
| `firmware/src/pipeline.py` | Изменить | Секция 13: переписать `_init_tokenizer()` на Qwen3 |
| `firmware/src/pipeline.py` | Изменить | Секция 11: таблицы всегда вырезаются, `--ai` только vision |
| `firmware/src/rag_config.yaml` | Изменить | `tokenizer`, `allow_degraded_fallback`, убрать `tiktoken` references |
| `firmware/src/test_rag_v2.py` | Изменить | Обновить тесты токенизатора |
| `docs/architecture/decision-records/adr-010-...md` | **Уже обновлено** | Исправлен архитектором (этот task) |
| `docs/architecture/rag-v2-architecture.md` | **Уже обновлено** | Исправлен архитектором (этот task) |

> **Примечание:** ADR-010 и rag-v2-architecture.md уже исправлены в рамках этого task (t_bff85f51).
> Coder должен ориентироваться на обновлённые версии в `docs/architecture/`.

---

## Delta Unit 1: Переписать _init_tokenizer() на Qwen3

**Приоритет:** Критический
**Строки:** ~70 (замена существующей ~30 строк)
**Файл:** `firmware/src/pipeline.py`, секция 13

### 1.1 Текущее состояние (родительский план)

```python
def _init_tokenizer(config: dict) -> Callable[[str], int]:
    encoding_name = config.get("defaults", {}).get("tokenizer", "cl100k_base")
    try:
        import tiktoken
        enc = tiktoken.get_encoding(encoding_name)
        return lambda text: len(enc.encode(text))
    except ImportError:
        ratio = float(config.get("defaults", {}).get("tokenizer_fallback_ratio", 3.5))
        log.warning("tiktoken не установлен — использую эвристику chars/token=...")
        return lambda text: max(1, int(len(text) / ratio))
```

### 1.2 Целевое состояние

```python
def _init_tokenizer(config: dict) -> tuple[Callable[[str], int], str]:
    """Загрузить Qwen3-Embedding-8B tokenizer через transformers.AutoTokenizer.

    config['defaults'] должен содержать:
      - tokenizer: str              ("Qwen/Qwen3-Embedding-8B")
      - tokenizer_revision: str     ("main")
      - allow_degraded_fallback: bool (default False)
      - tokenizer_fallback_ratio: float (default 3.5)

    Returns:
        (tokenize_fn, chunking_method)
        chunking_method: "qwen3" | "degraded_chars_per_token"

    Raises:
        RuntimeError — если токенизатор недоступен и fallback запрещён
    """
    model_id = config.get("defaults", {}).get("tokenizer", "Qwen/Qwen3-Embedding-8B")
    revision = config.get("defaults", {}).get("tokenizer_revision", "main")

    # Основной путь: Qwen3-native tokenizer
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            local_files_only=False,
            trust_remote_code=False,
        )
        log.info(f"Токенизатор: transformers/{model_id} (загружен)")
        return (lambda text: len(tokenizer.encode(text)), "qwen3")
    except ImportError:
        log.error("transformers не установлен — RAG-индексация невозможна.")
    except Exception as e:
        log.error(f"Не удалось загрузить токенизатор {model_id}: {e}")

    # Деградированный fallback (только если ЯВНО включён)
    allow_fallback = config.get("defaults", {}).get("allow_degraded_fallback", False)
    if allow_fallback:
        ratio = float(config.get("defaults", {}).get("tokenizer_fallback_ratio", 3.5))
        log.warning(
            f"Деградированный режим: chars/token={ratio}. "
            f"chunking_method='degraded_chars_per_token'"
        )
        return (lambda text: max(1, int(len(text) / ratio)), "degraded_chars_per_token")

    raise RuntimeError(
        "RAG-индексация невозможна: токенизатор Qwen3 недоступен. "
        "Установите: pip install transformers>=4.51.0, "
        "или включите allow_degraded_fallback: true в rag_config.yaml"
    )
```

### 1.3 Ключевые изменения сигнатуры

**Было:** `_init_tokenizer(config) -> Callable[[str], int]`
**Стало:** `_init_tokenizer(config) -> tuple[Callable[[str], int], str]`

Возвращает кортеж `(tokenize_fn, chunking_method)`, где `chunking_method` записывается
в каждую JSONL-строку (поле `chunking_method`).

### 1.4 Обновление всех мест вызова

Все места, где вызывается `_init_tokenizer()`, должны быть обновлены для приёма кортежа:

```python
# Было:
tokenize = _init_tokenizer(rag_config)

# Стало:
tokenize, chunking_method = _init_tokenizer(rag_config)
```

Передать `chunking_method` в `build_rag_jsonl_v2()` для записи в каждую JSONL-строку.

### Приёмка
- `_init_tokenizer({...})` с установленным `transformers` возвращает `(callable, "qwen3")`
- Вызов `tokenize("Привет мир")` возвращает int > 0
- При отсутствии `transformers` и `allow_degraded_fallback: false` → `RuntimeError`
- При отсутствии `transformers` и `allow_degraded_fallback: true` → `(callable, "degraded_chars_per_token")` + `log.warning`
- `chunking_method` проставляется в каждой JSONL-строке

---

## Delta Unit 2: Обновить rag_config.yaml

**Приоритет:** Критический
**Строки:** ~10 изменений
**Файл:** `firmware/src/rag_config.yaml`

### 2.1 Изменения в defaults

Заменить:

```yaml
defaults:
  max_chunk_tokens: 7000
  tokenizer: "cl100k_base"            # УДАЛИТЬ
  tokenizer_fallback_ratio: 3.5       # ОСТАВИТЬ (для degraded)
```

На:

```yaml
defaults:
  max_chunk_tokens: 7000
  tokenizer: "Qwen/Qwen3-Embedding-8B"   # НОВОЕ: HF model id
  tokenizer_revision: "main"             # НОВОЕ: фиксированная ревизия
  allow_degraded_fallback: false         # НОВОЕ: запретить молчаливый fallback
  tokenizer_fallback_ratio: 3.5          # chars/token (только при allow_degraded_fallback: true)
```

### 2.2 Поля, которые НЕ меняются

- `max_chunk_tokens: 7000` — без изменений, теперь считается Qwen3 tokenizer
- `default_status: "active"` — без изменений
- `documents.<slug>.status/status_reason/replaced_by_document_id` — без изменений
- `documents.<slug>.replaced_by_doc_key` — НОВОЕ поле (ключ каталога, опционально); добавлено в архитектуре t_8e1044ee
- `references.patterns` — без изменений

### Приёмка
- `load_rag_config("rag_config.yaml")` возвращает `tokenizer: "Qwen/Qwen3-Embedding-8B"`
- `allow_degraded_fallback` присутствует и равен `false`
- Нет упоминаний `tiktoken` или `cl100k_base`

---

## Delta Unit 3: Таблицы всегда вырезаются в image/

**Приоритет:** Важный
**Строки:** ~15 изменений
**Файл:** `firmware/src/pipeline.py`, секция 11 (`process_file`)

### 3.1 Текущее поведение (родительский план)

Таблицы вырезаются внутри условного блока, возможно связанного с `--ai`.

### 3.2 Целевое поведение

```python
def process_file(...):
    # ... OCR, парсинг, постобработка ...

    # ── Таблицы ВСЕГДА вырезаются (PyMuPDF, локально, без --ai) ──
    if file_type in ("pdf", "docx"):
        table_images = _extract_table_images_from_pdf(pdf_path, tables, output_img_dir)
        log.info(f"  Таблицы вырезаны: {len(table_images)} шт. → {output_img_dir}")

    # ── AI-коррекция — только с --ai ──
    if use_ai:
        md_text = ai_postprocess(md_text, config, file_label)

    # ── RAG-индексация — с --rag ──
    if use_rag:
        ...
```

### 3.3 Что проверить

- Вырезание таблиц через PyMuPDF (`fitz`) не требует `--ai`
- Функция вырезания таблиц уже существует или легко выделяется из существующей `extract_images_from_pdf()`
- `--ai` управляет только вызовом `ai_postprocess()`

### Приёмка
- `python3 pipeline.py -i file.pdf` (без `--ai`) → таблицы вырезаны в `image/table_N.png`
- `python3 pipeline.py -i file.pdf --ai` → таблицы вырезаны + AI-коррекция запущена
- `python3 pipeline.py -i file.pdf --rag` (без `--ai`) → таблицы вырезаны + JSONL/assets созданы

---

## Delta Unit 4: _run_rag_only() — перезапись JSONL/assets

**Приоритет:** Важный
**Строки:** ~5 изменений (только логи)
**Файл:** `firmware/src/pipeline.py`, секция 15

### 4.1 Изменения

Функция `_run_rag_only()` уже реализует безопасный режим (не модифицирует .md).
Требуются минимальные изменения:

1. Обновить лог-сообщения — явно указать «перезаписан»:
   ```python
   log.info(f"  RAG JSONL перезаписан: {rag_path} ({len(jsonl.splitlines())} строк)")
   log.info(f"  RAG Assets перезаписан: {assets_path}")
   ```

2. Добавить атомарную запись через `safe_write()` для JSONL:
   ```python
   safe_write(rag_path, jsonl)  # атомарно (write + rename)
   ```

3. Для `rag_assets.json` использовать `safe_write()` или явный `json.dump()`
   (JSONL уже использует `safe_write`, assets можно тем же способом).

### 4.2 Что НЕ менять

- Логика классификации `_classify_input()` — без изменений
- Проверка `image/` — без изменений
- Поиск doc_key — без изменений

### Приёмка
- `_run_rag_only()` НЕ модифицирует .md файл (md5 до и после совпадает)
- `rag_chunks.jsonl` перезаписывается при каждом вызове
- `rag_assets.json` перезаписывается при каждом вызове
- Повторный вызов `_run_rag_only()` даёт идентичный результат (идемпотентность)
- `image/` не модифицируется

---

## Delta Unit 5: Обновить тесты токенизатора

**Приоритет:** Важный
**Строки:** ~40 изменений/новых
**Файл:** `firmware/src/test_rag_v2.py`

### 5.1 Тесты на замену

Заменить tiktoken-тесты на Qwen3-тесты:

```python
class TestInitTokenizer:
    def test_init_qwen3_available(self):
        """При установленном transformers возвращает (callable, 'qwen3')."""
        ...

    def test_init_qwen3_tokenize_russian(self):
        """Токенизация русского текста через Qwen3 tokenizer."""
        ...

    def test_init_qwen3_tokenize_empty(self):
        """Пустой текст → 0 токенов."""
        ...

    def test_init_no_transformers_no_fallback_raises(self):
        """Без transformers и allow_degraded_fallback=false → RuntimeError."""
        ...

    def test_init_no_transformers_with_fallback(self):
        """Без transformers, но allow_degraded_fallback=true → degraded mode."""
        ...

    def test_init_transformers_import_error(self, mocker):
        """ImportError при загрузке transformers → degraded или ошибка."""
        ...

    def test_chunking_method_in_output(self):
        """chunking_method присутствует в каждой JSONL-строке."""
        ...
```

### 5.2 Тесты на удаление

Удалить тесты, специфичные для tiktoken:
- `test_init_tiktoken_available`
- `test_init_tiktoken_unavailable_fallback`
- `test_fallback_ratio_russian` (заменить на Qwen3-версию)

### Приёмка
- `python3 -m pytest firmware/src/test_rag_v2.py -v -k "tokenizer"` — все проходят
- Нет импортов `tiktoken` в тестах
- Есть хотя бы один тест, проверяющий `chunking_method` в JSONL-выводе

---

## Delta Unit 6: Обновить зависимости

**Приоритет:** Критический
**Файлы:** `requirements.txt` или эквивалент

### 6.1 Изменения

Добавить:
```
transformers>=4.51.0
```

Удалить (если был):
```
tiktoken>=0.5.0
```

### Приёмка
- `pip install transformers>=4.51.0` успешно
- `from transformers import AutoTokenizer` работает
- `AutoTokenizer.from_pretrained("Qwen/Qwen3-Embedding-8B")` загружает токенизатор

---

## Delta Unit 7: Обновить существующие тесты

**Приоритет:** Средний
**Файл:** `firmware/src/test_rag_jsonl.py`

### 7.1 Изменения

1. `test_load_rag_config_ok`: проверить `tokenizer: "Qwen/Qwen3-Embedding-8B"` (вместо `cl100k_base`)
2. `test_load_rag_config_ok`: проверить `allow_degraded_fallback: false`
3. `test_build_rag_jsonl_every_line_valid`: добавить `chunking_method` в список обязательных полей
4. Все тесты, проверяющие JSONL-вывод: убедиться, что `chunking_method` присутствует

### Приёмка
- Существующие тесты проходят после обновления ожиданий
- `chunking_method` проверяется в integration-тестах

---

## Порядок реализации

```
Шаг 1: Зависимости
  Delta Unit 6: Обновить requirements.txt           (~5 мин)

Шаг 2: Конфигурация
  Delta Unit 2: Обновить rag_config.yaml            (~10 мин)

Шаг 3: Токенизатор
  Delta Unit 1: Переписать _init_tokenizer()        (~45 мин)
  Delta Unit 5: Обновить тесты токенизатора         (~30 мин)

Шаг 4: CLI и behaviour
  Delta Unit 3: Таблицы всегда вырезаются           (~20 мин)
  Delta Unit 4: _run_rag_only() — перезапись        (~10 мин)

Шаг 5: Интеграция
  Delta Unit 7: Обновить существующие тесты         (~15 мин)
  Полный прогон тестов                              (~10 мин)
```

### Общая оценка

| Шаг | Часов |
|-----|-------|
| Зависимости | 0.1 |
| Конфигурация | 0.2 |
| Токенизатор | 1.25 |
| CLI и behaviour | 0.5 |
| Интеграция | 0.4 |
| **Всего** | **~2.45 ч** |

---

## Acceptance Criteria (финальные)

1. `_init_tokenizer()` использует `transformers.AutoTokenizer("Qwen/Qwen3-Embedding-8B")`
2. `tiktoken` полностью удалён из кодовой базы, конфига и зависимостей
3. При недоступном Qwen3 tokenizer и `allow_degraded_fallback: false` — `RuntimeError` (RAG отказывается)
4. При `allow_degraded_fallback: true` — деградированный режим с `log.warning` и `chunking_method: "degraded_chars_per_token"`
5. `--rag` не модифицирует `.md` и `image/`, атомарно перезаписывает `rag_chunks.jsonl` и `rag_assets.json`
6. Таблицы вырезаются в `image/table_N.png` всегда (без `--ai`); `--ai` только для vision/AI-коррекции
7. `.md --rag` не запускает OCR и не переизвлекает таблицы
8. `chunking_method` присутствует в каждой JSONL-строке
9. Все ранее принятые решения сохранены: status/status_reason, `replaced_by_document_id` (официальный номер документа, исправлено в t_8e1044ee), `replaced_by_doc_key` (ключ каталога, новое поле), `_source_page`, `rag_assets.json`, семантические asset/chunk IDs, `max_chunk_tokens: 7000`, таблица остаётся в финальном MD
10. Все тесты проходят (`test_rag_jsonl.py` + `test_rag_v2.py`)
11. Нет регрессии: существующий функционал `--ai`, `--config`, обработка PDF/DOCX не нарушен
