#!/usr/bin/env python3
"""
pipeline.py — Пайплайн конвертации PDF/DOCX/MD в Markdown через Yandex Vision OCR.

Модель: math-markdown (даёт markdown + tables + pictures + blocks).

Постобработка:
  — скриптовая (всегда): HTML-таблицы → MD, LaTeX-чистка, изображения → fig_N,
    OCR-артефакты, примечания, подписи
  — AI (флаг --ai): deepseek-v4-flash → gemini-3.5-flash (через DeepSeek / Provod)

Режим .md + --ai: только AI-постобработка готового .md файла, без OCR.

Использование:
  python3 pipeline.py -i file.pdf
  python3 pipeline.py -i file.pdf --ai --config config_ai.yaml
  python3 pipeline.py -i file.md --ai                 # только AI
  python3 pipeline.py -i dir/
"""

import argparse
import base64
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

import httpx
import yaml

# ── Логирование (базовое) ─────────────────────────────────────────────
log = logging.getLogger("create-md-ya")


# ═══════════════════════════════════════════════════════════════════════════
# 0. Секция импортов и констант
# ═══════════════════════════════════════════════════════════════════════════

# Yandex OCR API
YANDEX_OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeTextAsync"
YANDEX_POLL_URL = "https://ai.api.cloud.yandex.net/ocr/v1/getRecognition"

# Provod AI
PROVOD_BASE_URL = "https://api.provod.ai/v1"

# Лимиты
AI_MAX_CHARS = 24000
YANDEX_MAX_PAGES = 200
YANDEX_MAX_SIZE_MB = 10
YANDEX_POLL_TIMEOUT = 600  # 10 минут
YANDEX_POLL_INTERVAL = 2

# Поддерживаемые расширения
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".md"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. Утилиты
# ═══════════════════════════════════════════════════════════════════════════

def setup_logging(log_path: str | Path) -> logging.Logger:
    """Настроить логгер: консоль (INFO+) + файл (DEBUG+)."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Удаляем старые handler-ы
    for h in root.handlers[:]:
        root.removeHandler(h)

    # Консоль
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Файл
    fh = logging.FileHandler(str(log_path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    return logging.getLogger("create-md-ya")


def load_env(env_path: str | Path) -> dict:
    """Загрузить .env файл, вернуть dict переменных."""
    env_path = Path(env_path)
    env_vars = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                env_vars[key.strip()] = val.strip().strip("\"'")
                os.environ.setdefault(key.strip(), env_vars[key.strip()])
    return env_vars


def load_config(config_path: str | Path) -> dict:
    """Загрузить YAML-конфиг через PyYAML."""
    config_path = Path(config_path)
    if not config_path.exists():
        log.warning(f"Конфиг не найден: {config_path}, использую умолчания")
        return {
            "ai_postprocess": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "api_key_env": "DEEPSEEK_API_KEY",
                "base_url": "https://api.deepseek.com/v1",
                "fallback": {
                    "provider": "provod",
                    "model": "google/gemini-3.5-flash",
                    "api_key_env": "PROVOD_API_KEY",
                    "base_url": "https://api.provod.ai/v1",
                },
            }
        }
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dir(path: str | Path) -> Path:
    """Создать папку, если не существует."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_input_files(input_path: str) -> list[Path]:
    """Найти все PDF/DOCX/DOC файлы по пути (файл или папка)."""
    p = Path(input_path)
    if p.is_file():
        return [p]
    elif p.is_dir():
        files = []
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(sorted(p.rglob(f"*{ext}")))
        return files
    else:
        log.error(f"Путь не найден: {input_path}")
        return []


