#!/usr/bin/env python3
"""Тесты сквозной ID-маркировки таблиц (t_pN_M) для AI-сопоставления OCR ↔ vision.

Контракт: docs/architecture/table-id-marker-contract.md.
Маркеры рождаются в parse_yandex_json_to_md(); _inject_table_ids удалён;
component_images группируются ТОЛЬКО spatial-pass'ем extract_table_images().
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from pipeline import (
    _merge_by_component_images,
    _table_id_sort_key,
    run_script_postprocess,
)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _table_id_sort_key
# ═══════════════════════════════════════════════════════════════════════════


def test_component_image_stitch_merges_continuations_and_keeps_first_marker():
    """Tables sharing a multi-image component group become one table."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица Б.1*\n"
        "| Header A | Header B |\n| --- | --- |\n| first | row |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы Б.1*\n"
        "| second | row |\n| first | row |\n"
    )
    table_images = [
        {"id": "t_p1_0", "component_images": ["table_1.png", "table_2.png"]},
        {"id": "t_p2_0", "component_images": ["table_1.png", "table_2.png"]},
    ]

    out = _merge_by_component_images(md, table_images)

    assert out.count("| Header A | Header B |") == 1
    assert out.count("| first | row |") == 1
    assert "| second | row |" in out
    assert out.count("<!-- t_p") == 1
    assert "<!-- t_p1_0 -->" in out


def test_component_image_stitch_does_not_merge_single_image_tables():
    """Single-image groups remain separate for backward compatibility."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица 1*\n| A |\n| --- |\n| one |\n\n"
        "<!-- t_p2_0 -->\n*Таблица 2*\n| A |\n| --- |\n| two |\n"
    )
    table_images = [
        {"id": "t_p1_0", "component_images": ["table_1.png"]},
        {"id": "t_p2_0", "component_images": ["table_2.png"]},
    ]

    assert _merge_by_component_images(md, table_images) == md


def test_component_image_stitch_three_parts_from_parse_markers():
    """Многостраничная таблица из 3 компонентов с маркерами ИЗ parse (T8).

    Маркеры приходят в md_text уже из parse_yandex_json_to_md (без
    _inject_table_ids): первый маркер остаётся, маркеры продолжений глотаются.
    """
    md = (
        "<!-- t_p1_0 -->\n*Таблица Б.1*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы Б.1*\n"
        "| 3 | 4 |\n\n"
        "<!-- t_p3_0 -->\n*Продолжение таблицы Б.1*\n"
        "| 5 | 6 |\n"
    )
    shared = ["table_1.png", "table_2.png", "table_3.png"]
    table_images = [
        {"id": "t_p1_0", "component_images": shared},
        {"id": "t_p2_0", "component_images": shared},
        {"id": "t_p3_0", "component_images": shared},
    ]

    out = _merge_by_component_images(md, table_images)

    assert out.count("<!-- t_p") == 1
    assert "<!-- t_p1_0 -->" in out
    assert out.count("| A | B |") == 1
    for row in ("| 1 | 2 |", "| 3 | 4 |", "| 5 | 6 |"):
        assert row in out


def test_table_id_sort_key_orders_by_page_then_index():
    """t_p1_0 < t_p1_1 < t_p2_0 < t_p2_1 — порядок по странице, затем индексу."""
    ids = ["t_p2_1", "t_p1_1", "t_p2_0", "t_p1_0"]
    assert sorted(ids, key=_table_id_sort_key) == [
        "t_p1_0",
        "t_p1_1",
        "t_p2_0",
        "t_p2_1",
    ]


def test_table_id_sort_key_multidigit():
    """Двузначные страницы/индексы сортируются численно, не лексикографически."""
    ids = ["t_p10_0", "t_p2_9", "t_p2_10", "t_p1_11"]
    assert sorted(ids, key=_table_id_sort_key) == [
        "t_p1_11",
        "t_p2_9",
        "t_p2_10",
        "t_p10_0",
    ]


def test_table_id_sort_key_unknown_format_first():
    """Неизвестный формат возвращает (0, 0) и сортируется первым."""
    assert _table_id_sort_key("garbage") == (0, 0)
    assert _table_id_sort_key("t_p1_0") == (1, 0)
    assert sorted(["t_p1_0", "??"], key=_table_id_sort_key) == ["??", "t_p1_0"]


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run_script_postprocess (без page_boundaries — контракт §5.5)
# ═══════════════════════════════════════════════════════════════════════════


def test_run_script_postprocess_preserves_parse_markers(tmp_path):
    """Маркеры из parse проходят постобработку без пересчёта (T12).

    Вход — выход parse_yandex_json_to_md: маркеры уже стоят перед подписями.
    Постобработка не должна их удалять или пересчитывать.
    """
    md = (
        "Заголовок\n\n"
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n\n"
        "Текст\n\n"
        "<!-- t_p2_0 -->\n*Таблица 2*\n"
        "| C | D |\n"
        "| --- | --- |\n"
        "| 3 | 4 |\n"
    )
    table_images = [
        {"id": "t_p1_0", "component_images": ["table_1.png"]},
        {"id": "t_p2_0", "component_images": ["table_2.png"]},
    ]

    out = run_script_postprocess(md, tmp_path, table_images=table_images)

    assert out.count("<!-- t_p") == 2
    assert "<!-- t_p1_0 -->\n*Таблица 1*" in out
    assert "<!-- t_p2_0 -->\n*Таблица 2*" in out
    assert "| 1 | 2 |" in out
    assert "| 3 | 4 |" in out


def test_run_script_postprocess_stitches_component_images(tmp_path):
    """Интеграционно выполняется шаг 2c: маркеры из parse, склейка по группам."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица Б.1*\n| Header A | Header B |\n| --- | --- |\n| first | row |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы Б.1*\n| Header A | Header B |\n| --- | --- |\n| second | row |\n"
    )
    out = run_script_postprocess(
        md, tmp_path,
        table_images=[
            {"id": "t_p1_0", "component_images": ["table_1.png", "table_2.png"]},
            {"id": "t_p2_0", "component_images": ["table_1.png", "table_2.png"]},
        ],
    )
    assert out.count("| Header A | Header B |") == 1
    assert out.count("| --- | --- |") == 1
    assert "| second | row |" in out
    assert out.count("<!-- t_p") == 1
    assert "<!-- t_p1_0 -->" in out


def test_run_script_postprocess_without_ids_backward_compatible(tmp_path):
    """Без маркеров во входе и без table_images — поведение как раньше."""
    md = (
        "*Таблица 1*\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
    )
    out = run_script_postprocess(md, tmp_path)
    assert "<!-- t_p" not in out
    assert "| 1 | 2 |" in out


def test_run_script_postprocess_ids_only_when_table_images(tmp_path):
    """table_images=None → шаг 2c не выполняется, маркеры из parse сохраняются."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
    )
    out = run_script_postprocess(md, tmp_path, table_images=None)
    assert out.count("<!-- t_p") == 1
    assert "<!-- t_p1_0 -->" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
