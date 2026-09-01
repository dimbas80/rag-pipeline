import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from create_markdown import (
    BBox, Block, Cell, Document, Heading, Page, Picture, Table,
    parse_yandex_json_to_model,
)


def _page(blocks=None, tables=None, pictures=None, width=1000, height=2000):
    return [{"result": {"textAnnotation": {
        "width": width, "height": height,
        "blocks": blocks or [], "tables": tables or [], "pictures": pictures or [],
    }}}]


def _box(x0, y0, x1, y1):
    return {"vertices": [{"x": str(x0), "y": str(y0)}, {"x": str(x1), "y": str(y0)},
                          {"x": str(x1), "y": str(y1)}, {"x": str(x0), "y": str(y1)}]}


def _block(text, y, layout="LAYOUT_TYPE_TEXT", x=100, x1=500):
    return {"layoutType": layout, "boundingBox": _box(x, y, x1, y + 20),
            "lines": [{"text": text, "boundingBox": _box(x, y, x1, y + 20)}]}


def _table(y=300, text="value"):
    return {"boundingBox": _box(100, y, 900, y + 200), "rowCount": 2,
            "columnCount": 1, "cells": [
                {"boundingBox": _box(100, y, 900, y + 100), "rowIndex": "0", "columnIndex": "0",
                 "rowSpan": "1", "columnSpan": "1", "text": "header"},
                {"boundingBox": _box(100, y + 100, 900, y + 200), "rowIndex": "1", "columnIndex": "0",
                 "rowSpan": "1", "columnSpan": "1", "text": text},
            ]}


def test_model_extracts_heading_and_preserves_page_table_index():
    pages = [
        {"result": {"textAnnotation": {"width": 1000, "height": 2000,
            "blocks": [_block("1. Introduction", 50, "SECTION_HEADER", 100)], "tables": [_table()], "pictures": []}}},
        {"result": {"textAnnotation": {"width": 1000, "height": 2000,
            "blocks": [], "tables": [_table(1000)], "pictures": []}}},
    ]
    doc = parse_yandex_json_to_model(pages)
    assert isinstance(doc, Document)
    assert [t.table_index for t in doc.tables] == [0, 0]
    assert doc.headings[0] == Heading(0, 1, "1", "Introduction", "1. Introduction", 50.0)
    assert doc.pages[0].table_indices == [0]
    assert doc.pages[1].table_indices == [1]


def test_model_cells_have_structured_coordinates_and_caption_metadata():
    pages = _page(
        blocks=[_block("Т а б л и ц а 4.1 — Caption", 100, "CAPTION")],
        tables=[_table(300)],
        pictures=[{"boundingBox": _box(10, 800, 50, 850), "confidence": 0.87}],
    )
    doc = parse_yandex_json_to_model(pages)
    table = doc.tables[0]
    assert table.caption == "Таблица 4.1 — Caption"
    assert table.table_num == "4.1"
    assert table.cells == [Cell(0, 0, 1, 1, "header"), Cell(1, 0, 1, 1, "value")]
    assert doc.pictures[0] == Picture(0, BBox(10, 800, 50, 850), 0.87)


def test_model_filters_blocks_inside_table_and_detects_continuation():
    pages = _page(blocks=[_block("Продолжение таблицы 4.1", 100, "TEXT"),
                          _block("cell duplicate", 350, "TEXT"),
                          _block("outside", 700, "TEXT")], tables=[_table(300)])
    doc = parse_yandex_json_to_model(pages)
    assert [b.text for b in doc.pages[0].blocks] == ["Продолжение таблицы 4.1", "outside"]
    assert doc.tables[0].is_continuation is True
    assert doc.tables[0].caption == "Продолжение таблицы 4.1"


def test_model_handles_multiline_block_text_and_bbox_union():
    block = {"layoutType": "SECTION_HEADER", "boundingBox": _box(100, 50, 500, 100),
             "lines": [{"text": "2.1.", "boundingBox": _box(100, 50, 150, 70)},
                       {"text": "Subheading", "boundingBox": _box(100, 75, 500, 100)}]}
    doc = parse_yandex_json_to_model(_page(blocks=[block]))
    assert doc.pages[0].blocks[0].text == "2.1. Subheading"
    assert doc.pages[0].blocks[0].bbox == BBox(100, 50, 500, 100)
    assert doc.headings[0].level == 2
    assert doc.headings[0].text == "Subheading"


def test_empty_input_returns_empty_document():
    doc = parse_yandex_json_to_model([])
    assert doc == Document([], [], [], [], {})


def test_layout_enum_accepts_unprefixed_real_values():
    doc = parse_yandex_json_to_model(_page(blocks=[_block("3.2 Title", 20, "SECTION_HEADER", 100)]))
    assert doc.headings[0].number == "3.2"
    assert doc.headings[0].level == 2


