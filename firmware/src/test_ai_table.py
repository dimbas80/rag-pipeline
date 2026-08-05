#!/usr/bin/env python3
"""Тесты для vision-распознавания таблиц (в составе --ai)."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pipeline import (
    extract_table_images,
    _call_vision_api,
    recognize_tables_vision,
    process_file,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_pages_with_tables():
    """Страница с одной таблицей."""
    return [
        {
            "result": {
                "textAnnotation": {
                    "width": 1240,
                    "height": 1754,
                    "tables": [
                        {
                            "boundingBox": {
                                "vertices": [
                                    {"x": 100, "y": 200},
                                    {"x": 1140, "y": 200},
                                    {"x": 1140, "y": 600},
                                    {"x": 100, "y": 600},
                                ]
                            },
                            "rowCount": 3,
                            "columnCount": 4,
                            "cells": [
                                {"rowIndex": 0, "columnIndex": 0, "text": "Name", "rowSpan": 1, "columnSpan": 1},
                                {"rowIndex": 0, "columnIndex": 1, "text": "Value", "rowSpan": 1, "columnSpan": 1},
                            ],
                        }
                    ],
                    "blocks": [],
                    "pictures": [],
                }
            }
        }
    ]


@pytest.fixture
def mock_pages_no_tables():
    """Страница без таблиц."""
    return [
        {
            "result": {
                "textAnnotation": {
                    "width": 1240,
                    "height": 1754,
                    "tables": [],
                    "blocks": [],
                    "pictures": [],
                }
            }
        }
    ]


@pytest.fixture
def temp_dirs():
    """Временные папки для тестов."""
    with tempfile.TemporaryDirectory() as img_dir:
        with tempfile.TemporaryDirectory() as tmp_dir:
            yield Path(img_dir), Path(tmp_dir)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: parse_args (CLI)
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_flag_ai_table_removed():
    """Флаг --ai-table удалён: парсер его отвергает, --ai парсится."""
    from pipeline import parse_args

    # --ai-table больше не существует
    with pytest.raises(SystemExit):
        parse_args(["-i", "test.pdf", "--ai-table"])

    # --ai работает и включает полный AI-цикл
    args = parse_args(["-i", "test.pdf", "--ai"])
    assert args.ai is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: extract_table_images
# ═══════════════════════════════════════════════════════════════════════════


def test_extract_table_images_no_tables(mock_pages_no_tables, temp_dirs):
    """Нет таблиц → пустой список."""
    img_dir, _ = temp_dirs

    # Не создаём PDF — функция должна вернуть пустой список при пустых таблицах
    # (она не открывает PDF, если нет таблиц на страницах)
    result = extract_table_images(
        "/nonexistent/test.pdf",
        mock_pages_no_tables,
        img_dir,
    )
    assert result == []


def test_extract_table_images_needs_pdf(mock_pages_with_tables, temp_dirs):
    """С таблицами, но без PDF → ошибка открытия."""
    img_dir, _ = temp_dirs

    with pytest.raises(Exception):
        extract_table_images(
            "/nonexistent/test.pdf",
            mock_pages_with_tables,
            img_dir,
        )


def test_extract_table_images_ids(temp_dirs):
    """Каждая вырезанная таблица получает id t_p{page+1}_{index_on_page}."""
    import fitz

    img_dir, _ = temp_dirs
    pdf_path = img_dir.parent / "test_tables.pdf"

    # Минимальный PDF на 2 страницы
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), "Test")
    doc.save(str(pdf_path))
    doc.close()

    def _bbox(x0, y0, x1, y1):
        return {
            "boundingBox": {
                "vertices": [
                    {"x": x0, "y": y0},
                    {"x": x1, "y": y0},
                    {"x": x1, "y": y1},
                    {"x": x0, "y": y1},
                ]
            }
        }

    pages = [
        {
            "result": {
                "textAnnotation": {
                    "width": 1240,
                    "height": 1754,
                    "tables": [_bbox(100, 200, 1140, 600), _bbox(100, 700, 1140, 900)],
                    "blocks": [],
                    "pictures": [],
                }
            }
        },
        {
            "result": {
                "textAnnotation": {
                    "width": 1240,
                    "height": 1754,
                    "tables": [_bbox(100, 200, 1140, 600)],
                    "blocks": [],
                    "pictures": [],
                }
            }
        },
    ]

    result = extract_table_images(pdf_path, pages, img_dir)
    assert len(result) == 3
    assert [t["id"] for t in result] == ["t_p1_0", "t_p1_1", "t_p2_0"]
    assert [t["page"] for t in result] == [0, 0, 1]
    assert [t["table_idx"] for t in result] == [1, 2, 3]
    assert all(t["path"].startswith("table_") and t["path"].endswith(".png") for t in result)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _call_vision_api
# ═══════════════════════════════════════════════════════════════════════════


def test_call_vision_api_no_key():
    """Без API-ключа возвращает None."""
    result = _call_vision_api(
        image_b64="AAAA",
        prompt="test prompt",
        config={"model": "test-model", "base_url": "https://api.test.com/v1"},
        api_key="",
    )
    assert result is None


@patch("pipeline.httpx.Client")
def test_call_vision_api_success(mock_client):
    """Успешный ответ от vision API."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "| A | B |\n|---|---|\n| 1 | 2 |"}}]
    }
    mock_client.return_value.__enter__.return_value.post.return_value = mock_resp

    result = _call_vision_api(
        image_b64="AAAA",
        prompt="convert table",
        config={"model": "test-model", "base_url": "https://api.test.com/v1"},
        api_key="test-key-123",
    )
    assert result is not None
    assert "A" in result
    assert "B" in result
    assert "1" in result


