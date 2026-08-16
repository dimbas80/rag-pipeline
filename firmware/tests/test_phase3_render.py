import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pipeline import BBox, Block, Cell, Document, Page, Picture, Table, render_document_to_md, table_cells_to_md


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