def safe_write(path: str | Path, content: str) -> None:
    """Безопасно записать файл (создать папки)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# 2. Yandex OCR API
# ═══════════════════════════════════════════════════════════════════════════

def convert_docx_to_pdf(input_path: str | Path, tmp_dir: str | Path) -> Path:
    """Конвертировать DOCX/DOC в PDF через LibreOffice headless.

    Returns:
        Путь к сконвертированному PDF.

    Raises:
        RuntimeError: если LibreOffice не найден или конвертация не удалась.
    """
    input_path = Path(input_path).resolve()
    tmp_dir = Path(tmp_dir)
    ensure_dir(tmp_dir)

    log.info(f"Конвертация {input_path.name} → PDF")

    # Проверяем наличие LibreOffice
    if not shutil.which("libreoffice"):
        raise RuntimeError("LibreOffice не найден в системе. Установите: apt install libreoffice")

    cmd = [
        "libreoffice",
        "--headless",
        "--convert-to", "pdf",
        "--outdir", str(tmp_dir),
        str(input_path),
    ]

    log.debug(f"  Команда: {' '.join(cmd)}")
    start = time.time()

    result = subprocess_run(cmd, timeout=120)

    elapsed = time.time() - start
    log.info(f"  Конвертация: {elapsed:.1f}с, код={result.returncode}")

    if result.returncode != 0:
        stderr = result.stderr[:500] if result.stderr else "нет вывода"
        raise RuntimeError(f"LibreOffice завершился с кодом {result.returncode}: {stderr}")

    # Ищем созданный PDF
    pdf_name = input_path.stem + ".pdf"
    pdf_path = tmp_dir / pdf_name
    if not pdf_path.exists():
        # LibreOffice иногда меняет имя
        pdf_files = list(tmp_dir.glob("*.pdf"))
        if pdf_files:
            pdf_path = pdf_files[0]
        else:
            raise RuntimeError("PDF после конвертации не найден")

    log.info(f"  PDF: {pdf_path}")
    return pdf_path


def subprocess_run(cmd: list[str], timeout: int = 120) -> object:
    """Обёртка для subprocess.run (без прямого импорта subprocess в main)."""
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def send_to_yandex_ocr(
    pdf_path: str | Path,
    api_key: str,
    folder_id: str,
    model: str = "math-markdown",
    timeout: int = YANDEX_POLL_TIMEOUT,
) -> list[dict]:
    """Отправить PDF в Yandex Vision OCR (async), дождаться результата.

    Алгоритм:
      1. Прочитать PDF → base64
      2. POST /ocr/v1/recognizeTextAsync
      3. Извлечь operationId
      4. _poll_yandex_operation() — опрос статуса
      5. Вернуть список page-словарей

    Args:
        pdf_path: Путь к PDF-файлу.
        api_key: Yandex API-ключ (Api-Key).
        folder_id: Yandex Folder ID.
        model: Модель OCR (по умолчанию math-markdown).
        timeout: Максимальное время ожидания.

    Returns:
        list[dict] — страницы с textAnnotation.

    Raises:
        RuntimeError: при ошибке API или таймауте.
    """
    pdf_path = Path(pdf_path).resolve()
    log.info(f"Yandex OCR: {pdf_path.name} (model={model})")

    # Шаг 1: base64
    file_size = pdf_path.stat().st_size
    if file_size > YANDEX_MAX_SIZE_MB * 1024 * 1024:
        log.warning(f"  Размер > {YANDEX_MAX_SIZE_MB} МБ ({file_size / 1024 / 1024:.1f} МБ)")
    log.info(f"  Размер файла: {file_size / 1024:.0f} КБ")

    with open(pdf_path, "rb") as f:
        content_base64 = base64.b64encode(f.read()).decode("utf-8")

    headers = {
        "Authorization": f"Api-Key {api_key}",
        "x-folder-id": folder_id,
        "x-data-logging-enabled": "true",
    }

    body = {
        "mimeType": "application/pdf",
        "languageCodes": ["ru", "en"],
        "model": model,
        "content": content_base64,
    }

    # Шаг 2: отправка с ретраем
    log.info("  Отправка...")

    max_retries = 3
    last_error = None
    resp = None
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    YANDEX_OCR_URL,
                    headers=headers,
                    json=body,
                )
        except httpx.TimeoutException as e:
            last_error = f"Таймаут отправки (попытка {attempt + 1}/{max_retries})"
            log.warning(f"  {last_error}")
            if attempt < max_retries - 1:
                delay = (4 ** attempt) + 1  # ~1с, ~5с, ~17с
                time.sleep(delay)
            continue
        except httpx.HTTPError as e:
            last_error = f"HTTP ошибка: {e}"
            log.warning(f"  {last_error}, попытка {attempt + 1}/{max_retries}")
            if attempt < max_retries - 1:
                delay = (4 ** attempt) + 1
                time.sleep(delay)
            continue

        if resp.status_code == 200:
            break  # Успех — выходим из ретрая

        # 4xx/5xx — retry
        last_error = f"Yandex OCR: HTTP {resp.status_code}"
        log.warning(f"  {resp.status_code}, попытка {attempt + 1}/{max_retries}")
        if attempt < max_retries - 1:
            delay = (4 ** attempt) + 1  # exponential backoff: ~1, ~5, ~17 сек
            log.debug(f"  Пауза {delay}с перед ретраем")
            time.sleep(delay)
    else:
        # Все ретраи исчерпаны
        detail = ""
        if resp is not None:
            detail = f" — {resp.text[:300]}"
        raise RuntimeError(f"{last_error}{detail}")

    try:
        op_data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Ошибка парсинга ответа Yandex: {e}")

    operation_id = op_data.get("id", "")
    if not operation_id:
        raise RuntimeError(f"Нет operationId в ответе: {op_data}")

    log.info(f"  operation_id={operation_id}")

    # Шаг 3: опрос
    pages = _poll_yandex_operation(operation_id, api_key, folder_id,
                                    timeout=timeout)

    return pages


def _poll_yandex_operation(
    operation_id: str,
    api_key: str,
    folder_id: str,
    timeout: int = YANDEX_POLL_TIMEOUT,
    poll_interval: int = YANDEX_POLL_INTERVAL,
) -> list[dict]:
    """Опросить статус асинхронной операции Yandex OCR.

    Ответ — NDJSON: каждая строка — JSON страницы.

    Returns:
        list[dict] — страницы с textAnnotation.

    Raises:
        TimeoutError: при превышении timeout.
        RuntimeError: при ошибке API.
    """
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "x-folder-id": folder_id,
    }

    url = f"{YANDEX_POLL_URL}?operationId={operation_id}"

    start = time.time()
    retry_count = 0

    while time.time() - start < timeout:
        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(url, headers=headers)
        except Exception as e:
            retry_count += 1
            if retry_count > 5:
                raise RuntimeError(f"Ошибка опроса Yandex ({retry_count} попыток): {e}")
            log.debug(f"  Ошибка запроса: {e}, повтор...")
            time.sleep(poll_interval)
            continue

        if resp.status_code == 200 and resp.text.strip():
            # NDJSON формат: каждая строка — JSON страницы
            pages = []
            for line in resp.text.strip().splitlines():
                line = line.strip()
                if line:
                    try:
                        pages.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.warning(f"  Ошибка парсинга строки NDJSON: {line[:100]}")

            if pages:
                elapsed = time.time() - start
                log.info(f"  Готово: {len(pages)} стр, {elapsed:.0f}с")
                return pages

        elif resp.status_code == 200:
            # Пустой ответ — операция ещё выполняется
            elapsed = time.time() - start
            if int(elapsed) % 10 == 0:
                log.debug(f"  Ожидание... {elapsed:.0f}с")
        else:
            log.debug(f"  Статус: HTTP {resp.status_code}")

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Таймаут Yandex OCR ({timeout}с): operation_id={operation_id}"
    )


# ── Вспомогательные: извлечение подписей таблиц ──────────────────────────


def _normalize_spaced_text(text: str) -> str:
    """Схлопнуть пробелы внутри слова 'Т а б л и ц а' → 'Таблица'.

    Yandex OCR иногда ставит пробелы между буквами кириллицы.
    """
    # Специфичный паттерн: буквы с пробелами между ними
    # "Т а б л и ц а" → "Таблица"
    text = re.sub(
        r"Т\s+а\s+б\s+л\s+и\s+ц\s+а",
        "Таблица",
        text,
        flags=re.IGNORECASE,
    )
    # "П р и м е ч а н и е" → "Примечание"
    text = re.sub(
        r"П\s+р\s+и\s+м\s+е\s+ч\s+а\s+н\s+и\s+[ея]",
        "Примечание",
        text,
        flags=re.IGNORECASE,
    )
    # "Р и с у н о к" → "Рисунок"
    text = re.sub(
        r"Р\s+и\s+с\s+у\s+н\s+о\s+к",
        "Рисунок",
        text,
        flags=re.IGNORECASE,
    )
    return text


# ═══════════════════════════════════════════════════════════════════════════
# 3. JSON → Markdown Parser (основные функции)
# ═══════════════════════════════════════════════════════════════════════════

def parse_yandex_json_to_md(
    pages: list[dict] | None = None,
    json_path: str | Path | None = None,
) -> tuple[str, list[dict], list[tuple[int, int]]]:
    """Разобрать Yandex OCR JSON в Markdown + список картинок.

    АЛГОРИТМ (только JSON, без markdown-поля):
    Для каждой страницы:
      1. Собрать ВСЁ из textAnnotation.blocks[], tables[], pictures[]
      2. Каждый элемент получает Y-координату (min y bounding box'а)
      3. Отсортировать по Y → корректный порядок на странице
      4. Элементы рендерятся по типу:
         - block → _block_to_md() (форматирование по layoutType)
         - table → _table_to_md() + подпись от _find_table_caption_for()
         - picture -> '@@IMAGE_N@@' placeholder (заменяется _insert_images_into_md)

    Returns:
        (markdown_text, pictures_list, page_boundaries)
        page_boundaries: [(start_line, end_line), ...] для каждой страницы
                        в финальном md_text (0-индексированные строки, end exclusive)
        pictures_list: [{"page": int, "bbox": {"vertices": [...]}, ...}]
    """
    if pages is None:
        with open(json_path, encoding="utf-8") as f:
            pages = json.load(f)

    if not pages:
        return "", [], []

    page_parts: list[str] = []
    page_boundaries: list[tuple[int, int]] = []
    pictures: list[dict] = []
    pic_counter = 0  # счётчик для @@IMAGE_N@@ плейсхолдеров (по Y-порядку)
    page_has_continuation: list[bool] = []  # page-level: есть "Окончание/Продолжение таблицы" блок

    for page_idx, page in enumerate(pages):
        ta = page.get("result", {}).get("textAnnotation", {})
        blocks = ta.get("blocks", [])
        raw_tables = ta.get("tables", [])
        page_pictures_list = ta.get("pictures", [])

        # Определяем, есть ли на странице блоки "Окончание/Продолжение таблицы N"
        # (эти блоки подавляются _block_to_md(), т.к. содержат слово "Таблица",
        # поэтому их нужно детектить из сырого JSON до рендеринга)
        _has_cont = False
        for block in blocks:
            _lines = block.get("lines", [])
            _text = " ".join(
                _l.get("text", "") for _l in _lines
            ).strip()
            if re.search(
                r"(?:Окончани[ея]|Продолжени[ее]|Продолж\.?)\s+"
                r"(?:таблиц[аы]|табл\.?)",
                _text, re.IGNORECASE,
            ):
                _has_cont = True
                break
        page_has_continuation.append(_has_cont)

        # 1. Собираем все элементы страницы с Y-координатой
        elements: list[tuple[float, str, dict]] = []  # [(y, type, data), ...]

        for block in blocks:
            vertices = block.get("boundingBox", {}).get("vertices", [])
            if vertices:
                y = min(float(v.get("y", 0)) for v in vertices)
            else:
                y = 0.0
            elements.append((y, "block", block))

        for table in raw_tables:
            vertices = table.get("boundingBox", {}).get("vertices", [])
            if vertices:
                y = min(float(v.get("y", 0)) for v in vertices)
            else:
                y = 0.0
            elements.append((y, "table", table))

        for pic in page_pictures_list:
            vertices = pic.get("boundingBox", {}).get("vertices", [])
            if vertices:
                y = min(float(v.get("y", 0)) for v in vertices)
            else:
                y = 0.0
            elements.append((y, "picture", pic))
            pictures.append({"page": page_idx, "bbox": pic.get("boundingBox", {})})

        # 2. Сортируем по Y
        elements.sort(key=lambda x: x[0])

        # Определяем, какие блоки входят в bounding box таблиц
        # (это отдельные ячейки таблицы как text-блоки — их не рендерим,
        #  т.к. таблица будет рендериться из structured tables[])
        table_y_ranges: list[tuple[float, float]] = []
        for table in raw_tables:
            tv = table.get("boundingBox", {}).get("vertices", [])
            if tv:
                ys = [float(v.get("y", 0)) for v in tv]
                table_y_ranges.append((min(ys), max(ys)))

        def _block_in_table(block: dict) -> bool:
            """Проверить, находится ли блок внутри bounding box какой-либо таблицы."""
            bv = block.get("boundingBox", {}).get("vertices", [])
            if not bv or not table_y_ranges:
                return False
            bys = [float(v.get("y", 0)) for v in bv]
            b_y_min, b_y_max = min(bys), max(bys)
            for t_min, t_max in table_y_ranges:
                # Блок полностью внутри таблицы по Y
                if b_y_min >= t_min and b_y_max <= t_max:
                    return True
                # Блок частично внутри (пересечение > 50%)
                overlap = min(b_y_max, t_max) - max(b_y_min, t_min)
                b_height = b_y_max - b_y_min
                if b_height > 0 and overlap / b_height > 0.5:
                    return True
            return False

        # 3. Рендерим страницу в порядке Y
        page_lines: list[str] = []

        for y, etype, data in elements:
            if etype == "block":
                # Пропускаем блоки, которые являются частью таблицы
                if _block_in_table(data):
                    continue
                text = _block_to_md(data)
                if text:
                    page_lines.append(text)
            elif etype == "table":
                md_table, note_text = _table_to_md(data)
                if md_table:
                    caption = _find_table_caption_for(blocks, data, raw_tables)
                    if caption:
                        page_lines.append(f"*{caption}*")
                    page_lines.append(md_table)
                    if note_text:
                        page_lines.append(f"> {note_text}")
            elif etype == "picture":
                page_lines.append(f"@@IMAGE_{pic_counter}@@")
                pic_counter += 1

        page_text = "\n\n".join(page_lines).strip()
        if page_text:
            page_parts.append(page_text)

    # Собираем окончательный текст
    if page_parts:
        md_text = "\n\n".join(page_parts)
        # Вычисляем реальные границы страниц в финальном тексте
        pos = 0
        for idx, part in enumerate(page_parts):
            start = pos
            end = start + part.count("\n") + 1  # end exclusive
            page_boundaries.append((start, end))
            pos = end + 1  # +1 for the \n\n separator's extra \n
    else:
        md_text = ""
        page_boundaries = []

    # Склеиваем разорванные между страницами таблицы (Окончание/Продолжение)
    # Передаём page-level контекст, т.к. "Окончание таблицы N" блоки
    # подавляются _block_to_md() и не попадают в md_text
    md_text = _stitch_continuation_tables(
        md_text,
        page_boundaries=page_boundaries,
        page_has_continuation=page_has_continuation,
    )

    return md_text, pictures, page_boundaries


def _block_to_md(block: dict) -> str:
    """Форматировать блок текста по его layoutType.

    - LAYOUT_TYPE_SECTION_HEADER с текстом «Рисунок»/«Рис.» → *курсив*
    - LAYOUT_TYPE_SECTION_HEADER (остальное) → **жирный**
    - LAYOUT_TYPE_CAPTION → не выводится (используется как подпись таблицы)
    - LAYOUT_TYPE_LIST → - элемент списка
    - LAYOUT_TYPE_TEXT / LAYOUT_TYPE_UNSPECIFIED → обычный текст
    - Блоки с текстом «Т а б л и ц а» → не выводятся (подпись, будет у таблицы)
    - Блоки с текстом «П р и м е ч а н и е» → не выводятся (будет > blockquote у таблицы)

    Returns:
        Строка Markdown или пустая строка (если блок нужно пропустить).
    """
    lines = block.get("lines", [])
    text_parts: list[str] = []
    for line_data in lines:
        text = line_data.get("text", "").strip()
        if text:
            text_parts.append(text)
    text = " ".join(text_parts).strip()

    if not text:
        return ""

    raw_text = text

    # Нормализуем для проверки содержания
    normalized = _normalize_spaced_text(text)

    # Блоки с "Т а б л и ц а" — это подпись, будет вставлена с таблицей
    if re.search(r"Таблиц[аы]", normalized, re.IGNORECASE):
        return ""

    # Блоки с "П р и м е ч а н и е" — это примечание, будет в > blockquote у таблицы
    if re.search(r"Примечани[ея]", normalized, re.IGNORECASE):
        return ""

    layout_type = block.get("layoutType", "")

    # LAYOUT_TYPE_CAPTION — подпись таблицы/рисунка.
    # Рендерим как обычный текст. Если эта подпись будет использована
    # как caption для таблицы _find_table_caption_for, она появится
    # дважды — но на практике блок "Т а б л и ц а" (LAYOUT_TYPE_TEXT)
    # перехватывается выше, а CAPTION-блоки без "Таблица" — это
    # заголовки разделов между таблицами, их нужно выводить.
    if layout_type == "LAYOUT_TYPE_CAPTION":
        return raw_text

    if layout_type == "LAYOUT_TYPE_SECTION_HEADER":
        # Проверяем, содержит ли текст "Рисунок" или "Рис."
        if re.search(r"Р\s*и\s*с\s*(?:у\s*н\s*о\s*к|\.\s*)", raw_text, re.IGNORECASE):
            return f"*{raw_text}*"
        else:
            return f"**{raw_text}**"

    if layout_type == "LAYOUT_TYPE_LIST":
        return f"- {raw_text}"

    # LAYOUT_TYPE_TEXT, LAYOUT_TYPE_UNSPECIFIED, и всё остальное
    # Если текст — LaTeX-формула (начинается и заканчивается на $), обернуть в $$
    if raw_text.startswith("$") and raw_text.endswith("$"):
        return f"$$\n{raw_text[1:-1]}\n$$"
    return raw_text


def _find_table_caption_for(
    blocks: list[dict],
    table: dict,
    all_tables: list[dict],
) -> str:
    """Найти подпись для таблицы среди блоков.

    Ищет ближайший по Y сверху блок, который является:
    - LAYOUT_TYPE_CAPTION
    - или содержит текст «Т а б л и ц а» (с пробелами между буквами — OCR-артефакт)

    Исключает блоки, которые уже являются подписью для другой, более близкой
    по Y таблицы.

    Args:
        blocks: Все блоки страницы.
        table: Текущая таблица.
        all_tables: Все таблицы страницы (для избежания дублирования подписей).

    Returns:
        Нормализованный текст подписи, или пустая строка.
    """
    # Y-позиция текущей таблицы (min y)
    table_vertices = table.get("boundingBox", {}).get("vertices", [])
    if not table_vertices:
        return ""
    table_y = min(float(v.get("y", 0)) for v in table_vertices)

    # Y-позиции других таблиц (для определения, какая таблица ближе к caption)
    other_table_ys: list[float] = []
    for other in all_tables:
        if other is table:
            continue
        ov = other.get("boundingBox", {}).get("vertices", [])
        if ov:
            other_table_ys.append(min(float(v.get("y", 0)) for v in ov))

    # Сортируем блоки по Y (возрастание — сверху вниз)
    blocks_with_y: list[tuple[float, dict]] = []
    for block in blocks:
        bv = block.get("boundingBox", {}).get("vertices", [])
        if bv:
            by = min(float(v.get("y", 0)) for v in bv)
        else:
            by = 0.0
        blocks_with_y.append((by, block))
    blocks_with_y.sort(key=lambda x: x[0])

    # Ищем блоки, которые могут быть подписью для этой таблицы.
    # Они должны быть выше таблицы (y < table_y).
    # Выбираем САМЫЙ БЛИЖНИЙ сверху.
    best_caption: str = ""
    best_y_diff: float = float("inf")

    caption_pattern = re.compile(r"Т\s+а\s+б\s+л\s+и\s+ц\s+а", re.IGNORECASE)

    for by, block in blocks_with_y:
        if by >= table_y:
            continue  # Блок ниже таблицы — не подходит

        # Проверяем, не ближе ли другой таблице этот блок
        # (Если другая таблица находится между этим блоком и текущей таблицей,
        #  то блок относится к той, другой таблице)
        is_closer_to_other = False
        for ot_y in other_table_ys:
            if by < ot_y < table_y:
                is_closer_to_other = True
                break
        if is_closer_to_other:
            continue

        layout_type = block.get("layoutType", "")
        block_text = ""
        for line_data in block.get("lines", []):
            t = line_data.get("text", "").strip()
            if t:
                block_text += " " + t
        block_text = block_text.strip()

        is_caption_block = layout_type == "LAYOUT_TYPE_CAPTION"
        has_table_word = bool(caption_pattern.search(block_text))

        if is_caption_block or has_table_word:
            y_diff = table_y - by
            if y_diff < best_y_diff:
                best_y_diff = y_diff
                if has_table_word:
                    best_caption = _normalize_spaced_text(block_text)
                    best_caption = re.sub(r"\s+", " ", best_caption)
                else:
                    best_caption = block_text

    return best_caption


def _table_to_md(table: dict) -> tuple[str, str]:
    """Преобразовать структурированную таблицу в Markdown + примечание.

    Args:
        table: Словарь таблицы из textAnnotation.tables[].

    Returns:
        (md_table_string, note_text)
    """
    cells = table.get("cells", [])
    if not cells:
        return "", ""

    row_count = int(table.get("rowCount", 0))
    col_count = int(table.get("columnCount", 0))

    matrix = _build_cell_matrix(cells, row_count, col_count)
    if not matrix:
        return "", ""

    # Объединяем двухстрочный заголовок
    matrix = _merge_table_headers(matrix)

    # Извлекаем примечание из последней строки
    matrix, note_text = _detect_and_remove_note_row(matrix)

    if len(matrix) < 2:
        return "", ""

    md_table = _matrix_to_md_table(matrix)
    return md_table, note_text


def _build_cell_matrix(
    cells: list[dict],
    row_count: int = 0,
    col_count: int = 0,
) -> list[list[str]]:
    """Построить матрицу ячеек из списка cells Yandex OCR.

    Учитывает rowSpan/columnSpan. Пустые spanned-ячейки заполняются
    текстом из первой ячейки объединения.
    Если row_count/col_count переданы — использует их как размеры матрицы
    (гарантирует, что пустые ячейки на границах не теряются).
    """
    if not cells:
        return []

    if row_count > 0 and col_count > 0:
        max_row = row_count
        max_col = col_count
    else:
        max_row = max(
            int(c.get("rowIndex", 0)) + int(c.get("rowSpan", 1)) for c in cells
        )
        max_col = max(
            int(c.get("columnIndex", 0)) + int(c.get("columnSpan", 1)) for c in cells
        )
    if max_row == 0 or max_col == 0:
        return []

    matrix = [[""] * max_col for _ in range(max_row)]
    for cell in cells:
        r = int(cell.get("rowIndex", 0))
        c = int(cell.get("columnIndex", 0))
        rs = int(cell.get("rowSpan", 1))
        cs = int(cell.get("columnSpan", 1))
        text = cell.get("text", "").replace("\n", " ").strip()
        for dr in range(rs):
            for dc in range(cs):
                if r + dr < max_row and c + dc < max_col:
                    if not matrix[r + dr][c + dc]:
                        matrix[r + dr][c + dc] = text
    return matrix


def _merge_table_headers(matrix: list[list[str]]) -> list[list[str]]:
    """Склеить двухстрочный заголовок в одну строку.

    Если первые две строки матрицы образуют заголовок с rowSpan-шапкой
    (первая строка — общий заголовок через rowSpan, вторая — колонки),
    объединяет их в одну header-строку.

    Пример:
      Row 0: ['Общий заголовок' | 'Kc при числе...' | 'Kc при числе...']
      Row 1: ['Общий заголовок' | '2' | '3']
      → Row 0: ['Общий заголовок' | 'Kc при числе... 2' | 'Kc при числе... 3']
    """
    if len(matrix) < 2:
        return matrix

    row0 = list(matrix[0])
    row1 = list(matrix[1])
    max_cols = max(len(row0), len(row1))
    while len(row0) < max_cols:
        row0.append("")
    while len(row1) < max_cols:
        row1.append("")

    # Ищем колонку, где строки различаются — это граница объединения
    split_col = None
    for col in range(max_cols):
        if row0[col] != row1[col]:
            split_col = col
            break
    if split_col is None or split_col == 0:
        return matrix

    # Первая колонка — объединённая (rowSpan), её берём как есть
    # Остальные — склеиваем
    merged = list(row0)
    for col in range(split_col, max_cols):
        t0 = row0[col].strip()
        t1 = row1[col].strip()
        if t0 and t1:
            merged[col] = t0 + " " + t1
        elif t1:
            merged[col] = t1
    return [merged] + matrix[2:]


def _detect_and_remove_note_row(
    matrix: list[list[str]],
) -> tuple[list[list[str]], str]:
    """Обнаружить и удалить строку 'Примечание' из матрицы таблицы.

    Проверяет последнюю строку двумя способами:
    1. Первая ячейка начинается с 'Примечание' или 'Примечания'
    2. Все непустые ячейки последней строки имеют одинаковый текст
       (это примечание, вынесенное во все колонки)

    Returns:
        (matrix_without_note, note_text)
    """
    if len(matrix) < 2:
        return matrix, ""

    last_row = matrix[-1]
    if not last_row:
        return matrix, ""

    first_cell = last_row[0].strip() if last_row else ""
    note_pattern = re.compile(
        r"^(?:П\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]|Примечани[ея])",
        re.IGNORECASE,
    )

    if note_pattern.match(first_cell):
        # Берём текст из первой ячейки (colSpan может дублировать её по всем колонкам)
        note_text = first_cell
        # Если в других ячейках есть доп. текст (не дубликат первой) — добавляем
        extra = [
            c.strip() for c in last_row[1:]
            if c.strip() and c.strip() != first_cell
        ]
        if extra:
            note_text += " " + " ".join(extra)
        return matrix[:-1], note_text

    # Альтернативное условие: все непустые ячейки последней строки
    # имеют одинаковый текст — это примечание
    non_empty = [c.strip() for c in last_row if c.strip()]
    if len(non_empty) >= 2:
        first_text = non_empty[0]
        if all(c == first_text for c in non_empty[1:]):
            return matrix[:-1], first_text

    return matrix, ""


def _matrix_to_md_table(matrix: list[list[str]]) -> str:
    """Преобразовать матрицу ячеек в Markdown-таблицу."""
    if not matrix or not matrix[0]:
        return ""

    max_cols = max(len(r) for r in matrix)
    if max_cols == 0:
        return ""

    def escape(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    lines = []
    # Заголовок
    header = [escape(matrix[0][c]) if c < len(matrix[0]) else "" for c in range(max_cols)]
    lines.append("| " + " | ".join(header) + " |")
    # Разделитель
    lines.append("| " + " | ".join([":---:"] * max_cols) + " |")
    # Данные
    for row in matrix[1:]:
        vals = [escape(row[c]) if c < len(row) else "" for c in range(max_cols)]
        lines.append("| " + " | ".join(vals) + " |")

    return "\n".join(lines)


def _stitch_continuation_tables(
    md_text: str,
    page_boundaries: list[tuple[int, int]] | None = None,
    page_has_continuation: list[bool] | None = None,
) -> str:
    """Склеить таблицы, разорванные между страницами (Окончание/Продолжение).

    Ищет пары последовательных Markdown-таблиц, где в тексте между ними
    встречается «Окончание таблицы N» или «Продолжение таблицы N».

    Если page-контекст передан, дополнительно проверяет:
      — Находится ли текущая таблица на следующей странице относительно предыдущей
      — Есть ли на этой странице блок "Окончание/Продолжение таблицы" (из JSON)
      — Совпадают ли номера таблиц в caption

    Это нужно, потому что «Окончание таблицы N» — LAYOUT_TYPE_SECTION_HEADER,
    который подавляется _block_to_md() из-за слова «Таблица» и не попадает
    в финальный md_text.

    Args:
        md_text: Markdown-текст с возможными разорванными таблицами.
        page_boundaries: [(start_line, end_line), ...] для каждой страницы.
        page_has_continuation: [bool, ...] — есть ли на странице блок с
            "Окончание/Продолжение таблицы". Должен быть той же длины, что
            page_boundaries.

    Returns:
        Текст со склеенными таблицами.
    """
    lines = md_text.split("\n")
    if len(lines) < 6:
        return md_text

    # Находим все таблицы (строки с | ... |)
    table_starts: list[int] = []
    in_table = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and "|" in stripped[1:]:
            if not in_table:
                table_starts.append(i)
                in_table = True
        else:
            in_table = False

    if len(table_starts) < 2:
        return md_text

    def _get_table_end(start: int) -> int:
        """Вернуть индекс строки ПОСЛЕ последней строки таблицы (end exclusive)."""
        i = start
        while i < len(lines) and lines[i].strip().startswith("|"):
            i += 1
        return i

    def _extract_table_num(text: str) -> str | None:
        """Извлечь номер таблицы из текста вида «Окончание таблицы 7.6»."""
        # Сначала ищем «Окончание таблицы N» или «Продолжение таблицы N»
        m = re.search(
            r"(?:Окончани[ея]|Продолжени[ее]|Продолж\.?)\s+"
            r"(?:таблиц[аы]|table|табл\.?)\s+"
            r"(\d+(?:\.\d+)*)",
            text, re.IGNORECASE,
        )
        if m:
            return m.group(1)
        # Альтернатива: просто «Таблица N» (например, «*Таблица 7.6 — Пример*»)
        m2 = re.search(
            r"(?:Таблиц[аы]|table|табл\.?)\s+"
            r"(\d+(?:\.\d+)*)",
            text, re.IGNORECASE,
        )
        if m2:
            return m2.group(1)
        return None

    # Собираем таблицы с их номерами
    tables_info: list[dict] = []
    for start in table_starts:
        end = _get_table_end(start)
        prev_table_end = tables_info[-1]["end"] if tables_info else 0
        before_lines = lines[prev_table_end:start]
        before_text = " ".join(l.strip() for l in before_lines if l.strip())

        table_num = _extract_table_num(before_text)
        has_continuation = any(
            kw in before_text.lower()
            for kw in ["окончани", "продолжени", "продолж."]
        )

        tables_info.append({
            "start": start,
            "end": end,
            "lines": lines[start:end],
            "table_num": table_num,
            "has_continuation": has_continuation,
            "before_text": before_text,
        })

    # Вспомогательная: к какой странице относится строка line_num
    def _page_of_line(line_num: int) -> int | None:
        if page_boundaries is None:
            return None
        for page_idx, (p_start, p_end) in enumerate(page_boundaries):
            if p_start <= line_num < p_end:
                return page_idx
        return None

    # Склеиваем
    result: list[str] = []
    i = 0
    while i < len(tables_info):
        t = tables_info[i]

        # Проверка: это продолжение таблицы с предыдущей страницы?
        is_continuation = False
        if i > 0:
            prev = tables_info[i - 1]
            # Способ 1: явный маркер "Окончание/Продолжение" в тексте + совпадение номеров
            if (t["has_continuation"] and t["table_num"]
                    and prev["table_num"] == t["table_num"]):
                is_continuation = True
            # Способ 2: page-level контекст — таблица на другой странице,
            #          на той странице есть блок "Окончание/Продолжение",
            #          и предыдущая таблица имеет номер (продолжение без caption)
            elif (page_boundaries and page_has_continuation
                  and prev["table_num"] is not None):
                t_page = _page_of_line(t["start"])
                prev_page = _page_of_line(prev["start"])
                if (t_page is not None and prev_page is not None
                        and t_page != prev_page
                        and t_page < len(page_has_continuation)
                        and page_has_continuation[t_page]):
                    is_continuation = True

        if is_continuation and i > 0:
            prev = tables_info[i - 1]
            # Нашли пару — склеиваем: данные из t добавляем к result
            t_lines = t["lines"]
            sep_idx = -1
            for li, tl in enumerate(t_lines):
                if ":--" in tl or "---" in tl:
                    sep_idx = li
                    break
            if sep_idx >= 0 and sep_idx + 1 < len(t_lines):
                data_lines = t_lines[sep_idx + 1:]
                # Пропускаем первую строку данных, если она дублирует
                # последнюю строку предыдущей таблицы
                if prev["lines"] and data_lines:
                    last_prev = prev["lines"][-1].strip()
                    first_data = data_lines[0].strip()
                    if last_prev == first_data:
                        data_lines = data_lines[1:]
                result.extend(data_lines)
            i += 1
            continue

        # Не продолжение — добавляем как есть
        prev_end = tables_info[i - 1]["end"] if i > 0 else 0
        result.extend(lines[prev_end:t["start"]])
        result.extend(t["lines"])
        i += 1

    # Добавляем остаток после последней таблицы
    if tables_info:
        last_end = tables_info[-1]["end"]
        result.extend(lines[last_end:])

    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Извлечение изображений из PDF
# ═══════════════════════════════════════════════════════════════════════════

def extract_images_from_pdf(
    pdf_path: str | Path,
    pictures: list[dict],
    output_img_dir: str | Path,
    pages: list[dict] | None = None,
) -> list[dict]:
    """Вырезать изображения из PDF по координатам из pictures.

    Использует PyMuPDF (fitz) для:
      1. Открытия PDF
      2. Сопоставления bounding box'ов Yandex с изображениями в PDF
      3. Вырезания областей с высоким DPI

    Args:
        pdf_path: Путь к PDF.
        pictures: Список картинок от parse_yandex_json_to_md().
        output_img_dir: Папка для сохранения изображений.
        pages: Список страниц от send_to_yandex_ocr() — нужен для
               получения ta["width"]/ta["height"] (масштабирование
               Yandex-пикселей → PyMuPDF points, см. ADR-2).

    Returns:
        [{"fig_num": N, "page": P, "filename": "fig_N.png", "bbox": {...}}, ...]
    """
    import fitz  # PyMuPDF

    pdf_path = Path(pdf_path).resolve()
    output_img_dir = Path(output_img_dir)
    ensure_dir(output_img_dir)

    if not pictures:
        log.warning("  Нет pictures в JSON — пропускаю извлечение изображений")
        return []

    log.info(f"Извлечение изображений из PDF (pictures: {len(pictures)})")

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        log.error(f"  Не удалось открыть PDF: {e}")
        return []

    page_count = doc.page_count
    saved_images = []
    fig_counter = 0

    # Извлекаем размеры страниц из textAnnotation для масштабирования
    # Yandex возвращает координаты в пикселях, PyMuPDF использует points
    page_dims: dict[int, tuple[float, float]] = {}
    if pages:
        for page_idx, page_data in enumerate(pages):
            ta = page_data.get("result", {}).get("textAnnotation", {})
            tw = ta.get("width")
            th = ta.get("height")
            if tw and th:
                page_dims[page_idx] = (float(tw), float(th))

    # Группируем pictures по страницам
    pics_by_page: dict[int, list[dict]] = {}
    for pic in pictures:
        pg = pic.get("page", 0)
        if pg not in pics_by_page:
            pics_by_page[pg] = []
        pics_by_page[pg].append(pic)

    for page_idx in range(page_count):
        if page_idx not in pics_by_page:
            continue

        page = doc[page_idx]
        page_rect = page.rect  # points

        # ADR-2: Масштабирование координат Yandex → PyMuPDF
        # scale = ta[width] / page.rect.width  (пикселей на point)
        # Чтобы конвертировать пиксель в point: point = pixel / scale
        dims = page_dims.get(page_idx)
        if dims:
            scale_x = dims[0] / page_rect.width
            scale_y = dims[1] / page_rect.height
        else:
            # Fallback: предполагаем стандартное A4 (595 x 842 points) при 300 DPI
            # 300 DPI → A4 в пикселях: 2480 x 3508
            scale_x = 2480.0 / page_rect.width if page_rect.width > 0 else 1.0
            scale_y = 3508.0 / page_rect.height if page_rect.height > 0 else 1.0
            log.debug(f"  Страница {page_idx}: нет page_dims, fallback A4 300 DPI")

        for pic in pics_by_page[page_idx]:
            bbox = pic.get("bbox", {})
            vertices = bbox.get("vertices", [])
            if len(vertices) < 4:
                continue

            fig_counter += 1
            ext = ".png"

            # Преобразуем вершины в rect для fitz
            xs = [int(v.get("x", 0)) for v in vertices]
            ys = [int(v.get("y", 0)) for v in vertices]

            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)

            # Масштабируем: Yandex-пиксели → PyMuPDF points
            fitz_rect = fitz.Rect(
                x0 / scale_x, y0 / scale_y,
                x1 / scale_x, y1 / scale_y,
            )

            filename = f"fig_{fig_counter}{ext}"
            output_path = output_img_dir / filename

            success = _crop_and_save_image(page, fitz_rect, output_path, 2.0)
            if success:
                saved_images.append({
                    "fig_num": fig_counter,
                    "page": page_idx,
                    "filename": filename,
                    "bbox": bbox,
                })

    doc.close()
    log.info(f"  Сохранено изображений: {len(saved_images)}")
    return saved_images


def _crop_and_save_image(
    page: "fitz.Page",
    rect: "fitz.Rect",
    output_path: Path,
    dpi_scale: float = 2.0,
) -> bool:
    """Вырезать область страницы по rect и сохранить как изображение."""
    import fitz
    try:
        # Создаём матрицу для увеличения DPI
        matrix = fitz.Matrix(dpi_scale, dpi_scale)
        pix = page.get_pixmap(matrix=matrix, clip=rect)
        pix.save(str(output_path))
        return True
    except Exception as e:
        log.warning(f"  Не удалось вырезать изображение: {e}")
        return False


def _insert_images_into_md(
    md_text: str,
    extracted: list[dict],
    pages: list[dict] = None,
    page_boundaries: list[tuple[int, int]] | None = None,
) -> str:
    """Вставить ссылки на извлечённые изображения в Markdown, заменяя @@IMAGE_N@@ плейсхолдеры.

    Плейсхолдеры @@IMAGE_0@@, @@IMAGE_1@@, ... создаются в parse_yandex_json_to_md()
    в Y-отсортированном порядке. Извлечённые изображения (extracted) сортируются
    по (page, Y) и заменяют соответствующие плейсхолдеры.

    Если изображение не удалось извлечь — плейсхолдер остаётся как есть.

    Args:
        md_text: Markdown-текст с плейсхолдерами @@IMAGE_N@@.
        extracted: Список от extract_images_from_pdf().
        pages: Не используется (оставлено для совместимости).
        page_boundaries: Не используется (оставлено для совместимости).

    Returns:
        Markdown с заменёнными ссылками на изображения.
    """
    if not extracted:
        return md_text

    # Вспомогательная: Y-центр bounding box'а
    def _get_y_center(img):
        bbox = img.get("bbox", {})
        vertices = bbox.get("vertices", [])
        if vertices:
            ys = [int(v.get("y", 0)) for v in vertices]
            return sum(ys) / len(ys)
        return 0

    # Сортируем по странице и Y — тот же порядок, что и плейсхолдеры
    sorted_imgs = sorted(extracted, key=lambda x: (x["page"], _get_y_center(x)))

    # Заменяем @@IMAGE_N@@ на ![fig_N](image/fig_N.ext)
    for idx, img in enumerate(sorted_imgs):
        placeholder = f"@@IMAGE_{idx}@@"
        replacement = f"![fig_{img['fig_num']}](image/{img['filename']})"
        if placeholder in md_text:
            md_text = md_text.replace(placeholder, replacement, 1)

    return md_text


# ═══════════════════════════════════════════════════════════════════════════
# 4b. Vision-распознавание таблиц (--ai-table)
# ═══════════════════════════════════════════════════════════════════════════


def extract_table_images(
    pdf_path: str | Path,
    pages: list[dict],
    img_dir: str | Path,
) -> list[dict]:
    """Вырезать все таблицы из PDF по boundingBox из textAnnotation.tables[].

    Использует PyMuPDF (fitz) для вырезки каждой таблицы как PNG (dpi=200).
    Координаты преобразуются из Yandex-пикселей в PyMuPDF points
    с помощью ta["width"]/ta["height"] (тот же механизм, что в
    extract_images_from_pdf()).

    Args:
        pdf_path: Путь к PDF-файлу.
        pages: Список страниц от send_to_yandex_ocr().
        img_dir: Папка для сохранения PNG таблиц.

    Returns:
        [{"page": int, "table_idx": int, "path": "table_N.png"}, ...]
    """
    import fitz

    pdf_path = Path(pdf_path).resolve()
    img_dir = Path(img_dir)
    ensure_dir(img_dir)

    # Быстрая проверка: есть ли вообще таблицы на страницах?
    has_any_table = any(
        page_data.get("result", {}).get("textAnnotation", {}).get("tables", [])
        for page_data in pages
    )
    if not has_any_table:
        log.info("  Нет таблиц в JSON — пропускаю вырезку")
        return []

    doc = fitz.open(str(pdf_path))
    table_images: list[dict] = []
    table_counter = 0  # сквозной счётчик по всем страницам

    for pi, page_data in enumerate(pages):
        ta = page_data.get("result", {}).get("textAnnotation", {})
        tables = ta.get("tables", [])

        if not tables:
            continue

        page = doc[pi]
        ya_w = float(ta.get("width", 1))
        ya_h = float(ta.get("height", 1))
        sx = page.rect.width / ya_w if ya_w > 0 else 1.0
        sy = page.rect.height / ya_h if ya_h > 0 else 1.0

        for ti, table in enumerate(tables):
            bbox = table.get("boundingBox", {}).get("vertices", [])
            if len(bbox) < 4:
                log.warning(f"  Таблица стр.{pi+1}#{ti+1}: нет boundingBox")
                continue

            xs = [float(v.get("x", 0)) for v in bbox]
            ys = [float(v.get("y", 0)) for v in bbox]
            x0, y0 = min(xs) * sx - 2, min(ys) * sy - 2
            x1, y1 = max(xs) * sx + 2, max(ys) * sy + 2

            rect = fitz.Rect(x0, y0, x1, y1)
            pix = page.get_pixmap(clip=rect, dpi=200)

            table_counter += 1
            fname = f"table_{table_counter}.png"
            pix.save(str(img_dir / fname))

            table_images.append({
                "page": pi,
                "table_idx": table_counter,
                "path": fname,
            })
            log.info(f"  Вырезана таблица {table_counter}: стр.{pi+1}, {fname} ({rect.width:.0f}x{rect.height:.0f} px)")

    doc.close()
    return table_images


def _call_vision_api(
    image_b64: str,
    prompt: str,
    config: dict,
    api_key: str,
) -> str | None:
    """Отправить изображение в vision-модель (OpenAI-совместимый API).

    Args:
        image_b64: base64-encoded PNG.
        prompt: Текстовый промпт.
        config: Секция table_vision из config_ai.yaml.
        api_key: API-ключ.

    Returns:
        Распознанный текст (Markdown) или None при ошибке.
    """
    model = config.get("model", "google/gemini-2.5-flash-lite")
    base_url = config.get("base_url", "https://api.provod.ai/v1")
    fallback = config.get("fallback", {})

    if not api_key:
        log.warning("  Vision: API-ключ не задан")
        return None

    def _do_vision(mdl: str, url: str, key: str) -> str | None:
        payload = {
            "model": mdl,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }],
            "max_tokens": 8000,
            "temperature": 0.0,
        }
        for attempt in range(3):
            try:
                with httpx.Client(timeout=120) as client:
                    resp = client.post(
                        f"{url}/chat/completions",
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                    )
                if resp.status_code == 503:
                    log.warning(f"  {mdl}: 503, попытка {attempt + 1}/3")
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                # Убираем возможные markdown-обёртки
                content = re.sub(r"^```(?:markdown)?\s*\n?", "", content, flags=re.MULTILINE)
                content = re.sub(r"\n```\s*$", "", content, flags=re.MULTILINE)
                return content
            except Exception as e:
                log.warning(f"  {mdl}: {e}, попытка {attempt + 1}/3")
                time.sleep(5)
        return None

    # Primary
    log.info(f"  Vision: {model}")
    result = _do_vision(model, base_url, api_key)
    if result is not None:
        return result

    # Fallback
    if fallback:
        fb_model = fallback.get("model", "google/gemini-2.5-flash")
        fb_url = fallback.get("base_url", "https://api.provod.ai/v1")
        fb_key_env = fallback.get("api_key_env", "PROVOD_API_KEY")
        fb_key = os.environ.get(fb_key_env, "")
        if fb_key:
            log.info(f"  Fallback vision: {fb_model}")
            result = _do_vision(fb_model, fb_url, fb_key)
            if result is not None:
                return result
        else:
            log.warning(f"  Fallback {fb_key_env} не задан")

    log.error("  Vision: все модели недоступны")
    return None


def recognize_tables_vision(
    table_images: list[dict],
    img_dir: str | Path,
    config: dict,
    tmp_dir: str | Path,
    api_key: str,
) -> int:
    """Распознать вырезанные таблицы через vision-модель.

    Для каждой вырезанной PNG отправляет её в vision-модель
    (Gemini через Provod API) с промптом из config.
    Результат сохраняется как tmp/<file>/table_N.md.

    Args:
        table_images: Список от extract_table_images().
        img_dir: Папка с PNG таблиц.
        config: Полный конфиг (должен содержать table_vision секцию).
        tmp_dir: Папка tmp/<имя_файла> для сохранения .md результатов.
        api_key: API-ключ из PROVOD_API_KEY.

    Returns:
        Количество успешно распознанных таблиц.
    """
    vision_cfg = config.get("table_vision", {})
    if not vision_cfg:
        log.warning("  Нет секции table_vision в конфиге — пропускаю")
        return 0

    prompt = vision_cfg.get("prompt",
        "Переведи данную таблицу в markdown формат.\n"
        "Текст объединённых ячеек продублируй в каждой строке или столбце.\n"
        "Если в первом столбце есть цифровой ряд по порядку — это номер строки, выведи в отдельную колонку."
    )

    success = 0
    for ti in table_images:
        fname = ti["path"]
        img_path = Path(img_dir) / fname
        if not img_path.exists():
            log.warning(f"  Файл не найден: {img_path}")
            continue

        log.info(f"  Распознавание таблицы {ti['table_idx']}: {fname}")
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        result = _call_vision_api(b64, prompt, vision_cfg, api_key)
        if result:
            out_path = Path(tmp_dir) / f"table_{ti['table_idx']}.md"
            safe_write(out_path, result)
            log.info(f"    Результат: {out_path} ({len(result)} символов)")
            success += 1
        else:
            log.warning(f"    Таблица {ti['table_idx']}: не распознана")

    return success


# ═══════════════════════════════════════════════════════════════════════════
# 5. Постобработка: LaTeX
# ═══════════════════════════════════════════════════════════════════════════

def _clean_spaces_in_numbers(text: str) -> str:
    """Убрать пробелы внутри чисел: 0 , 4 2 9 -> 0,429."""
    text = re.sub(r"(\d)\s*,\s*(\d)", r"\1,\2", text)
    text = re.sub(r"(\d)\s+(\d)", r"\1\2", text)
    return text


def _simplify_math_commands(text: str) -> str:
    """Упростить LaTeX-команды."""
    text = re.sub(r"\\pmb\s*\{\s*([^}]+?)\s*\}", r"\1", text)
    text = re.sub(r"\\mathbf\s*\{\s*([^}]+?)\s*\}", r"\1", text)
    text = re.sub(r"\\mathsf\s*\{\s*([^}]+?)\s*\}", r"\1", text)
    text = re.sub(r"\\boldsymbol\s*\{\s*([^}]+?)\s*\}", r"\1", text)
    text = re.sub(r"\\mathfrak\s*\{\s*([^}]+?)\s*\}", r"\1", text)
    text = re.sub(r"\\mathrm\s*\{\s*_([^}]+?)\s*\}", r"\\text{\1}", text)
    text = re.sub(r"\\mathrm\s*\{\s*([^}]+?)\s*\}", r"\\text{\1}", text)
    # \\tt — переключатель шрифта
    text = re.sub(r"\\tt\s*", "", text)
    text = re.sub(r"\\tt\s*\{\s*([^}]+?)\s*\}", r"\1", text)
    # \\sf — переключатель шрифта sans-serif, не нужен
    text = re.sub(r"\\sf\s*", "", text)
    # \\f — OCR-артефакт, невалидная LaTeX-команда
    text = re.sub(r"\\f\s*", "", text)
    return text


def _fix_latex_ocr_artifacts(text: str) -> str:
    """Чистка OCR-артефактов в LaTeX."""
    text = re.sub(r"\\,\s*", "", text)
    text = re.sub(r"\\ldots", "...", text)
    text = re.sub(r"(\S)(\\\^)\{", r"\1 \2 {", text)
    text = re.sub(r"(\d)\\(')", r"\1 \2", text)
    return text


def _clean_extra_braces(text: str) -> str:
    """Убрать лишние фигурные скобки внутри LaTeX.

    Не трогает команды с фигурными скобками: \\frac, \\text, \\sqrt и т.п.
    Не трогает индексы: _{...}, ^{...}
    """
    protected_cmd = (
        r"(?:\\[dtc]?frac|\\text|\\tag|\\substack|\\sqrt|"
        r"\\sum|\\prod|\\int|\\limits|\\boldsymbol|\\mathbf|\\mathrm)"
    )
    result = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            before = text[max(0, i - 20):i].strip()
            is_protected = bool(re.search(protected_cmd + r"$", before))
            is_subscript = before.endswith("_")
            is_superscript = before.endswith("^")
            is_frac_second = False
            if not is_protected and not is_subscript and not is_superscript:
                if i > 0 and text[i - 1] == "}":
                    depth_frac = 1
                    k = i - 2
                    while k >= 0 and depth_frac > 0:
                        if text[k] == "}":
                            depth_frac += 1
                        elif text[k] == "{":
                            depth_frac -= 1
                        k -= 1
                    frac_prefix = text[max(0, k - 5):k + 1]
                    if re.search(r'\\[dtc]?frac$', frac_prefix):
                        is_frac_second = True
            if is_protected or is_subscript or is_superscript or is_frac_second:
                depth, j = 1, i + 1
                while j < len(text) and depth > 0:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                result.append(text[i:j])
                i = j
                continue
            depth, j, has_bs = 1, i + 1, False
            while j < len(text) and depth > 0:
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                if text[j] == "\\":
                    has_bs = True
                j += 1
            if has_bs or depth != 0:
                result.append(text[i:j])
            else:
                result.append(text[i + 1:j - 1].strip())
            i = j
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def _clean_formula(formula: str) -> str:
    """Полная очистка LaTeX-формулы."""
    text = formula
    text = _clean_spaces_in_numbers(text)
    text = _simplify_math_commands(text)
    text = _fix_latex_ocr_artifacts(text)
    text = _clean_extra_braces(text)
    return text


def cleanup_latex(md_text: str) -> str:
    """Найти и очистить все LaTeX-формулы в тексте."""
    def _rep_display(m):
        return f"$$\n{_clean_formula(m.group(1))}\n$$"
    # Yandex ставит $$...$$ на одной строке. Используем DOTALL для многострочных.
    md_text = re.sub(r"\$\$(.+?)\$\$", _rep_display, md_text, flags=re.DOTALL)

    def _rep_inline(m):
        return f"${_clean_formula(m.group(1))}$"
    md_text = re.sub(r"\$(.+?)\$", _rep_inline, md_text)
    return md_text


# ═══════════════════════════════════════════════════════════════════════════
# 6. Постобработка: таблицы (HTML → MD)
# ═══════════════════════════════════════════════════════════════════════════

def _parse_table_html(html: str) -> list[list[str]]:
    """Парсинг HTML-таблицы с разворотом rowspan/colspan."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    table_tag = soup.find("table")
    if table_tag is None:
        return []

    rows = table_tag.find_all("tr")
    if not rows:
        return []

    cell_data = []
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        row_cells = []
        for cell in cells:
            text = cell.get_text(separator=" ", strip=False)
            text = re.sub(r"[ \t]+", " ", text).strip()
            img_md = ""
            for img in cell.find_all("img"):
                src = img.get("src", "")
                if src:
                    img_md += f"![image]({src}) "
            if img_md:
                text = img_md.strip() + (" " + text if text else "")
            rowspan = int(cell.get("rowspan", 1))
            colspan = int(cell.get("colspan", 1))
            row_cells.append({"text": text, "rowspan": rowspan, "colspan": colspan})
        cell_data.append(row_cells)

    matrix = []
    for row_idx, row_cells in enumerate(cell_data):
        while len(matrix) <= row_idx:
            matrix.append([])
        col_idx = 0
        for cell in row_cells:
            text = cell["text"]
            rs, cs = cell["rowspan"], cell["colspan"]
            while col_idx < len(matrix[row_idx]) and matrix[row_idx][col_idx] is not None:
                col_idx += 1
            for r_off in range(rs):
                r_target = row_idx + r_off
                while len(matrix) <= r_target:
                    matrix.append([])
                while len(matrix[r_target]) < col_idx + cs:
                    matrix[r_target].append(None)
                for c_off in range(cs):
                    matrix[r_target][col_idx + c_off] = text
            col_idx += cs

    return [[c if c is not None else "" for c in row] for row in matrix]


def _matrix_to_markdown(matrix: list[list[str]]) -> str:
    """Преобразовать матрицу ячеек в Markdown-таблицу."""
    if not matrix:
        return "<!-- пустая таблица -->"
    max_cols = max(len(r) for r in matrix) if matrix else 0
    if max_cols == 0:
        return "<!-- пустая таблица -->"

    def _escape(text: str) -> str:
        """Экранировать | и переводы строк внутри ячейки."""
        return text.replace("|", "\\|").replace("\n", " ")

    aligned = []
    for row in matrix:
        r = list(row)
        while len(r) < max_cols:
            r.append("")
        aligned.append([_escape(c) for c in r])

    lines = []
    lines.append("| " + " | ".join(aligned[0]) + " |")
    lines.append("| " + " | ".join([":---:"] * max_cols) + " |")
    for row in aligned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _merge_header_rows(matrix: list[list[str]]) -> list[list[str]]:
    """Склеить двухстрочные заголовки таблиц."""
    if len(matrix) < 2:
        return matrix
    row0, row1 = list(matrix[0]), list(matrix[1])
    max_cols = max(len(row0), len(row1))
    while len(row0) < max_cols:
        row0.append("")
    while len(row1) < max_cols:
        row1.append("")

    split_col = None
    for col in range(max_cols):
        if row0[col] != row1[col]:
            split_col = col
            break
    if split_col is None or split_col == 0:
        return matrix

    merged = list(row0)
    for col in range(split_col, max_cols):
        t0, t1 = row0[col].strip(), row1[col].strip()
        if t0 and t1:
            merged[col] = t0 + " " + t1
        elif t1:
            merged[col] = t1
    return [merged] + matrix[2:]


def _clean_table_html(html: str) -> tuple[str, str]:
    """Извлечь примечание из последней строки HTML-таблицы."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return html, ""

    rows = table.find_all("tr")
    if len(rows) < 2:
        return html, ""

    last_row = rows[-1]
    cells = last_row.find_all(["td", "th"])

    if len(cells) == 1:
        colspan = int(cells[0].get("colspan", 1))
        first_row = rows[0]
        first_row_cells = first_row.find_all(["td", "th"])
        total_cols = sum(int(c.get("colspan", 1)) for c in first_row_cells)

        if colspan >= total_cols:
            note_text = cells[0].get_text(separator=" ", strip=True)
            note_text = re.sub(r"\s+", " ", note_text).strip()
            last_row.decompose()
            return str(soup), note_text

    return html, ""


def convert_html_tables(md_text: str) -> tuple[str, int]:
    """Найти и конвертировать HTML-таблицы в Markdown."""
    table_pattern = re.compile(r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)
    converted = 0

    def _replace(match):
        nonlocal converted
        html = match.group(0)
        clean_html, note = _clean_table_html(html)
        matrix = _parse_table_html(clean_html)
        if not matrix:
            if note:
                return f"<!-- пустая таблица -->\n\n> {note}"
            return "<!-- пустая таблица -->"
        matrix = _merge_header_rows(matrix)
        md_table = _matrix_to_markdown(matrix)
        converted += 1
        if note:
            md_table += f"\n\n> {note}"
        return md_table

    result = table_pattern.sub(_replace, md_text)
    return result, converted


def _find_table_boundaries(lines: list[str]) -> list[tuple[int, int]]:
    """Найти границы Markdown-таблиц (строки с | ... |)."""
    tables = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|"):
            start = i
            while i < len(lines) and "|" in lines[i]:
                i += 1
            end = i
            if end - start >= 3:
                tables.append((start, end))
        else:
            i += 1
    return tables


def _table_header(table_lines: list[str]) -> str:
    """Извлечь заголовок таблицы."""
    return table_lines[0].strip() if table_lines else ""


def _is_continuation(text: str) -> bool:
    """Проверить 'Продолжение...' или 'Окончание...'."""
    text = text.strip().lower()
    return any(kw in text for kw in ["продолжение", "окончание", "продолж.", "оконч."])


def _extract_table_number(text: str) -> str | None:
    """Извлечь номер таблицы из подписи."""
    m = re.search(r"(?:Таблиц[аы]|Table)\s+([A-ZА-Я]?\.?\d+(?:\.\d+)*)", text, re.IGNORECASE)
    return m.group(1) if m else None


def _same_table_caption(lines: list[str], t1_start: int, t2_start: int) -> bool:
    """Проверить, что у двух таблиц одинаковые номера в подписях."""
    num1 = None
    for offset in range(1, min(6, t1_start + 1)):
        line = lines[t1_start - offset].strip()
        num1 = _extract_table_number(line)
        if num1:
            break

    num2 = None
    for offset in range(1, min(6, t2_start + 1)):
        line = lines[t2_start - offset].strip()
        num2 = _extract_table_number(line)
        if num2:
            break

    if num1 is not None and num2 is not None:
        return num1 == num2
    return True


def merge_tables(md_text: str) -> str:
    """Объединить смежные Markdown-таблицы."""
    lines = md_text.split("\n")
    tables = _find_table_boundaries(lines)
    if len(tables) < 2:
        return md_text

    result = []
    prev_end = 0
    i = 0

    while i < len(tables):
        t_start, t_end = tables[i]
        result.extend(lines[prev_end:t_start])
        prev_end = t_end

        table_lines = lines[t_start:t_end]
        header = _table_header(table_lines)

        sep_idx = -1
        for li, line in enumerate(table_lines):
            if ":--" in line or "---" in line:
                sep_idx = li
                break

        if sep_idx == -1:
            result.extend(table_lines)
            i += 1
            continue

        current_data = list(table_lines[sep_idx + 1:])

        j = i + 1
        while j < len(tables):
            n_start, n_end = tables[j]
            between = "\n".join(lines[t_end:n_start]).strip()
            between_lines = [l for l in between.split("\n") if l.strip()]
            has_continuation = any(_is_continuation(bl) for bl in between_lines)

            next_lines = lines[n_start:n_end]
            next_header = _table_header(next_lines)

            next_sep = -1
            for li, line in enumerate(next_lines):
                if ":--" in line or "---" in line:
                    next_sep = li
                    break

            same_caption = _same_table_caption(lines, t_start, n_start)

            can_merge = has_continuation or (
                header and header == next_header and same_caption
            )

            if not can_merge:
                break

            next_data = next_lines[next_sep + 1:] if next_sep >= 0 else next_lines
            current_data.extend(next_data)
            t_end = n_end
            prev_end = t_end
            j += 1

        header_part = table_lines[:sep_idx + 1]
        merged_lines = list(header_part)
        seen = {header.strip()}
        for dl in current_data:
            s = dl.strip()
            if s and s not in seen:
                merged_lines.append(dl)
                seen.add(s)

        result.extend(merged_lines)
        i = j

    result.extend(lines[prev_end:])
    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Постобработка: изображения и подписи
# ═══════════════════════════════════════════════════════════════════════════

def rename_images(md_text: str, img_dir: str | Path) -> tuple[str, int]:
    """Переименовать хеш-изображения в fig_N, поправить ссылки."""
    img_dir = Path(img_dir)
    if not img_dir.exists():
        return md_text, 0

    img_files = sorted(img_dir.glob("*"))
    if not img_files:
        return md_text, 0

    rename_map = {}
    for i, img_path in enumerate(img_files):
        ext = img_path.suffix.lower()
        new_name = f"fig_{i + 1}{ext}"
        new_path = img_dir / new_name

        if img_path.name != new_name:
            shutil.move(str(img_path), str(new_path))
            rename_map[img_path.name] = new_name

    for old_name, new_name in sorted(
        rename_map.items(),
        key=lambda x: int(re.search(r'fig_(\d+)', x[1]).group(1))
    ):
        md_text = md_text.replace(f"](image/{old_name})", f"](image/{new_name})")
        fig_num_match = re.search(r'fig_(\d+)', new_name)
        fig_num = fig_num_match.group(1) if fig_num_match else "?"
        md_text = re.sub(
            rf'!\[image\]\(image/{re.escape(new_name)}\)',
            f"![Рисунок {fig_num}](image/{new_name})",
            md_text,
        )

    return md_text, len(rename_map)


def fix_image_captions(md_text: str) -> str:
    """Подписи под изображениями (строка после ссылки) -> курсив."""
    lines = md_text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "![Рисунок" in line or "![" in line:
            result.append(line)
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if re.match(r"^Рис\.?\s", next_line, re.IGNORECASE):
                    result.append(f"*{next_line}*")
                    i += 1
        else:
            result.append(line)
        i += 1
    return "\n".join(result)


def fix_table_fig_labels(md_text: str) -> str:
    """Форматировать подписи 'Таблица N', 'Рисунок/Рис. N'.

    - Убирает префикс # если был заголовком
    - Форматирует как *курсив*
    - Добавляет пустую строку перед подписью для визуального отделения
    """
    lines = md_text.split("\n")
    result = []

    # Паттерны: "Рисунок N", "Рис. N", "Таблица N" (с возможными пробелами между буквами OCR)
    fig_pattern = re.compile(
        r"^(?:#+\s*)?(Р\s*и\s*с\s*у\s*н\s*о\s*к|Р\s*и\s*с\s*\.?)\s*\d+",
        re.IGNORECASE,
    )
    table_pattern = re.compile(
        r"^(?:#+\s*)?(Т\s*а\s*б\s*л\s*и\s*ц\s*а|Таблиц[аы])\s*\d+",
        re.IGNORECASE,
    )

    for line in lines:
        stripped = line.strip()

        if not stripped:
            result.append(line)
            continue

        is_fig = fig_pattern.match(stripped)
        is_table = table_pattern.match(stripped)

        if is_fig or is_table:
            # Убираем # префикс если был заголовком
            caption = stripped.lstrip("#").strip()
            # Форматируем как курсив
            caption = f"*{caption}*"
            # Добавляем пустую строку перед, если её нет
            if result and result[-1].strip():
                result.append("")
            result.append(caption)
            continue

        result.append(line)

    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════════════
# 8. Постобработка: примечания и OCR-артефакты
# ═══════════════════════════════════════════════════════════════════════════

def fix_notes(md_text: str) -> str:
    """Примечания под таблицами -> цитаты.

    Обрабатывает:
    - Строки "Примечание" или "П р и м е ч а н и е" как отдельные строки
    - Последние строки Markdown-таблиц, начинающиеся с "Примечание"
    """
    lines = md_text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        note_match = re.match(
            r"^(?:П\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*е|Примечание)",
            stripped, re.IGNORECASE,
        )

        if note_match:
            result.append(f"> {stripped}")
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    break
                if s.startswith("|") or s.startswith("#"):
                    break
                result.append(f"> {s}")
                i += 1
            continue

        result.append(line)
        i += 1

    # Шаг 2: обрабатываем Markdown-таблицы — извлекаем "Примечание" из последней строки
    result = _extract_notes_from_md_tables(result)

    return "\n".join(result)


def _extract_notes_from_md_tables(lines: list[str]) -> list[str]:
    """Извлечь 'Примечание' из последней строки Markdown-таблиц.

    Если последняя строка таблицы содержит текст, начинающийся с 'Примечание'
    или 'Примечания', вырезает её из таблицы и добавляет как > blockquote
    после таблицы.
    """
    result = []
    i = 0

    # Паттерн для детекции "Примечание" в ячейке таблицы
    note_in_cell = re.compile(
        r"^(?:П\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]|Примечани[ея])",
        re.IGNORECASE,
    )

    while i < len(lines):
        # Ищем начало таблицы: строка с | и следом строка с :--- или ---
        stripped = lines[i].strip()
        if not stripped.startswith("|"):
            result.append(lines[i])
            i += 1
            continue

        # Собираем все строки таблицы
        table_start = i
        while i < len(lines) and lines[i].strip().startswith("|"):
            i += 1
        table_end = i

        table_lines = lines[table_start:table_end]

        # Минимум 3 строки для таблицы: заголовок, разделитель, данные
        if len(table_lines) < 3:
            result.extend(table_lines)
            continue

        # Проверяем последнюю строку таблицы на наличие "Примечание"
        last_row = table_lines[-1].strip()
        # Разбираем ячейки
        cells = [c.strip() for c in last_row.split("|")[1:-1]]  # пропускаем пустые до/после |
        first_cell = cells[0] if cells else ""

        if note_in_cell.match(first_cell):
            # Извлекаем примечание из таблицы
            table_body = table_lines[:-1]  # таблица без последней строки
            result.extend(table_body)

            # Собираем текст примечания из первой ячейки
            note_text = first_cell
            # Добавляем текст из остальных ячеек (если он не пустой и не дублируется)
            extra_texts = [c for c in cells[1:] if c.strip() and c.strip() != first_cell.strip()]
            if extra_texts:
                note_text += " " + " ".join(extra_texts)

            result.append("")
            result.append(f"> {note_text}")
            result.append("")
        else:
            result.extend(table_lines)

    return result


def fix_ocr_artifacts(md_text: str) -> str:
    """Исправить OCR-артефакты: разбитые слова, лишние пробелы."""
    md_text = re.sub(r"(\w)-\n(\w)", r"\1\2", md_text)

    _RUS_SINGLE_WORDS = set("ивскуоаяжбгдеёзйлмнпртфхцчшщъыьэю")

    def _should_merge(m):
        g1, g2 = m.group(1), m.group(2)
        if len(g1) == 1 and g1.lower() not in _RUS_SINGLE_WORDS:
            return g1 + g2
        if len(g2) == 1 and g2.lower() not in _RUS_SINGLE_WORDS:
            return g1 + g2
        return m.group(0)

    md_text = re.sub(r"\b([а-яё]{1,3})\s+([а-яё]{1,3})\b",
                     _should_merge,
                     md_text)

    md_text = re.sub(r" {2,}", " ", md_text)
    return md_text


# ═══════════════════════════════════════════════════════════════════════════
# 9. Полная скриптовая постобработка
# ═══════════════════════════════════════════════════════════════════════════

def run_script_postprocess(md_text: str, img_dir: str | Path) -> str:
    """Выполнить всю скриптовую постобработку.

    Порядок (без wrap_equations — в Yandex формулы уже в $$):
      1. HTML-таблицы → MD
      2. Объединение смежных таблиц
      3. LaTeX-чистка
      4. Переименование изображений
      5. Подписи → курсив
      6. Примечания → цитаты
      7. Подписи "Таблица N", "Рис. N"
      8. OCR-артефакты
    """
    log.info("Скриптовая постобработка:")

    md_text, n_tables = convert_html_tables(md_text)
    log.info(f"  1. HTML->MD таблиц: {n_tables}")

    md_text = merge_tables(md_text)
    log.info("  2. Таблицы: объединение")

    md_text = cleanup_latex(md_text)
    log.info("  3. LaTeX: очищен")

    md_text, n_imgs = rename_images(md_text, img_dir)
    log.info(f"  4. Изображения: {n_imgs} переименовано")

    md_text = fix_image_captions(md_text)
    log.info("  5. Подписи: курсив")

    md_text = fix_notes(md_text)
    log.info("  6. Примечания -> цитаты")

    md_text = fix_table_fig_labels(md_text)
    log.info("  7. Подписи: Таблица/Рис")

    md_text = fix_ocr_artifacts(md_text)
    log.info("  8. OCR-артефакты: исправлены")

    return md_text


# ═══════════════════════════════════════════════════════════════════════════
# 10. AI-постобработка
# ═══════════════════════════════════════════════════════════════════════════

AI_CLEANUP_DEFAULT_PROMPT = """Ты — редактор технических текстов. Исправь Markdown, полученный из OCR-парсера PDF.

ЧТО МОЖНО ДЕЛАТЬ (только это):
1. LaTeX: убрать \\mathtt{...}, \\mathsf{...}, \\mathfrak{...}, \\pmb{...}, \\boldsymbol{...}, \\mathbf{...}
2. LaTeX: \\mathrm{...} -> \\text{...}
3. OCR-артефакты: удалить мусор
4. Пробелы внутри чисел: 0 , 4 2 9 -> 0,429
5. Лишние фигурные скобки в LaTeX: {X} -> X
6. LaTeX-команды в обычном тексте: убрать
7. csv-блоки: оставить как есть
8. В ячейках таблиц LaTeX -> обернуть в $...$
9. Подписи под рисунками -> курсив: *текст*
10. Примечания -> цитаты: > Примечание ...
11. Объединить таблицы (Продолжение, Окончание, одинаковый заголовок)
12. Удалить разрывы слов, добавить пробелы

ЧТО НЕЛЬЗЯ:
- НЕ изменяй структуру Markdown-таблицы
- НЕ удаляй изображения: ![Рисунок N](image/fig_N.jpg)
- НЕ меняй нумерацию разделов, формул, таблиц
- НЕ добавляй разделители"""


def _chunk_text(text: str, max_chars: int = AI_MAX_CHARS) -> list[str]:
    """Разбить текст на части для AI, сохраняя целостность таблиц и кодовых блоков."""
    lines = text.split("\n")
    chunks = []
    current = ""
    in_table = False
    in_code = False
    para_lines = []
    code_lines = []

    def flush_para() -> None:
        nonlocal current, para_lines
        if not para_lines:
            return
        para_text = "\n".join(para_lines)
        para_lines = []
        para_len = len(para_text)
        if not current:
            current = para_text
        elif len(current) + 1 + para_len > max_chars and not in_table and not in_code:
            chunks.append(current.strip())
            current = para_text
        else:
            current += "\n\n" + para_text

    def flush_code() -> None:
        nonlocal current, code_lines, in_code
        if code_lines:
            if current:
                chunks.append(current.strip())
                current = ""
            code_text = "\n".join(code_lines)
            chunks.append(code_text)
            code_lines = []
        in_code = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if not in_code:
                flush_para()
                if current:
                    chunks.append(current.strip())
                    current = ""
                in_code = True
                code_lines.append(line)
            else:
                code_lines.append(line)
                flush_code()
            continue

        if in_code:
            code_lines.append(line)
            continue

        is_table_row = stripped.startswith("|") and "|" in stripped[1:]
        if is_table_row and not in_code:
            if not in_table:
                flush_para()
            in_table = True
        elif not is_table_row and in_table:
            if not stripped:
                flush_para()
                in_table = False
                continue

        if not stripped and not in_table:
            flush_para()
            continue

        para_lines.append(line)

    flush_para()
    flush_code()
    if current:
        chunks.append(current.strip())

    return [c for c in chunks if c]


def _call_ai_api(text: str, config: dict, context: str = "") -> str | None:
    """AI-постобработка через config-указанный провайдер.

    Поддерживает DeepSeek и Provod (OpenAI-совместимый API).
    При отсутствии или ошибках primary переходит на fallback.
    """
    ai_cfg = config.get("ai_postprocess", config)

    # Primary provider
    provider = ai_cfg.get("provider", "deepseek")
    model = ai_cfg.get("model", "deepseek-v4-flash")
    api_key_env = ai_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    base_url = ai_cfg.get("base_url", "https://api.deepseek.com/v1")
    prompt = ai_cfg.get("prompt", AI_CLEANUP_DEFAULT_PROMPT)

    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        log.error(f"{api_key_env} не задан, пробую fallback")
        fb = ai_cfg.get("fallback", {})
        if fb:
            provider = fb.get("provider", provider)
            model = fb.get("model", model)
            api_key_env = fb.get("api_key_env", api_key_env)
            base_url = fb.get("base_url", base_url)
            api_key = os.environ.get(api_key_env, "")
            if not api_key:
                log.error(f"Fallback {api_key_env} не задан")
                return None
            log.info(f"Fallback: {provider} / {model}")
        else:
            return None

    def _do_request(url: str, mdl: str, key: str, prv: str) -> str | None:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": mdl,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": 64000,
        }
        for attempt in range(3):
            try:
                with httpx.Client(timeout=600) as client:
                    resp = client.post(
                        f"{url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                if resp.status_code == 503:
                    log.warning(f"  {prv}/{mdl}: 503, попытка {attempt + 1}/3")
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```(?:markdown)?\s*\n?", "", content, flags=re.MULTILINE)
                content = re.sub(r"\n```\s*$", "", content, flags=re.MULTILINE)
                return content
            except Exception as e:
                log.warning(f"  {prv}/{mdl}: {e}, попытка {attempt + 1}/3")
                time.sleep(5)
        return None

    # Primary attempt
    log.info(f"AI: {provider} / {model}")
    result = _do_request(base_url, model, api_key, provider)
    if result is not None:
        return result

    # Fallback attempt
    fb = ai_cfg.get("fallback", {})
    if fb:
        fb_provider = fb.get("provider", "provod")
        fb_model = fb.get("model", "google/gemini-3.5-flash")
        fb_key_env = fb.get("api_key_env", "PROVOD_API_KEY")
        fb_base_url = fb.get("base_url", "https://api.provod.ai/v1")
        fb_key = os.environ.get(fb_key_env, "")
        if fb_key:
            log.info(f"Пробую fallback: {fb_provider} / {fb_model}")
            result = _do_request(fb_base_url, fb_model, fb_key, fb_provider)
            if result is not None:
                return result
        else:
            log.warning(f"Fallback {fb_key_env} не задан")

    log.error("AI: все модели недоступны")
    return None


def ai_postprocess(md_text: str, config: dict, file_label: str = "") -> str:
    """AI-постобработка Markdown через Provod с чекпойнтингом."""
    log.info(f"AI-постобработка ({file_label})")

    max_chars = AI_MAX_CHARS
    chunks = _chunk_text(md_text, max_chars=max_chars)
    log.info(f"  Чанков: {len(chunks)}")

    if len(chunks) == 1:
        result = _call_ai_api(md_text, config, file_label)
        return result if result else md_text

    # Чекпойнт: промежуточные результаты
    ckpt_dir = Path("tmp") / ".ai_checkpoints"
    safe_label = re.sub(r'[^a-zA-Z0-9_-]', '_', file_label or "default")
    ckpt_path = ckpt_dir / f"{safe_label}.json"
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    # Загружаем существующий чекпойнт
    results: list[str | None] = []
    if ckpt_path.exists():
        try:
            saved = json.loads(ckpt_path.read_text(encoding="utf-8"))
            if isinstance(saved, list) and len(saved) == len(chunks):
                results = saved
                done = sum(1 for r in results if r is not None)
                log.info(f"  Чекпойнт найден: обработано {done}/{len(chunks)} чанков")
        except Exception as e:
            log.warning(f"  Ошибка чтения чекпойнта: {e}")
            results = [None] * len(chunks)
    else:
        results = [None] * len(chunks)

    for i, r in enumerate(results):
        if r is None:
            log.info(f"  Часть {i + 1}/{len(chunks)} ({len(chunks[i]) if i < len(chunks) else '?'} символов)")
            result = _call_ai_api(chunks[i], config, f"{file_label} [ч.{i + 1}]")
            results[i] = result if result else chunks[i]
            try:
                ckpt_path.write_text(
                    json.dumps(results, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning(f"  Ошибка записи чекпойнта: {e}")
        else:
            log.info(f"  Часть {i + 1}/{len(chunks)} — пропущена (из чекпойнта)")

    # Удаляем чекпойнт после успешного завершения
    try:
        ckpt_path.unlink(missing_ok=True)
    except Exception:
        pass

    return "\n\n".join(results)


# ═══════════════════════════════════════════════════════════════════════════
# 11. CLI и Main
# ═══════════════════════════════════════════════════════════════════════════

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разобрать аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Пайплайн конвертации PDF/DOCX в Markdown через Yandex Vision OCR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\\
Примеры:
  %(prog)s -i file.pdf
  %(prog)s -i dir/ --ai
  %(prog)s -i file.pdf --ai --config my_config.yaml
  %(prog)s -i file.md --ai                 # только AI-постобработка, без OCR
  %(prog)s -i file.pdf --ai-table          # + vision-распознавание таблиц
  %(prog)s -i file.pdf --ai-table --ai     # + vision + AI-постобработка (gap-filling)
        """,
    )

    parser.add_argument(
        "-i", "--input",
        required=True,
        help="Входной файл или папка (PDF/DOCX/DOC/MD). Для .md обязателен флаг --ai",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Включить AI-постобработку. Для .md — единственный режим (без OCR)",
    )
    parser.add_argument(
        "--ai-table",
        action="store_true",
        help="AI-распознавание таблиц через vision-модель (вырезка из PDF + Gemini)",
    )
    parser.add_argument(
        "--config",
        default="./config_ai.yaml",
        help="Путь к config_ai.yaml (по умолч. ./config_ai.yaml)",
    )

    return parser.parse_args(argv)


