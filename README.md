# Create Markdown YA

Конвертация PDF/DOCX/DOC в Markdown через Yandex Vision OCR с AI-постобработкой.

## Назначение

Скрипт `create_markdown.py` распознаёт текст, таблицы, формулы и изображения из документов, собирает структурированный Markdown и улучшает результат с помощью AI (DeepSeek / Gemini через Provod). Таблицы проходят двойную обработку: OCR из текстового слоя + vision-распознавание из картинок, после чего AI сверяет обе версии по ID-маркерам и выдаёт исправленную таблицу на исходном месте в тексте.

## Быстрый старт

```bash
cd firmware/src

# Установка зависимостей
pip install httpx pyyaml pymupdf beautifulsoup4

# Базовое распознавание (без AI)
python3 create_markdown.py -i document.pdf

# Полный цикл с AI
python3 create_markdown.py -i document.pdf --ai

# Только AI-постобработка готового .md (без OCR)
python3 create_markdown.py -i result.md --ai
```

## Опции командной строки

| Опция | Описание |
|-------|----------|
| `-i`, `--input` | **Обязательно.** Входной файл (`.pdf`, `.docx`, `.doc`, `.md`) или папка с файлами |
| `--ai` | Полный AI-цикл: vision-распознавание таблиц → единый AI-проход (коррекция таблиц + постобработка). Для `.md` — только AI-постобработка (без OCR). |
| `--rag` | Генерация `rag_chunks.jsonl` для RAG-индексации (см. секцию «Генерация RAG JSONL») |
| `--config` | Путь к **единому конфигу** `create_markdown_config.yaml` (AI + RAG-секции). По умолчанию: `./create_markdown_config.yaml` |
| `--providers-config` | Путь к реестру провайдеров и ролей `providers.yaml`. По умолчанию: `./providers.yaml` |
| `--reg` | Интерактивная регистрация документа в per-document `<stem>_reg.yaml` рядом с итоговым Markdown (требует TTY) |

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

## Конфигурационные файлы

`create_markdown_config.yaml` содержит промпты и RAG-секции. Провайдеры, модели, capability и назначения ролей находятся в `providers.yaml`; описание схемы и разрешения ролей — в `docs/architecture/providers-registry.md`.

## Конфигурационный файл: create_markdown_config.yaml

Единый конфиг (заменил `config_ai.yaml` + `rag_config.yaml`). Содержит секции:

### `table_vision` — Vision-распознавание таблиц

Изображения таблиц (PNG, вырезанные из PDF) отправляются в vision-модель, назначенную ролью `create_markdown.table_vision` в `providers.yaml`.

| Поле | Описание |
|------|----------|
| `prompt` | Промпт для vision-модели — инструкция по переводу таблицы в Markdown |

### `ai_postprocess` — Единая AI-обработка (постобработка + сверка таблиц)

Финальная чистка всего документа + сверка таблиц с vision-эталонами по ID-маркерам.

| Поле | Описание |
|------|----------|
| `prompt` | **Объединённый промпт**: правила 1–13 (постобработка: LaTeX, OCR-артефакты, форматирование), правила 14–20 (сверка таблиц: сравнение по ID, заполнение ячеек, in-place редактирование) |

**Единый источник модели:** секция `ai_postprocess` задаёт провайдера/модель/fallback **один раз** — он же используется для LLM-слоя `--reg`.

### `reg_extract` — извлечение полей для `--reg`

Содержит **только `prompt`**. Провайдер/модель/fallback берутся из `ai_postprocess` (см. выше) — дублирования в конфиге нет.

### `defaults` / `references` — RAG-индексация

Параметры RAG v2: `max_chunk_tokens`, `tokenizer` (Qwen3), `extract_references`, `default_status` и regexp-паттерны кросс-ссылок.

**Важно:** если `prompt` не задан — скрипт завершится с ошибкой. Дефолтных промтов нет.

## Механизм ID-маркировки таблиц

Каждая таблица получает сквозной ID `t_p{страница}_{индекс}` на всём пути пайплайна:

1. **Вырезка** (`extract_table_images`): каждой таблице присваивается ID `t_p{страница}_{индекс}` по boundingBox Yandex
2. **Vision-распознавание**: ID записывается первой строкой `table_N.md` как `<!-- t_pN_M -->`
3. **Рендер MD** (`parse_yandex_json_to_md`): маркер `<!-- t_pN_M -->` вставляется перед названием таблицы в момент рендера — тот же порядок итерации `pages`/`tables[]`, что и в вырезке, поэтому ID совпадает
4. **AI-проход**: для каждого чанка извлекаются ID, подставляются только релевантные vision-эталоны. AI сверяет таблицы с одинаковыми ID, удаляет маркеры из выдачи
5. **Пост-проверка**: если после AI остались `<!-- t_p... -->` — warning в лог

## Генерация RAG JSONL (`--rag`)

