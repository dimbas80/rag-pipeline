#!/usr/bin/env python3
"""
create_markdown.py — Пайплайн конвертации PDF/DOCX/MD в Markdown через Yandex Vision OCR.

Модель: math-markdown (даёт markdown + tables + pictures + blocks).

Постобработка:
  — скриптовая (всегда): HTML-таблицы → MD, LaTeX-чистка, изображения → fig_N,
    OCR-артефакты, примечания, подписи
  — AI (флаг --ai): vision-распознавание таблиц → единый AI-проход
    (коррекция таблиц + постобработка) через deepseek / provod

Режим .md + --ai: только AI-постобработка готового .md файла, без OCR.

Использование:
  python3 create_markdown.py -i file.pdf
  python3 create_markdown.py -i file.pdf --ai --config create_markdown_config.yaml
  python3 create_markdown.py -i file.md --ai                 # только AI
  python3 create_markdown.py -i file.pdf --rag               # + RAG JSONL (секция 12)
  python3 create_markdown.py -i dir/
"""

import argparse
import base64
import datetime
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class Cell:
    row: int
    col: int
    rowspan: int
    colspan: int
    text: str


@dataclass
class Block:
    y: float
    layout_type: str
    text: str
    bbox: BBox
    heading_level: Optional[int] = None
    heading_number: Optional[str] = None
    is_table_caption: bool = False
    is_continuation_caption: bool = False


@dataclass
class Table:
    page: int
    table_index: int
    bbox: BBox
    cells: list[Cell]
    caption: Optional[str] = None
    table_num: Optional[str] = None
    is_continuation: bool = False
    component_images: Optional[list[str]] = None
    image_path: Optional[str] = None
    md_lines: Optional[tuple[int, int]] = None
    # Stable identity assigned by stitch_tables; None means not stitched.
    stitch_group_id: Optional[int] = None


@dataclass
class Picture:
    page: int
    bbox: BBox
    score: float
    image_path: Optional[str] = None


@dataclass(frozen=True)
class Heading:
    page: int
    level: int
    number: str
    text: str
    full_text: str
    y: float


@dataclass
class Page:
    index: int
    width: float
    height: float
    blocks: list[Block]
    table_indices: list[int] = field(default_factory=list)
    picture_indices: list[int] = field(default_factory=list)


@dataclass
class Document:
    pages: list[Page]
    headings: list[Heading]
    tables: list[Table]
    pictures: list[Picture]
    metadata: dict = field(default_factory=dict)


import httpx
import yaml

# ── Логирование (базовое) ─────────────────────────────────────────────
log = logging.getLogger("create-md-ya")


_RECOGNITION_FAILURE_RE = re.compile(
    r"(?:"
    r"не\s+(?:смо?г(?:ла)?|могу|удалось|удаётся|получилось)\s+(?:распознать|прочитать|обработать)"
    r"|не\s+распознал(?:а)?"
    r"|не\s+является\s+таблицей"
    r"|нет\s+таблицы"
    r"|извините[^\n]*не\s+(?:могу|смог(?:ла)?)"
    r"|(?:cannot|could\s*not|can't|unable\s+to)\s+(?:recognize|recognise|read|process|identify|parse)"
    r"|i['\s]?m\s+(?:sorry|unable)"
    r"|i\s+am\s+(?:sorry|unable)"
    r"|(?:no\s+table|not\s+a\s+table)"
    r")",
    re.IGNORECASE,
)


def _is_recognition_failure(content: str | None) -> bool:
    """Вернуть True для пустого ответа или явного отказа распознать содержимое."""
    return not content or not content.strip() or _RECOGNITION_FAILURE_RE.search(content) is not None


def _model_bbox(value: dict | None) -> BBox:
    vertices = (value or {}).get("vertices", [])
    if not vertices:
        return BBox(0.0, 0.0, 0.0, 0.0)
    xs = [float(v.get("x", 0)) for v in vertices]
    ys = [float(v.get("y", 0)) for v in vertices]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def _model_block_text(block: dict) -> str:
    return " ".join(str(line.get("text", "")).strip()
                     for line in block.get("lines", []) if line.get("text", "").strip()).strip()


def _model_heading_parts(text: str) -> tuple[str, str] | None:
    match = re.match(r"^(\d+(?:\.\d+)*\.?)(?:\s+|$)(.*)$", text.strip())
    if not match:
        return None
    number = match.group(1).rstrip(".")
    title = match.group(2).strip()
    if not title or len(text) >= 100 or len(text.split()) >= 10:
        return None
    return number, title


def parse_yandex_json_to_model(pages: list[dict] | None) -> Document:
    """Build the Phase 1 structured model without changing the Markdown parser."""
    if not pages:
        return Document([], [], [], [], {})
    document_pages: list[Page] = []
    headings: list[Heading] = []
    tables: list[Table] = []
    pictures: list[Picture] = []
    for page_index, page in enumerate(pages):
        ta = page.get("result", {}).get("textAnnotation", {})
        width = float(ta.get("width", 0) or 0)
        height = float(ta.get("height", 0) or 0)
        raw_blocks = ta.get("blocks", []) or []
        raw_tables = ta.get("tables", []) or []
        raw_pictures = ta.get("pictures", []) or []
        table_models: list[Table] = []
        table_boxes: list[BBox] = []
        for table_index, raw_table in enumerate(raw_tables):
            bbox = _model_bbox(raw_table.get("boundingBox"))
            table_boxes.append(bbox)
            cells = []
            for raw_cell in raw_table.get("cells", []) or []:
                cells.append(Cell(int(raw_cell.get("rowIndex", 0)), int(raw_cell.get("columnIndex", 0)),
                                  int(raw_cell.get("rowSpan", 1)), int(raw_cell.get("columnSpan", 1)),
                                  str(raw_cell.get("text", "")).replace("\n", " ").strip()))
            caption_block = _find_table_caption_block(raw_blocks, raw_table, raw_tables)
            if caption_block is None:
                for candidate in raw_blocks:
                    candidate_text = _model_block_text(candidate)
                    if re.search(r"(?:Окончани[ея]|Продолжени[ея]|Продолж\.?)\s+(?:таблиц[аы]|табл\.?)", candidate_text, re.IGNORECASE):
                        candidate_box = _model_bbox(candidate.get("boundingBox"))
                        if candidate_box.y1 <= bbox.y0:
                            caption_block = candidate
            caption = _caption_block_to_text(caption_block) or None
            table_num = _extract_table_num_from_text(caption or "")
            table_models.append(Table(page_index, table_index, bbox, cells, caption, table_num,
                                      _is_continuation_caption(caption or "")))
        tables.extend(table_models)
        table_indices = list(range(len(tables) - len(table_models), len(tables)))
        page_blocks: list[Block] = []
        raw_y: list[tuple[float, dict, BBox, str]] = []
        for raw_block in raw_blocks:
            bbox = _model_bbox(raw_block.get("boundingBox"))
            line_boxes = [_model_bbox(line.get("boundingBox")) for line in raw_block.get("lines", [])]
            if line_boxes:
                bbox = BBox(min(b.x0 for b in line_boxes), min(b.y0 for b in line_boxes),
                            max(b.x1 for b in line_boxes), max(b.y1 for b in line_boxes))
            text = _model_block_text(raw_block)
            raw_y.append((bbox.y0, raw_block, bbox, text))
        for y, raw_block, bbox, text in raw_y:
            if any(bbox.y0 >= tb.y0 and bbox.y1 <= tb.y1 and bbox.y1 > bbox.y0 for tb in table_boxes):
                continue
            layout = str(raw_block.get("layoutType", ""))
            caption_flag = _looks_like_table_caption(text) or layout.endswith("CAPTION")
            continuation_flag = _is_continuation_caption(text)
            heading_parts = _model_heading_parts(text) if layout.endswith("SECTION_HEADER") else None
            level = number = title = None
            if heading_parts and bbox.x0 / width < 0.50:
                number, title = heading_parts
                level = number.count(".") + 1
                nearby = [other_y for other_y, _, _, _ in raw_y if other_y != y and abs(other_y - y) <= 15]
                if nearby:
                    level = number = title = None
            block_model = Block(y, layout.removeprefix("LAYOUT_TYPE_"), text, bbox, level, number,
                                caption_flag, continuation_flag)
            page_blocks.append(block_model)
            if level is not None:
                headings.append(Heading(page_index, level, number, title, text, y))
        page_picture_indices = []
        for raw_picture in raw_pictures:
            picture = Picture(page_index, _model_bbox(raw_picture.get("boundingBox")),
                              float(raw_picture.get("score", raw_picture.get("confidence", 0.0)) or 0.0))
            page_picture_indices.append(len(pictures))
            pictures.append(picture)
        document_pages.append(Page(page_index, width, height, page_blocks, table_indices, page_picture_indices))
    return Document(document_pages, headings, tables, pictures, {"page_count": len(document_pages)})


def count_columns(table: Table) -> int:
    return max((cell.col + cell.colspan for cell in table.cells), default=0)


def geometry_says_same_table(
    page: Page,
    prev: Table,
    curr: Table,
    bottom_threshold_ratio: float = 0.75,
) -> bool:
    if curr.page != prev.page + 1 or count_columns(prev) != count_columns(curr):
        return False
    overlap = min(prev.bbox.x1, curr.bbox.x1) - max(prev.bbox.x0, curr.bbox.x0)
    span = max(prev.bbox.x1 - prev.bbox.x0, curr.bbox.x1 - curr.bbox.x0)
    return (
        overlap > 0 and span > 0 and overlap / span >= 0.5
        and page.height > 0
        and prev.bbox.y1 / page.height >= bottom_threshold_ratio
    )