def process_file(
    input_path: str,
    use_ai: bool,
    use_ai_table: bool,
    config: dict,
    api_key: str,
    folder_id: str,
    output_base: str,
    tmp_base: str,
) -> bool:
    """Обработать один файл: Yandex OCR → парсинг → изображения → постобработка → сохранение.

    Args:
        use_ai_table: Включить vision-распознавание таблиц (--ai-table).

    Returns:
        True при успехе, False при ошибке.
    """
    input_path = str(Path(input_path).resolve())
    file_stem = Path(input_path).stem
    log.info(f"{'=' * 60}")
    log.info(f"Обработка: {input_path}")

    # Выходные папки
    out_dir = Path(output_base) / file_stem
    img_dir = out_dir / "image"
    file_tmp_dir = Path(tmp_base) / file_stem
    ensure_dir(out_dir)
    ensure_dir(img_dir)
    ensure_dir(file_tmp_dir)

    # Режим: .md + --ai → только AI-постобработка, без OCR и скриптов
    ext = Path(input_path).suffix.lower()
    if ext == ".md":
        if use_ai:
            log.info(f"AI-постобработка MD: {file_stem}.md")
            md_text = Path(input_path).read_text(encoding="utf-8")
            ai_cfg = config.get("ai_postprocess", config.get("postprocess", config))
            result = ai_postprocess(md_text, ai_cfg, file_stem)
            if result:
                out_path = Path(input_path).parent / f"{file_stem}_ai.md"
                safe_write(out_path, result)
                log.info(f"Результат: {out_path} ({len(result)} символов)")
                log.info(f"{'=' * 60}")
                return True
            else:
                log.error("AI-постобработка не дала результата")
                return False
        else:
            log.error(".md файл требует флаг --ai")
            return False

    # Этап 1: DOCX → PDF (если нужно)
    pdf_path = input_path
    was_docx = False
    if ext in (".docx", ".doc"):
        try:
            pdf_path = str(convert_docx_to_pdf(input_path, file_tmp_dir))
            was_docx = True
        except Exception as e:
            log.error(f"  Ошибка конвертации DOCX: {e}")
            return False

    try:
        # Этап 2: Yandex OCR
        pages = send_to_yandex_ocr(pdf_path, api_key, folder_id)

        if not pages:
            log.warning("  Yandex OCR вернул пустой результат")
            return False

        # Сохраняем JSON
        json_path = file_tmp_dir / "yandex_result.json"
        safe_write(json_path, json.dumps(pages, ensure_ascii=False, indent=2))

        # Этап 3: Парсинг JSON → Markdown
        md_text, pictures, page_boundaries = parse_yandex_json_to_md(pages=pages)

        if not md_text.strip():
            log.warning("  Markdown пуст после парсинга JSON")
            return False

        # Сохраняем raw.md
        safe_write(file_tmp_dir / "raw.md", md_text)

        # Этап 4: Извлечение изображений
        if pictures:
            extracted = extract_images_from_pdf(
                pdf_path, pictures, img_dir, pages=pages,
            )
            # Вставляем ссылки на изображения в текст по координатам
            md_text = _insert_images_into_md(md_text, extracted, pages=pages, page_boundaries=page_boundaries)
            log.info(f"  Извлечено изображений: {len(extracted)}")
        else:
            log.info("  Нет pictures для извлечения")

        # Этап 4b: Vision-распознавание таблиц (если --ai-table)
        if use_ai_table and pages:
            log.info("Vision-распознавание таблиц:")
            table_images = extract_table_images(pdf_path, pages, img_dir)
            log.info(f"  Вырезано таблиц: {len(table_images)}")

            if table_images:
                vision_api_key = os.environ.get(
                    config.get("table_vision", {}).get("api_key_env", "PROVOD_API_KEY"),
                    "",
                )
                if vision_api_key:
                    recognized = recognize_tables_vision(
                        table_images, img_dir, config, file_tmp_dir, vision_api_key,
                    )
                    log.info(f"  Распознано таблиц: {recognized}/{len(table_images)}")
                else:
                    log.warning("  PROVOD_API_KEY не задан — vision-распознавание пропущено")

        # Этап 5: Скриптовая постобработка
        md_text = run_script_postprocess(md_text, img_dir)

        # Этап 6: AI-постобработка (если --ai)
        if use_ai:
            ai_cfg = config.get("ai_postprocess", config.get("postprocess", config))
            md_text = ai_postprocess(md_text, ai_cfg, file_stem)

        # Этап 6b: Gap-filling — проверить таблицы vision-результатом (если --ai + --ai-table)
        if use_ai and use_ai_table:
            table_files = sorted(Path(file_tmp_dir).glob("table_*.md"))
            if table_files:
                log.info("Gap-filling: сверка таблиц с vision-результатом")
                table_texts = "\n\n".join(
                    f.read_text(encoding="utf-8") for f in table_files
                )
                # Передаём полный текст: markdown + таблицы из vision
                gap_input = (
                    f"Проверь все таблицы в Markdown-файле ниже с файлами таблиц, "
                    f"полученными из vision-распознавания. "
                    f"Дополни пропущенные или нераспознанные данные в таблицах. "
                    f"Не выдумывай данные.\n\n"
                    f"=== Markdown-файл ===\n{md_text}\n\n"
                    f"=== Файлы таблиц (vision-распознавание) ===\n{table_texts}\n"
                )
                ai_cfg = config.get("ai_postprocess", config.get("postprocess", config))
                gap_result = _call_ai_api(gap_input, ai_cfg)
                if gap_result:
                    md_text = gap_result
                    log.info(f"  Gap-filling: применён ({len(gap_result)} символов)")
                else:
                    log.warning("  Gap-filling: модель не ответила")

        # Сохраняем итоговый .md
        md_path = out_dir / f"{file_stem}.md"
        safe_write(md_path, md_text)
        log.info(f"Итоговый Markdown: {md_path} ({len(md_text)} символов)")

    except Exception as e:
        log.error(f"  Ошибка обработки: {e}")
        return False
    finally:
        # Сконвертированный PDF из DOCX сохраняется в tmp/<file>/ согласно SPEC_YA
        pass

    log.info(f"{'=' * 60}")
    return True


