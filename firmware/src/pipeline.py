#!/usr/bin/env python3
"""
pipeline.py — Пайплайн конвертации PDF/DOCX/MD в Markdown через Yandex Vision OCR.

Модель: math-markdown (даёт markdown + tables + pictures + blocks).

Постобработка:
  — скриптовая (всегда): HTML-таблицы → MD, LaTeX-чистка, изображения → fig_N,
    OCR-артефакты, примечания, подписи
  — AI (флаг --ai): vision-распознавание таблиц → единый AI-проход
    (коррекция таблиц + постобработка) через deepseek / provod

Режим .md + --ai: только AI-постобработка готового .md файла, без OCR.

Использование:
  python3 pipeline.py -i file.pdf
  python3 pipeline.py -i file.pdf --ai --config config_ai.yaml
  python3 pipeline.py -i file.md --ai                 # только AI
  python3 pipeline.py -i file.pdf --rag               # + RAG JSONL (секция 12)
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
import tempfile
import time
from pathlib import Path
from typing import Callable

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
AI_MAX_CHARS = 80000
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

    # Подавляем DEBUG от httpcore/httpx (сильно тормозит на больших ответах)
    for noisy in ("httpcore", "httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

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
    """Атомарно записать файл (temp-file + os.replace).

    Пишет во временный файл в той же директории (создавая её при
    необходимости), затем os.replace() — перезапись атомарна: читатели
    видят либо старое, либо новое содержимое, никогда частичную запись.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Убираем временный файл при ошибке, не прячем исходную ошибку
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
# 3b. Heading Extractor (ADR-8)
# ═══════════════════════════════════════════════════════════════════════════

_HEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.\s")
_HEADING_Y_TOLERANCE = 15.0
_HEADING_MAX_LEN = 100
_HEADING_MAX_WORDS = 10
_HEADING_REL_X_MAX = 0.50


def _extract_headings_from_json(pages: list[dict] | None = None) -> list[dict]:
    """Извлечь заголовки разделов из Yandex OCR JSON.

    5 правил (ADR-8):
      1. Текст начинается с номера раздела: ^\\d+\\.(\\d+\\.)*\\s
      2. Длина: len(text) < 100 символов И слов < 10
      3. Левый край: x_left / page_width < 0.50
      4. Один блок на строке: на той же странице и Y (±15px) нет других блоков
      5. Уровень по глубине номера: "3"→1, "3.2"→2, "3.2.1"→3, "3.2.1.1"→4

    Returns:
        [{"page": N, "y": Y, "level": L, "number": "3.2.1",
          "text": "Молниеприемники", "full_text": "3.2.1. Молниеприемники"}, ...]
    """
    if not pages:
        return []

    headings: list[dict] = []

    for page_idx, page in enumerate(pages):
        ta = page.get("result", {}).get("textAnnotation", {})
        blocks = ta.get("blocks", [])
        if not blocks:
            continue

        page_width = float(ta.get("width") or 0) or 1.0

        # Per-page Y-индекс всех блоков (правило 4: один блок на строке)
        y_tops: list[float] = []
        for block in blocks:
            vertices = block.get("boundingBox", {}).get("vertices", [])
            if vertices:
                y_tops.append(min(float(v.get("y", 0)) for v in vertices))

        for block in blocks:
            vertices = block.get("boundingBox", {}).get("vertices", [])
            if not vertices:
                continue

            # Текст блока — как в _block_to_md(): join строк с пробелом
            text_parts: list[str] = []
            for line_data in block.get("lines", []):
                t = line_data.get("text", "").strip()
                if t:
                    text_parts.append(t)
            text = " ".join(text_parts).strip()
            if not text:
                continue

            # Правило 1: текст начинается с номера раздела
            m = _HEADING_NUMBER_RE.match(text)
            if not m:
                continue

            # Правило 2: длина и число слов
            if len(text) >= _HEADING_MAX_LEN or len(text.split()) >= _HEADING_MAX_WORDS:
                continue

            # Правило 3: левый край страницы (не таблица/центр)
            x_left = min(float(v.get("x", 0)) for v in vertices)
            if page_width and x_left / page_width >= _HEADING_REL_X_MAX:
                continue

            # Правило 4: один блок на строке (±15px) — отсекает ячейки таблиц
            y_top = min(float(v.get("y", 0)) for v in vertices)
            blocks_at_y = sum(
                1 for y in y_tops if abs(y - y_top) <= _HEADING_Y_TOLERANCE
            )
            if blocks_at_y != 1:
                continue

            # Правило 5: уровень по глубине номера
            number = m.group(1)
            level = number.count(".") + 1

            headings.append(
                {
                    "page": page_idx,
                    "y": y_top,
                    "level": level,
                    "number": number,
                    "text": text[m.end():].strip(),
                    "full_text": text,
                }
            )

    return headings


def _apply_headings_to_md(md_text: str, headings: list[dict] | None = None) -> str:
    """Заменить **жирные** заголовки разделов на Markdown-заголовки.

    Для каждого heading ищет в md_text строку **full_text** и заменяет
    на '#' * (level + 1) + ' ' + full_text через re.sub(count=1) —
    дубликаты (например, в оглавлении) остаются жирным текстом.

    Если headings пуст — вернуть md_text без изменений.
    """
    if not headings or not md_text:
        return md_text

    for heading in headings:
        full_text = heading.get("full_text", "")
        level = heading.get("level", 1)
        if not full_text:
            continue
        pattern = r"\*\*" + re.escape(full_text) + r"\*\*"
        replacement = "#" * (level + 1) + " " + full_text
        md_text = re.sub(pattern, replacement, md_text, count=1)

    return md_text


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
# 4b. Vision-распознавание таблиц (в составе --ai)
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
        [{"page": int, "table_idx": int, "path": "table_N.png", "id": "t_pN_M"}, ...]
        id — сквозная метка таблицы: t_p{page+1}_{table_index_on_page}
        (page+1 — 1-based страница, index — 0-based индекс на странице).
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
            x0 = min(xs) * sx - 2
            y0 = max(0, min(ys) * sy - 32)  # +30px вверх для заголовка
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
                "id": f"t_p{pi + 1}_{ti}",
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
    Результат сохраняется как tmp/<file>/table_N.md;
    первой строкой файла идёт ID-маркер <!-- t_pN_M --> (из table_images[].id).

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

    prompt = vision_cfg.get("prompt")
    if not prompt:
        log.error("не задан промт: укажите prompt в секции table_vision конфига")
        sys.exit(1)

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
            safe_write(out_path, f"<!-- {ti['id']} -->\n{result}")
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
    # \\f — OCR-артефакт, невалидная LaTeX-команда; НЕ трогать \\frac, \\flat и др.
    # (?![a-zA-Z]) — удалять только если за \\f не идёт буква (иначе съедаем команду)
    text = re.sub(r"\\f(?![a-zA-Z])\s*", "", text)
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
    # Защита: пустой img_dir = Path('.') — переименовал бы текущую папку
    if not str(img_dir).strip() or img_dir == Path("."):
        log.warning("  rename_images: img_dir пуст — переименование пропущено")
        return md_text, 0
    if not img_dir.exists():
        return md_text, 0

    # Все изображения кроме table_N.png (их не переименовываем — это вырезанные таблицы)
    img_files = sorted(
        p for p in img_dir.glob("*")
        if p.is_file() and not p.name.startswith("table_")
    )
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


def fix_latex_caret_spaces(md_text: str) -> str:
    """Добавить пробелы вокруг ^ внутри LaTeX-формул ($...$ и $$...$$).

    Правило (как в AI-промпте config_ai.yaml, но без --ai):
      - внутри формул перед ^ ставится пробел, если его нет;
      - внутри формул после ^ ставится пробел, если его нет;
      - ^ вне формул не трогается;
      - экранированный \\^ (например 90\\^{\\circ}) не трогается;
      - уже существующие пробелы не дублируются.
    """
    # $$...$$ (многострочные) обрабатываем первыми, чтобы $...$ не «съел» их границы.
    _formula_re = re.compile(r"\$\$((?s:.+?))\$\$|\$(.+?)\$")

    def _fix_carets(content: str) -> str:
        # пробел перед ^ (если перед ним не пробел и это не \\^)
        content = re.sub(r"(\S)(?<!\\)\^", r"\1 ^", content)
        # пробел после ^ (если после него не пробел и это не \\^)
        content = re.sub(r"(?<!\\)\^(\S)", r"^ \1", content)
        return content

    def _rep(m):
        if m.group(1) is not None:
            return f"$${_fix_carets(m.group(1))}$$"
        return f"${_fix_carets(m.group(2))}$"

    return _formula_re.sub(_rep, md_text)


# ═══════════════════════════════════════════════════════════════════════════
# 9. Полная скриптовая постобработка
# ═══════════════════════════════════════════════════════════════════════════

