#!/usr/bin/env python3
"""Тесты контракта ID-маркеров таблиц (docs/architecture/table-id-marker-contract.md).

Покрывают тест-план §10: T1-T18.
  A. parse_yandex_json_to_md — маркеры рождаются в parse (T1-T5)
  B. склейки и маркеры (T6-T8)
  C. extract_table_images — component_images только spatial-pass'ем (T9-T18)
     T14-T18 (R-2): явная подпись «Продолжение/Окончание таблицы N»
     присоединяется spatial-pass'ем к текущей группе при совпадающем номере
  D. интеграция постобработки / AI-резолв (T12-T13)
"""

import os
import re
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from pipeline import (  # noqa: E402
    _merge_by_component_images,
    _stitch_continuation_tables,
    _table_id_sort_key,
    extract_table_images,
    merge_tables,
    parse_yandex_json_to_md,
    run_script_postprocess,
)


# ═══════════════════════════════════════════════════════════════════════════
# Фикстуры: синтетические pages в стиле test_chunk_overlap_table_bbox.py
# ═══════════════════════════════════════════════════════════════════════════

YA_W, YA_H = 1240, 1754


def _bbox(y_top, height=60, x0=100, x1=1140):
    """Прямоугольный boundingBox таблицы/блока от y_top."""
    return {
        "vertices": [
            {"x": x0, "y": y_top},
            {"x": x1, "y": y_top},
            {"x": x1, "y": y_top + height},
            {"x": x0, "y": y_top + height},
        ]
    }


def _caption_block(y_top, text, height=30):
    """Блок-подпись таблицы (LAYOUT_TYPE_CAPTION)."""
    return {
        "boundingBox": _bbox(y_top, height=height),
        "layoutType": "LAYOUT_TYPE_CAPTION",
        "lines": [{"text": text}],
    }


def _table(y_top, rows=2, cols=2, bottom=None):
    """Таблица Yandex: boundingBox + cells (тексты по умолчанию A/B, 1/2...)."""
    if bottom is None:
        bottom = y_top + 60
    cells = []
    for r in range(rows):
        for c in range(cols):
            cells.append({
                "rowIndex": r,
                "columnIndex": c,
                "rowSpan": 1,
                "columnSpan": 1,
                "text": f"{chr(65 + c)}{r + 1}",
            })
    return {
        "boundingBox": {
            "vertices": [
                {"x": 100, "y": y_top},
                {"x": 1140, "y": y_top},
                {"x": 1140, "y": bottom},
                {"x": 100, "y": bottom},
            ]
        },
        "rowCount": rows,
        "columnCount": cols,
        "cells": cells,
    }


def _page(tables, blocks=None):
    """Одна страница Yandex OCR JSON (список из одного элемента)."""
    return [{
        "result": {
            "textAnnotation": {
                "width": YA_W,
                "height": YA_H,
                "blocks": blocks or [],
                "tables": tables,
                "pictures": [],
            }
        }
    }]


def _make_pdf(tmp_path, n_pages=1):
    """Создать PDF с n_pages страницами (A4 по умолчанию fitz)."""
    import fitz
    pdf_path = tmp_path / "test.pdf"
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _markers_in_order(md: str) -> list[str]:
    """Все ID-маркеры в порядке появления в md."""
    return re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->", md)


# ═══════════════════════════════════════════════════════════════════════════
# A. Маркеры в parse_yandex_json_to_md (T1-T5)
# ═══════════════════════════════════════════════════════════════════════════


def test_t1_page_boundary_marker_correct_id():
    """T1: 2 страницы; на стр.2 «Таблица 2» получает t_p2_1 (НЕ t_p2_0/соседний)."""
    pages = (
        _page(
            [_table(200)],
            [_caption_block(150, "Таблица 1 — Тест")],
        )
        + _page(
            [_table(200), _table(500)],
            [
                _caption_block(150, "Таблица 1 — Ещё"),
                _caption_block(450, "Таблица 2 — Приложение 2"),
            ],
        )
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)

    markers = _markers_in_order(md)
    assert markers == ["t_p1_0", "t_p2_0", "t_p2_1"]

    # «Таблица 2» Приложения 2 (вторая на стр.2) — маркер t_p2_1, не t_p2_0
    pos_marker = md.index("<!-- t_p2_1 -->")
    pos_caption = md.index("*Таблица 2 — Приложение 2*")
    assert pos_marker < pos_caption
    assert "<!-- t_p2_0 -->\n\n*Таблица 1 — Ещё*" in md