@patch("pipeline.httpx.Client")
def test_call_vision_api_retry(mock_client):
    """503 → retry → success."""
    mock_resp_503 = MagicMock()
    mock_resp_503.status_code = 503

    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.json.return_value = {
        "choices": [{"message": {"content": "table data"}}]
    }

    mock_client.return_value.__enter__.return_value.post.side_effect = [
        mock_resp_503, mock_resp_503, mock_resp_ok
    ]

    result = _call_vision_api(
        image_b64="AAAA",
        prompt="convert",
        config={"model": "test-model", "base_url": "https://api.test.com/v1"},
        api_key="test-key",
    )
    assert result == "table data"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: recognize_tables_vision
# ═══════════════════════════════════════════════════════════════════════════


def test_recognize_tables_vision_no_config(temp_dirs):
    """Без table_vision в конфиге → 0."""
    img_dir, tmp_dir = temp_dirs
    result = recognize_tables_vision(
        table_images=[],
        img_dir=img_dir,
        config={},
        tmp_dir=tmp_dir,
        api_key="key",
    )
    assert result == 0


def test_recognize_tables_vision_no_images(temp_dirs):
    """Пустой список изображений → 0."""
    img_dir, tmp_dir = temp_dirs
    config = {
        "table_vision": {
            "model": "test",
            "base_url": "https://test.com/v1",
            "prompt": "convert",
        }
    }
    result = recognize_tables_vision(
        table_images=[],
        img_dir=img_dir,
        config=config,
        tmp_dir=tmp_dir,
        api_key="key",
    )
    assert result == 0


def test_recognize_tables_vision_missing_file(temp_dirs):
    """Файл PNG не найден → пропускается, результат 0."""
    img_dir, tmp_dir = temp_dirs
    config = {
        "table_vision": {
            "model": "test",
            "base_url": "https://test.com/v1",
            "prompt": "convert",
        }
    }
    table_images = [{"page": 0, "table_idx": 1, "path": "table_1.png"}]
    result = recognize_tables_vision(
        table_images=table_images,
        img_dir=img_dir,
        config=config,
        tmp_dir=tmp_dir,
        api_key="key",
    )
    assert result == 0


@patch("pipeline._call_vision_api")
def test_recognize_tables_vision_writes_id_marker(mock_vision, temp_dirs):
    """table_N.md начинается с ID-маркера <!-- id --> первой строкой."""
    img_dir, tmp_dir = temp_dirs
    (img_dir / "table_1.png").write_bytes(b"fake-png")
    config = {
        "table_vision": {
            "model": "test",
            "base_url": "https://test.com/v1",
            "prompt": "convert",
        }
    }
    mock_vision.return_value = "| A | B |\n|---|---|\n| 1 | 2 |"

    table_images = [
        {"page": 0, "table_idx": 1, "path": "table_1.png", "id": "t_p1_0"}
    ]
    result = recognize_tables_vision(
        table_images=table_images,
        img_dir=img_dir,
        config=config,
        tmp_dir=tmp_dir,
        api_key="key",
    )
    assert result == 1
    content = (tmp_dir / "table_1.md").read_text(encoding="utf-8")
    assert content.startswith("<!-- t_p1_0 -->\n")
    assert "| A | B |" in content


# ═══════════════════════════════════════════════════════════════════════════
# Tests: process_file integration
# ═══════════════════════════════════════════════════════════════════════════


def test_process_file_signature():
    """process_file() не принимает use_ai_table (объединён в use_ai)."""
    import inspect
    sig = inspect.signature(process_file)
    params = list(sig.parameters.keys())
    assert "use_ai_table" not in params, f"use_ai_table не должен быть в {params}"
    assert "use_ai" in params, f"use_ai должен быть в {params}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
