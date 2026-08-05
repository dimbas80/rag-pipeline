# Create Markdown YA

Конвертация PDF/DOCX/DOC в Markdown через Yandex Vision OCR с AI-постобработкой.

## Назначение

Скрипт `pipeline.py` распознаёт текст, таблицы, формулы и изображения из документов, собирает структурированный Markdown и улучшает результат с помощью AI (DeepSeek / Gemini через Provod). Таблицы проходят двойную обработку: OCR из текстового слоя + vision-распознавание из картинок, после чего AI сверяет обе версии по ID-маркерам и выдаёт исправленную таблицу на исходном месте в тексте.

## Быстрый старт

```bash
cd firmware/src

# Установка зависимостей
pip install httpx pyyaml pymupdf beautifulsoup4 "transformers>=4.51.0"

# Базовое распознавание (без AI)
python3 pipeline.py -i document.pdf

# Полный цикл с AI
python3 pipeline.py -i document.pdf --ai

# Только AI-постобработка готового .md (без OCR)
python3 pipeline.py -i result.md --ai
```

## Опции командной строки

| Опция | Описание |
|-------|----------|
| `-i`, `--input` | **Обязательно.** Входной файл (`.pdf`, `.docx`, `.doc`, `.md`) или папка с файлами |
| `--ai` | Полный AI-цикл: vision-распознавание таблиц → единый AI-проход (коррекция таблиц + постобработка). Для `.md` — только AI-постобработка (без OCR). |
| `--rag` | Генерация `rag_chunks.jsonl` для RAG-индексации (см. секцию «Генерация RAG JSONL») |
| `--rag-config` | Путь к `rag_config.yaml`. По умолчанию: `./rag_config.yaml` |
| `--config` | Путь к конфигурационному файлу AI. По умолчанию: `./config_ai.yaml` |

## Пайплайн обработки

### Для PDF/DOCX

```
DOCX → PDF (LibreOffice)
  → Yandex Vision OCR (модель math-markdown)
  → JSON → Markdown (блоки, таблицы, картинки, page_boundaries)
  → Заголовки разделов из JSON (##/###/####)
  → Извлечение изображений из PDF → вставка ![fig_N] по координатам
  → [--ai] Vision-распознавание таблиц (Gemini → table_N.md с ID-маркером)
  → Скриптовая постобработка:
      1. HTML-таблицы → MD
      2. Объединение таблиц (Продолжение/Окончание)
      2b. Вставка ID-маркеров <!-- t_p{page}_{idx} --> перед названиями таблиц
      3. LaTeX-очистка
      4. Переименование изображений
      5. Подписи → курсив
      6. Примечания → цитаты
      7. Подписи «Таблица N», «Рис. N»
      8. OCR-артефакты
      9. Пробелы вокруг ^ в формулах
  → [--ai] Единый AI-проход (DeepSeek v4-pro, чанки по 80K):
      · md_text чанкуется отдельно (по границам разделов)
      · для каждого чанка подставляются только релевантные vision-таблицы (по ID)
      · AI сверяет таблицы с одинаковыми ID, исправляет/дополняет данные in-place
      · выполняет полную постобработку: LaTeX, OCR-артефакты, форматирование
      · удаляет все ID-маркеры из выдачи
      · пост-проверка: warning при неснятых маркерах
      · лог: «Чанков с эталонами: N/M»
  → Сохранение: Markdown/<имя_файла>/<имя_файла>.md
```

### Для .md + --ai

Только AI-постобработка готового Markdown (без OCR и скриптовой части). Использует тот же объединённый промпт из `ai_postprocess`.

## Конфигурационный файл: config_ai.yaml

Содержит две независимые секции:

### `table_vision` — Vision-распознавание таблиц

Изображения таблиц (PNG, вырезанные из PDF) отправляются в Gemini.