def test_t2_markers_before_own_captions():
    """T2: две таблицы на странице — маркеры t_p1_0, t_p1_1 перед своими подписями.

    Вторая таблица имеет «Продолжение»-подпись с ДРУГИМ номером (2 ≠ 1), чтобы
    parse-склейка не съела её маркер (T6 — отдельный тест на склейку).
    """
    pages = _page(
        [_table(200), _table(500)],
        [
            _caption_block(150, "Таблица 1 — Первая"),
            _caption_block(450, "Продолжение таблицы 2"),
        ],
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)

    markers = _markers_in_order(md)
    assert markers == ["t_p1_0", "t_p1_1"]
    assert "<!-- t_p1_0 -->\n\n*Таблица 1 — Первая*" in md
    assert "<!-- t_p1_1 -->\n\n*Продолжение таблицы 2*" in md


def test_t3_marker_without_caption():
    """T3: таблица без подписи — маркер непосредственно перед |-строками."""
    pages = _page([_table(200)])  # blocks отсутствуют
    md, _, _ = parse_yandex_json_to_md(pages=pages)

    assert "<!-- t_p1_0 -->" in md
    # между маркером и первой |-строкой нет подписи
    marker_idx = md.index("<!-- t_p1_0 -->")
    assert md.index("| A1 | B1 |") > marker_idx
    assert "Таблица" not in md[marker_idx:md.index("| A1 | B1 |")]


def test_t4_array_order_vs_y_order():
    """T4: Yandex массив «нижняя, верхняя» → маркеры t_p1_0 (нижняя), t_p1_1 (верхняя).

    ti — ИНДЕКС МАССИВА raw_tables, а не счётчик рендера по Y.
    """
    bottom = _table(500)
    top = _table(200)
    pages = _page(
        [bottom, top],  # массив: сначала нижняя, потом верхняя
        [
            _caption_block(450, "Таблица B — низ"),
            _caption_block(150, "Таблица A — верх"),
        ],
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)

    # Y-порядок рендера: верхняя (t_p1_1) раньше нижней (t_p1_0)
    markers = _markers_in_order(md)
    assert markers == ["t_p1_1", "t_p1_0"]
    assert "<!-- t_p1_1 -->\n\n*Таблица A — верх*" in md
    assert "<!-- t_p1_0 -->\n\n*Таблица B — низ*" in md


def test_t5_ids_match_extract(tmp_path):
    """T5: id маркеров из parse == id вырезки extract_table_images (mock fitz)."""
    pages = (
        _page(
            [_table(200)],
            [_caption_block(150, "Таблица 1 — Тест")],
        )
        + _page(
            [_table(200), _table(500)],
            [
                _caption_block(150, "Таблица 1 — Ещё"),
                _caption_block(450, "Таблица 2 — Приложение"),
            ],
        )
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)
    parse_ids = _markers_in_order(md)

    pdf_path = _make_pdf(tmp_path, n_pages=2)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        table_images = extract_table_images(str(pdf_path), pages, img_dir)

    extract_ids = [t["id"] for t in table_images]
    assert extract_ids == ["t_p1_0", "t_p2_0", "t_p2_1"]
    assert parse_ids == extract_ids


# ═══════════════════════════════════════════════════════════════════════════
# B. Склейки и маркеры (T6-T8)
# ═══════════════════════════════════════════════════════════════════════════


def test_t6_stitch_continuation_keeps_first_marker():
    """T6: _stitch_continuation_tables — маркер t_p2_0 глотается, t_p1_0 остаётся."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы 1*\n"
        "| A | B |\n| --- | --- |\n| 3 | 4 |\n"
    )
    out = _stitch_continuation_tables(md)

    markers = _markers_in_order(out)
    assert markers == ["t_p1_0"]
    assert "| 3 | 4 |" in out  # строки продолжения влиты
    assert "| 1 | 2 |" in out
    assert "Продолжение таблицы 1" not in out  # подпись продолжения съедена


def test_t7_merge_tables_keeps_first_marker():
    """T7: merge_tables — одна таблица, маркер первого компонента, второй удалён."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы 1*\n"
        "| A | B |\n| --- | --- |\n| 3 | 4 |\n"
    )
    out = merge_tables(md)

    markers = _markers_in_order(out)
    assert markers == ["t_p1_0"]
    assert out.count("| A | B |") == 1
    assert "| 3 | 4 |" in out


def test_t8_merge_by_component_images_from_parse_markers():
    """T8: _merge_by_component_images — маркеры пришли ИЗ parse (без _inject_table_ids)."""
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

    assert _markers_in_order(out) == ["t_p1_0"]
    assert "| 1 | 2 |" in out and "| 3 | 4 |" in out and "| 5 | 6 |" in out


# ═══════════════════════════════════════════════════════════════════════════
# C. extract_table_images — component_images только spatial-pass (T9-T18)
# ═══════════════════════════════════════════════════════════════════════════