def test_heading_requires_left_alignment_and_y_isolation():
    blocks = [_block("1. Bad", 20, "SECTION_HEADER", 600),
              _block("2. Also bad", 100, "SECTION_HEADER", 100),
              _block("body", 105, "TEXT", 100)]
    doc = parse_yandex_json_to_model(_page(blocks=blocks))
    assert doc.headings == []
    assert len(doc.pages[0].blocks) == 3


def test_picture_score_accepts_score_alias():
    doc = parse_yandex_json_to_model(_page(pictures=[{"boundingBox": _box(1, 2, 3, 4), "score": 0.4}]))
    assert doc.pictures[0].score == 0.4


def test_table_index_follows_array_order_not_y_order():
    tables = [_table(900, "first"), _table(100, "second")]
    doc = parse_yandex_json_to_model(_page(tables=tables))
    assert [(t.table_index, t.bbox.y0) for t in doc.tables] == [(0, 900), (1, 100)]
    assert doc.pages[0].table_indices == [0, 1]


def test_captionless_table_has_none_caption():
    doc = parse_yandex_json_to_model(_page(tables=[_table()]))
    assert doc.tables[0].caption is None
    assert doc.tables[0].table_num is None
    assert doc.tables[0].is_continuation is False


def test_bbox_from_empty_vertices_is_zero():
    doc = parse_yandex_json_to_model(_page(blocks=[{"layoutType": "TEXT", "lines": [{"text": "x"}]}]))
    assert doc.pages[0].blocks[0].bbox == BBox(0, 0, 0, 0)
    assert doc.pages[0].blocks[0].y == 0

def test_block_is_caption_metadata():
    doc = parse_yandex_json_to_model(_page(blocks=[_block("Таблица 1", 10, "CAPTION")]))
    b = doc.pages[0].blocks[0]
    assert b.is_table_caption and not b.is_continuation_caption


def test_document_metadata_records_source_page_count():
    doc = parse_yandex_json_to_model(_page())
    assert doc.metadata["page_count"] == 1
    assert doc.pages[0].width == 1000
    assert doc.pages[0].height == 2000


def test_heading_split_number_and_title_lines():
    block = {"layoutType": "SECTION_HEADER", "boundingBox": _box(100, 50, 500, 100),
             "lines": [{"text": "4.", "boundingBox": _box(100, 50, 130, 70)},
                       {"text": "Title", "boundingBox": _box(100, 75, 300, 100)}]}
    doc = parse_yandex_json_to_model(_page(blocks=[block]))
    assert doc.headings[0].full_text == "4. Title"
    assert doc.headings[0].text == "Title"


def test_table_caption_nearest_above_table():
    doc = parse_yandex_json_to_model(_page(blocks=[_block("Таблица 1", 10, "TEXT"), _block("Таблица 2", 250, "TEXT")], tables=[_table(300)]))
    assert doc.tables[0].caption == "Таблица 2"
    assert doc.tables[0].table_num == "2"


def test_table_page_cross_reference_for_multiple_tables():
    doc = parse_yandex_json_to_model(_page(tables=[_table(100), _table(500)], pictures=[{"boundingBox": _box(1, 1, 2, 2)}]))
    assert doc.pages[0].table_indices == [0, 1]
    assert doc.pages[0].picture_indices == [0]


def test_non_table_block_bbox_uses_all_lines():
    block = {"layoutType": "TEXT", "boundingBox": _box(999, 999, 999, 999),
             "lines": [{"text": "a", "boundingBox": _box(1, 2, 3, 4)}, {"text": "b", "boundingBox": _box(4, 5, 8, 9)}]}
    doc = parse_yandex_json_to_model(_page(blocks=[block]))
    assert doc.pages[0].blocks[0].bbox == BBox(1, 2, 8, 9)


def test_document_tables_are_page_ordered_even_if_page_table_array_is_not_y_ordered():
    doc = parse_yandex_json_to_model([{"result": {"textAnnotation": {"width": 1, "height": 1, "blocks": [], "tables": [_table(50), _table(10)], "pictures": []}}}])
    assert [t.table_index for t in doc.tables] == [0, 1]


def test_heading_y_isolation_rejects_nearby_block():
    doc = parse_yandex_json_to_model(_page(blocks=[_block("1. Head", 100, "TEXT", 100), _block("2. Head", 110, "SECTION_HEADER", 100)]))
    assert doc.headings == []


def test_table_cells_text_is_normalized():
    t = _table(); t["cells"][1]["text"] = "a\nb"
    doc = parse_yandex_json_to_model(_page(tables=[t]))
    assert doc.tables[0].cells[1].text == "a b"