Флаг `--rag` создаёт файл `rag_chunks.jsonl` для индексации документа в RAG-системах (ChromaDB, Qdrant и т.д.).

### Использование

```bash
python3 create_markdown.py -i file.pdf --rag                # только JSONL
python3 create_markdown.py -i file.pdf --ai --rag           # AI + JSONL
python3 create_markdown.py -i file.pdf --rag --config my_config.yaml
```

### Формат выхода

Каждая строка `rag_chunks.jsonl` — валидный JSON-объект:

```json
{
  "document_id": "СО 153-34.21.122-2003",
  "title": "Инструкция по устройству молниезащиты...",
  "chapter": "3. Защита от прямых ударов молнии",
  "section": "3.2. Внешняя молниезащитная система",
  "clause": "3.2.1. Молниеприемники",
  "text": "Молниеприемники могут быть специально установленными...",
  "_source_page": 12,
  "references": ["п. 3.2.2", "табл. 3.4"]
}
```

### Алгоритм

1. MD-документ разбирается на структурные единицы (chapter/section/clause) по заголовкам `##`/`###`/`####`
2. Каждая единица с непустым текстом становится отдельной JSON-строкой
3. Номера страниц сохраняются в служебное поле `_source_page` (в публичный JSONL не попадают); для `.md` — `null`
4. Кросс-ссылки («см. п. 3.2.2», «табл. 3.4») извлекаются по regexp-паттернам из конфига
5. Чанки длиннее `max_chunk_tokens` (по умолчанию 7000) разбиваются по токенам Qwen3 — суффикс `(ч. N)` в `clause` и `chunk_id` `.../part_N`

### Конфигурация RAG: секции `defaults` / `references` (единый конфиг)

| Секция | Поле | Описание |
|--------|------|----------|
| `defaults` | `max_chunk_tokens` | Максимальный размер чанка в токенах Qwen3 (по умолчанию 7000) |
| `defaults` | `tokenizer` | HF model id токенизатора (`Qwen/Qwen3-Embedding-8B`) |
| `defaults` | `extract_references` | Извлекать кросс-ссылки (`true`/`false`) |
| `defaults` | `default_status` | Статус документа по умолчанию (`active`) |
| `references` | `patterns` | Список regexp для поиска ссылок |

### Per-document записи: `<stem>_reg.yaml`

Каталог документов **удалён из конфига**. Метаданные каждого документа живут в
per-document файле `<stem>_reg.yaml` рядом с итоговым Markdown
(`Markdown/<stem>/<stem>_reg.yaml` для PDF/DOCX, рядом с `.md` для `--rag` по готовому Markdown).
Формат записи:

```yaml
documents:
  pue_7:                                           # slug — любое уникальное латинское имя
    document_id: "ПУЭ"                             # официальный номер/обозначение
    document_id_alt: null                          # старое обозначение если было (СНиП → СП)
    title: "Правила устройства электроустановок"   # полное название
    edition: "7"                                   # редакция
    date_enacted: "2003-01-01"                     # дата ввода в действие
    date_amended: "2022-01-01"                     # дата последних изменений (null если не было)
    amended_by: "Приказ Минэнерго №123"            # кем изменён (null если не было)
    source_file: "ПУЭ_7.pdf"                       # ИМЯ ФАЙЛА — по нему ищется документ при --rag
    status: active                                 # active или inactive
    status_reason: null                            # для inactive: причина (для active: null)
    replaced_by_document_id: null                  # для inactive: официальный номер преемника
    replaced_by_doc_key: null                      # для inactive: slug преемника
    ignore_sections:                               # какие разделы пропустить при индексации
      - "Содержание"
      - "Предисловие"
```

При запуске `--rag`/`--reg` такой overlay автоматически накладывается на единый
конфиг (`load_rag_config(config_path, reg_path=<stem>_reg.yaml)`); флаг `--reg`
создаёт/дополняет запись интерактивно.

## Переменные окружения (.env)

Файл `.env` в `firmware/src/`:

| Переменная | Назначение |
|------------|------------|
| `YANDEX_API_KEY` | API-ключ Yandex Vision OCR |
| `YANDEX_FOLDER_ID` | ID каталога Yandex Cloud |
| `DEEPSEEK_API_KEY` | API-ключ DeepSeek (primary `ai_postprocess`) |
| `PROVOD_API_KEY` | API-ключ Provod (vision `table_vision`, fallback `ai_postprocess`) |
| `ANYMODEL_API_KEY` | API-ключ AnyModel (fallback `table_vision`) |

## Выходные файлы

```
<входная_папка>/
├── Markdown/
│   └── <имя_файла>/
│       ├── <имя_файла>.md       # итоговый Markdown
│       ├── rag_chunks.jsonl      # RAG JSONL (если --rag)
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
- Доступ к API: Yandex Cloud, DeepSeek, AnyModel