def test_t9_three_same_number_tables_not_grouped(tmp_path):
    """T9: три «Таблица 2» с подписями на разных страницах — НЕ одна группа."""
    pages = []
    for page_y in (200, 200, 200):
        pages.extend(_page(
            [_table(page_y)],
            [_caption_block(page_y - 50, "Таблица 2 — Разные таблицы")],
        ))

    pdf_path = _make_pdf(tmp_path, n_pages=3)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 3
    # Ни один item не имеет общей группы: component_images отсутствует или == [свой path]
    for item in result:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"table_num-группировка вернулась: {ci}"
    paths = [item["path"] for item in result]
    assert len(set(paths)) == 3


def test_t10_spatial_pass_multi_page_b1(tmp_path):
    """T10: Б.1 на 3 страницы — component_images = [t1, t2, t3] через spatial-pass.

    стр.1: «Таблица Б.1» с подписью, упирается в низ (bottom ≥ 90%);
    стр.2: первая таблица БЕЗ подписи, упирается в низ;
    стр.3: первая таблица БЕЗ подписи.
    """
    # bottom ≥ 90% высоты (YA_H=1754): bottom_y >= 0.9*1754 ≈ 1579
    p1 = _page(
        [_table(1600, bottom=1700)],
        [_caption_block(1550, "Таблица Б.1 — Допустимые токи")],
    )
    p2 = _page([_table(1600, bottom=1700)])  # без подписи
    p3 = _page([_table(1600, bottom=1700)])  # без подписи
    pages = p1 + p2 + p3

    pdf_path = _make_pdf(tmp_path, n_pages=3)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 3
    expected = [item["path"] for item in result]
    assert expected == ["table_1.png", "table_2.png", "table_3.png"]
    for item in result:
        assert item["component_images"] == expected
        assert item["caption"] == "Таблица Б.1 — Допустимые токи"


def test_t11_spatial_pass_no_false_merges(tmp_path):
    """T11: spatial-pass не сцепляет (а) с подписью, (б) предыдущая не внизу, (в) разные группы."""
    pdf_path = _make_pdf(tmp_path, n_pages=2)
    img_dir = tmp_path / "img"

    # (а) продолжение С подписью, но номер НЕ совпадает (2 ≠ 1) → не группируется.
    #     (Совпадающий номер «Продолжение таблицы 1» теперь группируется — R-2, T14.)
    pages_a = (
        _page([_table(1600, bottom=1700)], [_caption_block(1550, "Таблица 1 — Низ")])
        + _page([_table(200)], [_caption_block(150, "Продолжение таблицы 2")])
    )
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result_a = extract_table_images(str(pdf_path), pages_a, img_dir)
    for item in result_a:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"(а) ложно склеено: {ci}"

    # (б) первая таблица стр.2 без подписи, но стр.1 НЕ упирается в низ (<90%)
    pages_b = (
        _page([_table(200, bottom=300)], [_caption_block(150, "Таблица 1 — Низ")])
        + _page([_table(200)])
    )
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result_b = extract_table_images(str(pdf_path), pages_b, img_dir)
    for item in result_b:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"(б) ложно склеено: {ci}"

    # (в) стр.2 начинается с «Таблица 2», стр.1 заканчивается «Таблица 1» → разные группы
    pages_c = (
        _page([_table(1600, bottom=1700)], [_caption_block(1550, "Таблица 1 — Низ")])
        + _page([_table(200)], [_caption_block(150, "Таблица 2 — Другая")])
    )
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result_c = extract_table_images(str(pdf_path), pages_c, img_dir)
    for item in result_c:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"(в) ложно склеено: {ci}"


def test_t14_continuation_caption_joins_group(tmp_path):
    """T14: стр.2 «Продолжение таблицы Б.1» присоединяется к стр.1 «Таблица Б.1» (R-2).

    стр.1 упирается в низ (bottom ≥ 90%); стр.2 — первая на странице,
    подпись-продолжение с совпадающим номером → component_images = [table_1, table_2]
    у обоих; caption головы распространён на продолжение.
    """
    p1 = _page(
        [_table(1600, bottom=1700)],
        [_caption_block(1550, "Таблица Б.1 — Допустимые токи")],
    )
    p2 = _page(
        [_table(200)],
        [_caption_block(150, "Продолжение таблицы Б.1")],
    )
    pages = p1 + p2

    pdf_path = _make_pdf(tmp_path, n_pages=2)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 2
    expected = [item["path"] for item in result]
    assert expected == ["table_1.png", "table_2.png"]
    for item in result:
        assert item["component_images"] == expected
        assert item["caption"] == "Таблица Б.1 — Допустимые токи"