def main() -> None:
    """Точка входа. Парсинг аргументов, итерация по файлам, process_file()."""
    args = parse_args()

    # Определяем выходные папки относительно входного файла/папки
    input_path = Path(args.input).resolve()
    if input_path.is_file():
        base_dir = input_path.parent
    else:
        base_dir = input_path

    output_base = str(base_dir / "Markdown")
    tmp_base = str(base_dir / "tmp")

    # Настраиваем логгирование
    log_path = Path(tmp_base) / "Create_Markdown_VisionOCR.log"
    setup_logging(log_path)

    log.info(f"Вход: {args.input}")
    log.info(f"AI: {args.ai}")
    log.info(f"AI-Table: {args.ai_table}")
    log.info(f"Выход: {output_base}")
    log.info(f"Лог: {log_path}")

    # Находим файлы
    files = find_input_files(args.input)
    if not files:
        log.error("Файлы не найдены")
        sys.exit(1)

    log.info(f"Найдено файлов: {len(files)}")

    # Загружаем .env
    env_path = Path(__file__).parent / ".env"
    load_env(env_path)
    api_key = os.environ.get("YANDEX_API_KEY", "")
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "")

    # Yandex API ключи нужны только если есть не-.md файлы
    need_yandex = any(f.suffix.lower() != ".md" for f in files)
    if need_yandex:
        if not api_key or not folder_id:
            log.error("YANDEX_API_KEY и YANDEX_FOLDER_ID должны быть заданы в .env")
            sys.exit(1)

    # Загружаем конфиг AI (если нужен — для --ai или --ai-table)
    config = load_config(args.config) if (args.ai or args.ai_table) else {}

    # Обрабатываем каждый файл
    success = 0
    failed = 0
    for file_path in files:
        ok = process_file(
            str(file_path),
            args.ai,
            args.ai_table,
            config,
            api_key,
            folder_id,
            output_base,
            tmp_base,
        )
        if ok:
            success += 1
        else:
            failed += 1

    log.info(f"{'=' * 60}")
    log.info(f"Завершено: успешно {success}, ошибок {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