| Поле | Описание |
|------|----------|
| `provider` | Провайдер: `provod` |
| `model` | Модель: `google/gemini-2.5-flash-lite` |
| `api_key_env` | Переменная окружения с API-ключом: `PROVOD_API_KEY` |
| `base_url` | Базовый URL API: `https://api.provod.ai/v1` |
| `prompt` | Промпт для vision-модели — инструкция по переводу таблицы в Markdown |
| `fallback` | Резервный провайдер (если primary недоступен) |

### `ai_postprocess` — Единая AI-обработка (постобработка + сверка таблиц)

Финальная чистка всего документа + сверка таблиц с vision-эталонами по ID-маркерам.

| Поле | Описание |
|------|----------|
| `provider` | Провайдер: `deepseek` |
| `model` | Модель: `deepseek-v4-pro` |
| `api_key_env` | Переменная окружения: `DEEPSEEK_API_KEY` |
| `base_url` | `https://api.deepseek.com/v1` |
| `prompt` | **Объединённый промпт**: правила 1–13 (постобработка: LaTeX, OCR-артефакты, форматирование), правила 14–20 (сверка таблиц: сравнение по ID, заполнение ячеек, in-place редактирование) |
| `fallback` | Резервный провайдер: `provod` / `google/gemini-3.5-flash` |

**Важно:** если `prompt` не задан — скрипт завершится с ошибкой. Дефолтных промтов нет.

## Механизм ID-маркировки таблиц

Каждая таблица получает сквозной ID `t_p{страница}_{индекс}` на всём пути пайплайна:

1. **Вырезка** (`extract_table_images`): каждой таблице присваивается ID по странице и порядковому индексу на странице
2. **Vision-распознавание**: ID записывается первой строкой `table_N.md` как `<!-- t_pN_M -->`
3. **Скриптовая постобработка**: `_inject_table_ids()` вставляет такой же маркер перед названием md-таблицы, определяя страницу по `page_boundaries`
4. **AI-проход**: для каждого чанка извлекаются ID, подставляются только релевантные vision-эталоны. AI сверяет таблицы с одинаковыми ID, удаляет маркеры из выдачи
5. **Пост-проверка**: если после AI остались `<!-- t_p... -->` — warning в лог

## Генерация RAG JSONL v2 (`--rag`)

Флаг `--rag` создаёт/перезаписывает файлы `rag_chunks.jsonl` (JSONL v2, ADR-010)
и `rag_assets.json` (реестр таблиц/изображений) для индексации документа в
RAG-системах (ChromaDB, Qdrant и т.д.).

### Трёхэтапный режим (ADR-010e)

1. **Генерация** (PDF/DOCX): таблицы ВСЕГДА вырезаются в `image/table_N.png`
   локально через PyMuPDF, независимо от `--ai`; `--ai` добавляет
   vision/AI-коррекцию.
2. **Проверка человеком**: правки в `Markdown/<файл>/<файл>.md`.
3. **RAG-индексация** (`.md` внутри `Markdown/` + `--rag`): читает только
   проверенный Markdown, БЕЗ OCR/AI/извлечения, атомарно перезаписывает
   `rag_chunks.jsonl` + `rag_assets.json`. `.md` и `image/` не модифицируются.

### Использование

```bash
python3 pipeline.py -i file.pdf --rag                # JSONL v2 + assets
python3 pipeline.py -i file.pdf --ai --rag           # AI + RAG
python3 pipeline.py -i file.pdf --rag --rag-config my_rag.yaml
python3 pipeline.py -i Markdown/file/file.md --rag   # RAG-индексация проверенного MD
```

### Формат выхода (JSONL v2)

Каждая строка `rag_chunks.jsonl` — валидный JSON-объект:

```json
{
  "document_id": "СО 153-34.21.122-2003",
  "title": "Инструкция по устройству молниезащиты...",
  "status": "active",
  "chunk_id": "so153_molniezashita/3.2.1",
  "chapter": "3",
  "section": "3.2",
  "clause": "3.2.1",
  "section_path": "3 → 3.2 → 3.2.1",
  "heading_texts": {"chapter": "3. ЗАЩИТА...", "section": "3.2. Внешняя...", "clause": "3.2.1. Молниеприемники"},
  "text": "Молниеприемники могут быть специально установленными...",
  "source": {"file": "СО153-...pdf"},
  "_source_page": 7,
  "assets": ["so153_molniezashita/table/1"],
  "references": ["п. 3.2.2", "табл. 3.4"],
  "chunk_tokens": 1450,
  "chunking_method": "qwen3"
}
```

### Алгоритм

1. MD-документ разбирается на структурные единицы (chapter/section/clause) по заголовкам `##`/`###`/`####`
2. Каждая единица с непустым текстом становится отдельной JSON-строкой
3. Токены считаются Qwen3-native токенизатором (`Qwen/Qwen3-Embedding-8B` через `transformers.AutoTokenizer`)
4. Номера страниц сохраняются как внутреннее `_source_page` (публичное `source.page` удалено)
5. Кросс-ссылки («см. п. 3.2.2», «табл. 3.4») извлекаются по regexp-паттернам из конфига
6. Чанки длиннее `max_chunk_tokens` (по умолчанию 7000) разбиваются с суффиксом `/part_N` и «(ч. N)»

### Конфигурация: rag_config.yaml

| Секция | Поле | Описание |
|--------|------|----------|
| `defaults` | `max_chunk_tokens` | Максимальный размер чанка в токенах (по умолчанию 7000) |
| `defaults` | `tokenizer` | HF model id токенизатора: `Qwen/Qwen3-Embedding-8B` |
| `defaults` | `allow_degraded_fallback` | `false`: токенизатор обязателен; `true`: явный chars/token fallback с warning |
| `defaults` | `tokenizer_fallback_ratio` | chars/token для degraded-режима (по умолчанию 3.5) |
| `defaults` | `extract_references` | Извлекать кросс-ссылки (`true`/`false`) |
| `references` | `patterns` | Список regexp для поиска ссылок |
| `documents` | `<doc_key>` | Метаданные документа: `document_id`, `title`, `edition`, `source_file`, `status` |
| `documents` | `status` | `active` / `inactive`; для inactive — `status_reason`, `replaced_by_document_id` (официальный номер), `replaced_by_doc_key` (slug, опционально) |
| `documents` | `ignore_sections` | Секции, исключаемые из индексации (например, «Содержание») |

### Сопоставление файлов

Ключ документа (`doc_key`) определяется по полю `source_file` в `rag_config.yaml` — он должен совпадать с именем входного файла. Если документ не найден в конфиге — генерация JSONL пропускается с warning.

## Переменные окружения (.env)

Файл `.env` в `firmware/src/`:

| Переменная | Назначение |
|------------|------------|
| `YANDEX_API_KEY` | API-ключ Yandex Vision OCR |
| `YANDEX_FOLDER_ID` | ID каталога Yandex Cloud |
| `DEEPSEEK_API_KEY` | API-ключ DeepSeek |
| `PROVOD_API_KEY` | API-ключ Provod (Gemini) |

## Выходные файлы

```
<входная_папка>/
├── Markdown/
│   └── <имя_файла>/
│       ├── <имя_файла>.md       # итоговый Markdown
│       ├── rag_chunks.jsonl      # RAG JSONL v2 (если --rag)
│       ├── rag_assets.json       # реестр таблиц/изображений (если --rag)
│       └── image/                # извлечённые изображения
│           ├── fig_1.png
│           └── ...
└── tmp/
    └── <имя_файла>/
        ├── yandex_result.json    # сырой ответ Yandex OCR
        ├── raw.md                # Markdown до постобработки
        ├── table_N.md            # результаты vision-распознавания таблиц
        └── ...
```

## Требования

- Python 3.10+
- LibreOffice (для конвертации DOCX/DOC)
- PyMuPDF (`fitz`) — для вырезки таблиц и изображений
- Доступ к API: Yandex Cloud, DeepSeek, Provod