def _inject_table_ids(
    md_text: str,
    page_boundaries: list[tuple[int, int]] | None = None,
    table_images: list[dict] | None = None,
) -> str:
    """Вставить ID-маркеры <!-- t_pN_M --> перед Markdown-таблицами.

    Сквозная маркировка для AI-сопоставления OCR-таблиц с vision-эталонами
    (тот же формат id, что в extract_table_images(): t_p{page+1}_{index}).

    Логика:
      - md_text разбивается на строки; для каждой строки определяется номер
        страницы через page_boundaries ([(start, end), ...], 0-based строки,
        end exclusive).
      - Таблица = группа строк |...|, перед которой (пропуская пустые строки)
        идёт строка-название (не |...|-строка). Несколько |...|-строк подряд —
        ОДНА таблица.
      - Индекс таблицы на странице (idx) сбрасывается на новой странице.
      - Маркер <!-- t_p{page+1}_{idx} --> вставляется отдельной строкой
        ПЕРЕД строкой-названием.

    Порядок таблиц в md_text совпадает с порядком в Yandex JSON (один
    источник — parse_yandex_json_to_md), поэтому индексы совпадают с id
    из extract_table_images().

    Args:
        md_text: Markdown-текст (после шагов 1–2 постобработки).
        page_boundaries: Границы страниц в md_text.
        table_images: Список от extract_table_images() — используется как
            признак «включить маркировку» (проверяется в
            run_script_postprocess()).

    Returns:
        md_text с вставленными ID-маркерами.
    """
    if not page_boundaries:
        return md_text

    lines = md_text.split("\n")

    def _is_row(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("|") and "|" in stripped[1:]

    def _page_of_line(line_num: int) -> int:
        for page_idx, (p_start, p_end) in enumerate(page_boundaries):
            if p_start <= line_num < p_end:
                return page_idx
        # Строки за пределами последней границы относим к последней странице
        return len(page_boundaries) - 1

    # Проход 1: найти таблицы — пары (индекс строки-названия, индекс первой |-строки)
    table_starts: list[tuple[int, int]] = []
    prev_nonblank = -1
    prev_nonblank_is_row = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        is_row = _is_row(line)
        if is_row and not prev_nonblank_is_row:
            table_starts.append((prev_nonblank, i))
        prev_nonblank = i
        prev_nonblank_is_row = is_row

    if not table_starts:
        return md_text

    # Собираем маркеры: по одному на строку-название, индекс — на страницу
    markers: dict[int, str] = {}
    page_table_idx: dict[int, int] = {}
    for name_idx, first_row_idx in table_starts:
        page = _page_of_line(first_row_idx)
        idx = page_table_idx.get(page, 0)
        markers[name_idx] = f"<!-- t_p{page + 1}_{idx} -->"
        page_table_idx[page] = idx + 1

    # Проход 2: собрать результат, вставляя маркер перед строкой-названием
    result: list[str] = []
    # Таблица в самом начале документа без строки-названия — маркер в начало
    prefix = markers.pop(-1, None)
    if prefix is not None:
        result.append(prefix)
    for i, line in enumerate(lines):
        marker = markers.get(i)
        if marker is not None:
            result.append(marker)
        result.append(line)

    return "\n".join(result)


def _table_id_sort_key(tid: str) -> tuple[int, int]:
    """Ключ сортировки ID-маркеров таблиц в порядке t_p1_0, t_p1_1, t_p2_0, ...

    Парсит ID вида t_p{page}_{index} и возвращает (page, index).
    Неизвестный формат сортируется первым ((0, 0)).
    """
    m = re.match(r"t_p(\d+)_(\d+)", tid)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def run_script_postprocess(
    md_text: str,
    img_dir: str | Path,
    page_boundaries: list[tuple[int, int]] | None = None,
    table_images: list[dict] | None = None,
) -> str:
    """Выполнить всю скриптовую постобработку.

    Порядок (без wrap_equations — в Yandex формулы уже в $$):
      1. HTML-таблицы → MD
      2. Объединение смежных таблиц
      2b. Вставка ID-маркеров <!-- t_pN_M --> перед таблицами
          (если переданы page_boundaries и table_images)
      3. LaTeX-чистка
      4. Переименование изображений
      5. Подписи → курсив
      6. Примечания → цитаты
      7. Подписи "Таблица N", "Рис. N"
      8. OCR-артефакты
      9. Пробелы вокруг ^ в LaTeX-формулах

    Args:
        md_text: Markdown-текст.
        img_dir: Папка с изображениями.
        page_boundaries: [(start_line, end_line), ...] — границы страниц
            в md_text (0-based строки, end exclusive), из parse_yandex_json_to_md().
        table_images: Список от extract_table_images() — включает id каждой
            вырезанной таблицы. Маркеры вставляются только при наличии обоих
            параметров (иначе пайплайн ведёт себя как раньше).
    """
    log.info("Скриптовая постобработка:")

    md_text, n_tables = convert_html_tables(md_text)
    log.info(f"  1. HTML->MD таблиц: {n_tables}")

    md_text = merge_tables(md_text)
    log.info("  2. Таблицы: объединение")

    if page_boundaries and table_images:
        md_text = _inject_table_ids(md_text, page_boundaries, table_images)
        log.info("  2b. Таблицы: ID-маркеры вставлены")

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

    md_text = fix_latex_caret_spaces(md_text)
    log.info("  9. LaTeX: пробелы вокруг ^")

    return md_text


# ═══════════════════════════════════════════════════════════════════════════
# 10. AI-постобработка
# ═══════════════════════════════════════════════════════════════════════════

# Границы разделов для section-aware chunking (ADR-007):
#   **N. Title**, **N.N. Title**, ### N.N. Title, #### N.N.N. Title,
#   а также заголовки без числового префикса: ### Title, **TITLE**
# Любая markdown-заголовок (#...##### + пробел + непустой символ) ИЛИ
# строка целиком из **жирного текста** (ровно одна пара звёздочек).
# Примечание: $ стоит внутри группы, привязывая к концу строки только
# bold-ветку — ветка заголовка матчит префикс строки (сам заголовок).
SECTION_BOUNDARY_RE = re.compile(
    r"^(?:#{1,5}\s+\S|\*\*[^*]+\*\*$)"
)


def _split_oversized_section(text: str, max_chars: int = AI_MAX_CHARS) -> list[str]:
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


def _chunk_text(text: str, max_chars: int = AI_MAX_CHARS) -> list[str]:
    """Разбить текст на части для AI по границам разделов.

    Двухфазный алгоритм (ADR-007):
      1. Партиционирование: ищем границы разделов (SECTION_BOUNDARY_RE).
         Секция = строки от заголовка (включительно) до следующего заголовка.
         Текст до первого заголовка — преамбула, отдельная секция.
      2. Сборка чанков из целых секций, пока помещаются в max_chars;
         секции > max_chars разбиваются через _split_oversized_section().
         Если границ разделов не найдено — fallback на старую логику.
    """
    lines = text.split("\n")

    # Фаза 1: найти границы разделов
    boundary_idx = [i for i, line in enumerate(lines) if SECTION_BOUNDARY_RE.match(line)]
    if not boundary_idx:
        # Заголовков разделов нет — старая логика
        return _split_oversized_section(text, max_chars)

    sections: list[str] = []
    # Преамбула: текст до первого заголовка
    if boundary_idx[0] > 0:
        sections.append("\n".join(lines[:boundary_idx[0]]))
    for i, idx in enumerate(boundary_idx):
        end = boundary_idx[i + 1] if i + 1 < len(boundary_idx) else len(lines)
        sections.append("\n".join(lines[idx:end]))

    # Фаза 2: сборка чанков из целых секций
    chunks: list[str] = []
    current = ""
    for section in sections:
        if not current:
            # Первая секция / после сброса: если сама секция-гигант — разбить
            if len(section) > max_chars:
                chunks.extend(_split_oversized_section(section, max_chars))
            else:
                current = section
        elif len(current) + len(section) + 2 <= max_chars:
            current += "\n\n" + section
        elif len(section) > max_chars:
            # Секция-гигант: разбить по параграфам
            chunks.append(current.strip())
            current = ""
            chunks.extend(_split_oversized_section(section, max_chars))
        else:
            chunks.append(current.strip())
            current = section
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
    prompt = ai_cfg.get("prompt")
    if not prompt:
        log.error("не задан промт: укажите prompt в секции ai_postprocess конфига")
        sys.exit(1)

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
            chunk = chunks[i]
            if i > 0:
                # Добавляем последние 3 строки предыдущего чанка как контекст
                prev_lines = chunks[i - 1].strip().split("\n")
                context = "\n".join(prev_lines[-3:]) if len(prev_lines) >= 3 else chunks[i - 1].strip()
                chunk = f"[Контекст из предыдущего чанка]\n{context}\n[Конец контекста]\n\n{chunk}"
            result = _call_ai_api(chunk, config, f"{file_label} [ч.{i + 1}]")
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
# 12. RAG JSONL Converter (ADR-9)
# ═══════════════════════════════════════════════════════════════════════════

_MD_HEADING_RE = re.compile(r"^(#{2,5})\s+(.+)$")
_RAG_DEFAULT_MAX_CHARS = 1500  # v1 (deprecated, ADR-9)
_RAG_DEFAULT_MAX_TOKENS = 7000  # v2 (ADR-010)

# Префиксы канонических кросс-ссылок: определяются по ключевым словам
# regexp-паттерна (паттерны в rag_config.yaml захватывают только номер).
_REF_LABEL_HINTS: list[tuple[str, str]] = [
    ("табл", "табл."),
    ("пункт", "п."),
    ("разд", "разд."),
    ("гл", "гл."),
]


def load_rag_config(config_path: str | Path) -> dict:
    """Загрузить rag_config.yaml.

    Returns:
        Полный словарь конфига с секциями defaults, references, documents.

    Raises:
        FileNotFoundError: файл не найден.
        yaml.YAMLError: ошибка парсинга YAML.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"rag_config не найден: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Валидация (ADR-010b): warnings при ошибках, конфиг продолжает работать
    for err in validate_rag_config(config):
        log.warning(f"  rag_config: {err}")

    return config


def validate_rag_config(config: dict) -> list[str]:
    """Проверить rag_config на корректность. Возвращает список ошибок.

    Правила (ADR-010b):
      - defaults.max_chunk_tokens — int 100..100000
      - documents.<slug>.status — "active" | "inactive" (отсутствие → warning
        и использование default_status)
      - status=active требует null для status_reason / replaced_by_document_id
        / replaced_by_doc_key
    """
    errors: list[str] = []

    defaults = config.get("defaults", {})
    mt = defaults.get("max_chunk_tokens", 0)
    if not isinstance(mt, int) or mt < 100 or mt > 100000:
        errors.append(f"defaults.max_chunk_tokens должен быть int 100..100000, получено {mt}")

    default_status = defaults.get("default_status", "active")
    for slug, doc in config.get("documents", {}).items():
        status = doc.get("status")
        if status is None:
            errors.append(
                f"documents.{slug}: status не указан "
                f"(будет '{default_status}')"
            )
        elif status not in ("active", "inactive"):
            errors.append(
                f"documents.{slug}: status='{status}' — допустимы только active/inactive"
            )

        if status == "active":
            if doc.get("status_reason") is not None:
                errors.append(f"documents.{slug}: status=active, но status_reason не null")
            if doc.get("replaced_by_document_id") is not None:
                errors.append(
                    f"documents.{slug}: status=active, но replaced_by_document_id не null"
                )
            if doc.get("replaced_by_doc_key") is not None:
                errors.append(
                    f"documents.{slug}: status=active, но replaced_by_doc_key не null"
                )

    return errors


def _extract_heading_number(heading_text: str | None) -> str | None:
    """Извлечь номер раздела из текста заголовка.

    Примеры:
        '3.2.1. Молниеприемники' → '3.2.1'
        '1.1. Общие положения' → '1.1'
        'Введение' → None

    Regex: ^(\\d+(?:\\.\\d+)*)\\.\\s
    """
    if not heading_text:
        return None
    m = re.match(r"^(\d+(?:\.\d+)*)\.\s", heading_text)
    return m.group(1) if m else None


def _build_ancestors(
    headings: list[dict],
    idx: int,
) -> dict[str, str | None]:
    """Построить chapter/section/clause для данного heading по индексу.

    Ищет ближайшие предшествующие heading нужного уровня:
      - chapter: свой номер для ##, иначе ближайший предыдущий ##
      - section: свой номер для ###, иначе ближайший предыдущий ###
      - clause:  свой номер для ####/#####, иначе ближайший предыдущий ####/#####

    Returns:
        {'chapter': '3', 'section': '3.2', 'clause': '3.2.1'}.
        Поля, для которых предок не найден, равны None.
    """
    current = headings[idx]
    level = current.get("level", 0)

    chapter = section = clause = None

    if level == 2:
        chapter = current.get("number")
    else:
        for j in range(idx - 1, -1, -1):
            if headings[j].get("level") == 2:
                chapter = headings[j].get("number")
                break

    if level == 3:
        section = current.get("number")
    else:
        for j in range(idx - 1, -1, -1):
            if headings[j].get("level") == 3:
                section = headings[j].get("number")
                break

    if level >= 4:
        clause = current.get("number")
        if clause is None:
            # Ненумерованный подпункт — наследуем от предыдущего ####/#####
            for j in range(idx - 1, -1, -1):
                if (headings[j].get("level") or 0) >= 4:
                    clause = headings[j].get("number")
                    break

    return {"chapter": chapter, "section": section, "clause": clause}


def _build_heading_texts(
    headings: list[dict],
    idx: int,
) -> dict[str, str | None]:
    """Построить heading_texts (названия) для данного heading по индексу.

    Аналог _build_ancestors(), но возвращает ТЕКСТЫ заголовков, а не номера:
      - chapter: текст ближайшего ##
      - section: текст ближайшего ###
      - clause:  текст текущего ####/##### (или ближайшего предыдущего)

    Returns:
        {'chapter': '3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ', ...}.
        Поля, для которых предок не найден, равны None.
    """
    current = headings[idx]
    level = current.get("level", 0)

    chapter_text = section_text = clause_text = None

    if level == 2:
        chapter_text = current.get("heading_text")
    else:
        for j in range(idx - 1, -1, -1):
            if headings[j].get("level") == 2:
                chapter_text = headings[j].get("heading_text")
                break

    if level == 3:
        section_text = current.get("heading_text")
    else:
        for j in range(idx - 1, -1, -1):
            if headings[j].get("level") == 3:
                section_text = headings[j].get("heading_text")
                break

    if level >= 4:
        clause_text = current.get("heading_text")
        if clause_text is None:
            for j in range(idx - 1, -1, -1):
                if (headings[j].get("level") or 0) >= 4:
                    clause_text = headings[j].get("heading_text")
                    break

    return {"chapter": chapter_text, "section": section_text, "clause": clause_text}


def _build_section_path(ancestors: dict[str, str | None]) -> str | None:
    """Построить section_path вида '3 → 3.2 → 3.2.1'.

    Соединяет непустые chapter/section/clause через ' → '.
    Если все уровни пусты — None.
    """
    parts = [
        p for p in (ancestors.get("chapter"), ancestors.get("section"), ancestors.get("clause"))
        if p
    ]
    return " → ".join(parts) if parts else None


def parse_md_structure(md_text: str) -> list[dict]:
    """Разобрать Markdown на иерархию clause.

    Алгоритм:
      1. Разбить текст на строки (splitlines)
      2. Найти все строки-заголовки: ^(#{2,5})\\s+(.+)$
      3. Для каждого заголовка: level, heading_text, number, line_num
      4. next_line_num = строка следующего заголовка (или len(lines))

    Returns:
        [{'level': 2, 'number': '3', 'heading_text': '3. ...',
          'line_num': 42, 'next_line_num': 78}, ...]
    """
    lines = md_text.splitlines()
    headings: list[dict] = []
    for i, line in enumerate(lines):
        m = _MD_HEADING_RE.match(line)
        if not m:
            continue
        heading_text = m.group(2).strip()
        headings.append(
            {
                "level": len(m.group(1)),
                "number": _extract_heading_number(heading_text),
                "heading_text": heading_text,
                "line_num": i,
                "next_line_num": len(lines),
            }
        )
    for i in range(len(headings) - 1):
        headings[i]["next_line_num"] = headings[i + 1]["line_num"]
    return headings


def extract_clause_text(
    md_text: str,
    heading_line: int,
    next_heading_line: int,
) -> str:
    """Извлечь текст clause между двумя заголовками.

    Таблицы и изображения сохраняются как есть (Markdown).
    Не модифицирует текст.

    Returns: текст clause (может быть пустой строкой).
    """
    lines = md_text.splitlines()
    content = lines[heading_line + 1 : next_heading_line]
    return "\n".join(content).strip()


def _reference_label(pattern: str) -> str:
    """Канонический префикс ссылки по содержимому regexp-паттерна.

    Паттерны захватывают только номер ('3.2.1'); префикс
    ('п.', 'табл.', 'разд.', 'гл.') выводится из ключевых слов.
    """
    for hint, label in _REF_LABEL_HINTS:
        if hint in pattern:
            return label
    return ""


def extract_references(text: str, patterns: list[str] | None) -> list[str]:
    """Извлечь кросс-ссылки из текста clause.

    Алгоритм:
      1. Для каждого regexp-паттерна из patterns: re.findall(pattern, text)
      2. Каждое совпадение приводится к канонической форме
         (напр. 'см. п. 3.2.1' → 'п. 3.2.1')
      3. Дедупликация (set → sorted list)

    Returns: list[str] уникальных ссылок в порядке возрастания.
    """
    refs: set[str] = set()
    for pattern in patterns or []:
        label = _reference_label(pattern)
        try:
            matches = re.findall(pattern, text)
        except re.error:
            log.warning(f"  Некорректный regexp для references: {pattern}")
            continue
        for m in matches:
            if isinstance(m, tuple):
                m = m[0]
            refs.add(f"{label} {m}".strip())
    return sorted(refs)


def _get_page_for_heading(
    number: str | None,
    json_headings: list[dict] | None,
) -> int | None:
    """Найти номер страницы для clause по номеру раздела.

    Использует результат _extract_headings_from_json() (ADR-8):
      json_headings = [{'page': N, 'number': '3.2.1', ...}, ...]

    Первое вхождение каждого номера (оглавление → корректная страница).

    Returns: page (0-based), или None.
    """
    if not json_headings or not number:
        return None
    for h in json_headings:
        if h.get("number") == number:
            return h.get("page")
    return None


def _split_oversized_clause(text: str, max_chars: int) -> list[str]:
    """Разбить текст clause на подчанки по границам параграфов.

    Алгоритм:
      1. Если len(text) <= max_chars → [text]
      2. Разбить на атомарные блоки: таблицы (строки с '|') и
         кодовые блоки (```...```) не разрываются
      3. Объединить блоки в группы ≤ max_chars
      4. Одиночный блок (параграф/таблица/код) > max_chars публикуется
         как есть (log.warning)

    Returns: список подчанков (всегда минимум 1 элемент).
    """
    if len(text) <= max_chars:
        return [text]

    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].lstrip()
        if stripped.startswith("```"):
            # Кодовый блок до закрывающего fence
            j = i + 1
            while j < n and not lines[j].lstrip().startswith("```"):
                j += 1
            if j < n:
                j += 1  # включить закрывающий fence
            blocks.append("\n".join(lines[i:j]))
            i = j
        elif stripped.startswith("|"):
            # Таблица: смежные строки с '|' — атомарный блок
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            blocks.append("\n".join(lines[i:j]))
            i = j
        elif stripped == "":
            i += 1  # разделитель параграфов
        else:
            # Параграф: до пустой строки / таблицы / кодового блока
            j = i
            while j < n:
                l2 = lines[j].strip()
                if l2 == "" or l2.startswith("```") or l2.startswith("|"):
                    break
                j += 1
            blocks.append("\n".join(lines[i:j]))
            i = j

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in blocks:
        block_len = len(block)
        if block_len > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            log.warning(
                f"  Clause-блок размером {block_len} > max_chars={max_chars} "
                "(таблица/код/абзац) — публикуется как есть"
            )
            chunks.append(block)
            continue
        if current and current_len + 2 + block_len > max_chars:
            chunks.append("\n\n".join(current))
            current, current_len = [], 0
        if current:
            current_len += 2  # разделитель "\n\n"
        current.append(block)
        current_len += block_len
    if current:
        chunks.append("\n\n".join(current))

    return chunks if chunks else [text]


def _split_oversized_clause_tokens(
    text: str,
    max_tokens: int,
    tokenize: Callable[[str], int],
    overhead: int = 200,
) -> list[str]:
    """Разбить текст clause на подчанки ≤ max_tokens - overhead токенов.

    Аналог _split_oversized_clause(), но лимит считается в токенах
    через переданный tokenize() (Qwen3 tokenizer или degraded эвристика).

    Алгоритм (ADR-010 §3.3):
      1. Если tokenize(text) <= effective_limit → [text]
      2. Разбить на атомарные блоки (таблицы, код, параграфы)
      3. Объединить блоки в группы ≤ effective_limit токенов
      4. Одиночный блок > effective_limit публикуется как есть (log.warning)

    Returns: список подчанков (всегда минимум 1 элемент).
    """
    effective_limit = max_tokens - overhead
    if effective_limit <= 0:
        effective_limit = max_tokens
    if tokenize(text) <= effective_limit:
        return [text]

    blocks = _tokenize_blocks(text, tokenize)

    chunks: list[str] = []
    current_blocks: list[str] = []
    current_tokens = 0

    for block, block_tokens in blocks:
        if block_tokens > effective_limit:
            # Гигантский блок (таблица/код) — публикуем как есть
            if current_blocks:
                chunks.append("\n\n".join(current_blocks))
                current_blocks, current_tokens = [], 0
            log.warning(
                f"  Блок {block_tokens} токенов > лимита {effective_limit} — как есть"
            )
            chunks.append(block)
            continue

        sep_tokens = tokenize("\n\n") if current_blocks else 0
        if current_tokens + sep_tokens + block_tokens > effective_limit:
            chunks.append("\n\n".join(current_blocks))
            current_blocks, current_tokens = [], 0

        current_blocks.append(block)
        current_tokens += block_tokens + (sep_tokens if len(current_blocks) > 1 else 0)

    if current_blocks:
        chunks.append("\n\n".join(current_blocks))

    return chunks if chunks else [text]


def _tokenize_blocks(
    text: str,
    tokenize: Callable[[str], int],
) -> list[tuple[str, int]]:
    """Разбить текст на атомарные блоки и посчитать токены каждого.

    Таблицы (строки с '|') и кодовые блоки (```...```) не разрываются.
    Разделители '\\n\\n' между блоками не включаются в блоки (их токены
    добавляются при объединении в _split_oversized_clause_tokens).

    Returns: [(block_text, token_count), ...]
    """
    lines = text.splitlines()
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].lstrip()
        if stripped.startswith("```"):
            j = i + 1
            while j < n and not lines[j].lstrip().startswith("```"):
                j += 1
            if j < n:
                j += 1  # включить закрывающий fence
            blocks.append("\n".join(lines[i:j]))
            i = j
        elif stripped.startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            blocks.append("\n".join(lines[i:j]))
            i = j
        elif stripped == "":
            i += 1  # разделитель параграфов
        else:
            j = i
            while j < n:
                l2 = lines[j].strip()
                if l2 == "" or l2.startswith("```") or l2.startswith("|"):
                    break
                j += 1
            blocks.append("\n".join(lines[i:j]))
            i = j

    return [(block, tokenize(block)) for block in blocks if block]


def _find_doc_key(input_path: str | Path, rag_config: dict | None) -> str | None:
    """Сопоставить входной файл с doc_key из rag_config.documents.

    Сравнивает source_file документа с именем входного файла
    без учёта регистра и разделителей (-/_), чтобы пережить
    различия 'СО153-34_21_122' vs 'СО153-34-21-122'.

    Returns:
        slug (doc_key) или None, если документ не найден.
    """
    documents = (rag_config or {}).get("documents", {})
    if not documents:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^0-9a-zа-яё]", "", s.lower())

    input_name = norm(Path(input_path).name)
    input_stem = norm(Path(input_path).stem)
    for slug, meta in documents.items():
        source_file = (meta or {}).get("source_file", "")
        if not source_file:
            continue
        if input_name == norm(source_file) or input_stem == norm(Path(source_file).stem):
            return slug
    return None


def build_rag_jsonl_v1(
    md_text: str,
    json_headings: list[dict] | None,
    rag_config: dict,
    doc_key: str,
) -> str:
    """Построить JSONL-строку для RAG-индексации (v1, ADR-9, deprecated).

    Сохранён для обратной совместимости. Новый контракт — build_rag_jsonl_v2()
    (ADR-010): status, chunk_id, section_path, heading_texts, chunk_tokens,
    chunking_method, без source.page.

    Алгоритм:
      1. doc_meta = rag_config['documents'][doc_key] (ValueError если нет)
      2. defaults = rag_config['defaults']
      3. patterns = rag_config['references']['patterns'] (если extract_references)
      4. ignore_sections = doc_meta.get('ignore_sections', [])
      5. clauses = parse_md_structure(md_text)
      6. Для каждого clause:
         a. ignore_sections → пропустить секцию и её подразделы
         b. text = extract_clause_text(); пустой → пропустить
         c. chapter/section/clause = _build_ancestors()
         d. source.page = _get_page_for_heading()
         e. references = extract_references()
         f. oversized → _split_oversized_clause() с суффиксом «(ч. N)»
      7. Вернуть JSONL ("\\n".join) + "\\n"

    Returns:
        JSONL-строка, каждая строка — валидный JSON-объект.
    """
    documents = rag_config.get("documents", {})
    doc_meta = documents.get(doc_key)
    if doc_meta is None:
        raise ValueError(f"doc_key '{doc_key}' не найден в rag_config")

    defaults = rag_config.get("defaults", {})
    max_chars = int(defaults.get("max_chunk_chars", _RAG_DEFAULT_MAX_CHARS))
    extract_refs = defaults.get("extract_references", True)
    patterns: list[str] = []
    if extract_refs:
        patterns = rag_config.get("references", {}).get("patterns", []) or []

    ignore_sections = doc_meta.get("ignore_sections", []) or []
    clauses = parse_md_structure(md_text)

    # Отфильтровать игнорируемые секции (и их подразделы): они не попадают
    # ни в выход, ни в цепочку предков для _build_ancestors()
    active_clauses: list[dict] = []
    skip_until_level: int | None = None
    for clause in clauses:
        level = clause["level"]
        heading_text = clause["heading_text"]
        # Продолжаем пропуск игнорируемой секции (её подразделы)
        if skip_until_level is not None:
            if level > skip_until_level:
                continue
            skip_until_level = None
        # Начало игнорируемой секции
        if any(s and s.lower() in heading_text.lower() for s in ignore_sections):
            skip_until_level = level
            continue
        active_clauses.append(clause)

    source_file = doc_meta.get("source_file")
    json_lines: list[str] = []

    for i, clause in enumerate(active_clauses):
        text = extract_clause_text(md_text, clause["line_num"], clause["next_line_num"])
        if not text:
            continue

        ancestors = _build_ancestors(active_clauses, i)
        page = _get_page_for_heading(clause.get("number"), json_headings)
        refs = extract_references(text, patterns) if patterns else []

        base = {
            "document_id": doc_meta.get("document_id"),
            "document_id_alt": doc_meta.get("document_id_alt"),
            "title": doc_meta.get("title"),
            "edition": doc_meta.get("edition"),
            "date_enacted": doc_meta.get("date_enacted"),
            "date_amended": doc_meta.get("date_amended"),
            "amended_by": doc_meta.get("amended_by"),
            "chapter": ancestors["chapter"],
            "section": ancestors["section"],
            "clause": ancestors["clause"],
            "text": text,
            "source": {"file": source_file, "page": page},
            "references": refs,
        }

        parts = _split_oversized_clause(text, max_chars)
        if len(parts) <= 1:
            json_lines.append(json.dumps(base, ensure_ascii=False))
        else:
            for n, part in enumerate(parts, 1):
                rec = dict(base)
                rec["text"] = part
                if ancestors["clause"]:
                    rec["clause"] = f"{ancestors['clause']} (ч. {n})"
                json_lines.append(json.dumps(rec, ensure_ascii=False))

    return "\n".join(json_lines) + ("\n" if json_lines else "")


# v1 сохраняется под псевдонимом для обратной совместимости (ADR-010 §7.5)
build_rag_jsonl = build_rag_jsonl_v1


def build_rag_jsonl_v2(
    md_text: str,
    json_headings: list[dict] | None,
    rag_config: dict,
    doc_key: str,
    tokenize: Callable[[str], int],
    chunking_method: str = "qwen3",
) -> str:
    """Построить JSONL-строку для RAG-индексации (v2, ADR-010).

    Отличия от v1 (ADR-9):
      - Лимит чанка — в токенах (max_chunk_tokens, по умолчанию 7000)
      - source.page удалён из публичной схемы; остаётся _source_page
      - Поля status / status_reason / replaced_by_document_id
      - Стабильный chunk_id без страниц: {doc_slug}/{clause_number}[/part_{N}]
      - section_path, heading_texts, chunk_tokens, chunking_method
      - assets: пустой список (заполняется _link_assets_to_chunks)

    Алгоритм:
      1. doc_meta = rag_config['documents'][doc_key] (ValueError если нет)
      2. defaults = rag_config['defaults']
      3. status: doc_meta.status или defaults.default_status (warning если нет)
      4. ignore_sections = doc_meta.get('ignore_sections', [])
      5. clauses = parse_md_structure(md_text)
      6. Для каждого clause:
         a. ignore_sections → пропустить секцию и её подразделы
         b. text = extract_clause_text(); пустой → пропустить
         c. chapter/section/clause = _build_ancestors()
         d. heading_texts = _build_heading_texts()
         e. section_path = _build_section_path()
         f. chunk_id = {doc_slug}/{clause_number} (или /_h{idx} без номера)
         g. _source_page = _get_page_for_heading()
         h. references = extract_references()
         i. chunk_tokens = tokenize(text)
         j. oversized → _split_oversized_clause_tokens() с /part_{N} и «(ч. N)»
      7. Вернуть JSONL ("\\n".join) + "\\n"

    Returns:
        JSONL-строка, каждая строка — валидный JSON-объект.
    """
    documents = rag_config.get("documents", {})
    doc_meta = documents.get(doc_key)
    if doc_meta is None:
        raise ValueError(f"doc_key '{doc_key}' не найден в rag_config")

    defaults = rag_config.get("defaults", {})
    max_tokens = int(defaults.get("max_chunk_tokens", _RAG_DEFAULT_MAX_TOKENS))
    extract_refs = defaults.get("extract_references", True)
    patterns: list[str] = []
    if extract_refs:
        patterns = rag_config.get("references", {}).get("patterns", []) or []

    # Статус документа (ADR-010b): явный или default_status; warning если нет
    status = doc_meta.get("status")
    if status is None:
        status = defaults.get("default_status", "active")
        log.warning(
            f"  {doc_key}: status не указан — использую default_status='{status}'"
        )
    status_reason = doc_meta.get("status_reason")
    replaced_by_document_id = doc_meta.get("replaced_by_document_id")
    replaced_by_doc_key = doc_meta.get("replaced_by_doc_key")

    ignore_sections = doc_meta.get("ignore_sections", []) or []
    clauses = parse_md_structure(md_text)

    # Отфильтровать игнорируемые секции (и их подразделы): они не попадают
    # ни в выход, ни в цепочку предков для _build_ancestors()
    active_clauses: list[dict] = []
    skip_until_level: int | None = None
    for clause in clauses:
        level = clause["level"]
        heading_text = clause["heading_text"]
        # Продолжаем пропуск игнорируемой секции (её подразделы)
        if skip_until_level is not None:
            if level > skip_until_level:
                continue
            skip_until_level = None
        # Начало игнорируемой секции
        if any(s and s.lower() in heading_text.lower() for s in ignore_sections):
            skip_until_level = level
            continue
        active_clauses.append(clause)

    source_file = doc_meta.get("source_file")
    json_lines: list[str] = []

    for i, clause in enumerate(active_clauses):
        text = extract_clause_text(md_text, clause["line_num"], clause["next_line_num"])
        if not text:
            continue

        ancestors = _build_ancestors(active_clauses, i)
        heading_texts = _build_heading_texts(active_clauses, i)
        section_path = _build_section_path(ancestors)
        page = _get_page_for_heading(clause.get("number"), json_headings)
        refs = extract_references(text, patterns) if patterns else []

        base_chunk_id = _make_chunk_id(doc_key, active_clauses, i)
        chunk_tokens = tokenize(text)

        base = {
            "document_id": doc_meta.get("document_id"),
            "document_id_alt": doc_meta.get("document_id_alt"),
            "title": doc_meta.get("title"),
            "edition": doc_meta.get("edition"),
            "date_enacted": doc_meta.get("date_enacted"),
            "date_amended": doc_meta.get("date_amended"),
            "amended_by": doc_meta.get("amended_by"),
            "status": status,
            "status_reason": status_reason,
            "replaced_by_document_id": replaced_by_document_id,
            "replaced_by_doc_key": replaced_by_doc_key,
            "chunk_id": base_chunk_id,
            "chapter": ancestors["chapter"],
            "section": ancestors["section"],
            "clause": ancestors["clause"],
            "section_path": section_path,
            "heading_texts": heading_texts,
            "text": text,
            "source": {"file": source_file},
            "_source_page": page,
            "assets": [],
            "references": refs,
            "chunk_tokens": chunk_tokens,
            "chunking_method": chunking_method,
        }

        if chunk_tokens <= max_tokens:
            json_lines.append(json.dumps(base, ensure_ascii=False))
        else:
            parts = _split_oversized_clause_tokens(text, max_tokens, tokenize)
            for n, part in enumerate(parts, 1):
                rec = dict(base)
                rec["text"] = part
                rec["chunk_id"] = f"{base_chunk_id}/part_{n}"
                rec["chunk_tokens"] = tokenize(part)
                if ancestors["clause"]:
                    rec["clause"] = f"{ancestors['clause']} (ч. {n})"
                json_lines.append(json.dumps(rec, ensure_ascii=False))

    return "\n".join(json_lines) + ("\n" if json_lines else "")


def _make_chunk_id(
    doc_key: str,
    active_clauses: list[dict],
    idx: int,
) -> str:
    """Построить стабильный chunk_id: {doc_slug}/{clause_number}[/part_{N}].

    doc_slug — ключ документа в rag_config (каталоговый slug).

    Fallback для ненумерованных заголовков (ADR-010f):
      clause_number = None → "{doc_slug}/_h{idx}", где idx — порядковый
      номер заголовка в пределах родительского раздела.
    """
    clause = active_clauses[idx]
    number = clause.get("number")
    if number:
        return f"{doc_key}/{number}"
    # Ненумерованный заголовок: номер в пределах родительского раздела
    parent_idx = -1
    level = clause.get("level", 0)
    for j in range(idx - 1, -1, -1):
        if (active_clauses[j].get("level") or 0) < level:
            parent_idx = j
            break
    ordinal = idx - parent_idx
    return f"{doc_key}/_h{ordinal}"


def _write_rag_jsonl(
    md_text: str,
    json_headings: list[dict] | None,
    rag_config: dict | None,
    input_path: str | Path,
    out_dir: str | Path,
    file_stem: str,
) -> None:
    """Сгенерировать Markdown/<file>/rag_chunks.jsonl + rag_assets.json (--rag).

    doc_key определяется через _find_doc_key(); при отсутствии документа
    в конфиге — log.warning и пропуск (архитектура §7 error handling).

    Атомарно перезаписывает rag_chunks.jsonl и rag_assets.json
    (производные файлы); .md и image/ не модифицируются.
    """
    if not rag_config:
        return
    doc_key = _find_doc_key(input_path, rag_config)
    if doc_key is None:
        log.warning(f"  Документ не найден в rag_config: {file_stem}")
        return
    try:
        run_rag_pipeline(
            md_text,
            json_headings,
            rag_config,
            doc_key,
            Path(out_dir) / "image",
            out_dir,
        )
    except Exception as e:
        log.error(f"  Ошибка RAG-генерации: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 13. Tokenizer Chunking (ADR-010)
# ═══════════════════════════════════════════════════════════════════════════


def _init_tokenizer(config: dict) -> tuple[Callable[[str], int], str]:
    """Загрузить Qwen3-Embedding-8B tokenizer через transformers.AutoTokenizer.

    config['defaults'] должен содержать:
      - tokenizer: str            (HF model id, "Qwen/Qwen3-Embedding-8B")
      - tokenizer_revision: str   (опционально, "main")
      - allow_degraded_fallback: bool (default False)
      - tokenizer_fallback_ratio: float (default 3.5, только при allow_degraded_fallback)

    Returns:
        (tokenize_fn, chunking_method)
        tokenize_fn: text → token_count
        chunking_method: "qwen3" | "degraded_chars_per_token"

    Raises:
        RuntimeError — если токенизатор недоступен и fallback запрещён.
    """
    model_id = config.get("defaults", {}).get("tokenizer", "Qwen/Qwen3-Embedding-8B")
    revision = config.get("defaults", {}).get("tokenizer_revision", "main")

    # Попытка 1: transformers + HuggingFace
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            local_files_only=False,   # первый раз: скачать
            trust_remote_code=False,
        )
        log.info(f"Токенизатор: transformers/{model_id} (загружен)")
        return (lambda text: len(tokenizer.encode(text)), "qwen3")
    except ImportError:
        log.error(
            "transformers не установлен — RAG-индексация невозможна. "
            "Установите: pip install transformers>=4.51.0"
        )
    except Exception as e:
        log.error(f"Не удалось загрузить токенизатор {model_id}: {e}")

    # Попытка 2: явный деградированный fallback (только если разрешён)
    allow_fallback = config.get("defaults", {}).get("allow_degraded_fallback", False)
    if allow_fallback:
        ratio = float(config.get("defaults", {}).get("tokenizer_fallback_ratio", 3.5))
        log.warning(
            f"Работаю в деградированном режиме: chars/token={ratio}. "
            f"chunking_method='degraded_chars_per_token'"
        )
        return (lambda text: max(1, int(len(text) / ratio)), "degraded_chars_per_token")

    # Без fallback: жёсткая ошибка
    raise RuntimeError(
        "RAG-индексация невозможна: токенизатор Qwen3 недоступен. "
        "Установите transformers>=4.51.0 и убедитесь в доступе к HuggingFace Hub, "
        "либо включите allow_degraded_fallback: true в rag_config.yaml (с потерей точности)."
    )


# ═══════════════════════════════════════════════════════════════════════════
# 14. Asset Registry (ADR-010d)
# ═══════════════════════════════════════════════════════════════════════════

_TABLE_CAPTION_RE = re.compile(
    r"^(?:\*\s*)?(?:Т\s*а\s*б\s*л\s*и\s*ц\s*а|Таблиц[аы])\s*(\d+(?:\.\d+)*)",
    re.IGNORECASE,
)


def _extract_tables_from_md(md_text: str) -> list[dict]:
    """Найти все Markdown-таблицы в тексте.

    Алгоритм (ADR-010 §4.1):
      1. Разбить md_text на строки
      2. Детектить таблицы: строка содержит '|', следующая строка — '|---|'
      3. Для каждой таблицы:
         - caption: подпись до или после таблицы («Таблица N» или «Таблица N.M — ...»)
         - md_lines: [start_line, end_line)
         - image_path: "image/table_{N}.png" (N — порядковый номер)

    Returns:
        [{asset_id, asset_type, caption, md_lines, image_path, row_count,
          chunk_ids, _md_block}, ...]
        asset_id заполняется позже (doc_slug); _md_block — приватное поле
        для связывания с чанками (удаляется при записи).
    """
    lines = md_text.splitlines()
    tables: list[dict] = []
    i = 0
    n = 0  # счётчик таблиц
    while i < len(lines):
        stripped = lines[i].strip()
        # Детектим начало таблицы: строка с '|' И следующая с '|---'
        if stripped.startswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r"^\|[\s\-:|]+\|$", next_line):
                n += 1
                start = i
                # Ищем конец таблицы (строки с '|')
                while i < len(lines) and lines[i].strip().startswith("|"):
                    i += 1
                end = i
                caption = _find_table_caption_md(lines, start, end, n)
                md_block = "\n".join(lines[start:end])
                tables.append({
                    "asset_id": None,  # doc_slug добавляется позже
                    "asset_type": "table",
                    "caption": caption,
                    "md_lines": [start, end],
                    "image_path": f"image/table_{n}.png",
                    "row_count": max(0, (end - start) - 2),  # минус заголовок и разделитель
                    "chunk_ids": [],
                    "_md_block": md_block,
                })
                continue
        i += 1
    return tables


def _find_table_caption_md(
    lines: list[str],
    start: int,
    end: int,
    n: int,
) -> str:
    """Найти подпись таблицы в строках Markdown.

    Ищет «Таблица N» / «Таблица N.M» до таблицы (пропуская пустые строки
    и курсивные маркеры '*') и после неё. Возвращает нормализованную
    подпись или пустую строку.
    """
    def _match(line: str) -> str | None:
        m = _TABLE_CAPTION_RE.match(line.strip().strip("*"))
        if m:
            return re.sub(r"\s+", " ", line.strip().strip("*"))
        return None

    # Перед таблицей: до 3 непустых строк выше
    j = start - 1
    scanned = 0
    while j >= 0 and scanned < 3:
        if lines[j].strip():
            scanned += 1
            cap = _match(lines[j])
            if cap:
                return cap
        j -= 1

    # После таблицы: до 3 непустых строк ниже
    j = end
    scanned = 0
    while j < len(lines) and scanned < 3:
        if lines[j].strip():
            scanned += 1
            cap = _match(lines[j])
            if cap:
                return cap
        j += 1

    return ""


def _extract_images_from_md(md_text: str) -> list[dict]:
    """Найти все изображения в Markdown.

    Паттерн: ![caption](image/fig_N.ext)
    """
    images: list[dict] = []
    for m in re.finditer(r"!\[(.*?)\]\((image/.*?)\)", md_text):
        line_num = md_text[: m.start()].count("\n")
        images.append({
            "asset_id": None,  # заполняется позже
            "asset_type": "image",
            "caption": m.group(1),
            "image_path": m.group(2),
            "md_line": line_num,
            "chunk_ids": [],
        })
    return images


def _build_asset_registry(
    md_text: str,
    doc_slug: str,
    img_dir: str | Path | None,
    document_id: str | None = None,
) -> dict:
    """Построить реестр активов rag_assets.json.

    Регистрирует таблицы и изображения из итогового Markdown.
    Пути image_path — относительные к директории .md (image/).

    Если img_dir передан и файл изображения не существует — актив
    пропускается с log.warning (архитектура §9 error handling).
    """
    tables = _extract_tables_from_md(md_text)
    images = _extract_images_from_md(md_text)

    for i, table in enumerate(tables, 1):
        table["asset_id"] = f"{doc_slug}/table/{i}"
    for i, img in enumerate(images, 1):
        img["asset_id"] = f"{doc_slug}/fig/{i}"

    # Если image/ не найден — реестр активов пуст (архитектура §9):
    # без директории изображений активы не имеют смысла.
    img_dir_path = Path(img_dir) if img_dir else None
    if img_dir_path is None:
        log.warning(f"  image/ не найден: реестр активов будет пустым")
        return {
            "document_id": document_id,
            "doc_slug": doc_slug,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "image_dir": "image",
            "assets": {"tables": [], "images": []},
        }

    kept_tables = []
    for table in tables:
        if (img_dir_path / Path(table["image_path"]).name).exists():
            kept_tables.append(table)
        else:
            log.warning(
                f"  Пропускаю asset {table['asset_id']}: "
                f"{table['image_path']} не существует"
            )
    tables = kept_tables

    kept_images = []
    for img in images:
        if (img_dir_path / Path(img["image_path"]).name).exists():
            kept_images.append(img)
        else:
            log.warning(
                f"  Пропускаю asset {img['asset_id']}: "
                f"{img['image_path']} не существует"
            )
    images = kept_images

    return {
        "document_id": document_id,
        "doc_slug": doc_slug,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image_dir": "image",
        "assets": {
            "tables": tables,
            "images": images,
        },
    }


def _link_assets_to_chunks(
    assets: dict,
    chunks: list[dict],
) -> None:
    """Для каждого актива найти чанки, в которых он упоминается.

    Мутирует assets in-place (заполняет chunk_ids) и чанки (assets).

    Таблица связывается с чанком, если текст чанка содержит её Markdown-блок
    (точное совпадение) или подпись. Изображение — если текст чанка содержит
    его image_path или ссылку ![..](image_path).
    """
    for table in assets["assets"]["tables"]:
        md_block = table.get("_md_block", "")
        caption = table.get("caption") or ""
        for chunk in chunks:
            text = chunk.get("text", "")
            if (md_block and md_block in text) or (caption and caption in text):
                if chunk["chunk_id"] not in table["chunk_ids"]:
                    table["chunk_ids"].append(chunk["chunk_id"])
                if table["asset_id"] not in chunk.get("assets", []):
                    chunk.setdefault("assets", []).append(table["asset_id"])

    for img in assets["assets"]["images"]:
        img_ref = f"![]({img['image_path']})"
        for chunk in chunks:
            text = chunk.get("text", "")
            if img["image_path"] in text or img_ref in text:
                if chunk["chunk_id"] not in img["chunk_ids"]:
                    img["chunk_ids"].append(chunk["chunk_id"])
                if img["asset_id"] not in chunk.get("assets", []):
                    chunk.setdefault("assets", []).append(img["asset_id"])


def write_rag_assets(assets: dict, output_path: str | Path) -> None:
    """Записать rag_assets.json (атомарно), убрав приватные поля."""
    out = json.loads(json.dumps(assets, ensure_ascii=False))  # глубокое копирование
    for table in out["assets"]["tables"]:
        table.pop("_md_block", None)
    content = json.dumps(out, ensure_ascii=False, indent=2)
    safe_write(output_path, content)


# ═══════════════════════════════════════════════════════════════════════════
# 15. RAG Pipeline Orchestrator (ADR-010)
# ═══════════════════════════════════════════════════════════════════════════


def _classify_input(input_path: Path) -> str:
    """Классифицировать входной файл.

    Returns:
        "pdf": PDF, требует OCR (таблицы ВСЕГДА вырезаются в image/)
        "docx": DOCX/DOC, требует конвертации + OCR
        "md_standalone": .md вне Markdown/ → AI-постобработка
        "md_rag": .md внутри Markdown/ → RAG-индексация (только --rag)
        "unknown": неподдерживаемый тип
    """
    suffix = input_path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".docx", ".doc"):
        return "docx"
    if suffix == ".md":
        if "Markdown" in input_path.parts:
            return "md_rag"
        return "md_standalone"
    return "unknown"


def _run_rag_only(md_path: Path, rag_config: dict | None) -> bool:
    """RAG-индексация существующего Markdown (без OCR/постобработки).

    Вход: Markdown/<file>/<file>.md
    Действия:
      1. Читает .md файл (read-only)
      2. Проверяет наличие image/ рядом
      3. Инициализирует токенизатор (Qwen3 или degraded fallback)
      4. build_rag_jsonl_v2() → ПЕРЕЗАПИСЫВАЕТ rag_chunks.jsonl
      5. _build_asset_registry() → ПЕРЕЗАПИСЫВАЕТ rag_assets.json
    НЕ модифицирует .md, НЕ запускает OCR, НЕ извлекает изображения.
    """
    md_text = md_path.read_text(encoding="utf-8")
    doc_dir = md_path.parent
    img_dir = doc_dir / "image"

    if not img_dir.is_dir():
        log.warning(f"  image/ не найден: {img_dir} — реестр активов будет пустым")

    doc_key = _find_doc_key(md_path.name, rag_config)
    if doc_key is None:
        log.warning(f"Документ не найден в rag_config: {md_path.name}")
        return False

    if rag_config is None:
        log.warning(f"rag_config не загружен: {md_path.name}")
        return False

    try:
        run_rag_pipeline(
            md_text,
            None,
            rag_config,
            doc_key,
            img_dir if img_dir.is_dir() else None,
            doc_dir,
        )
        return True
    except Exception as e:
        log.error(f"  Ошибка RAG-индексации: {e}")
        return False


def run_rag_pipeline(
    md_text: str,
    json_headings: list[dict] | None,
    rag_config: dict,
    doc_key: str,
    img_dir: str | Path | None,
    out_dir: str | Path,
) -> bool:
    """Оркестратор RAG: токенизатор → JSONL v2 → assets → атомарная запись.

    Атомарно ПЕРЕЗАПИСЫВАЕТ:
      - {out_dir}/rag_chunks.jsonl
      - {out_dir}/rag_assets.json
    .md и image/ не модифицируются.

    Returns:
        True при успехе.
    """
    tokenize, chunking_method = _init_tokenizer(rag_config)

    jsonl = build_rag_jsonl_v2(
        md_text, json_headings, rag_config, doc_key, tokenize, chunking_method,
    )

    # Чанки (для связывания активов)
    chunks: list[dict] = []
    for line in jsonl.splitlines():
        if line.strip():
            chunks.append(json.loads(line))

    # Реестр активов
    document_id = rag_config.get("documents", {}).get(doc_key, {}).get("document_id")
    assets = _build_asset_registry(md_text, doc_key, img_dir, document_id=document_id)
    _link_assets_to_chunks(assets, chunks)

    # Пересобрать JSONL с заполненными assets в чанках
    jsonl_lines = [json.dumps(c, ensure_ascii=False) for c in chunks]
    jsonl_out = "\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")

    rag_path = Path(out_dir) / "rag_chunks.jsonl"
    safe_write(rag_path, jsonl_out)
    log.info(f"  RAG JSONL перезаписан: {rag_path} ({len(chunks)} строк)")

    assets_path = Path(out_dir) / "rag_assets.json"
    write_rag_assets(assets, assets_path)
    log.info(f"  RAG Assets перезаписан: {assets_path}")

    return True


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
  %(prog)s -i file.pdf --rag               # дополнительно RAG JSONL + assets
  %(prog)s -i file.pdf --ai --rag          # AI-постобработка + RAG
  %(prog)s -i Markdown/file/file.md --rag  # RAG-индексация проверенного MD
        """,
    )

    parser.add_argument(
        "-i", "--input",
        required=True,
        help="Входной файл или папка (PDF/DOCX/DOC/MD). "
        ".md вне Markdown/ требует --ai; .md внутри Markdown/ требует --rag",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Полный AI-цикл: vision-распознавание таблиц + gap-filling + "
        "AI-постобработка. Для .md вне Markdown/ — единственный режим (без OCR). "
        "Таблицы вырезаются в image/ ВСЕГДА (без --ai); --ai добавляет vision-коррекцию",
    )
    parser.add_argument(
        "--config",
        default="./config_ai.yaml",
        help="Путь к config_ai.yaml (по умолч. ./config_ai.yaml)",
    )
    parser.add_argument(
        "--rag",
        action="store_true",
        help="Сгенерировать/перезаписать RAG-файлы "
        "(Markdown/<файл>/rag_chunks.jsonl + rag_assets.json). "
        "Для .md внутри Markdown/ — единственный режим: только индексация "
        "без OCR/AI, .md и image/ не модифицируются",
    )
    parser.add_argument(
        "--rag-config",
        default="./rag_config.yaml",
        help="Путь к rag_config.yaml (по умолч. ./rag_config.yaml)",
    )

    return parser.parse_args(argv)


def process_file(
    input_path: str,
    use_ai: bool,
    config: dict,
    api_key: str,
    folder_id: str,
    output_base: str,
    tmp_base: str,
    use_rag: bool = False,
    rag_config: dict | None = None,
) -> bool:
    """Обработать один файл: Yandex OCR → парсинг → изображения → постобработка → сохранение.

    Режимы (ADR-010e, трёхэтапный CLI):
      - pdf/docx: OCR + постобработка; таблицы ВСЕГДА вырезаются в image/
        (PyMuPDF, без --ai); --ai дополнительно запускает vision/AI-коррекцию
      - .md внутри Markdown/ + --rag: только RAG-индексация проверенного MD
        (без OCR/AI/извлечения), перезаписывает rag_chunks.jsonl + rag_assets.json
      - .md вне Markdown/: AI-постобработка (--ai) и/или RAG (--rag)

    Returns:
        True при успехе, False при ошибке.
    """
    input_path = str(Path(input_path).resolve())
    input_path_obj = Path(input_path)
    file_stem = input_path_obj.stem
    log.info(f"{'=' * 60}")
    log.info(f"Обработка: {input_path}")

    file_type = _classify_input(input_path_obj)

    # ── Режим .md внутри Markdown/ + --rag: ТОЛЬКО индексация ──
    if file_type == "md_rag":
        if not use_rag:
            log.error(".md в Markdown/: требуется флаг --rag")
            return False
        log.info(f"RAG-индексация проверенного Markdown: {input_path}")
        ok = _run_rag_only(input_path_obj, rag_config)
        log.info(f"{'=' * 60}")
        return ok

    if file_type == "unknown":
        log.error(f"Неподдерживаемый тип файла: {input_path}")
        return False

    # Выходные папки
    out_dir = Path(output_base) / file_stem
    img_dir = out_dir / "image"
    file_tmp_dir = Path(tmp_base) / file_stem
    ensure_dir(out_dir)
    ensure_dir(img_dir)
    ensure_dir(file_tmp_dir)

    # Режим: .md вне Markdown/ → только постобработка, без OCR и скриптов
    ext = input_path_obj.suffix.lower()
    if ext == ".md":
        if use_ai or use_rag:
            log.info(f"Обработка MD: {file_stem}.md (ai={use_ai}, rag={use_rag})")
            md_text = input_path_obj.read_text(encoding="utf-8")
            if use_ai:
                ai_cfg = config.get("ai_postprocess", config.get("postprocess", config))
                result = ai_postprocess(md_text, ai_cfg, file_stem)
                if not result:
                    log.error("AI-постобработка не дала результата")
                    return False
                out_path = input_path_obj.parent / f"{file_stem}_ai.md"
                safe_write(out_path, result)
                log.info(f"Результат: {out_path} ({len(result)} символов)")
                md_text = result
            # --rag: JSONL + assets из готового MD (_source_page = null, нет Yandex JSON)
            if use_rag:
                _write_rag_jsonl(md_text, None, rag_config, input_path, out_dir, file_stem)
            log.info(f"{'=' * 60}")
            return True
        else:
            log.error(".md файл требует флаг --ai (или --rag)")
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

        # Этап 3b: Заголовки разделов из Yandex JSON → ##/###/#### (ADR-8)
        headings = _extract_headings_from_json(pages)
        if headings:
            md_text = _apply_headings_to_md(md_text, headings)
            log.info(f"  Заголовков заменено: {len(headings)}")
        else:
            log.info("  Заголовки не найдены — MD без изменений")

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

        # Этап 4b: Вырезание таблиц в image/table_N.png — ВСЕГДА (ADR-010e)
        # Локальная операция PyMuPDF, не требует --ai.
        table_images: list[dict] = []
        if pages:
            log.info("Вырезание таблиц из PDF:")
            table_images = extract_table_images(pdf_path, pages, img_dir)
            log.info(f"  Вырезано таблиц: {len(table_images)}")

            # Vision/AI-распознавание таблиц — только с --ai
            if use_ai and table_images:
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
        md_text = run_script_postprocess(
            md_text, img_dir,
            page_boundaries=page_boundaries,
            table_images=table_images if use_ai else None,
        )

        # Этап 6+7 (объединённый): AI-коррекция таблиц + постобработка (если --ai)
        if use_ai:
            # Системный промпт из ai_postprocess (уже объединён с правилами таблиц)
            combined_prompt = config.get("ai_postprocess", {}).get("prompt", "")

            # Конфиг для вызова: настройки провайдера из ai_postprocess,
            # промпт — объединённый
            ai_cfg = dict(config.get("ai_postprocess", config.get("postprocess", config)))
            ai_cfg["prompt"] = combined_prompt

            # 1. Загрузить все vision-таблицы как dict[id] = текст
            vision_tables: dict[str, str] = {}
            table_files = sorted(Path(file_tmp_dir).glob("table_*.md"))
            for f in table_files:
                content = f.read_text(encoding="utf-8")
                # Извлечь ID из первой строки: <!-- t_pN_M -->
                m = re.match(r"<!--\s*(t_p\d+_\d+)\s*-->", content)
                if m:
                    vision_tables[m.group(1)] = content
            if table_files:
                log.info(
                    f"  Vision-таблиц для сверки: {len(table_files)} "
                    f"(с ID: {len(vision_tables)})"
                )

            # 2. Чанковать ТОЛЬКО md_text
            md_chunks = _chunk_text(md_text, AI_MAX_CHARS)

            # Чекпойнтинг (как в ai_postprocess)
            ckpt_dir = Path("tmp") / ".ai_checkpoints"
            safe_label = re.sub(r'[^a-zA-Z0-9_-]', '_', file_stem)
            ckpt_path = ckpt_dir / f"{safe_label}.json"
            ckpt_dir.mkdir(parents=True, exist_ok=True)

            results: list[str | None] = [None] * len(md_chunks)
            if ckpt_path.exists():
                try:
                    saved = json.loads(ckpt_path.read_text(encoding="utf-8"))
                    if isinstance(saved, list) and len(saved) == len(md_chunks):
                        results = saved
                        done = sum(1 for r in results if r is not None)
                        log.info(f"  Чекпойнт: {done}/{len(md_chunks)}")
                except Exception as e:
                    log.warning(f"  Ошибка чекпойнта: {e}")

            # 3. Для каждого чанка — свой набор эталонов
            chunks_with_tables = 0
            for i, r in enumerate(results):
                if r is not None:
                    log.info(f"  Часть {i + 1}/{len(md_chunks)} — из чекпойнта")
                    continue
                chunk = md_chunks[i]

                # Найти все ID в чанке
                ids_in_chunk = set(re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->", chunk))

                # Собрать только matching vision-таблицы
                matching: list[str] = []
                for tid in sorted(ids_in_chunk, key=_table_id_sort_key):
                    if tid in vision_tables:
                        matching.append(vision_tables[tid])

                if matching:
                    chunk_input = (
                        f"=== Markdown-файл ===\n{chunk}\n\n"
                        f"=== Эталонные таблицы ===\n"
                        + "\n\n".join(matching) + "\n"
                    )
                    chunks_with_tables += 1
                else:
                    chunk_input = chunk

                # Контекст из предыдущего чанка
                if i > 0:
                    prev_lines = md_chunks[i - 1].strip().split("\n")
                    ctx = (
                        "\n".join(prev_lines[-3:])
                        if len(prev_lines) >= 3
                        else md_chunks[i - 1].strip()
                    )
                    chunk_input = f"[Контекст]\n{ctx}\n[/Контекст]\n\n{chunk_input}"

                log.info(f"  Часть {i + 1}/{len(md_chunks)} ({len(chunk_input)} символов)")
                result = _call_ai_api(chunk_input, ai_cfg, f"{file_stem} [ч.{i + 1}]")
                results[i] = result if result else chunk
                try:
                    ckpt_path.write_text(
                        json.dumps(results, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            md_text = "\n\n".join(r for r in results if r)

            # Логировать покрытие
            log.info(f"  Чанков с эталонами: {chunks_with_tables}/{len(md_chunks)}")

            # Пост-проверка: остались ли неснятые ID-маркеры
            remaining_ids = set(re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->", md_text))
            if remaining_ids:
                log.warning(
                    f"  ⚠ Неснятые ID-маркеры ({len(remaining_ids)}): "
                    f"{', '.join(sorted(remaining_ids, key=_table_id_sort_key))}"
                    f" — сверка для этих таблиц не прошла"
                )
            else:
                log.info("  Все ID-маркеры сняты")

            try:
                ckpt_path.unlink(missing_ok=True)
            except Exception:
                pass

            log.info(f"  AI-обработка: {len(md_chunks)} чанков → {len(md_text)} символов")

        # Сохраняем итоговый .md
        md_path = out_dir / f"{file_stem}.md"
        safe_write(md_path, md_text)
        log.info(f"Итоговый Markdown: {md_path} ({len(md_text)} символов)")

        # Этап 8b: RAG JSONL (если --rag) — из финального MD + Yandex JSON
        if use_rag:
            _write_rag_jsonl(md_text, headings, rag_config, input_path, out_dir, file_stem)

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
    log.info(f"RAG: {args.rag}")
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

    # Загружаем конфиг AI (если нужен — для --ai)
    config = load_config(args.config) if args.ai else {}

    # Загружаем rag_config (если --rag); при ошибке — пропустить RAG-генерацию
    rag_config = None
    if args.rag:
        try:
            rag_config = load_rag_config(args.rag_config)
            log.info(f"RAG-конфиг загружен: {args.rag_config}")
        except Exception as e:
            log.error(f"Не удалось загрузить rag_config: {e} — RAG-генерация пропущена")

    # Обрабатываем каждый файл
    success = 0
    failed = 0
    for file_path in files:
        ok = process_file(
            str(file_path),
            args.ai,
            config,
            api_key,
            folder_id,
            output_base,
            tmp_base,
            use_rag=args.rag,
            rag_config=rag_config,
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