def test_t15_continuation_caption_wrong_number_not_joined(tmp_path):
    """T15: стр.2 «Продолжение таблицы А.1» при голове «Таблица Б.1» — НЕ склеены.

    Номер не совпал (А.1 ≠ Б.1) → у каждого своя группа:
    component_images отсутствует или == [свой path].
    """
    p1 = _page(
        [_table(1600, bottom=1700)],
        [_caption_block(1550, "Таблица Б.1 — Допустимые токи")],
    )
    p2 = _page(
        [_table(200)],
        [_caption_block(150, "Продолжение таблицы А.1")],
    )
    pages = p1 + p2

    pdf_path = _make_pdf(tmp_path, n_pages=2)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 2
    for item in result:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"номер не совпал, но склеено: {ci}"


def test_t16_bare_table_2_not_continuation(tmp_path):
    """T16: регрессия дефекта 2 — три босые «Таблица 2» на разных страницах.

    Предикат _is_continuation_caption НЕ должен зацепить «Таблица 2»:
    никакой общей группы.
    """
    pages = []
    for page_y in (200, 200, 200):
        pages.extend(_page(
            [_table(page_y)],
            [_caption_block(page_y - 50, "Таблица 2 — Разные таблицы")],
        ))

    pdf_path = _make_pdf(tmp_path, n_pages=3)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 3
    for item in result:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"«Таблица 2» зацеплена предикатом: {ci}"


def test_t17_continuation_caption_geometry_fail(tmp_path):
    """T17: голова НЕ упирается в низ (bottom < 90%) — «Продолжение таблицы Б.1» НЕ склеено."""
    p1 = _page(
        [_table(200, bottom=300)],
        [_caption_block(150, "Таблица Б.1 — Допустимые токи")],
    )
    p2 = _page(
        [_table(200)],
        [_caption_block(150, "Продолжение таблицы Б.1")],
    )
    pages = p1 + p2

    pdf_path = _make_pdf(tmp_path, n_pages=2)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 2
    for item in result:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"геометрия нарушена, но склеено: {ci}"


def test_t18_continuation_caption_not_first_on_page(tmp_path):
    """T18: «Продолжение таблицы Б.1» НЕ первая на стр.2 (ti != 0) — НЕ склеена."""
    p1 = _page(
        [_table(1600, bottom=1700)],
        [_caption_block(1550, "Таблица Б.1 — Допустимые токи")],
    )
    p2 = _page(
        [_table(200), _table(500)],
        [
            _caption_block(150, "Таблица 9 — Другая"),
            _caption_block(450, "Продолжение таблицы Б.1"),
        ],
    )
    pages = p1 + p2

    pdf_path = _make_pdf(tmp_path, n_pages=2)
    img_dir = tmp_path / "img"
    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    assert len(result) == 3
    for item in result:
        ci = item.get("component_images")
        assert ci is None or ci == [item["path"]], f"ti != 0, но склеено: {ci}"


# ═══════════════════════════════════════════════════════════════════════════
# D. Интеграция постобработки / AI-резолв (T12-T13)
# ═══════════════════════════════════════════════════════════════════════════


def test_t12_postprocess_without_page_boundaries(tmp_path):
    """T12: run_script_postprocess без page_boundaries — маркеры из parse сохраняются."""
    md = (
        "Заголовок\n\n"
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "Текст между\n\n"
        "<!-- t_p2_0 -->\n*Таблица 2*\n"
        "| C | D |\n| --- | --- |\n| 3 | 4 |\n"
    )
    out = run_script_postprocess(md, tmp_path, table_images=[])
    assert _markers_in_order(out) == ["t_p1_0", "t_p2_0"]
    assert "<!-- t_p1_0 -->" in out and "<!-- t_p2_0 -->" in out


def test_t13_ai_resolve_t_p23_1_not_neighbor():
    """T13: чанк с t_p23_1 резолвится в «Таблица 2» Приложения 2, НЕ в t_p23_0.

    Воспроизводит фрагмент AI-этапа process_file (≈5303-5317): ids_in_chunk →
    matching через vision_tables.
    """
    chunk = (
        "Текст перед\n"
        "<!-- t_p23_1 -->\n"
        "*Таблица 2 — Приложение 2*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    )
    vision_tables = {
        "t_p23_0": "Таблица 1 — Допустимые токи",
        "t_p23_1": "Таблица 2 — Приложение 2",
    }

    ids_in_chunk = set(re.findall(r"<!--\s*(t_p\d+_\d+)\s*-->", chunk))
    matching = [
        vision_tables[tid]
        for tid in sorted(ids_in_chunk, key=_table_id_sort_key)
        if tid in vision_tables
    ]

    assert ids_in_chunk == {"t_p23_1"}
    assert matching == ["Таблица 2 — Приложение 2"]
    assert "Таблица 1 — Допустимые токи" not in matching


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
