import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from create_markdown import (
    BBox, Block, Cell, Document, Page, Picture, Table,
    merge_tables_by_model, render_document_to_md, table_cells_to_md,
)


def _doc(blocks=None, tables=None, pictures=None):
    blocks = blocks or []
    tables = tables or []
    pictures = pictures or []
    page = Page(0, 1000, 1000, blocks, list(range(len(tables))), list(range(len(pictures))))
    return Document([page], [], tables, pictures)


def test_render_document_orders_headings_lists_tables_and_pictures_by_y():
    table = Table(0, 0, BBox(0, 300, 500, 450), [Cell(0, 0, 1, 1, "H"), Cell(1, 0, 1, 1, "v")], "Таблица 1 — Caption")
    picture = Picture(0, BBox(0, 600, 100, 700), 1.0, "fig_1.png")
    doc = _doc(
        [Block(100, "SECTION_HEADER", "1. Title", BBox(0, 100, 500, 120), 1),
         Block(200, "LIST", "item", BBox(0, 200, 500, 220))], [table], [picture]
    )
    rendered = render_document_to_md(doc)
    assert rendered.index("## 1. Title") < rendered.index("- item") < rendered.index("*Таблица 1 — Caption*") < rendered.index("![Рис. 1](image/fig_1.png)")
    assert table.md_lines is not None


def test_table_cells_to_md_escapes_pipes_and_handles_spans():
    result = table_cells_to_md([Cell(0, 0, 1, 2, "a|b"), Cell(1, 0, 1, 1, "c")])
    assert "a\\|b" in result
    assert "| :---: | :---: |" in result


def test_render_document_skips_continuation_captions():
    continuation = Block(
        100, "SECTION_HEADER", "Продолжение таблицы 4.1",
        BBox(0, 100, 500, 120), is_continuation_caption=True,
    )
    normal = Block(200, "TEXT", "После таблицы", BBox(0, 200, 500, 220))
    assert render_document_to_md(_doc([continuation, normal])) == "После таблицы"


def test_render_document_empty_document_and_page_are_empty():
    assert render_document_to_md(Document([], [], [], [])) == ""
    assert render_document_to_md(_doc()) == ""


def test_render_document_table_caption_without_cells_renders_caption_only():
    table = Table(0, 0, BBox(0, 100, 500, 150), [], "Таблица 1 — Caption")
    assert render_document_to_md(_doc(tables=[table])) == "*Таблица 1 — Caption*"


def test_render_document_picture_without_image_path_is_skipped():
    picture = Picture(0, BBox(0, 100, 100, 200), 1.0)
    assert render_document_to_md(_doc(pictures=[picture])) == ""


def test_render_document_orders_multiple_pages_by_page_index():
    pages = [
        Page(1, 1000, 1000, [Block(100, "TEXT", "second", BBox(0, 100, 1, 2))], [], []),
        Page(0, 1000, 1000, [Block(100, "TEXT", "first", BBox(0, 100, 1, 2))], [], []),
    ]
    assert render_document_to_md(Document(pages, [], [], [])) == "first\n\nsecond"


def test_table_cells_to_md_handles_empty_cells_and_rowspan():
    result = table_cells_to_md([
        Cell(0, 0, 2, 1, "head"), Cell(0, 1, 1, 1, ""), Cell(1, 1, 1, 1, "tail"),
    ])
    assert "| head |  |" in result
    assert "| head | tail |" in result


def test_render_document_table_ranges_are_global_across_pages():
    first = Table(0, 0, BBox(0, 100, 500, 150), [Cell(0, 0, 1, 1, "h"), Cell(1, 0, 1, 1, "a")], "Таблица 1")
    second = Table(1, 0, BBox(0, 100, 500, 150), [Cell(0, 0, 1, 1, "h"), Cell(1, 0, 1, 1, "b")], "Таблица 1")
    doc = Document([
        Page(0, 1000, 1000, [], [0], []),
        Page(1, 1000, 1000, [Block(50, "TEXT", "after", BBox(0, 50, 1, 60))], [1], []),
    ], [], [first, second], [])
    rendered = render_document_to_md(doc)
    assert rendered.splitlines()[first.md_lines[0]] == "*Таблица 1*"
    assert second.md_lines[0] > first.md_lines[1]


def test_merge_tables_by_model_keeps_one_header_and_deduplicates_rows():
    first = Table(0, 0, BBox(0, 100, 500, 150), [], "Таблица 4.1", md_lines=(0, 4), stitch_group_id=1)
    second = Table(1, 0, BBox(0, 100, 500, 150), [], "Таблица 4.1", md_lines=(5, 9), stitch_group_id=1)
    doc = Document([], [], [first, second], [])
    md = (
        "*Таблица 4.1*\n| H |\n| :---: |\n| I |\n\n"
        "*Таблица 4.1*\n| H |\n| :---: |\n| I |\n"
    )
    out = merge_tables_by_model(md, doc)
    assert out.count("*Таблица 4.1*") == 1
    assert out.count("| H |") == 1
    assert out.count("| I |") == 1


def test_merge_tables_by_model_does_not_merge_different_groups():
    one = Table(0, 0, BBox(0, 0, 1, 1), [], "A", md_lines=(0, 3), stitch_group_id=1)
    two = Table(0, 1, BBox(0, 0, 1, 1), [], "B", md_lines=(4, 7), stitch_group_id=2)
    doc = Document([], [], [one, two], [])
    md = "*A*\n| a |\n| --- |\n| 1 |\n\n*B*\n| b |\n| --- |\n| 2 |\n"
    assert merge_tables_by_model(md, doc) == md