def _table_stitching_config() -> dict:
    """Load table stitching options, retaining safe defaults when absent."""
    config = {"bottom_threshold_ratio": 0.75, "require_table_num_match": True}
    candidates = [
        Path.cwd() / "create_markdown_config.yaml",
        Path(__file__).with_name("create_markdown_config.yaml"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                section = (yaml.safe_load(stream) or {}).get("table_stitching", {})
            if isinstance(section, dict):
                config.update({key: section[key] for key in config if key in section})
            break
        except (OSError, yaml.YAMLError) as exc:
            log.warning("Не удалось загрузить table_stitching из %s: %s", path, exc)
    return config


def stitch_tables(doc: Document) -> None:
    """Stitch structured tables; explicit matching continuations override geometry."""
    config = _table_stitching_config()
    threshold = float(config["bottom_threshold_ratio"])
    require_num = bool(config["require_table_num_match"])
    ordered = sorted(doc.tables, key=lambda table: (table.page, table.table_index))
    current_head = current_last = None
    current_num = None
    group_id = 0
    for table in ordered:
        table.stitch_group_id = group_id
        if table.caption and not table.is_continuation:
            group_id += 1
            table.stitch_group_id = group_id
            current_head = current_last = table
            current_num = table.table_num
        elif (table.is_continuation and current_head is not None and
              (not require_num or table.table_num == current_num)):
            table.caption = current_head.caption
            table.stitch_group_id = current_head.stitch_group_id
            current_last = table
        elif (not table.caption and current_last is not None and
              geometry_says_same_table(
                  doc.pages[current_last.page], current_last, table, threshold
              )):
            table.caption = current_head.caption
            table.stitch_group_id = current_head.stitch_group_id
            current_last = table
        else:
            group_id += 1
            table.stitch_group_id = group_id
            current_head = current_last = table
            current_num = table.table_num


def _caption_bbox_for_table(doc: Document, table: Table) -> BBox | None:
    candidates = [b for b in doc.pages[table.page].blocks if b.is_table_caption and b.bbox.y1 <= table.bbox.y0]
    return max(candidates, key=lambda b: b.bbox.y1).bbox if candidates else None


def extract_table_images_from_model(pdf_path: str | Path, doc: Document, img_dir: str | Path) -> None:
    import fitz
    img_dir = ensure_dir(img_dir)
    with fitz.open(str(pdf_path)) as pdf:
        for ordinal, table in enumerate(doc.tables, 1):
            if table.page >= pdf.page_count:
                continue
            page = pdf[table.page]
            model_page = doc.pages[table.page]
            sx = page.rect.width / model_page.width if model_page.width else 1.0
            sy = page.rect.height / model_page.height if model_page.height else 1.0
            caption_bbox = _caption_bbox_for_table(doc, table)
            top = caption_bbox.y0 if caption_bbox else table.bbox.y0
            rect = fitz.Rect(table.bbox.x0 * sx, top * sy, table.bbox.x1 * sx, table.bbox.y1 * sy)
            path = img_dir / f"table_{ordinal}.png"
            if _crop_and_save_image(page, rect, path):
                table.image_path = str(path)


def extract_pictures_from_model(pdf_path: str | Path, doc: Document, img_dir: str | Path) -> None:
    import fitz
    img_dir = ensure_dir(img_dir)
    with fitz.open(str(pdf_path)) as pdf:
        for ordinal, picture in enumerate(doc.pictures, 1):
            if picture.page >= pdf.page_count:
                continue
            page = pdf[picture.page]
            model_page = doc.pages[picture.page]
            sx = page.rect.width / model_page.width if model_page.width else 1.0
            sy = page.rect.height / model_page.height if model_page.height else 1.0
            rect = fitz.Rect(picture.bbox.x0 * sx, picture.bbox.y0 * sy, picture.bbox.x1 * sx, picture.bbox.y1 * sy)
            path = img_dir / f"fig_{ordinal}.png"
            if _crop_and_save_image(page, rect, path):
                picture.image_path = str(path)


def populate_component_images(doc: Document) -> None:
    groups: dict[int, list[Table]] = {}
    for table in sorted(doc.tables, key=lambda item: (item.page, item.table_index)):
        if not table.caption or table.stitch_group_id is None:
            continue
        groups.setdefault(table.stitch_group_id, []).append(table)
    for group in groups.values():
        paths = [table.image_path for table in group if table.image_path]
        if len(group) > 1:
            for table in group:
                table.component_images = list(paths)


def table_cells_to_md(cells: list[Cell]) -> str:
    """Convert structured table cells to a Markdown table."""
    if not cells:
        return ""
    max_row = max(cell.row + cell.rowspan for cell in cells)
    max_col = max(cell.col + cell.colspan for cell in cells)
    matrix = [["" for _ in range(max_col)] for _ in range(max_row)]
    for cell in sorted(cells, key=lambda item: (item.row, item.col)):
        for row in range(cell.row, min(max_row, cell.row + cell.rowspan)):
            for col in range(cell.col, min(max_col, cell.col + cell.colspan)):
                if not matrix[row][col]:
                    matrix[row][col] = cell.text
    return _matrix_to_md_table(matrix)


def render_document_to_md(document: Document) -> str:
    """Render a fully populated structured document in one Markdown pass."""
    lines: list[str] = []
    for page in sorted(document.pages, key=lambda item: item.index):
        page_lines: list[str] = []
        page_ranges: list[tuple[Table, int, int]] = []
        elements = [(block.y, 0, block) for block in page.blocks]
        elements += [(document.tables[index].bbox.y0, 1, document.tables[index]) for index in page.table_indices]
        elements += [(document.pictures[index].bbox.y0, 2, document.pictures[index]) for index in page.picture_indices]
        for _, _, element in sorted(elements, key=lambda item: (item[0], item[1])):
            if isinstance(element, Block):
                if element.is_table_caption or element.is_continuation_caption or not element.text:
                    continue
                if element.heading_level is not None:
                    page_lines.append("#" * (element.heading_level + 1) + " " + element.text)
                elif element.layout_type == "LIST":
                    page_lines.append("- " + element.text)
                else:
                    page_lines.append(element.text)
            elif isinstance(element, Table):
                start = len(page_lines)
                if element.caption:
                    page_lines.append("*" + element.caption + "*")
                table_md = table_cells_to_md(element.cells)
                if table_md:
                    page_lines.extend(table_md.splitlines())
                page_ranges.append((element, start, len(page_lines)))
            elif element.image_path:
                ordinal = document.pictures.index(element) + 1
                page_lines.append(f"![Рис. {ordinal}](image/{Path(element.image_path).name})")
        if not page_lines:
            continue
        if lines:
            lines.append("")
        page_offset = len(lines)
        lines.extend(page_lines)
        for element, start, end in page_ranges:
            element.md_lines = (page_offset + start, page_offset + end)
    return "\n".join(lines)


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
# Доля длины AI-ответа относительно исходного чанка, ниже которой ответ
# считается неполным (LLM молча пропустила фрагмент): чанк идёт в итог
# без изменений — потеря текста недопустима.
AI_MIN_OUTPUT_RATIO = 0.85
YANDEX_MAX_PAGES = 200
YANDEX_MAX_SIZE_MB = 10
YANDEX_POLL_TIMEOUT = 600  # 10 минут
YANDEX_POLL_INTERVAL = 2


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


def load_providers_config(path: str | Path) -> dict:
    """Load and validate the provider registry YAML.

    Provider configuration is deliberately separate from prompts and RAG
    settings so it can be edited by the UI without exposing API secrets.
    """
    provider_path = Path(path)
    if not provider_path.exists():
        raise ValueError(f"Файл providers.yaml не найден: {provider_path}")
    try:
        with provider_path.open(encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError(f"Некорректный YAML providers.yaml: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise ValueError("Некорректный providers.yaml: отсутствует секция providers")

    seen_keys: dict[str, str] = {}
    for name, provider in config["providers"].items():
        if not isinstance(provider, dict):
            raise ValueError(f"Некорректный провайдер: {name}")
        key_name = provider.get("api_key_env")
        if not key_name:
            raise ValueError(f"У провайдера {name} не задан api_key_env")
        if key_name in seen_keys:
            raise ValueError(
                f"Дублирующийся api_key_env: {key_name} "
                f"(провайдеры {seen_keys[key_name]} и {name})"
            )
        seen_keys[key_name] = str(name)
    return config


def resolve_role(role_cfg: dict, providers: dict) -> dict:
    """Resolve a provider/model reference to the flat API client config."""
    if not isinstance(role_cfg, dict):
        raise ValueError("Некорректная конфигурация роли")
    registry = providers.get("providers", providers)

    def resolve_ref(ref: dict) -> dict:
        if not isinstance(ref, dict):
            raise ValueError("Некорректная ссылка на провайдера")
        provider_name = ref.get("provider")
        if provider_name not in registry:
            raise ValueError(f"Неизвестный провайдер: {provider_name}")
        provider = registry[provider_name]
        model = ref.get("model")
        models = provider.get("models", {})
        if model not in models:
            raise ValueError(f"Модель не в списке провайдера {provider_name}: {model}")
        return {
            "provider": provider_name,
            "model": model,
            "api_key_env": provider.get("api_key_env"),
            "base_url": provider.get("base_url"),
        }

    resolved = resolve_ref(role_cfg)
    if role_cfg.get("fallback") is not None:
        resolved["fallback"] = resolve_ref(role_cfg["fallback"])
    return resolved


def ensure_dir(path: str | Path) -> Path:
    """Создать папку, если не существует."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_input_files(input_path: str) -> list[Path]:
    """Проверить входной путь: только один файл (PDF/DOCX/DOC/MD).

    Батч-режим удалён: папки не принимаются, возвращается максимум один файл.
    """
    p = Path(input_path)
    if p.is_file():
        return [p]
    if p.is_dir():
        log.error(
            f"Входная папка не поддерживается: {input_path} — "
            "укажите один файл (PDF/DOCX/DOC/MD)"
        )
        return []
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

        def _next_rendered(index: int) -> tuple[str, dict] | None:
            """Следующий элемент после index, который не является блоком-ячейкой таблицы."""
            for j in range(index + 1, len(elements)):
                jtype, jdata = elements[j][1], elements[j][2]
                if jtype == "block" and _block_in_table(jdata):
                    continue
                return (jtype, jdata)
            return None

        # Индекс таблицы в массиве ta["tables"] — единственный источник id.
        # Тот же список итерирует extract_table_images() через enumerate(ta["tables"]),
        # поэтому ti здесь == ti в вырезке (тождество id, контракт table-id-marker §4).
        # НЕ порядковый счётчик Y-сортировки и НЕ позиция в page_lines.
        table_raw_index: dict[int, int] = {
            id(t): i for i, t in enumerate(raw_tables)
        }

        for idx, (y, etype, data) in enumerate(elements):
            if etype == "block":
                # Пропускаем блоки, которые являются частью таблицы
                if _block_in_table(data):
                    continue
                block_text = _block_text(data)
                nxt = _next_rendered(idx)
                # «Окончание/Продолжение …» — служебный маркер ТОЛЬКО если сразу
                # за ним идёт таблица; иначе это обычный текст (дефект: проза
                # со словом «окончание» ошибочно принималась за маркер).
                if _CONTINUATION_MARKER_RE.match(block_text) and nxt and nxt[0] == "table":
                    continue
                # Одинокая «Т» — оторванная первая буква подписи «Т а б л и ц а …»
                # (OCR делит подпись на два блока); подавляем только перед подписью.
                if (block_text == "Т" and nxt and nxt[0] == "block"
                        and _CAPTION_START_RE.match(_block_text(nxt[1]))):
                    continue
                text = _block_to_md(data)
                if text:
                    page_lines.append(text)
            elif etype == "table":
                md_table, note_text = _table_to_md(data)
                if md_table:
                    caption = _find_table_caption_for(blocks, data, raw_tables)
                    ti = table_raw_index.get(id(data))
                    if ti is not None:
                        # ID-маркер рождается здесь (parse), а не в постобработке:
                        # (page_idx, таблица) доступны в том же порядке, что в вырезке.
                        # Маркер всегда перед подписью; без подписи — перед строками таблицы.
                        page_lines.append(f"<!-- t_p{page_idx + 1}_{ti} -->")
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

    # Подпись «Т а б л и ц а …» — только если слово стоит в НАЧАЛЕ блока:
    # абзац с упоминанием «(таблица Б.1)» в середине — это текст, не подпись.
    if _CAPTION_START_RE.match(text):
        return ""

    # «Окончание/Продолжение таблицы N» — служебная подпись-продолжение.
    # Блок-маркер БЕЗ слова «таблица», за которым не идёт таблица, отсекается
    # контекстно в parse_yandex_json_to_md и сюда не доходит.
    if _CONTINUATION_CAPTION_RE.search(normalized):
        return ""

    # «П р и м е ч а н и е» — только в начале блока (аналогично подписи).
    if _PRIM_NOTE_START_RE.match(text):
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


_TABLE_WORD_RE = re.compile(r"Т\s*а\s*б\s*л\s*и\s*ц\s*а", re.IGNORECASE)

# Якорные проверки: слово стоит в НАЧАЛЕ блока. Слово «таблица»/«примечание»
# в середине обычного абзаца (например, «…(таблица Б.1)…») не делает блок
# подписью/примечанием — такие блоки выводятся как текст (иначе теряется текст).
# _CAPTION_START_RE учитывает OCR-разбиение подписи: «Т а б л и ц а …» и
# оторванную первую букву («Т» отдельным блоком + «а б л и ц а …»).
_CAPTION_START_RE = re.compile(r"^\s*Т?\s*а\s*б\s*л\s*и\s*ц\s*а\b", re.IGNORECASE)
_PRIM_NOTE_START_RE = re.compile(
    r"^\s*П\s*р\s*и\s*м\s*е\s*ч\s*а\s*н\s*и\s*[ея]\b", re.IGNORECASE
)
# Маркер продолжения таблицы: «Окончание/Продолжение …» в начале строки.
# Слово «таблица» может отсутствовать (OCR); валидность маркера определяется
# контекстом (за ним должна идти таблица), а не наличием слова. \b отсекает
# словоформы («окончанием короткого замыкания») — не маркеры.
_CONTINUATION_MARKER_RE = re.compile(
    r"^\s*\*{0,2}\s*(?:Окончани[ея]|Продолжени[ея]|Продолж\.?)\b", re.IGNORECASE
)
# Шум полосы страницы между таблицами: колонтитул «ГОСТ …», номер страницы.
_PAGE_NOISE_RE = re.compile(
    r"^(?:ГОСТ\s+\d+[—\-–\s]*\d*|\d{1,4}|—\s*\d{1,4}\s*—)\s*$", re.IGNORECASE
)


def _looks_like_table_caption(text: str) -> bool:
    """Якорная проверка: блок является подписью таблицы.

    «Таблица…» в начале блока (включая OCR-варианты «Т а б л и ц а» и
    «а б л и ц а …» без первой буквы) либо явное «Окончание/Продолжение
    таблицы N». Слово «таблица» в середине обычного абзаца подписью
    не является.
    """
    t = text.strip()
    return bool(_CAPTION_START_RE.match(t) or _CONTINUATION_CAPTION_RE.search(t))


def _block_text(block: dict) -> str:
    """Собрать текст блока из строк OCR (без хвостовых пробелов)."""
    text = ""
    for line_data in block.get("lines", []):
        t = line_data.get("text", "").strip()
        if t:
            text += " " + t
    return text.strip()


def _find_table_caption_block(
    blocks: list[dict],
    table: dict,
    all_tables: list[dict],
) -> dict | None:
    """Найти блок-подпись для таблицы (с boundingBox) или None.

    Выбирается ближайший сверху блок, который является LAYOUT_TYPE_CAPTION
    или содержит текст «Т а б л и ц а» (с произвольными пробелами между
    буквами — OCR-артефакт), и при этом не относится к другой, более
    близкой по Y таблице. Возвращает сам блок (с геометрией), а не текст,
    чтобы вырезка могла учесть высоту подписи.
    """
    table_vertices = table.get("boundingBox", {}).get("vertices", [])
    if not table_vertices:
        return None
    table_y = min(float(v.get("y", 0)) for v in table_vertices)

    # Y-позиции других таблиц (для определения, какая таблица ближе к caption).
    other_table_ys: list[float] = []
    for other in all_tables:
        if other is table:
            continue
        ov = other.get("boundingBox", {}).get("vertices", [])
        if ov:
            other_table_ys.append(min(float(v.get("y", 0)) for v in ov))

    # Сортируем блоки по Y (возрастание — сверху вниз).
    blocks_with_y: list[tuple[float, dict]] = []
    for block in blocks:
        bv = block.get("boundingBox", {}).get("vertices", [])
        by = min(float(v.get("y", 0)) for v in bv) if bv else 0.0
        blocks_with_y.append((by, block))
    blocks_with_y.sort(key=lambda x: x[0])

    best_block: dict | None = None
    best_y_diff: float = float("inf")

    for by, block in blocks_with_y:
        if by >= table_y:
            continue  # Блок ниже таблицы — не подходит

        # Не ближе ли другая таблица к этому блоку (тогда блок — её подпись)?
        if any(by < ot_y < table_y for ot_y in other_table_ys):
            continue

        layout_type = block.get("layoutType", "")
        block_text = _block_text(block)
        is_caption_block = layout_type == "LAYOUT_TYPE_CAPTION"
        has_table_word = bool(
            _TABLE_WORD_RE.search(block_text)
            or _CONTINUATION_CAPTION_RE.search(block_text)
        )

        if is_caption_block or has_table_word:
            y_diff = table_y - by
            if y_diff < best_y_diff:
                best_y_diff = y_diff
                best_block = block

    return best_block


def _caption_block_to_text(block: dict | None) -> str:
    """Нормализованный текст подписи из блока (пустая строка, если блока нет)."""
    if not block:
        return ""
    block_text = _block_text(block)
    if (_TABLE_WORD_RE.search(block_text)
            or _CONTINUATION_CAPTION_RE.search(block_text)):
        caption = _normalize_spaced_text(block_text)
        return re.sub(r"\s+", " ", caption)
    return block_text


def _find_table_caption_for(
    blocks: list[dict],
    table: dict,
    all_tables: list[dict],
) -> str:
    """Найти подпись для таблицы среди блоков.

    Args:
        blocks: Все блоки страницы.
        table: Текущая таблица.
        all_tables: Все таблицы страницы (для избежания дублирования подписей).

    Returns:
        Нормализованный текст подписи, или пустая строка.
    """
    return _caption_block_to_text(_find_table_caption_block(blocks, table, all_tables))


def _extract_table_num_from_text(text: str) -> str | None:
    """Извлечь номер таблицы, включая буквенные номера (например, Б.1).

    Используется для связывания OCR-вырезок, относящихся к одной таблице на
    нескольких страницах. Номер берётся только после слова «таблица» или
    явного маркера продолжения, чтобы не связывать случайные числа в тексте.
    """
    normalized = _normalize_spaced_text(text)
    match = re.search(
        r"(?:Окончани[ея]|Продолжени[ея]|Продолж\.?)\s+"
        r"(?:таблиц[аы]|табл\.?|table)\s+"
        r"([А-ЯA-Z]?\.?\d+(?:\.\d+)*)",
        normalized,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"(?:Таблиц[аы]|табл\.?|table)\s+"
            r"([А-ЯA-Z]?\.?\d+(?:\.\d+)*)",
            normalized,
            re.IGNORECASE,
        )
    return match.group(1).upper().replace(".", ".") if match else None


_CONTINUATION_CAPTION_RE = re.compile(
    r"(?:Окончани[ея]|Продолжени[ея]|Продолж\.?)\s+(?:таблиц[аы]|табл\.?|table)",
    re.IGNORECASE,
)


def _is_continuation_caption(caption: str) -> bool:
    """Подпись явно помечает продолжение/окончание таблицы (не босая «Таблица N»).

    Гарантия: босая подпись «Таблица 2» под предикат НЕ попадает — это
    защита от дефекта 2 (ложная склейка одноимённых таблиц).
    """
    return bool(caption) and bool(_CONTINUATION_CAPTION_RE.search(_normalize_spaced_text(caption)))


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
            # Способ 1: явный маркер "Окончание/Продолжение" в тексте + совпадение номеров.
            # Для уже склеенной группы сверяемся с номером её головы, а не
            # с номером непосредственного (возможно captionless) предшественника.
            prev_head_num = prev.get("head_table_num", prev["table_num"])
            if (t["has_continuation"] and t["table_num"]
                    and prev_head_num == t["table_num"]):
                is_continuation = True
            # Способ 2: page-level контекст — таблица на другой странице,
            # на этой странице есть continuation-блок, а номер ТЕКУЩЕЙ таблицы
            # явно совпадает с номером головы активной группы. Отсутствующий
            # номер не является совпадением: это предотвращает false-stitch
            # независимых captionless-таблиц на continuation-помеченной странице.
            elif (page_boundaries and page_has_continuation
                  and prev_head_num is not None
                  and t["table_num"] is not None
                  and t["table_num"] == prev_head_num):
                t_page = _page_of_line(t["start"])
                prev_page = _page_of_line(prev["start"])
                if (t_page is not None and prev_page is not None
                        and t_page != prev_page
                        and t_page < len(page_has_continuation)
                        and page_has_continuation[t_page]):
                    is_continuation = True

        if is_continuation and i > 0:
            prev = tables_info[i - 1]
            t["head_table_num"] = prev.get("head_table_num", prev["table_num"])
            # Нашли пару — склеиваем: данные из t добавляем к result
            t_lines = t["lines"]
            sep_idx = -1
            for li, tl in enumerate(t_lines):
                if ":--" in tl or "---" in tl:
                    sep_idx = li
                    break
            if sep_idx >= 0 and sep_idx + 1 < len(t_lines):
                data_lines = t_lines[sep_idx + 1:]
                # Маркеры продолжений должны быть частью стека перед итоговой
                # таблицей. Комментарий между строками Markdown-таблицы
                # завершает таблицу и поэтому недопустим.
                continuation_markers = [
                    line.strip()
                    for line in lines[prev["end"]:t["start"]]
                    if _TABLE_ID_MARKER_RE.match(line.strip())
                ]
                if continuation_markers:
                    marker_positions = [
                        index for index, line in enumerate(result)
                        if _TABLE_ID_MARKER_RE.match(line.strip())
                    ]
                    insert_at = marker_positions[-1] + 1 if marker_positions else len(result)
                    result[insert_at:insert_at] = continuation_markers
                # Пропускаем только повторяющийся префикс шапки; данные не
                # дедуплицируются (одинаковые строки rowspan легитимны).
                prev_sep = next(
                    (idx for idx, line in enumerate(prev["lines"])
                     if ":--" in line or "---" in line),
                    -1,
                )
                if prev_sep >= 0:
                    data_lines = _trim_repeated_header_prefix(
                        prev["lines"][prev_sep + 1:], data_lines,
                    )
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
        t["head_table_num"] = t["table_num"]
        prev_end = tables_info[i - 1]["end"] if i > 0 else 0
        result.extend(lines[prev_end:t["start"]])
        result.extend(t["lines"])
        i += 1

    # Добавляем остаток после последней таблицы
    if tables_info:
        last_end = tables_info[-1]["end"]
        result.extend(lines[last_end:])

    return "\n".join(result)


_TABLE_ID_MARKER_RE = re.compile(r"<!--\s*(t_p\d+_\d+)\s*-->")
_WARN_MARKER_RE = re.compile(r"<!--\s*WARN_([0-9A-Za-zА-Яа-я.]+)\s*-->")


def _extract_warn_tables(md_text: str) -> list[str]:
    """Вернуть номера таблиц, помеченных AI как требующие проверки.

    Номера сохраняются в порядке появления, повторные маркеры одной таблицы
    сворачиваются. Детекция выполняется по итоговому тексту независимо от
    наличия ID-маркеров и от границ AI-чанков.
    """
    found: list[str] = []
    for match in _WARN_MARKER_RE.finditer(md_text):
        number = match.group(1).strip()
        if number and number not in found:
            found.append(number)
    return found


def _normalize_inline_latex_delimiters(text: str) -> str:
    """Привести единственный поддерживаемый inline-делимитер к ``$...$``."""
    return re.sub(r"\\\((.*?)\\\)", r"$\1$", text, flags=re.DOTALL)


# ═══════════════════════════════════════════════════════════════════════════
# 3b. Heading Extractor (ADR-8)
# ═══════════════════════════════════════════════════════════════════════════

_HEADING_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:\.\s|\s)")
# Римские цифры I–XXX с точкой и пробелом: «II. Состав разделов»
_HEADING_ROMAN_RE = re.compile(
    r"^(M{0,3}(?:CM|CD|D?C{0,3})?(?:XC|XL|L?X{0,3})?(?:IX|IV|V?I{0,3}))\.\s",
    re.IGNORECASE,
)
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
            is_roman = False
            if not m:
                # Правило 1r: римские цифры «II. Состав разделов...»
                m = _HEADING_ROMAN_RE.match(text)
                if not m:
                    continue
                is_roman = True

            # Правило 1b: однобуквенные номера без точек — только 1-2 цифры (главы)
            # «200 кА» — не заголовок; «11 Гарантии» — заголовок
            # Для римских цифр правило не применяется
            if not is_roman:
                number_raw = m.group(1)
                if "." not in number_raw and len(re.sub(r"[^\d]", "", number_raw)) > 2:
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
        with fitz.open(str(pdf_path)) as doc:
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
                        log.warning(
                            f"  ({page_idx}): картинка без bbox.vertices (<4) — "
                            "ПРОПУЩЕНА, fig_N не присвоен"
                        )
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
        
                    pix = _crop_and_save_image(page, fitz_rect, output_path, 2.0)
                    if pix is not None:
                        # Реальные размеры сохранённого pixmap
                        log.info(
                            f"  fig_{fig_counter} (стр.{page_idx}): извлечено "
                            f"({pix.width}x{pix.height})"
                        )
                        saved_images.append({
                            "fig_num": fig_counter,
                            "page": page_idx,
                            "filename": filename,
                            "bbox": bbox,
                        })
                    else:
                        # Проверяем, rect ли за границей страницы
                        if not page_rect.intersects(fitz_rect):
                            log.warning(
                                f"  fig_{fig_counter} (стр.{page_idx}): ПРОПУЩЕНО — "
                                "rect за границей страницы"
                            )
                        else:
                            log.warning(
                                f"  fig_{fig_counter} (стр.{page_idx}): ПРОПУЩЕНО — "
                                "ошибка вырезки"
                            )
        
    except Exception as e:
        log.error(f"  Не удалось открыть PDF: {e}")
        return []

    # Итоговый summary
    # total = fig_counter, т.к. картинки без bbox.vertices (<4) не получают fig_N,
    # и тогда len(pictures) дал бы «фантомные» номера в списке пропущенных.
    total = fig_counter
    saved = len(saved_images)
    skipped = total - saved
    figs_ok = [str(im["fig_num"]) for im in saved_images]
    figs_skip = [
        str(n)
        for n in range(1, total + 1)
        if n not in {im["fig_num"] for im in saved_images}
    ]

    if skipped:
        if saved:
            log.info(f"  Извлечено: {saved}/{total} (fig_{', fig_'.join(figs_ok)})")
        else:
            log.info(f"  Извлечено: {saved}/{total}")
        log.warning(f"  Пропущено: {skipped} (fig_{', fig_'.join(figs_skip)})")
    else:
        log.info(f"  Извлечено: {saved}/{total}")
    return saved_images


def _crop_and_save_image(
    page: "fitz.Page",
    rect: "fitz.Rect",
    output_path: Path,
    dpi_scale: float = 2.0,
) -> "fitz.Pixmap | None":
    """Вырезать область страницы по rect и сохранить как изображение.

    Returns:
        Сохранённый pixmap при успехе (нужен для реальных размеров
        width/height), None при ошибке вырезки.
    """
    import fitz
    try:
        log.debug(f"    crop rect={rect} -> {output_path}")
        # Создаём матрицу для увеличения DPI
        matrix = fitz.Matrix(dpi_scale, dpi_scale)
        pix = page.get_pixmap(matrix=matrix, clip=rect)
        pix.save(str(output_path))
        return pix
    except Exception as e:
        log.warning(f"  Не удалось вырезать изображение: {e}")
        # pix.save() мог успеть создать пустой файл — убираем «фантом»
        output_path.unlink(missing_ok=True)
        return None


def _insert_images_into_md(
    md_text: str,
    extracted: list[dict],
    pictures: list[dict] | None = None,
    pages: list[dict] = None,
    page_boundaries: list[tuple[int, int]] | None = None,
) -> str:
    """Вставить ссылки на извлечённые изображения в Markdown, заменяя @@IMAGE_N@@ плейсхолдеры.

    Плейсхолдеры @@IMAGE_0@@, @@IMAGE_1@@, ... создаются в parse_yandex_json_to_md()
    в порядке pictures (по Y на странице).

    Сопоставление идёт ПО ПОЗИЦИИ В pictures (индекс N = плейсхолдер N):
    для каждого picture ищется extracted-изображение по page + bbox.vertices.
    Это важно, когда extract_images_from_pdf() не смог вырезать часть картинок:
    номера fig_N остаются привязанными к своим местам, а не сдвигаются к
    началу списка извлечённых (старое поведение по индексу в sorted_imgs).

    Если для picture нет matching extracted (вырезка провалилась) — плейсхолдер
    очищается (заменяется пустой строкой), чтобы в финальном MD не оставалось
    служебных токенов @@IMAGE_N@@.

    Args:
        md_text: Markdown-текст с плейсхолдерами @@IMAGE_N@@.
        extracted: Список от extract_images_from_pdf().
        pictures: Список картинок от parse_yandex_json_to_md() — позиция
            элемента = номер плейсхолдера. Если None — используется старый
            порядок (сортировка extracted по page/Y) для совместимости.
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

    def _bbox_key(bbox) -> tuple | None:
        """Сравнимый ключ bbox: кортеж округлённых вершин (x, y)."""
        vertices = (bbox or {}).get("vertices", [])
        if not vertices:
            return None
        return tuple(
            (round(float(v.get("x", 0)), 2), round(float(v.get("y", 0)), 2))
            for v in vertices
        )

    def _matches(pic: dict, img: dict) -> bool:
        """Совпадает ли extracted-изображение с picture (page + bbox)."""
        if pic.get("page") != img.get("page"):
            return False
        pk = _bbox_key(pic.get("bbox", {}))
        ik = _bbox_key(img.get("bbox", {}))
        return pk is not None and pk == ik

    if pictures is not None:
        # Сопоставление по позиции в pictures: pictures[N] -> @@IMAGE_N@@
        for idx, pic in enumerate(pictures):
            placeholder = f"@@IMAGE_{idx}@@"
            if placeholder not in md_text:
                continue
            match = next((im for im in extracted if _matches(pic, im)), None)
            if match is not None:
                replacement = f"![fig_{match['fig_num']}](image/{match['filename']})"
                md_text = md_text.replace(placeholder, replacement, 1)
            else:
                # Вырезка не удалась — убираем плейсхолдер, чтобы в MD
                # не оставалось незаменённых @@IMAGE_N@@.
                md_text = md_text.replace(placeholder, "", 1)
        # Защита: плейсхолдеров не должно быть больше, чем pictures
        # (они создаются 1:1), но если вдруг остались — очищаем.
        md_text = re.sub(r"@@IMAGE_\d+@@", "", md_text)
    else:
        # Старый порядок (совместимость): сортируем extracted по (page, Y)
        # и заменяем плейсхолдеры по индексу в отсортированном списке.
        sorted_imgs = sorted(extracted, key=lambda x: (x["page"], _get_y_center(x)))
        for idx, img in enumerate(sorted_imgs):
            placeholder = f"@@IMAGE_{idx}@@"
            replacement = f"![fig_{img['fig_num']}](image/{img['filename']})"
            if placeholder in md_text:
                md_text = md_text.replace(placeholder, replacement, 1)

    return md_text


# ═══════════════════════════════════════════════════════════════════════════
# 4b. Vision-распознавание таблиц (в составе --ai)
# ═══════════════════════════════════════════════════════════════════════════


def _table_crop_top(caption_block: dict | None, table_top: float, sy: float) -> float:
    """Верхняя граница вырезки таблицы: включает подпись целиком.

    Если подпись найдена, верх вырезки — это верхняя грань блока подписи
    (с отступом 2pt). Иначе — фиксированный запас 64pt над таблицей.
    """
    if caption_block:
        bv = caption_block.get("boundingBox", {}).get("vertices", [])
        if bv:
            caption_top = min(float(v.get("y", 0)) for v in bv) * sy
            return max(0.0, caption_top - 2)
    return max(0.0, table_top - 64)


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

    with fitz.open(str(pdf_path)) as doc:
        table_images: list[dict] = []
        # Геометрия хранится отдельно, чтобы не менять публичный формат
        # table_images.json и не добавлять служебные поля в результат.
        table_geometry: dict[int, tuple[int, int, float, float]] = {}
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

                # Подпись таблицы: находим блок (с геометрией), чтобы вырезка
                # сверху включила её целиком (динамический отступ).
                caption_block = _find_table_caption_block(
                    ta.get("blocks", []), table, tables
                )
                caption = _caption_block_to_text(caption_block)
                y0 = _table_crop_top(caption_block, min(ys) * sy, sy)

                x1, y1 = max(xs) * sx + 2, max(ys) * sy + 2

                rect = fitz.Rect(x0, y0, x1, y1)
                pix = page.get_pixmap(clip=rect, dpi=200)

                table_counter += 1
                fname = f"table_{table_counter}.png"
                pix.save(str(img_dir / fname))

                table_num = _extract_table_num_from_text(caption)
                item = {
                    "page": pi,
                    "table_idx": table_counter,
                    "path": fname,
                    "id": f"t_p{pi + 1}_{ti}",
                }
                if caption:
                    item["caption"] = caption
                if table_num:
                    item["table_num"] = table_num
                table_images.append(item)
                table_geometry[id(item)] = (pi, ti, max(ys) * sy, page.rect.height)
                log.info(f"  Вырезана таблица {table_counter}: стр.{pi+1}, {fname} ({rect.width:.0f}x{rect.height:.0f} px)")

    # component_images формируются ТОЛЬКО spatial-pass'ем ниже.
    # Группировка по голому table_num удалена (контракт table-id-marker §5.3):
    # номер «Таблица N» не уникален между разделами/приложениями — три несвязанные
    # таблицы с одинаковым номером склеивались в одну и удалялись склейкой.

    # На продолжении Yandex OCR часто не выдаёт ни caption, ни table_num.
    tables_by_page: dict[int, list[dict]] = {}
    for item in table_images:
        tables_by_page.setdefault(item["page"], []).append(item)

    current_group: dict | None = None
    current_group_members: list[dict] = []
    for pi in sorted(tables_by_page):
        page_tables = tables_by_page[pi]
        for ti, item in enumerate(page_tables):
            caption = item.get("caption") or ""
            has_caption = bool(item.get("caption") or item.get("table_num"))
            is_continuation = _is_continuation_caption(caption)

            # Обычная подпись (не «Продолжение/Окончание таблицы N») — новая группа.
            if has_caption and not is_continuation:
                current_group = item
                current_group_members = [item]
                continue

            # caption-less ИЛИ явная подпись-продолжение → попытка присоединиться
            # к текущей группе (как caption-less продолжение).
            if ti != 0 or pi <= 0 or current_group is None:
                if is_continuation:
                    # Не присоединилась (нет группы / не первая на странице) —
                    # собственная новая группа, чтобы следующие caption-less
                    # таблицы не приклеились к чужой голове.
                    current_group = item
                    current_group_members = [item]
                continue

            previous_tables = tables_by_page.get(pi - 1, [])
            if not previous_tables or previous_tables[-1] is not current_group_members[-1]:
                if is_continuation:
                    current_group = item
                    current_group_members = [item]
                continue
            previous = previous_tables[-1]
            _, _, bottom, page_height = table_geometry[id(previous)]
            if bottom < page_height * 0.9 and not is_continuation:
                continue

            # Для подписи-продолжения номер обязан совпасть с головой группы:
            # «Продолжение таблицы А.1» при голове «Таблица Б.1» — не склеивать.
            if is_continuation:
                item_num = item.get("table_num")
                group_num = current_group.get("table_num")
                if item_num and group_num and item_num != group_num:
                    current_group = item
                    current_group_members = [item]
                    continue

            paths = list(current_group.get("component_images") or [current_group["path"]])
            if item["path"] not in paths:
                paths.append(item["path"])
            for member in current_group_members:
                member["component_images"] = paths
                if current_group.get("caption"):
                    member["caption"] = current_group["caption"]
            current_group_members.append(item)
            item["component_images"] = paths
            if current_group.get("caption"):
                item["caption"] = current_group["caption"]

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
        config: Секция table_vision из create_markdown_config.yaml.
        api_key: API-ключ.

    Returns:
        Распознанный текст (Markdown) или None при ошибке.
    """
    model = config.get("model", "google/gemini-2.5-flash-lite")
    base_url = config.get("base_url", "https://api.provod.ai/v1")
    fallback = config.get("fallback", {})

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
                if resp.status_code in (400, 401, 403):
                    log.warning(f"  {mdl}: HTTP {resp.status_code}, primary недоступен — переход к fallback")
                    return None
                if resp.status_code == 503:
                    log.warning(f"  {mdl}: 503, попытка {attempt + 1}/3")
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                # Убираем возможные markdown-обёртки
                content = re.sub(r"^```(?:markdown)?\s*\n?", "", content, flags=re.MULTILINE)
                content = re.sub(r"\n```\s*$", "", content, flags=re.MULTILINE)
                if _is_recognition_failure(content):
                    log.warning(f"  {mdl}: модель сообщила о невозможности распознать — переход к fallback")
                    return None
                return content
            except Exception as e:
                log.warning(f"  {mdl}: {e}, попытка {attempt + 1}/3")
                time.sleep(5)
        return None

    # Primary
    if api_key:
        log.info(f"  Vision: {model}")
        result = _do_vision(model, base_url, api_key)
        if result is not None:
            return result
    else:
        log.warning("  Vision: primary API-ключ не задан — переход к fallback")

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


def _vision_table_missing(
    table_images: list[dict], tmp_dir: str | Path
) -> list[dict]:
    """Вернуть таблицы без валидного table_<table_idx>.md в tmp_dir.

    «Валидный» = файл существует И первая непустая строка совпадает с
    _TABLE_ID_MARKER_RE (тот же признак, по которому AI-этап собирает
    vision_tables). Таблицы без маркера считаются нераспознанными.
    """
    tmp = Path(tmp_dir)
    missing: list[dict] = []
    for ti in table_images:
        # Ключ соответствия — table_idx (как создаётся table_<idx>.md в
        # recognize_tables_vision). Запись без table_idx (legacy/битый ввод)
        # не может иметь валидного кэша — считается недостающей.
        table_idx = ti.get("table_idx")
        if table_idx is None:
            missing.append(ti)
            continue
        md_path = tmp / f"table_{table_idx}.md"
        if not md_path.is_file():
            missing.append(ti)
            continue
        try:
            lines = [ln for ln in md_path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
        except OSError:
            missing.append(ti)
            continue
        if not lines or not _TABLE_ID_MARKER_RE.match(lines[0].strip()):
            missing.append(ti)
    return missing


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


def _between_is_service_only(between_lines: list[str]) -> bool:
    """Между таблицами нет содержательного текста?

    Допустимое «служебное» содержимое: ID-маркеры таблиц, подписи
    («Таблица N — …», «Окончание/Продолжение таблицы N», слово «таблица»
    может отсутствовать из-за OCR) и шум полосы страницы (колонтитул,
    номер страницы). Любая содержательная строка — это текст, и слияние
    таблиц с его удалением запрещено.
    """
    for raw_line in between_lines:
        line = raw_line.strip()
        if not line or _TABLE_ID_MARKER_RE.match(line) or _PAGE_NOISE_RE.match(line):
            continue
        bare = line.strip("*").strip()
        if (_CONTINUATION_MARKER_RE.match(bare)
                or _CAPTION_START_RE.match(_normalize_spaced_text(bare))):
            continue
        return False
    return True


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


def _row_norm_sig(line: str) -> tuple[str, ...]:
    """Нормализованная сигнатура строки таблицы для common-prefix среза."""
    if not line.strip().startswith("|"):
        return ()
    return tuple(
        re.sub(r"\\s+", " ", cell).strip().lower()
        for cell in line.strip().strip("|").split("|")
        if cell.strip()
    )


def _trim_repeated_header_prefix(previous: list[str], incoming: list[str]) -> list[str]:
    """Удалить только повторяющийся ведущий префикс, сохранив строки данных."""
    count = 0
    while count < len(previous) and count < len(incoming):
        previous_sig = _row_norm_sig(previous[count])
        incoming_sig = _row_norm_sig(incoming[count])
        if not previous_sig or previous_sig != incoming_sig:
            break
        count += 1
    return incoming[count:]


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
        absorbed_markers: list[str] = []

        j = i + 1
        while j < len(tables):
            n_start, n_end = tables[j]
            between = "\n".join(lines[t_end:n_start]).strip()
            between_lines = [l for l in between.split("\n") if l.strip()]
            # Слияние допустимо только когда между таблицами нет содержательного
            # текста (дефект: подстроки «окончание/продолжение» в обычной прозе
            # между таблицами считались маркером продолжения, и текст между
            # таблицами молча удалялся).
            service_only = _between_is_service_only(between_lines)
            has_continuation = service_only and any(
                _CONTINUATION_MARKER_RE.match(bl.strip().strip("*").strip())
                for bl in between_lines
            )

            next_lines = lines[n_start:n_end]
            next_header = _table_header(next_lines)

            next_sep = -1
            for li, line in enumerate(next_lines):
                if ":--" in line or "---" in line:
                    next_sep = li
                    break

            same_caption = _same_table_caption(lines, t_start, n_start)

            can_merge = service_only and (
                has_continuation or (header and header == next_header and same_caption)
            )

            if not can_merge:
                break

            absorbed_markers.extend(
                line.strip() for line in between_lines
                if _TABLE_ID_MARKER_RE.match(line.strip())
            )

            next_data = next_lines[next_sep + 1:] if next_sep >= 0 else next_lines
            # При реальном слиянии удаляем только общий префикс повторной шапки;
            # одинаковые строки данных из разных rowspan-групп сохраняем.
            next_data = _trim_repeated_header_prefix(current_data, next_data)
            current_data.extend(next_data)
            t_end = n_end
            prev_end = t_end
            j += 1

        header_part = table_lines[:sep_idx + 1]
        merged_lines = list(header_part)
        if absorbed_markers:
            marker_positions = [
                index for index, line in enumerate(result)
                if _TABLE_ID_MARKER_RE.match(line.strip())
            ]
            insert_at = marker_positions[-1] + 1 if marker_positions else len(result)
            result[insert_at:insert_at] = absorbed_markers
        # Данные могут быть байт-идентичны в разных rowspan-группах — это
        # легитимные строки, поэтому глобальная дедупликация запрещена.
        merged_lines.extend(current_data)

        result.extend(merged_lines)
        i = j

    result.extend(lines[prev_end:])
    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Постобработка: изображения и подписи
# ═══════════════════════════════════════════════════════════════════════════

def _fig_num(p: Path) -> int:
    """Числовой номер из имени fig_N (fig_10 → 10); 0 для не-fig имён."""
    m = re.search(r"fig_(\d+)", p.name)
    return int(m.group(1)) if m else 0


def _fig_sort_key(p: Path) -> tuple[int, int, str]:
    """Ключ сортировки для rename_images: fig_N по номеру, не-fig после.

    Возвращает (группа, номер, имя):
      - fig_N файлы: (0, N, name) — числовой порядок (fig_2 перед fig_10);
      - не-fig (хэш-имена от старого экстрактора): (1, 0, name) — после
        всех fig_N, детерминированно по имени, не затирая fig_N.
    """
    n = _fig_num(p)
    return (0 if n else 1, n, p.name)


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
    # БАГ: алфавитная сортировка ставила "fig_10" перед "fig_2" — fig_10
    # переименовывался в fig_2 и затирал оригинал (из 14 файлов оставалось 6).
    # Сортируем ПО ЧИСЛОВОМУ номеру fig_ (см. _fig_sort_key): при полном
    # извлечении fig_1..fig_14 переименований нет вовсе, при выпадении
    # вырезок (fig_1, fig_3, ...) номера схлопываются без перезаписи.
    img_files = sorted(
        (p for p in img_dir.glob("*")
         if p.is_file() and not p.name.startswith("table_")),
        key=_fig_sort_key,
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
        # Обновляем alt-текст для ЛЮБОГО значения в [...], а не только
        # литерального "image": _insert_images_into_md пишет ![fig_N](...),
        # HTML-таблицы дают ![image](...) — оба должны стать ![Рисунок N].
        md_text = re.sub(
            rf'!\[[^\]]*\]\(image/{re.escape(new_name)}\)',
            f"![Рисунок {fig_num}](image/{new_name})",
            md_text,
        )

    # Нормализация alt-текста для НЕпереименованных fig_N (имя файла уже
    # совпадало с целевым, rename_map их не содержит): ![fig_N](image/fig_N.ext)
    # → ![Рисунок N](image/fig_N.ext). Иначе в MD остаётся смесь ![fig_N] и
    # ![Рисунок N] (приёмка: все ссылки вида ![Рисунок N](image/fig_N.png)).
    md_text = re.sub(
        r"!\[fig_(\d+)\]\(image/fig_\1(\.[a-zA-Z0-9]+)\)",
        r"![Рисунок \1](image/fig_\1\2)",
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

    Правило (как в AI-промпте create_markdown_config.yaml, но без --ai):
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

def _table_id_sort_key(tid: str) -> tuple[int, int]:
    """Ключ сортировки ID-маркеров таблиц в порядке t_p1_0, t_p1_1, t_p2_0, ...

    Парсит ID вида t_p{page}_{index} и возвращает (page, index).
    Неизвестный формат сортируется первым ((0, 0)).
    """
    m = re.match(r"t_p(\d+)_(\d+)", tid)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def _merge_by_component_images(md_text: str, table_images: list[dict] | None) -> str:
    """Склеить части многостраничных таблиц по ``component_images``.

    ID-маркеры ставятся непосредственно перед подписью таблицы, поэтому их
    можно использовать как устойчивые якоря даже если продолжение начинается
    с обычной строки данных и не содержит строки-разделителя Markdown.
    Таблицы с одной (или отсутствующей) компонентой намеренно не меняются.
    """
    if not md_text or not table_images:
        return md_text

    components_by_id = {
        item.get("id"): tuple(item.get("component_images") or ())
        for item in table_images
        if item.get("id")
    }
    components_by_id = {
        marker_id: components
        for marker_id, components in components_by_id.items()
        if len(components) >= 2
    }
    if not components_by_id:
        return md_text

    lines = md_text.split("\n")
    marker_re = re.compile(r"^\s*<!--\s*(t_p\d+_\d+)\s*-->\s*$")

    def is_row(line: str) -> bool:
        stripped = line.strip()
        return stripped.startswith("|") and "|" in stripped[1:]

    blocks: list[tuple[str, int, int, int]] = []
    for index, line in enumerate(lines):
        match = marker_re.match(line)
        if not match or match.group(1) not in components_by_id:
            continue
        row_start = index + 1
        # The caption/name is between the marker and the first table row.
        while row_start < len(lines) and not lines[row_start].strip():
            row_start += 1
        if row_start < len(lines) and not is_row(lines[row_start]):
            row_start += 1
            while row_start < len(lines) and not lines[row_start].strip():
                row_start += 1
        if row_start >= len(lines) or not is_row(lines[row_start]):
            continue
        row_end = row_start
        while row_end < len(lines) and is_row(lines[row_end]):
            row_end += 1
        blocks.append((match.group(1), index, row_start, row_end))

    groups: dict[tuple[str, ...], list[tuple[str, int, int, int]]] = {}
    for block in blocks:
        groups.setdefault(components_by_id[block[0]], []).append(block)
    merge_groups = [parts for parts in groups.values() if len(parts) >= 2]
    if not merge_groups:
        return md_text

    replacements: list[tuple[int, int, list[str]]] = []
    for parts in merge_groups:
        parts.sort(key=lambda part: part[1])
        first_id, first_marker, first_row, first_end = parts[0]
        first_lines = lines[first_marker:first_end]
        separator_index = next(
            (i for i, line in enumerate(first_lines)
             if ":--" in line or "---" in line),
            None,
        )
        if separator_index is None:
            continue
        merged = list(first_lines)
        first_data = first_lines[separator_index + 1:]
        for _, _, row_start, row_end in parts[1:]:
            continuation_rows = lines[row_start:row_end]
            continuation_separator = next(
                (i for i, row in enumerate(continuation_rows)
                 if ":--" in row or "---" in row),
                None,
            )
            if continuation_separator is not None:
                continuation_rows = continuation_rows[continuation_separator + 1:]
            continuation_rows = _trim_repeated_header_prefix(first_data, continuation_rows)
            merged.extend(continuation_rows)
            first_data = continuation_rows
        # Переносим ID каждого компонента в стек перед подписью итоговой таблицы.
        component_markers = [f"<!-- {part[0]} -->" for part in parts[1:]]
        if component_markers:
            insertion = 1 if merged and marker_re.match(merged[0]) else 0
            merged[insertion:insertion] = component_markers
        replacements.append((first_marker, first_end, merged))
        for _, marker, _, end in parts[1:]:
            replacements.append((marker, end, []))

    if not replacements:
        return md_text
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = replacement
    return "\n".join(lines)


def merge_tables_by_model(md_text: str, doc: Document) -> str:
    """Merge structured table blocks using global ``Table.md_lines`` ranges."""
    if not md_text or not doc:
        return md_text
    lines = md_text.split("\n")
    groups: dict[int, list[Table]] = {}
    for table in doc.tables:
        table_range = table.md_lines
        if table.stitch_group_id is not None and table_range is not None and table_range[1] > table_range[0]:
            groups.setdefault(table.stitch_group_id, []).append(table)

    def row(line: str) -> bool:
        value = line.strip()
        return value.startswith("|") and "|" in value[1:]

    def separator(line: str) -> bool:
        return row(line) and "---" in line

    replacements: list[tuple[int, int, list[str]]] = []
    for tables in groups.values():
        if len(tables) < 2:
            continue
        tables.sort(key=lambda table: table.md_lines[0] if table.md_lines else 0)
        first_range = tables[0].md_lines
        if first_range is None:
            continue
        start, end = first_range
        first = lines[start:end]
        sep = next((idx for idx, line in enumerate(first) if separator(line)), None)
        if sep is None:
            continue
        merged = first[:sep + 1] + first[sep + 1:]
        seen = {line.strip() for line in first[sep + 1:] if row(line)}
        for table in tables[1:]:
            table_range = table.md_lines
            if table_range is None:
                continue
            part = lines[table_range[0]:table_range[1]]
            part_sep = next((idx for idx, line in enumerate(part) if separator(line)), None)
            for line in part[part_sep + 1:] if part_sep is not None else part:
                if row(line) and line.strip() in seen:
                    continue
                if row(line):
                    seen.add(line.strip())
                merged.append(line)
        replacements.append((start, end, merged))
        replacements.extend(
            (table.md_lines[0], table.md_lines[1], [])
            for table in tables[1:] if table.md_lines is not None
        )
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = replacement
    return "\n".join(lines)


def run_script_postprocess(
    md_text: str,
    img_dir: str | Path,
    table_images: list[dict] | None = None,
    doc: Document | None = None,
) -> str:
    """Выполнить всю скриптовую постобработку.

    Порядок (без wrap_equations — в Yandex формулы уже в $$):
      1. HTML-таблицы → MD
      2. Объединение смежных таблиц
      2c. Склейка по component_images (если переданы table_images)
      3. LaTeX-чистка
      4. Переименование изображений
      5. Подписи → курсив
      6. Примечания → цитаты
      7. Подписи "Таблица N", "Рис. N"
      8. OCR-артефакты
      9. Пробелы вокруг ^ в LaTeX-формулах

    ID-маркеры <!-- t_pN_M --> рождаются в parse_yandex_json_to_md() и уже
    присутствуют в md_text; здесь они не пересчитываются (контракт
    table-id-marker §5.5: шаг 2b удалён, page_boundaries не нужны).

    Args:
        md_text: Markdown-текст.
        img_dir: Папка с изображениями.
        table_images: Список от extract_table_images() — включает id каждой
            вырезанной таблицы. Склейка по component_images выполняется
            только при наличии списка (иначе пайплайн ведёт себя как раньше).
    """
    log.info("Скриптовая постобработка:")

    md_text, n_tables = convert_html_tables(md_text)
    log.info(f"  1. HTML->MD таблиц: {n_tables}")

    md_text = merge_tables(md_text)
    log.info("  2. Таблицы: объединение")

    if doc is not None:
        md_text = merge_tables_by_model(md_text, doc)
        log.info("  2c. Таблицы: структурная склейка")
    elif table_images:
        md_text = _merge_by_component_images(md_text, table_images)
        log.info("  2c. Таблицы: склейка по component_images")

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


def _ai_result_or_original(result: str | None, chunk: str, context: str) -> str:
    """Гард полноты AI-ответа: при частичной потере вернуть исходный чанк.

    LLM на длинном входе может молча пропустить фрагмент текста. Пустой
    ответ уже обработан вызывающим кодом; здесь ловим ЧАСТИЧНУЮ потерю:
      - ответ заметно короче входного чанка (< AI_MIN_OUTPUT_RATIO);
      - из ответа исчезли ID-маркеры таблиц, присутствовавшие в чанке.
    В обоих случаях в итоговый Markdown уходит исходный чанк.
    """
    if not result:
        return chunk
    if len(result) < AI_MIN_OUTPUT_RATIO * len(chunk):
        log.error(
            "  ⚠ %s: AI-ответ короче входа (%d из %d символов) — оставляю исходный чанк",
            context, len(result), len(chunk),
        )
        return chunk
    chunk_ids = set(re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->", chunk))
    result_ids = set(re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->", result))
    lost = chunk_ids - result_ids
    if lost:
        log.error(
            "  ⚠ %s: AI-ответ потерял ID-маркеры таблиц (%s) — оставляю исходный чанк",
            context, ", ".join(sorted(lost, key=_table_id_sort_key)),
        )
        return chunk
    return result


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
                if resp.status_code in (400, 401, 403):
                    log.warning(f"  {prv}/{mdl}: HTTP {resp.status_code}, primary недоступен — переход к fallback")
                    return None
                if resp.status_code == 503:
                    log.warning(f"  {prv}/{mdl}: 503, попытка {attempt + 1}/3")
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    log.warning(
                        f"  {prv}/{mdl}: ответ усечён (finish_reason=length) — "
                        f"считаю неудачей, попытка {attempt + 1}/3"
                    )
                    time.sleep(5)
                    continue
                content = choice["message"].get("content") or ""
                content = re.sub(r"^```(?:markdown)?\s*\n?", "", content, flags=re.MULTILINE)
                content = re.sub(r"\n```\s*$", "", content, flags=re.MULTILINE)
                if _is_recognition_failure(content):
                    log.warning(f"  {prv}/{mdl}: модель сообщила о невозможности распознать — переход к fallback")
                    return None
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


def load_vision_tables_from_model(doc: Document, tmp_dir: str | Path) -> list[dict]:
    """Загрузить vision-эталоны и связать их со структурными таблицами.

    Имя PNG, записанное в ``Table.image_path``, является стабильным ключом
    для OCR-результата ``table_N.md``. Отсутствующие картинки или результаты
    Vision не считаются ошибкой: соответствующая таблица просто пропускается.
    """
    tmp_path = Path(tmp_dir)
    result: list[dict] = []
    for table in doc.tables:
        if not table.image_path:
            continue
        crop_path = tmp_path / f"{Path(table.image_path).stem}.md"
        if not crop_path.is_file():
            continue
        try:
            markdown = crop_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Не удалось прочитать vision-таблицу %s: %s", crop_path, exc)
            continue
        result.append({
            "caption": table.caption,
            "table_num": table.table_num,
            "markdown": markdown,
        })
    return result


def ai_postprocess_json_native(
    md_text: str,
    config: dict,
    file_label: str = "",
    vision_tables: list[dict] | None = None,
) -> str:
    """AI-постобработка JSON-native Markdown без ID-маркеров.

    Эталонные таблицы добавляются только в те чанки, где встречается их
    подпись или номер. Это сохраняет старый API AI и не загрязняет unrelated
    chunks большими Vision-ответами.
    """
    vision_tables = vision_tables or []
    vision_by_caption = {
        value["caption"]: value["markdown"]
        for value in vision_tables
        if value.get("caption") and value.get("markdown")
    }
    vision_by_num = {
        str(value["table_num"]): value["markdown"]
        for value in vision_tables
        if value.get("table_num") and value.get("markdown")
    }
    chunks = _chunk_text(md_text, max_chars=AI_MAX_CHARS)
    results: list[str] = []
    for index, chunk in enumerate(chunks):
        relevant: list[str] = []
        for caption, markdown in vision_by_caption.items():
            if caption in chunk and markdown not in relevant:
                relevant.append(markdown)
        for number, markdown in vision_by_num.items():
            pattern = rf"(?:Таблица|Table)\s+{re.escape(number)}(?:\b|\.)"
            if re.search(pattern, chunk, flags=re.IGNORECASE) and markdown not in relevant:
                relevant.append(markdown)
        prompt = chunk
        if relevant:
            prompt = (
                "=== Markdown-файл ===\n"
                f"{chunk}\n\n"
                "=== Эталонные таблицы ===\n"
                + "\n\n".join(relevant)
            )
        response = _call_ai_api(prompt, config, f"{file_label} [ч.{index + 1}]")
        results.append(response if response is not None else chunk)
    return "\n\n".join(results)


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
# regexp-паттерна (паттерны в конфиге, секция references, захватывают только номер).
_REF_LABEL_HINTS: list[tuple[str, str]] = [
    ("табл", "табл."),
    ("пункт", "п."),
    ("разд", "разд."),
    ("гл", "гл."),
]


def _derive_reg_path(input_path: str | Path, output_dir: str | Path | None = None) -> Path:
    """Вернуть per-document конфиг рядом с исходным/итоговым Markdown."""
    path = Path(input_path)
    directory = Path(output_dir) if output_dir is not None else path.parent
    return directory / f"{path.stem}_reg.yaml"


def load_rag_config(
    config_path: str | Path,
    reg_path: str | Path | None = None,
) -> dict:
    """Загрузить глобальный RAG-конфиг и наложить per-document запись."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"rag_config не найден: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    if reg_path and Path(reg_path).exists():
        with open(reg_path, encoding="utf-8") as f:
            per_doc = yaml.safe_load(f) or {}
        documents = per_doc.get("documents", {})
        config.setdefault("documents", {}).update(documents)

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
        (только в пределах текущей главы)
      - clause:  свой номер для ####/#####, иначе ближайший предыдущий ####/#####
        (только в пределах текущей главы)

    Новый top-level chapter (##) всегда сбрасывает section/clause в None:
    нельзя наследовать ###/#### из предыдущей главы (регрессия: повторный
    top-level «## 1/2/3» получал чужой section, напр. «4.7»).

    Returns:
        {'chapter': '3', 'section': '3.2', 'clause': '3.2.1'}.
        Поля, для которых предок не найден, равны None.
    """
    current = headings[idx]
    level = current.get("level", 0)

    chapter = section = clause = None

    if level == 2:
        # Новый top-level chapter: section/clause сбрасываются — предыдущие
        # ###/#### относятся к старой главе и не могут быть предками.
        return {"chapter": current.get("number"), "section": None, "clause": None}

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
            # Section берётся только из текущей главы: не выходим за ##
            if headings[j].get("level") == 2:
                break

    if level >= 4:
        clause = current.get("number")
        if clause is None:
            # Ненумерованный подпункт — наследуем от предыдущего ####/#####
            # только в пределах текущей главы: не выходим за ##
            for j in range(idx - 1, -1, -1):
                if headings[j].get("level") == 2:
                    break
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
      - section: текст ближайшего ### (только в пределах текущей главы)
      - clause:  текст текущего ####/##### (или ближайшего предыдущего,
        только в пределах текущей главы)

    Новый top-level chapter (##) всегда сбрасывает section/clause в None.

    Returns:
        {'chapter': '3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ', ...}.
        Поля, для которых предок не найден, равны None.
    """
    current = headings[idx]
    level = current.get("level", 0)

    chapter_text = section_text = clause_text = None

    if level == 2:
        # Новый top-level chapter: section/clause сбрасываются.
        return {"chapter": current.get("heading_text"), "section": None, "clause": None}

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
            # Section берётся только из текущей главы: не выходим за ##
            if headings[j].get("level") == 2:
                break

    if level >= 4:
        clause_text = current.get("heading_text")
        if clause_text is None or current.get("number") is None:
            # Ненумерованный подпункт (или заголовок без текста) — наследуем
            # текст от предыдущего ####/#####, но только в пределах текущей
            # главы: не выходим за ## (аналог ограничения для section).
            clause_text = None
            for j in range(idx - 1, -1, -1):
                if headings[j].get("level") == 2:
                    break
                if (headings[j].get("level") or 0) >= 4:
                    clause_text = headings[j].get("heading_text")
                    break

    return {"chapter": chapter_text, "section": section_text, "clause": clause_text}


# Порядок заголовков для embedding_text (детерминированный).
_EMBEDDING_HEADING_LABELS = (
    ("chapter", "Заголовок главы"),
    ("section", "Заголовок раздела"),
    ("clause", "Заголовок пункта"),
)


def _build_embedding_text(
    heading_texts: dict[str, str | None] | None,
    text: str,
) -> str:
    """Построить embedding_text: заголовки chapter/section/clause + исходный текст.

    Формат (детерминированный и читаемый):
        Заголовок главы: {chapter}
        Заголовок раздела: {section}
        Заголовок пункта: {clause}

        {text}

    Пустые/None заголовки не добавляются; если заголовков нет — возвращается
    только исходный текст. Публичное поле text не изменяется.
    """
    parts: list[str] = []
    if heading_texts:
        for key, label in _EMBEDDING_HEADING_LABELS:
            value = heading_texts.get(key)
            if value:
                parts.append(f"{label}: {value}")
    if not parts:
        return text
    return "\n".join(parts) + "\n\n" + text


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
    """Построить JSONL для RAG v2, исключив служебные Markdown-маркеры."""
    md_text_for_rag = _TABLE_ID_MARKER_RE.sub("", md_text)
    md_text_for_rag = _WARN_MARKER_RE.sub("", md_text_for_rag)

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

    status = doc_meta.get("status")
    if status is None:
        status = defaults.get("default_status", "active")
        log.warning(f"  {doc_key}: status не указан — использую default_status='{status}'")
    status_reason = doc_meta.get("status_reason")
    replaced_by_document_id = doc_meta.get("replaced_by_document_id")
    replaced_by_doc_key = doc_meta.get("replaced_by_doc_key")

    ignore_sections = doc_meta.get("ignore_sections", []) or []
    clauses = parse_md_structure(md_text_for_rag)
    active_clauses: list[dict] = []
    skip_until_level: int | None = None
    for clause in clauses:
        level = clause["level"]
        heading_text = clause["heading_text"]
        if skip_until_level is not None:
            if level > skip_until_level:
                continue
            skip_until_level = None
        if any(s and s.lower() in heading_text.lower() for s in ignore_sections):
            skip_until_level = level
            continue
        active_clauses.append(clause)

    source_file = doc_meta.get("source_file")
    json_lines: list[str] = []
    occurrence_counts: dict[str, int] = {}

    for i, clause in enumerate(active_clauses):
        text = extract_clause_text(
            md_text_for_rag, clause["line_num"], clause["next_line_num"]
        )
        if not text:
            continue

        ancestors = _build_ancestors(active_clauses, i)
        heading_texts = _build_heading_texts(active_clauses, i)
        section_path = _build_section_path(ancestors)
        page = _get_page_for_heading(clause.get("number"), json_headings)
        refs = extract_references(text, patterns) if patterns else []

        # Уникальность chunk_id в пределах результата: первый вхождения
        # сохраняет базовый ID, повторы получают детерминированный
        # occurrence-суффикс ДО /part_{N} для oversized чанков.
        raw_chunk_id = _make_chunk_id(doc_key, active_clauses, i)
        count = occurrence_counts.get(raw_chunk_id, 0)
        if count == 0:
            base_chunk_id = raw_chunk_id
        else:
            base_chunk_id = f"{raw_chunk_id}/occurrence_{count + 1}"
        occurrence_counts[raw_chunk_id] = count + 1

        chunk_tokens = tokenize(text)
        embedding_text = _build_embedding_text(heading_texts, text)
        embedding_tokens = tokenize(embedding_text)

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
            "embedding_text": embedding_text,
            "embedding_tokens": embedding_tokens,
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
                rec["embedding_text"] = _build_embedding_text(heading_texts, part)
                rec["embedding_tokens"] = tokenize(rec["embedding_text"])
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
    """Сгенерировать {file_stem}_chunks.jsonl + {file_stem}_assets.json (--rag).

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
            file_stem,
        )
    except Exception as e:
        log.error(f"  Ошибка RAG-генерации: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 12b. --reg: интерактивная регистрация документа в per-document <stem>_reg.yaml
# Контракт: docs/architecture/rag-register-flag.md
# ═══════════════════════════════════════════════════════════════════════════

_REG_HEAD_LINES = 50
_REG_FIELD_ORDER = (
    "document_id", "document_id_alt", "document_type", "domain", "title",
    "edition", "date_enacted", "date_amended", "amended_by", "source_file",
    "status", "status_reason", "replaced_by_document_id", "replaced_by_doc_key",
    "ignore_sections",
)
# Порядок диалога §3: те же поля без source_file (авто) и без inactive-блока
_REG_DIALOG_ORDER = (
    "document_id", "document_id_alt", "document_type", "domain", "title",
    "edition", "date_enacted", "date_amended", "amended_by",
)
# «Полностью определена» (§5.3) — эти поля обязаны быть не-None
_REG_COMPLETENESS_FIELDS = (
    "document_id", "title", "document_type", "domain", "edition",
    "date_enacted", "source_file",
)
_REG_VALID_TYPES = ("ГОСТ", "СП", "СО", "СНиП", "ПУЭ")
_REG_PREFIX_MAP = {"ГОСТ": "GOST", "СП": "SP", "СО": "SO", "СНиП": "SNIP", "ПУЭ": "PUE"}
_REG_DEFAULT_IGNORE = ["Предисловие", "Содержание"]
_REG_SLUG_RE = re.compile(r"^[A-Za-z0-9_]+$")
# Обозначение: префикс + первая группа цифр (тире любые из -—–−)
_REG_DOC_ID_RE = re.compile(r"(ГОСТ\s*Р?\s*№?|СП|СО|СНиП|ПУЭ)\s*№?\s*(\d[\d.\-—–−]*)")
# Транслитерация кириллицы для хвоста slug (§5.1 п.3): «Молниезащита»→molniezashita
_REG_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
# Спецификации полей диалога: auto — есть авто-слой (подтверждение), иначе B-промпт
_REG_FIELD_SPECS = {
    "document_id": {"auto": True, "comment": "не определено"},
    "document_id_alt": {"comment": "старое обозначение (напр. СНиП II-35-76)"},
    "document_type": {"auto": True, "comment": "ГОСТ, СП, ПУЭ, СО, СНиП"},
    "domain": {"comment": "семантика документа (напр. «Кабели»)"},
    "title": {"auto": True, "comment": "заголовок документа (напр. Кабели силовые)"},
    "edition": {"auto": True, "comment": "год издания, напр. 2016"},
    "date_enacted": {
        "comment": "дата ввода в действие, формат ГГГГ-ММ-ДД (напр. 2017-06-17)",
        "date": True,
    },
    "date_amended": {"comment": "дата последних изменений, ГГГГ-ММ-ДД", "date": True},
    "amended_by": {"comment": "чем изменён (напр. Приказ Минстроя РФ № 295/пр от 18.05.2021)"},
}


def run_registration(
    input_path: str | Path,
    md_text: str,
    rag_config_path: str | Path,
    rag_config: dict,
    ai_config: dict | None,
) -> dict | None:
    """Оркестратор --reg: TTY-проверка → идемпотентность → извлечение
    (regex+LLM) → пошаговый диалог → append/null-fill + гейт + safe_write.

    Контракт: docs/architecture/rag-register-flag.md.

    Returns:
        Обновлённый dict конфига (успех: новая запись / дополнение / skip)
        или None (отказ/отмена/не-TTY).
    """
    # §8: без TTY — понятная ошибка, конфиг не мутируется
    if not sys.stdin.isatty():
        log.error(
            "--reg требует интерактивного терминала (TTY); конфиг не изменён. "
            "Запустите в терминале или добавьте запись в <stem>_reg.yaml вручную"
        )
        return None

    documents = rag_config.setdefault("documents", {})
    source_file = Path(input_path).name

    # §5.3: идемпотентность через _find_doc_key
    doc_key = _find_doc_key(input_path, rag_config)
    if doc_key is not None:
        record = documents.get(doc_key, {}) or {}
        if _reg_record_complete(record):
            log.info(f"Документ уже зарегистрирован: {doc_key} — регистрация пропущена")
            return rag_config
        log.info(f"Документ найден: {doc_key} — дополнение недостающих полей")
        return _reg_fill_existing(
            md_text, rag_config_path, rag_config, doc_key, record, ai_config, source_file,
        )

    return _reg_create_new(md_text, rag_config_path, rag_config, ai_config, source_file)


def _reg_record_complete(record: dict) -> bool:
    """§5.3: запись «полностью определена» — все ключевые поля не-None."""
    return all(record.get(k) is not None for k in _REG_COMPLETENESS_FIELDS)


def _reg_create_new(
    md_text: str,
    rag_config_path: str | Path,
    rag_config: dict,
    ai_config: dict | None,
    source_file: str,
) -> dict | None:
    """Новая запись: извлечение → диалог → гейт → append + safe_write."""
    documents = rag_config.setdefault("documents", {})
    head = _reg_head(md_text)
    extracted = _reg_extract_fields(head, ai_config)
    existing_slugs = set(documents.keys())

    result = _reg_interactive_fill(extracted, source_file, existing_slugs, None, None)
    if result is None:
        return None
    slug, fields, _filled = result

    block = _reg_render_record_block(slug, fields)
    print("── Запись будет добавлена в per-document <stem>_reg.yaml: ──")
    print(block, end="")
    ans = _reg_ask_write()
    if ans is None:
        return None
    if not ans:
        print("Регистрация отменена, конфиг не изменён")
        return None

    try:
        config_file = Path(rag_config_path)
        text = config_file.read_text(encoding="utf-8") if config_file.exists() else "documents:\n"
    except Exception as e:
        log.error(f"Не удалось прочитать {rag_config_path}: {e}")
        return None
    new_text = _reg_append_record(text, slug, fields)
    if new_text is None:
        return None
    safe_write(rag_config_path, new_text)
    log.info(f"Документ зарегистрирован: {slug} → {rag_config_path}")
    documents[slug] = fields
    return rag_config


def _reg_fill_existing(
    md_text: str,
    rag_config_path: str | Path,
    rag_config: dict,
    doc_key: str,
    record: dict,
    ai_config: dict | None,
    source_file: str,
) -> dict | None:
    """§5.3 режим дополнения: спрашиваем только недостающие (None) поля."""
    documents = rag_config.setdefault("documents", {})
    head = _reg_head(md_text)
    extracted = _reg_extract_fields(head, ai_config)

    result = _reg_interactive_fill(
        extracted, source_file, set(documents.keys()), record, doc_key,
    )
    if result is None:
        return None
    slug, fields, filled = result
    if not filled:
        print("Нет недостающих полей — запись не изменена")
        return rag_config

    block = _reg_render_record_block(slug, fields)
    print("── Запись будет дополнена в per-document <stem>_reg.yaml: ──")
    print(block, end="")
    ans = _reg_ask_write()
    if ans is None:
        return None
    if not ans:
        print("Регистрация отменена, конфиг не изменён")
        return None

    config_path = Path(rag_config_path)
    if config_path.exists():
        try:
            text = config_path.read_text(encoding="utf-8")
        except Exception as e:
            log.error(f"Не удалось прочитать {rag_config_path}: {e}")
            return None
        new_text = _reg_fill_nulls(text, doc_key, filled)
    else:
        # A legacy record may exist only in the global config.  Materialize the
        # complete merged record in the new per-document overlay; writing only
        # ``filled`` would discard legacy fields when overlays are merged.
        new_text = _reg_append_record("documents:\n", doc_key, fields)
    if new_text is None:
        return None
    safe_write(rag_config_path, new_text)
    log.info(f"Запись дополнена: {doc_key} → {rag_config_path}")
    documents[doc_key] = fields
    return rag_config


def _reg_head(md_text: str) -> str:
    """Первые 50 строк финального .md (§4.3 — титульный лист)."""
    lines = (md_text or "").splitlines()
    return "\n".join(lines[:_REG_HEAD_LINES])


def _reg_extract_fields(md_head: str, ai_config: dict | None) -> dict:
    """Двухуровневое извлечение (§4): regex-слой всегда + LLM-слой опционально.

    Returns:
        {document_id, title, document_type, edition, domain_hint}
    """
    result: dict = {
        "document_id": None, "title": None,
        "document_type": None, "edition": None, "domain_hint": None,
    }

    # ── regex-слой (§4.1) ──
    m = _REG_DOC_ID_RE.search(md_head or "")
    if m:
        doc_id = re.sub(r"\s+", " ", m.group(0)).strip()
        result["document_id"] = doc_id
        prefix = m.group(1).replace("Р", "").replace("№", "").strip()
        if prefix in _REG_VALID_TYPES:
            result["document_type"] = prefix

    # title: первая строка ^#{1,2}, иначе самая длинная КАПС-строка (≥4 слов)
    m = re.search(r"^#{1,2}\s+(.+)", md_head or "", re.MULTILINE)
    if m:
        result["title"] = m.group(1).strip()
    else:
        best = None
        for line in (md_head or "").splitlines():
            line = line.strip()
            if not line:
                continue
            caps_words = re.findall(r"[А-ЯЁ]{2,}", line)
            if len(caps_words) >= 4 and (best is None or len(line) > len(best)):
                best = line
        result["title"] = best

    # ── LLM-слой (§4.2), опционально ──
    # Провайдер/модель/fallback берём из ai_postprocess (единый источник),
    # prompt — из reg_extract. В конфиге reg_extract содержит только prompt.
    reg_prompt = None
    base_ai = None
    if ai_config:
        reg_prompt = (ai_config.get("reg_extract") or {}).get("prompt")
        base_ai = ai_config.get("ai_postprocess") or {}
    if reg_prompt and base_ai:
        reg_cfg = dict(base_ai)
        reg_cfg["prompt"] = reg_prompt
        try:
            resp = _call_ai_api(md_head, reg_cfg)
        except Exception as e:
            log.warning(f"  --reg: ошибка LLM-извлечения: {e} — работаю на regex-слое")
            resp = None
        if resp:
            llm = _reg_parse_llm_json(resp)
            if llm is None:
                log.warning("  --reg: LLM вернул не-JSON — работаю на regex-слое")
            else:
                if llm.get("document_id"):
                    result["document_id"] = str(llm["document_id"]).strip()
                if llm.get("title"):
                    result["title"] = str(llm["title"]).strip()
                if llm.get("domain_hint"):
                    result["domain_hint"] = str(llm["domain_hint"]).strip()
    else:
        log.warning("  --reg: секция reg_extract отсутствует или пустой prompt — regex-слой")

    # Тип и год пересчитываем по ИТОГОВОМУ document_id (LLM мог его заменить)
    if result["document_id"]:
        result["document_type"] = _reg_type_from_id(result["document_id"])
        result["edition"] = _reg_edition_from_id(result["document_id"])
    return result


def _reg_type_from_id(document_id: str) -> str | None:
    """Префикс обозначения → нормализованный тип (ГОСТ/СП/СО/СНиП/ПУЭ)."""
    m = re.match(r"(ГОСТ|СП|СО|СНиП|ПУЭ)", document_id.strip())
    if not m:
        return None
    prefix = m.group(1)
    return "ГОСТ" if prefix == "ГОСТ" else prefix


def _reg_parse_llm_json(resp: str) -> dict | None:
    """Первый {...} в ответе LLM → dict; отказ → None."""
    m = re.search(r"\{.*\}", resp, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _reg_edition_from_id(document_id: str | None) -> str | None:
    """§5.2: год из обозначения. 4 цифры — как есть; 2 — век (≥50 → 19xx)."""
    if not document_id:
        return None
    groups = re.findall(r"\d+", document_id)
    if not groups:
        return None
    last = groups[-1]
    if len(last) == 4:
        return last
    if len(last) == 2:
        n = int(last)
        return f"19{n}" if n >= 50 else f"20{n}"
    return None


def _reg_translit(word: str) -> str:
    """Кириллица → латиница (нижний регистр); небуквенный мусор вырезаем.

    §5.1 п.3: пример контракта `Кабели`→`kabel` (и существующие ключи
    `GOST_18410_kabel`/`GOST_31996_kabel`) — отсекаем конечную «и» у
    транслитерированного слова, если перед ней согласная (это даёт основу
    слова: кабели→кабел). «Отопление»→otoplenie и «Котельные»→kotelnye
    не затрагиваются (оканчиваются на -е).
    """
    out: list[str] = []
    for ch in word.lower():
        if ch in _REG_TRANSLIT:
            out.append(_REG_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
    s = "".join(out)
    if len(s) > 1 and s.endswith("i") and s[-2].isalpha() and s[-2] not in "aeiouy":
        s = s[:-1]
    return s


def _reg_make_slug(
    document_id: str,
    document_type: str | None,
    domain: str | None,
    existing: set[str],
) -> str:
    """§5.1: {PREFIX}[_number][_domain_word]; коллизии → _2, _3, …

    Префикс — ВЕРХНИЙ регистр (GOST/SP/SO/SNIP/PUE), по нормативному правилу
    §5.1 п.4 и существующим ключам (GOST_18410_kabel).
    """
    prefix = "doc"
    if document_type:
        for k, v in _REG_PREFIX_MAP.items():
            if k.lower() == document_type.strip().lower():
                prefix = v
                break
    m = re.search(r"\d+", document_id or "")
    number = m.group(0) if m else ""
    tail = ""
    if domain:
        first_word = re.split(r"\s+", domain.strip())[0]
        tail = _reg_translit(first_word)

    parts = [prefix]
    if number:
        parts.append(number)
    if tail:
        parts.append(tail)
    base = "_".join(parts)
    slug = base
    n = 2
    while slug in existing:
        slug = f"{base}_{n}"
        n += 1
    return slug


def _reg_valid_date(s: str) -> bool:
    try:
        datetime.datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _reg_escape_yaml(s: str) -> str:
    """Экранирование строкового значения для YAML (§6.3): \\ и " экранируются,
    контрол-символы вырезаются."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return "".join(ch for ch in s if ord(ch) >= 32)


def _reg_ask_write() -> bool | None:
    """Финальный гейт «Записать? [Y/n]». True — да, False — нет, None — отмена."""
    try:
        ans = input("Записать? [Y/n]: ")
    except (EOFError, KeyboardInterrupt):
        return None
    return ans.strip().lower() in ("", "y", "yes")


def _reg_interactive_fill(
    extracted: dict,
    source_file: str,
    existing_slugs: set[str],
    existing_record: dict | None,
    existing_slug: str | None,
) -> tuple[str, dict, dict] | None:
    """Пошаговый диалог §7. Возвращает (slug, fields, filled) или None (отмена)."""
    def ask(prompt_text: str) -> str | None:
        """None = EOF/Ctrl-C (отмена)."""
        try:
            return input(prompt_text)
        except (EOFError, KeyboardInterrupt):
            print()
            return None

    print("════ РЕГИСТРАЦИЯ ДОКУМЕНТА В per-document <stem>_reg.yaml ════")
    print(f"Файл: {source_file}")

    fields: dict = {}
    if existing_record:
        fields.update(existing_record)
    else:
        fields = {k: None for k in _REG_FIELD_ORDER}
        fields["ignore_sections"] = list(_REG_DEFAULT_IGNORE)
        fields["source_file"] = source_file
    if existing_slug:
        print(f"Дополнение записи: {existing_slug}")

    # ── Поля по порядку §3 ──
    for key in _REG_DIALOG_ORDER:
        # Режим дополнения: существующие значения не переспрашиваем
        if existing_record is not None and fields.get(key) is not None:
            continue
        spec = _REG_FIELD_SPECS[key]
        auto = extracted.get(key) if spec.get("auto") else None
        if auto:
            print(f"Определено: {auto}")
            ans = ask(f"{key} [Enter = подтвердить / или новое значение]: ")
            if ans is None:
                return None
            fields[key] = auto if ans.strip() == "" else ans.strip()
        else:
            print(f"{key} — {spec['comment']}")
            if key == "domain" and extracted.get("domain_hint"):
                print(f"Подсказка ИИ: {extracted['domain_hint']}")
            while True:
                ans = ask("[Enter = пропустить / или значение]: ")
                if ans is None:
                    return None
                val = ans.strip()
                if spec.get("date") and val and not _reg_valid_date(val):
                    print(f"Некорректная дата: {val} — формат ГГГГ-ММ-ДД")
                    continue
                fields[key] = val if val else None
                break

    # source_file — авто, без отдельного промпта (§3)
    if fields.get("source_file") is None:
        fields["source_file"] = source_file

    # status — пользователь, дефолт active
    if existing_record is None or fields.get("status") is None:
        while True:
            ans = ask("Статус документа [active]: ")
            if ans is None:
                return None
            val = ans.strip().lower()
            if val == "":
                fields["status"] = "active"
                break
            if val in ("active", "inactive"):
                fields["status"] = val
                break
            print("Допустимо: active или inactive")

    # inactive-поля (§3): обязательный status_reason + опциональные replaced_by_*
    if fields.get("status") == "inactive":
        if existing_record is None or fields.get("status_reason") is None:
            print("status_reason — причина недействования (напр. «Заменён на СП 60.13330.2012»)")
            while True:
                ans = ask("[обязательное поле]: ")
                if ans is None:
                    return None
                val = ans.strip()
                if val:
                    fields["status_reason"] = val
                    break
                print("Поле обязательно при статусе inactive")
        if existing_record is None or fields.get("replaced_by_document_id") is None:
            ans = ask(
                "replaced_by_document_id — официальный номер преемника "
                "(напр. СП 60.13330.2012), НЕ slug\n[Enter = пропустить / или значение]: "
            )
            if ans is None:
                return None
            fields["replaced_by_document_id"] = ans.strip() or None
        if existing_record is None or fields.get("replaced_by_doc_key") is None:
            ans = ask(
                "replaced_by_doc_key — ключ каталога-преемника (slug), если известен\n"
                "[Enter = пропустить / или значение]: "
            )
            if ans is None:
                return None
            fields["replaced_by_doc_key"] = ans.strip() or None
    elif existing_record is None:
        # active: инвариант validate_rag_config — поля inactive = null
        fields["status_reason"] = None
        fields["replaced_by_document_id"] = None
        fields["replaced_by_doc_key"] = None

    # ignore_sections — авто + подтверждение
    if existing_record is None or fields.get("ignore_sections") is None:
        default = list(_REG_DEFAULT_IGNORE)
        print(f"Определено: {', '.join(default)}")
        ans = ask("ignore_sections [Enter = подтвердить / или список через запятую]: ")
        if ans is None:
            return None
        if ans.strip():
            items = [x.strip() for x in ans.split(",") if x.strip()]
            fields["ignore_sections"] = items or default
        else:
            fields["ignore_sections"] = default

    # slug (§5.1) — только для новой записи; для дополнения ключ уже есть
    if existing_slug:
        slug = existing_slug
    else:
        candidate = _reg_make_slug(
            fields.get("document_id") or "",
            fields.get("document_type"),
            fields.get("domain"),
            set(existing_slugs),
        )
        while True:
            ans = ask(f"Ключ записи (slug): {candidate} — Enter = подтвердить или введите свой: ")
            if ans is None:
                return None
            val = ans.strip()
            if val == "":
                slug = candidate
                break
            if not _REG_SLUG_RE.match(val):
                print("Slug: допустимы только латиница, цифры и _")
                continue
            if val.lower() in {s.lower() for s in existing_slugs}:
                print(f"Ключ уже занят: {val} — выберите другой")
                continue
            slug = val
            break

    # Только что заполненные поля (для точечного null-fill и гейта)
    if existing_record is not None:
        filled = {
            k: v for k, v in fields.items()
            if existing_record.get(k) is None and v is not None
        }
    else:
        filled = dict(fields)

    return slug, fields, filled


def _reg_render_field_line(key: str, val) -> str:
    """Одна строка YAML для поля (или многострочный ignore_sections)."""
    if val is None:
        return f"    {key}: null"
    if key == "status":
        return f"    {key}: {val}"
    if key == "ignore_sections":
        if not val:
            return "    ignore_sections: []"
        parts = ["    ignore_sections:"]
        for item in val:
            parts.append(f'      - "{_reg_escape_yaml(str(item))}"')
        return "\n".join(parts)
    return f'    {key}: "{_reg_escape_yaml(str(val))}"'


def _reg_render_record_block(slug: str, fields: dict) -> str:
    """§6.3: текстовый блок записи точно в стиле существующих записей
    (2 пробела — slug, 4 — поля, двойные кавычки, null-литералы)."""
    doc_id = fields.get("document_id") or slug
    today = datetime.date.today().isoformat()
    lines = [""]
    lines.append(f"  # ── {doc_id} — добавлено --reg {today} ──")
    lines.append(f"  {slug}:")
    for key in _REG_FIELD_ORDER:
        lines.append(_reg_render_field_line(key, fields.get(key)))
    return "\n".join(lines) + "\n"


def _reg_find_record_block(text: str, slug: str) -> tuple[int, int] | None:
    """Индексы (start, end) блока documents.<slug> в тексте конфига."""
    lines = text.splitlines(keepends=True)
    start_idx = None
    for i, line in enumerate(lines):
        if re.match(rf"^  {re.escape(slug)}:\s*$", line):
            start_idx = i
            break
    if start_idx is None:
        return None
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped and not lines[j].startswith("    "):
            end_idx = j
            break
    start = sum(len(x) for x in lines[:start_idx])
    end = sum(len(x) for x in lines[:end_idx])
    return start, end


def _reg_append_record(text: str, slug: str, fields: dict) -> str | None:
    """§6: append нового блока + safe_load-гейт. None — гейт отклонил."""
    # Защита от дубля ключа: YAML last-wins проглотил бы повторный ключ,
    # оставив две записи с одним slug в тексте
    if _reg_find_record_block(text, slug) is not None:
        log.error(f"Гейт YAML: запись {slug} уже существует — append отменён")
        return None
    block = _reg_render_record_block(slug, fields)
    new_text = text.rstrip("\n") + "\n" + block
    try:
        parsed = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        log.error(f"Гейт YAML: append не распарсился: {e} — запись отменена")
        return None
    doc = (parsed.get("documents") or {}).get(slug)
    if doc is None:
        log.error(f"Гейт YAML: documents.{slug} отсутствует после append — запись отменена")
        return None
    for k, v in fields.items():
        if doc.get(k) != v:
            log.error(
                f"Гейт YAML: documents.{slug}.{k} = {doc.get(k)!r}, "
                f"ожидалось {v!r} — запись отменена"
            )
            return None
    return new_text


def _reg_fill_nulls(text: str, slug: str, fields: dict) -> str | None:
    """§6.1: точечная замена строк '    field: null' в блоке записи + гейт.

    Меняются только строки целевой записи (поиск блока по slug);
    комментарии других строк не трогаются. None — гейт отклонил.
    """
    region = _reg_find_record_block(text, slug)
    if region is None:
        log.error(f"Запись {slug} не найдена в rag_config — null-fill невозможен")
        return None
    start, end = region
    block_lines = text[start:end].splitlines(keepends=True)

    pending = {k: v for k, v in fields.items() if v is not None}
    out: list[str] = []
    for line in block_lines:
        m = re.match(r"^    (\S+): null\s*(?:#.*)?\n?$", line)
        if m and m.group(1) in pending:
            key = m.group(1)
            rendered = _reg_render_field_line(key, pending[key])
            out.append(rendered + "\n")
            del pending[key]
        else:
            out.append(line)
    if pending:
        missing = ", ".join(pending.keys())
        log.error(f"Не найдены строки для заполнения в {slug}: {missing}")
        return None
    new_text = text[:start] + "".join(out) + text[end:]

    # Гейт: распарсилось и содержит ожидаемые значения
    try:
        parsed = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        log.error(f"Гейт YAML: null-fill не распарсился: {e} — запись отменена")
        return None
    doc = (parsed.get("documents") or {}).get(slug)
    if doc is None:
        log.error(f"Гейт YAML: documents.{slug} отсутствует после null-fill")
        return None
    for k, v in fields.items():
        if v is None:
            continue
        if doc.get(k) != v:
            log.error(
                f"Гейт YAML: documents.{slug}.{k} = {doc.get(k)!r}, "
                f"ожидалось {v!r} — запись отменена"
            )
            return None
    return new_text


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
        "либо включите allow_degraded_fallback: true в create_markdown_config.yaml (с потерей точности)."
    )


# ═══════════════════════════════════════════════════════════════════════════
# 14. Asset Registry (ADR-010d)
# ═══════════════════════════════════════════════════════════════════════════

_TABLE_CAPTION_RE = re.compile(
    r"^(?:\*\s*)?(?:Т\s*а\s*б\s*л\s*и\s*ц\s*а|Таблиц[аы])\s*((?:[А-ЯA-Z]\.)?\d+(?:\.\d+)*|(?:[IVXLCDM]+))",
    re.IGNORECASE,
)


def _extract_tables_from_md(
    md_text: str,
    table_image_map: dict[str, str] | None = None,
    table_components: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Найти все Markdown-таблицы в тексте.

    Алгоритм (ADR-010 §4.1):
      1. Разбить md_text на строки
      2. Детектить таблицы: строка содержит '|', следующая строка — '|---|'
      3. Для каждой таблицы:
         - caption: подпись до или после таблицы («Таблица N» или «Таблица N.M — ...»),
           без переиспользования подписей, привязанных к другой таблице
         - md_lines: [start_line, end_line)
         - image_path: берётся из карты id → path по ID-маркеру <!-- t_pN_M -->,
           если маркер перед таблицей есть и id есть в карте; иначе None
           (привязка по содержимому выполняется позже, в _build_asset_registry).
           Позиционный счётчик как источник image_path не используется:
           неверная картинка хуже отсутствующей.

    Args:
        md_text: Markdown-текст.
        table_image_map: Карта id → "table_N.png" из table_images.json
            (tmp/<file_stem>/table_images.json), создаётся extract_table_images().

    Returns:
        [{asset_id, asset_type, caption, md_lines, image_path, row_count,
          chunk_ids, _md_block, _image_source}, ...]
        asset_id заполняется позже (doc_slug); _md_block и _image_source —
        приватные поля (удаляются при записи).
    """
    lines = md_text.splitlines()
    table_components = table_components or {}
    # Проход 1: детект таблиц (пары start/end строк)
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if re.match(r"^\|[\s\-:|]+\|$", next_line):
                start = i
                while i < len(lines) and lines[i].strip().startswith("|"):
                    i += 1
                spans.append((start, i))
                continue
        i += 1

    used_caption_lines: set[int] = set()
    tables: list[dict] = []
    for n, (start, end) in enumerate(spans, 1):
        caption = _find_table_caption_md(lines, start, end, n, used_caption_lines)
        marker_id = _find_table_marker_md(lines, start)
        image_path: str | None = None
        image_source: str | None = None
        if marker_id:
            if table_image_map and marker_id in table_image_map:
                image_path = f"image/{table_image_map[marker_id]}"
                image_source = "marker"
            else:
                log.warning(
                    f"  Таблица {n}: маркер {marker_id} есть, но id нет в карте "
                    f"таблиц — привязка по содержимому"
                )
        component_paths = table_components.get(marker_id, []) if marker_id else []
        md_block = "\n".join(lines[start:end])
        tables.append({
            "asset_id": None,  # doc_slug добавляется позже
            "asset_type": "table",
            "caption": caption,
            "md_lines": [start, end],
            "image_path": image_path,
            "image_paths": [f"image/{p}" for p in component_paths],
            "row_count": max(0, (end - start) - 2),  # минус заголовок и разделитель
            "chunk_ids": [],
            "_md_block": md_block,
            "_image_source": image_source,
        })
    return tables


def _find_table_marker_md(lines: list[str], start: int) -> str | None:
    """Найти верхний ID-маркер перед таблицей в расширенном стеке."""
    j = start - 1
    found: str | None = None
    scanned = 0
    while j >= 0 and scanned < 12:
        if lines[j].strip():
            scanned += 1
            m = re.match(r"<!--\s*(t_p\d+_\d+)\s*-->", lines[j].strip())
            if m:
                found = m.group(1)
        j -= 1
    return found


def _find_table_caption_md(
    lines: list[str],
    start: int,
    end: int,
    n: int,
    used_caption_lines: set[int] | None = None,
) -> str:
    """Найти подпись таблицы в строках Markdown.

    Ищет «Таблица N» / «Таблица N.M» / «Таблица А.1» / «Таблица I» до таблицы
    (до 3 непустых строк выше) и сразу под ней (только пустые строки между
    концом таблицы и подписью). Возвращает нормализованную подпись или пустую строку.

    Ограничения (дедуп подписей):
      - подпись, уже привязанная к другой таблице (used_caption_lines), не
        переиспользуется;
      - подпись ниже строки начала таблицы НЕ подтягивается, если между концом
        таблицы и подписью есть непустые строки (она принадлежит следующей таблице);
      - если подписи нет — возвращается "".
    """
    if used_caption_lines is None:
        used_caption_lines = set()

    def _match(line: str) -> str | None:
        m = _TABLE_CAPTION_RE.match(line.strip().strip("*"))
        if m:
            return re.sub(r"\s+", " ", line.strip().strip("*"))
        return None

    # Перед таблицей: до 12 непустых строк выше (включая стек ID-маркеров)
    j = start - 1
    scanned = 0
    while j >= 0 and scanned < 12:
        if lines[j].strip():
            scanned += 1
            if j in used_caption_lines:
                j -= 1
                continue
            cap = _match(lines[j])
            if cap:
                used_caption_lines.add(j)
                return cap
        j -= 1

    # После таблицы: только если подпись сразу под таблицей (между ними лишь
    # пустые строки). Иначе подпись относится к следующей таблице.
    j = end
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and j not in used_caption_lines:
        cap = _match(lines[j])
        if cap:
            used_caption_lines.add(j)
            return cap

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


def _normalize_table_cells(text: str) -> set[str]:
    """Нормализованные ячейки Markdown-таблицы как set строк.

    Нормализация (ТЗ: привязка по содержимому):
      - сжать пробелы, снять `*_$\\`, регистр в lower;
      - отбросить ячейки длиной <=1 и ячейки-разделители (---, :---:).
    """
    cells: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        for part in stripped.strip("|").split("|"):
            cell = re.sub(r"\s+", " ", part).strip()
            cell = re.sub(r"[*_$\\]", "", cell)
            if re.fullmatch(r":?-{2,}:?", cell):
                continue
            cell = cell.lower()
            if len(cell) > 1:
                cells.add(cell)
    return cells


# Порог уверенной привязки по содержимому (Jaccard). Подобран на СП 89:
# правильные пары дают 0.13–1.00 (слияния таблиц снижают оценку), чужие — < 0.10.
_TABLE_CONTENT_MATCH_THRESHOLD = 0.10


def _match_table_by_content(
    md_block: str,
    crop_texts: dict[str, set[str]],
    exclude: set[str] | None = None,
) -> tuple[str | None, float, float]:
    """Подобрать OCR-вырезку для Markdown-таблицы по содержимому ячеек.

    Метрика — Jaccard над нормализованными наборами ячеек (см.
    _normalize_table_cells). Берётся вырезка с максимальным пересечением;
    привязка считается уверенной при score >= _TABLE_CONTENT_MATCH_THRESHOLD.

    Args:
        md_block: Markdown-блок таблицы.
        crop_texts: {filename: set(ячеек)} — OCR-вырезки tmp/table_N.md.
        exclude: Имена вырезок, уже привязанных к другим таблицам.

    Returns:
        (filename | None, best_score, second_score)
    """
    md_cells = _normalize_table_cells(md_block)
    if not md_cells:
        return None, 0.0, 0.0
    exclude = exclude or set()
    best_name: str | None = None
    best_score = 0.0
    second_score = 0.0
    for name, crop_cells in crop_texts.items():
        if name in exclude:
            continue
        if not crop_cells:
            continue
        inter = len(md_cells.intersection(crop_cells))
        union = len(md_cells.union(crop_cells))
        score = inter / union if union else 0.0
        if score > best_score:
            second_score = best_score
            best_score = score
            best_name = name
        elif score > second_score:
            second_score = score
    if best_name is not None and best_score >= _TABLE_CONTENT_MATCH_THRESHOLD:
        return best_name, best_score, second_score
    return None, best_score, second_score


def _load_table_image_map(
    tmp_dir: str | Path | None,
    asset_dir: str | Path | None = None,
) -> dict[str, str]:
    """Загрузить карту сначала рядом с итоговым MD, затем из legacy tmp."""
    candidates = []
    if asset_dir:
        candidates.append(Path(asset_dir) / "table_images.json")
    if tmp_dir:
        candidates.append(Path(tmp_dir) / "table_images.json")
    p = next((candidate for candidate in candidates if candidate.exists()), None)
    if p is None:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {
                item["id"]: item["path"]
                for item in data
                if item.get("id") and item.get("path")
            }
    except Exception as e:
        log.warning(f"  Не удалось прочитать карту таблиц {p}: {e}")
    return {}


def _load_table_components(
    tmp_dir: str | Path | None,
    asset_dir: str | Path | None = None,
) -> dict[str, list[str]]:
    """Загрузить компоненты таблиц по ID и имени изображения.

    Для таблиц, объединённых на нескольких страницах, ``component_images``
    содержит полный список вырезок в порядке страниц.  AI-постобработка может
    удалить ID-маркеры из Markdown, поэтому тот же список индексируется по
    имени каждой входящей картинки: это позволяет восстановить компоненты
    после привязки итоговой таблицы по содержимому.
    """
    candidates = []
    if asset_dir:
        candidates.append(Path(asset_dir) / "table_images.json")
    if tmp_dir:
        candidates.append(Path(tmp_dir) / "table_images.json")
    path = next((candidate for candidate in candidates if candidate.exists()), None)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return {}

        components_by_key: dict[str, list[str]] = {}
        for item in data:
            item_id = item.get("id")
            item_path = item.get("path")
            if not item_id or not item_path:
                continue
            components = list(item.get("component_images") or [item_path])
            components_by_key[item_id] = components
            # Ключи — именно имена файлов: image/table_N.png и table_N.png
            # должны работать одинаково при чтении старых карт.
            for component in components:
                component_name = Path(component).name
                components_by_key[component_name] = components
        return components_by_key
    except (OSError, TypeError, ValueError, KeyError) as e:
        log.warning(f"  Не удалось прочитать компоненты таблиц {path}: {e}")
        return {}


def _load_table_crop_texts(tmp_dir: str | Path | None) -> dict[str, set[str]]:
    """Загрузить OCR-вырезки tmp/table_N.md как {filename: set(ячеек)}.

    Первая строка ID-маркера (<!-- t_pN_M -->) отбрасывается; имя файла
    table_N.md → table_N.png — то самое имя, что в карте extract_table_images().
    """
    result: dict[str, set[str]] = {}
    if not tmp_dir:
        return result
    tmp_dir_path = Path(tmp_dir)
    if not tmp_dir_path.is_dir():
        return result
    for f in sorted(tmp_dir_path.glob("table_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        text = re.sub(r"^<!--\s*t_p\d+_\d+\s*-->\s*\n?", "", text)
        cells = _normalize_table_cells(text)
        if cells:
            result[f.name[:-3] + ".png"] = cells
    return result


def _bind_tables_by_content(tables: list[dict], crop_texts: dict[str, set[str]]) -> None:
    """Привязать таблицы без маркера к OCR-вырезкам по содержимому (in-place).

    Мутирует tables: заполняет image_path и _image_source ("content").
    Каждая вырезка привязывается максимум к одной таблице.
    """
    used = {Path(t["image_path"]).name for t in tables if t.get("image_path")}
    for table in tables:
        if table.get("image_path"):
            continue
        block = table.get("_md_block", "")
        name, score, second = _match_table_by_content(block, crop_texts, used)
        if name is not None:
            table["image_path"] = f"image/{name}"
            table["_image_source"] = "content"
            used.add(name)
            if score - second < 0.05:
                log.warning(
                    f"  Слабая уверенность привязки {table['asset_id']} "
                    f"«{table.get('caption') or ''}» → {name} "
                    f"(Jaccard {score:.2f}, второй кандидат {second:.2f})"
                )
        else:
            log.warning(
                f"  Без image_path: {table['asset_id']} "
                f"«{table.get('caption') or ''}» (Jaccard {score:.2f})"
            )


def _build_asset_registry(
    md_text: str,
    doc_slug: str,
    img_dir: str | Path | None,
    document_id: str | None = None,
    document_type: str | None = None,
    domain: str | None = None,
    tmp_dir: str | Path | None = None,
    asset_dir: str | Path | None = None,
) -> dict:
    """Построить реестр активов rag_assets.json.

    Регистрирует таблицы и изображения из итогового Markdown.
    Пути image_path — относительные к директории .md (image/).

    Привязка таблиц к картинкам (ADR-010d + ТЗ t_0ace7f4f):
      1. По ID-маркеру <!-- t_pN_M --> перед таблицей и карте id → path
         (tmp/<file_stem>/table_images.json);
      2. Иначе по содержимому ячеек с OCR-вырезками tmp/table_N.md (Jaccard);
      3. Иначе image_path: null + log.warning (неверная картинка хуже отсутствующей).

    Если img_dir передан и файл изображения не существует — актив
    пропускается с log.warning (архитектура §9 error handling).
    """
    table_image_map = _load_table_image_map(tmp_dir, asset_dir) if (tmp_dir or asset_dir) else {}
    table_components = _load_table_components(tmp_dir, asset_dir) if (tmp_dir or asset_dir) else {}
    crop_texts = _load_table_crop_texts(tmp_dir) if tmp_dir else {}
    tables = _extract_tables_from_md(
        md_text,
        table_image_map=table_image_map,
        table_components=table_components,
    )
    images = _extract_images_from_md(md_text)

    for i, table in enumerate(tables, 1):
        table["asset_id"] = f"{doc_slug}/table/{i}"
    for i, img in enumerate(images, 1):
        img["asset_id"] = f"{doc_slug}/fig/{i}"

    # Если OCR не дал подпись, используем подпись из vision-результата.
    for table in tables:
        if table.get("caption") or not table.get("image_path") or not tmp_dir:
            continue
        vision_file = Path(tmp_dir) / (Path(table["image_path"]).stem + ".md")
        if vision_file.exists():
            try:
                vision_text = vision_file.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.search(
                r"(?im)^\s*[*_]?\s*((?:Таблиц[аы]|табл\.?)\s+[^\n*_]+)",
                vision_text,
            )
            if match:
                table["caption"] = re.sub(r"\s+", " ", match.group(1).strip())

    # Если image/ не найден — реестр активов пуст (архитектура §9):
    # без директории изображений активы не имеют смысла.
    img_dir_path = Path(img_dir) if img_dir else None
    if img_dir_path is None:
        log.warning(f"  image/ не найден: реестр активов будет пустым")
        return {
            "document_id": document_id,
            "doc_slug": doc_slug,
            "document_type": document_type,
            "domain": domain,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "image_dir": "image",
            "assets": {"tables": [], "images": []},
        }

    # Привязка по содержимому для таблиц без маркера (AI мог снять маркер
    # или таблица появилась при слиянии)
    _bind_tables_by_content(tables, crop_texts)

    # AI может удалить ID-маркеры и объединить несколько OCR-вырезок в одну
    # Markdown-таблицу. После content binding известна исходная картинка,
    # поэтому восстанавливаем полный список компонентов по имени файла.
    # Для marker binding это также служит защитой от неполной image_paths.
    for table in tables:
        image_path = table.get("image_path")
        if not image_path:
            continue
        image_name = Path(image_path).name
        components = table_components.get(image_name)
        if components:
            table["image_paths"] = [f"image/{Path(path).name}" for path in components]
        else:
            table["image_paths"] = [image_path]

    # Валидация: сводка привязок + дубликаты подписей
    n_marker = sum(1 for t in tables if t.get("_image_source") == "marker")
    n_content = sum(1 for t in tables if t.get("_image_source") == "content")
    n_unbound = sum(1 for t in tables if not t.get("image_path"))
    log.info(
        f"  Таблицы: MD={len(tables)}, OCR-вырезок={len(crop_texts)}, "
        f"привязано по маркеру={n_marker}, по содержимому={n_content}, "
        f"без привязки={n_unbound}"
    )
    if len(tables) != len(crop_texts):
        log.warning(
            f"  Число MD-таблиц ({len(tables)}) ≠ OCR-вырезок ({len(crop_texts)}) "
            f"— при слиянии таблиц это норма"
        )
    seen_captions: dict[str, str] = {}
    for table in tables:
        cap = (table.get("caption") or "").strip()
        if cap:
            if cap in seen_captions:
                log.warning(
                    f"  Дубликат подписи «{cap}»: "
                    f"{seen_captions[cap]} и {table['asset_id']}"
                )
            else:
                seen_captions[cap] = table["asset_id"]

    kept_tables = []
    for table in tables:
        ip = table.get("image_path")
        if ip is None:
            # Валидный актив без картинки (неверная картинка хуже отсутствующей)
            kept_tables.append(table)
        elif (img_dir_path / Path(ip).name).exists():
            kept_tables.append(table)
        else:
            log.warning(
                f"  Пропускаю asset {table['asset_id']}: "
                f"{ip} не существует"
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
        "document_type": document_type,
        "domain": domain,
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
        table.pop("_image_source", None)
    content = json.dumps(out, ensure_ascii=False, indent=2)
    safe_write(output_path, content)


def _derive_tmp_dir(out_dir: str | Path, file_stem: str) -> Path | None:
    """Вывести tmp/<file_stem> из out_dir вида <source>/Markdown/<file_stem>.

    Используется для загрузки карты таблиц (table_images.json) и OCR-вырезок
    (table_N.md) при сборке реестра активов — в т.ч. в режиме --rag без OCR.
    """
    out = Path(out_dir)
    try:
        return out.parents[1] / "tmp" / file_stem
    except IndexError:
        return None



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
            md_path.stem,
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
    file_stem: str,
) -> bool:
    """Оркестратор RAG: токенизатор → JSONL v2 → assets → атомарная запись.

    Атомарно ПЕРЕЗАПИСЫВАЕТ:
      - {out_dir}/{file_stem}_chunks.jsonl
      - {out_dir}/{file_stem}_assets.json
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
    doc_cfg = rag_config.get("documents", {}).get(doc_key, {})
    document_id = doc_cfg.get("document_id")
    document_type = doc_cfg.get("document_type")
    domain = doc_cfg.get("domain")
    tmp_dir = _derive_tmp_dir(out_dir, file_stem)
    assets = _build_asset_registry(
        md_text, doc_key, img_dir,
        document_id=document_id, document_type=document_type, domain=domain,
        tmp_dir=tmp_dir,
        asset_dir=out_dir,
    )
    _link_assets_to_chunks(assets, chunks)

    # Пересобрать JSONL с заполненными assets в чанках
    jsonl_lines = [json.dumps(c, ensure_ascii=False) for c in chunks]
    jsonl_out = "\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")

    rag_path = Path(out_dir) / f"{file_stem}_chunks.jsonl"
    safe_write(rag_path, jsonl_out)
    log.info(f"  RAG JSONL перезаписан: {rag_path} ({len(chunks)} строк)")

    assets_path = Path(out_dir) / f"{file_stem}_assets.json"
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
  %(prog)s -i file.pdf --ai
  %(prog)s -i file.pdf --ai --config my_config.yaml
  %(prog)s -i file.md --ai                 # только AI-постобработка, без OCR
  %(prog)s -i file.pdf --rag               # дополнительно RAG JSONL + assets
  %(prog)s -i file.pdf --ai --rag          # AI-постобработка + RAG
  %(prog)s -i file.pdf --ai --reg --rag    # + интерактивная регистрация в <stem>_reg.yaml
  %(prog)s -i Markdown/file/file.md --rag  # RAG-индексация проверенного MD
        """,
    )

    parser.add_argument(
        "-i", "--input",
        required=True,
        help="Входной файл (PDF/DOCX/DOC/MD). "
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
        "--json-native",
        action="store_true",
        help="Использовать структурный JSON-native путь рендеринга вместо legacy-парсера",
    )
    parser.add_argument(
        "--config",
        default="./create_markdown_config.yaml",
        help="Путь к единому конфигу (по умолч. ./create_markdown_config.yaml)",
    )
    parser.add_argument(
        "--providers-config",
        default="./providers.yaml",
        help="Путь к providers.yaml (реестр провайдеров + роли)",
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
        "--reg",
        action="store_true",
        help="После прогона интерактивно зарегистрировать документ "
        "в per-document <stem>_reg.yaml (требует TTY)",
    )
    parser.add_argument(
        "--base-dir",
        default="",
        help="Корневая папка документов: итог в <base-dir>/Markdown/<stem>/, "
        "временные данные в <base-dir>/tmp/<stem>/. По умолчанию — переменная "
        "окружения BASE_DIR; без неё папки определяются от входного файла",
    )

    return parser.parse_args(argv)


def _process_json_native_pages(
    pages: list[dict],
    pdf_path: str,
    file_tmp_dir: Path,
    img_dir: Path,
    use_ai: bool,
    config: dict,
    file_stem: str,
) -> str:
    """Обработать OCR JSON структурным JSON-native конвейером."""
    doc = parse_yandex_json_to_model(pages)
    stitch_tables(doc)
    extract_table_images_from_model(pdf_path, doc, img_dir)
    extract_pictures_from_model(pdf_path, doc, img_dir)
    populate_component_images(doc)
    md_text = render_document_to_md(doc)
    md_text = merge_tables_by_model(md_text, doc)
    md_text = run_script_postprocess(md_text, img_dir, doc=doc)

    if use_ai:
        vision_api_key = os.environ.get(
            config.get("table_vision", {}).get("api_key_env", "PROVOD_API_KEY"), ""
        )
        table_images = []
        for ordinal, table in enumerate(doc.tables, 1):
            if not table.image_path:
                continue
            table_images.append({
                "path": Path(table.image_path).name,
                "table_idx": ordinal,
                "id": f"t_p{table.page + 1}_{table.table_index}",
            })
        if table_images:
            recognized = recognize_tables_vision(
                table_images, img_dir, config, file_tmp_dir, vision_api_key,
            )
            log.info("  Распознано native-таблиц: %s/%s", recognized, len(table_images))
        vision_tables = load_vision_tables_from_model(doc, file_tmp_dir)
        ai_cfg = dict(config.get("ai_postprocess", config.get("postprocess", config)))
        if not ai_cfg.get("prompt"):
            ai_cfg["prompt"] = config.get("table_vision", {}).get("prompt")
        if not ai_cfg.get("prompt"):
            raise ValueError("не задан промт для JSON-native AI-постобработки")
        md_text = ai_postprocess_json_native(md_text, ai_cfg, file_stem, vision_tables)
    return md_text


def _load_cached_pages(file_tmp_dir: str | Path) -> list[dict] | None:
    """Загрузить и провалидировать yandex_result.json из tmp/<stem>.

    Возвращает список страниц, если файл есть и корректен (непустой list
    страниц, хотя бы одна с result.textAnnotation), иначе None (→ полный OCR).
    """
    json_path = Path(file_tmp_dir) / "yandex_result.json"
    if not json_path.is_file():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        log.warning("  Кэш yandex_result.json повреждён — выполняется OCR")
        return None
    if not isinstance(data, list) or not data:
        return None
    if not any(
        isinstance(p, dict) and p.get("result", {}).get("textAnnotation")
        for p in data
    ):
        return None
    return data


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
    use_reg: bool = False,
    use_json_native: bool = False,
) -> bool:
    """Обработать один файл: Yandex OCR → парсинг → изображения → постобработка → сохранение.

    Режимы (ADR-010e, трёхэтапный CLI):
      - pdf/docx: OCR + постобработка; таблицы ВСЕГДА вырезаются в image/
        (PyMuPDF, без --ai); --ai дополнительно запускает vision/AI-коррекцию
      - .md внутри Markdown/ + --rag: только RAG-индексация проверенного MD
        (без OCR/AI/извлечения), перезаписывает rag_chunks.jsonl + rag_assets.json
      - .md вне Markdown/: AI-постобработка (--ai) и/или RAG (--rag)
      - --reg (любой поток): после получения финального .md — интерактивная
        регистрация документа в per-document <stem>_reg.yaml (контракт rag-register-flag.md);
        обновлённый конфиг передаётся в --rag того же запуска

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
        if not (use_rag or use_reg):
            log.error(".md в Markdown/: требуется флаг --rag (или --reg)")
            return False
        # Per-document config lives beside the source Markdown.
        document_reg_path = _derive_reg_path(input_path_obj)
        # --reg: регистрация по готовому .md (до RAG-индексации)
        if use_reg:
            if rag_config is None:
                log.error("--reg требует загруженного rag_config")
                return False
            md_reg_text = input_path_obj.read_text(encoding="utf-8")
            new_config = run_registration(
                input_path_obj, md_reg_text, document_reg_path, rag_config, config,
            )
            if new_config is None:
                return False
            rag_config = new_config
        if not use_rag:
            log.info(f"{'=' * 60}")
            return True
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

    document_reg_path = _derive_reg_path(input_path_obj, out_dir)
    # Режим: .md вне Markdown/ → только постобработка, без OCR и скриптов
    ext = input_path_obj.suffix.lower()
    if ext == ".md":
        if use_ai or use_rag or use_reg:
            log.info(f"Обработка MD: {file_stem}.md (ai={use_ai}, rag={use_rag}, reg={use_reg})")
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
            # --reg: интерактивная регистрация (после AI, до RAG)
            if use_reg:
                if rag_config is None:
                    log.error("--reg требует загруженного rag_config")
                    return False
                new_config = run_registration(
                    input_path_obj, md_text, document_reg_path, rag_config, config,
                )
                if new_config is None:
                    return False
                rag_config = new_config
            # --rag: JSONL + assets из готового MD (_source_page = null, нет Yandex JSON)
            if use_rag:
                _write_rag_jsonl(md_text, None, rag_config, input_path, out_dir, file_stem)
            log.info(f"{'=' * 60}")
            return True
        else:
            log.error(".md файл требует флаг --ai (или --rag, или --reg)")
            return False

    # Этап 1: DOCX → PDF (если нужно)
    pdf_path = input_path
    if ext in (".docx", ".doc"):
        try:
            pdf_path = str(convert_docx_to_pdf(input_path, file_tmp_dir))
        except Exception as e:
            log.error(f"  Ошибка конвертации DOCX: {e}")
            return False

    try:
        # Этап 2: Yandex OCR. При наличии валидного кэша yandex_result.json
        # (tmp/<stem>/) пайплайн возобновляется из кэша без вызова API и без
        # требования YANDEX_API_KEY; иначе — полный прогон OCR.
        pages = _load_cached_pages(file_tmp_dir)

        if pages is None:
            # Полный прогон: кэша нет или он битый — нужен реальный OCR
            if not api_key or not folder_id:
                log.error(
                    "YANDEX_API_KEY и YANDEX_FOLDER_ID должны быть заданы в .env "
                    "(кэш yandex_result.json отсутствует)"
                )
                return False
            log.info(
                "  Полный прогон: кэш yandex_result.json отсутствует — выполняется OCR"
            )
            pages = send_to_yandex_ocr(pdf_path, api_key, folder_id)

            if not pages:
                log.warning("  Yandex OCR вернул пустой результат")
                return False

            # Сохраняем JSON в обоих режимах: это исходный OCR-артефакт, а не raw.md.
            json_path = file_tmp_dir / "yandex_result.json"
            safe_write(json_path, json.dumps(pages, ensure_ascii=False, indent=2))
        else:
            # Возобновление: данные Яндекса из кэша
            log.info("  Данные Яндекса получены из кэша (tmp/<stem>/yandex_result.json)")

        if use_json_native:
            md_text = _process_json_native_pages(
                pages, pdf_path, file_tmp_dir, img_dir, use_ai, config, file_stem,
            )
            if not md_text.strip():
                log.warning("  Markdown пуст после JSON-native конвейера")
                return False
            headings = _extract_headings_from_json(pages)
            md_path = out_dir / f"{file_stem}.md"
            safe_write(md_path, md_text)
            log.info(f"Итоговый Markdown: {md_path} ({len(md_text)} символов)")
            if use_reg:
                if rag_config is None:
                    log.error("--reg требует загруженного rag_config")
                    return False
                rag_config = run_registration(
                    input_path_obj, md_text, document_reg_path, rag_config, config,
                )
                if rag_config is None:
                    return False
            if use_rag:
                _write_rag_jsonl(md_text, headings, rag_config, input_path, out_dir, file_stem)
            return True

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
            md_text = _insert_images_into_md(
                md_text, extracted,
                pictures=pictures, pages=pages, page_boundaries=page_boundaries,
            )
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

            # Персистим карту id → path (tmp/<file_stem>/table_images.json),
            # чтобы --rag привязывал image_path и при повторном запуске без OCR
            try:
                map_payload = []
                for ti in table_images:
                    if not (ti.get("id") and ti.get("path")):
                        continue
                    entry = {
                        "id": ti.get("id"), "path": ti.get("path"),
                        "page": ti.get("page"), "table_idx": ti.get("table_idx"),
                    }
                    for key in ("caption", "table_num", "component_images"):
                        if key in ti:
                            entry[key] = ti[key]
                    map_payload.append(entry)
                safe_write(
                    file_tmp_dir / "table_images.json",
                    json.dumps(map_payload, ensure_ascii=False, indent=2),
                )
                # Карта является производным артефактом документа и должна
                # переживать удаление tmp для повторного --rag.
                safe_write(
                    out_dir / "table_images.json",
                    json.dumps(map_payload, ensure_ascii=False, indent=2),
                )
            except Exception as e:
                log.warning(f"  Не удалось сохранить карту таблиц: {e}")

            # Vision/AI-распознавание таблиц — только с --ai.
            # При возобновлении уже распознанные table_<idx>.md (валидный маркер
            # _TABLE_ID_MARKER_RE) пропускаются; vision дозапускается только по
            # недостающему подмножеству таблиц.
            if use_ai and table_images:
                vision_api_key = os.environ.get(
                    config.get("table_vision", {}).get("api_key_env", "PROVOD_API_KEY"),
                    "",
                )
                missing = _vision_table_missing(table_images, file_tmp_dir)
                if missing:
                    log.info(
                        f"  Распознавание таблиц: не хватает {len(missing)} из "
                        f"{len(table_images)} — дозапуск vision"
                    )
                    recognized = recognize_tables_vision(
                        missing, img_dir, config, file_tmp_dir, vision_api_key,
                    )
                    log.info(
                        f"  Распознано таблиц (дозапуск): {recognized}/{len(missing)}"
                    )
                else:
                    log.info(
                        f"  Распознанные таблицы: {len(table_images)} из "
                        f"{len(table_images)} (из кэша)"
                    )

        # Этап 5: Скриптовая постобработка
        md_text = run_script_postprocess(
            md_text, img_dir,
            table_images=table_images if use_ai else None,
        )

        # Этап 6+7 (объединённый): AI-коррекция таблиц + постобработка (если --ai)
        if use_ai:
            # Системный промпт: ai_postprocess.prompt, fallback — table_vision.prompt
            # (аналогично _call_ai_api/recognize_tables_vision)
            combined_prompt = config.get("ai_postprocess", {}).get("prompt")
            if not combined_prompt:
                combined_prompt = config.get("table_vision", {}).get("prompt")
            if not combined_prompt:
                log.error(
                    "не задан промт: укажите prompt в секции ai_postprocess "
                    "или table_vision конфига"
                )
                sys.exit(1)

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
                candidate = _ai_result_or_original(result, chunk, f"{file_stem} [ч.{i + 1}]")
                results[i] = _normalize_inline_latex_delimiters(candidate)
                try:
                    ckpt_path.write_text(
                        json.dumps(results, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

            md_text = "\n\n".join(r for r in results if r)

            warn_tables = _extract_warn_tables(md_text)
            if warn_tables:
                for table_number in warn_tables:
                    log.warning(
                        "⚠ Требует ручной проверки таблица %s — файл %s",
                        table_number,
                        out_dir / f"{file_stem}.md",
                    )
            else:
                log.info("  AI не пометила ни одну таблицу как изменённую")

            # ID-маркеры сохраняются в итоговом Markdown по политике B.

            # Пост-проверка: остались ли неснятые ID-маркеры
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

        # Этап 8a: --reg — интерактивная регистрация документа в <stem>_reg.yaml
        # (после финального .md, до RAG-генерации; контракт rag-register-flag.md)
        if use_reg:
            if rag_config is None:
                log.error("--reg требует загруженного rag_config")
                return False
            new_config = run_registration(
                input_path_obj, md_text, document_reg_path, rag_config, config,
            )
            if new_config is None:
                return False
            rag_config = new_config

        # Этап 8b: RAG JSONL (если --rag) — из финального MD + Yandex JSON
        if use_rag:
            _write_rag_jsonl(md_text, headings, rag_config, input_path, out_dir, file_stem)

    except Exception as e:
        log.error(f"  Ошибка обработки: {e}")
        return False

    log.info(f"{'=' * 60}")
    return True


def main() -> None:
    """Точка входа. Парсинг аргументов, обработка одного файла."""
    args = parse_args()

    # Корень документов: --base-dir → BASE_DIR из окружения → папка входного файла.
    # При явном корне итог всегда в <base_dir>/Markdown/<stem>/, временные данные
    # в <base_dir>/tmp/<stem>/ (интерфейс и бот передают корень через общий .env).
    input_path = Path(args.input).resolve()
    base_dir = Path(args.base_dir) if args.base_dir else Path(
        os.environ.get("BASE_DIR") or input_path.parent
    )
    output_base = str(base_dir / "Markdown")
    tmp_base = str(base_dir / "tmp")

    # Без явного корня: если входной .md лежит внутри Markdown/ → лог пишем в tmp/
    # рядом с Markdown/, а не внутри Markdown/<file>/tmp/. Это тот же tmp/, куда
    # пишется лог пайплайна.
    if not args.base_dir and "Markdown" in input_path.parts:
        # Найти сегмент Markdown/ в пути и взять его родителя
        mdx = list(input_path.parts).index("Markdown")
        source_dir = Path(*input_path.parts[:mdx])
        tmp_base = str(source_dir / "tmp")

    # Настраиваем логгирование
    log_path = Path(tmp_base) / "Create_Markdown_VisionOCR.log"
    setup_logging(log_path)

    log.info(f"Вход: {args.input}")
    log.info(f"AI: {args.ai}")
    log.info(f"RAG: {args.rag}")
    log.info(f"REG: {args.reg}")
    log.info(f"Выход: {output_base}")
    log.info(f"Лог: {log_path}")

    # Проверяем входной файл (папки не поддерживаются)
    files = find_input_files(args.input)
    if not files:
        log.error("Файл не найден")
        sys.exit(1)
    input_file = files[0]
    log.info(f"Обрабатываю файл: {input_file}")

    # Загружаем .env
    env_path = Path(__file__).parent / ".env"
    load_env(env_path)
    api_key = os.environ.get("YANDEX_API_KEY", "")
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "")

    # Yandex API ключи нужны только для не-.md файлов БЕЗ валидного кэша
    # tmp/<stem>/yandex_result.json: проверка перенесена в process_file (точка OCR),
    # чтобы возобновление из кэша работало без YANDEX_API_KEY/YANDEX_FOLDER_ID.

    # Загружаем конфиг AI и разрешаем роли из отдельного реестра.
    config = {}
    if args.ai or args.reg:
        try:
            config = load_config(args.config)
            if not isinstance(config, dict):
                raise ValueError(f"Некорректный YAML конфигурации: {args.config}")
            providers = load_providers_config(args.providers_config)
            role_cfg = providers.get("roles", {}).get("create_markdown", {})
            vision_role = role_cfg.get("table_vision")
            ai_role = role_cfg.get("ai_postprocess")
            if args.ai and not isinstance(vision_role, dict):
                raise ValueError("Не найдена роль roles.create_markdown.table_vision")
            if not isinstance(ai_role, dict):
                raise ValueError("Не найдена роль roles.create_markdown.ai_postprocess")
            if args.ai:
                resolved_vision = resolve_role(vision_role, providers)
                resolved_vision["prompt"] = config.get("table_vision", {}).get("prompt")
                config["table_vision"] = resolved_vision
            resolved_ai = resolve_role(ai_role, providers)
            resolved_ai["prompt"] = config.get("ai_postprocess", {}).get("prompt")
            config["ai_postprocess"] = resolved_ai
            config["reg_extract"] = {
                "prompt": config.get("reg_extract", {}).get("prompt")
            }
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.error(f"Не удалось разрешить providers.yaml: {exc}")
            sys.exit(1)

    # Загружаем RAG-часть единого конфига (если --rag или --reg).
    # При ошибке загрузки:
    #   --rag — как раньше: warning, RAG-генерация пропущена;
    #   --reg — жёсткая ошибка (exit 1): регистрация невозможна без конфига
    #   (при обоих флагах ошибка --reg приоритетна, контракт §2).
    rag_config = None
    if args.rag or args.reg:
        try:
            # PDF/DOCX output is written under Markdown/<stem>/; registration
            # overlays must be resolved beside that generated Markdown.
            reg_path = _derive_reg_path(
                input_file,
                None if input_file.suffix.lower() == ".md"
                else Path(output_base) / input_file.stem,
            )
            rag_config = load_rag_config(args.config, reg_path=reg_path)
            log.info(f"RAG-конфиг загружен: {args.config}")
        except Exception as e:
            log.error(f"Не удалось загрузить конфиг: {e}")
            if args.reg:
                log.error("--reg требует корректного create_markdown_config.yaml — регистрация невозможна")
                sys.exit(1)
            log.error("RAG-генерация пропущена")

    # Обрабатываем один файл
    ok = process_file(
        str(input_file),
        args.ai,
        config,
        api_key,
        folder_id,
        output_base,
        tmp_base,
        use_rag=args.rag,
        rag_config=rag_config,
        use_reg=args.reg,
        use_json_native=args.json_native,
    )

    log.info(f"{'=' * 60}")
    log.info(f"Завершено: {'успешно' if ok else 'ошибка'}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
