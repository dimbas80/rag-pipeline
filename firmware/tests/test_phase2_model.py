import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from pipeline import BBox, Cell, Table, Page, Document, stitch_tables, geometry_says_same_table, populate_component_images, count_columns


def _doc(tables):
    pages = [Page(i, 100, 100, []) for i in range(3)]
    return Document(pages, [], tables, [], {})


def test_explicit_continuation_ignores_geometry_and_propagates_caption():
    head = Table(0, 0, BBox(0, 0, 50, 20), [Cell(0, 0, 1, 2, 'x')], 'Таблица 4.1', '4.1')
    cont = Table(1, 0, BBox(50, 0, 90, 30), [Cell(0, 0, 1, 2, 'y')], 'Продолжение таблицы 4.1', '4.1', True)
    doc = _doc([head, cont])
    stitch_tables(doc)
    assert cont.caption == head.caption


def test_captionless_geometry_requires_all_conditions():
    prev = Table(0, 0, BBox(10, 75, 90, 90), [Cell(0, 0, 1, 2, '')])
    curr = Table(1, 0, BBox(10, 5, 90, 20), [Cell(0, 0, 1, 2, '')])
    assert geometry_says_same_table(Page(0, 100, 100, []), prev, curr)
    curr.cells.append(Cell(0, 2, 1, 1, ''))
    assert not geometry_says_same_table(Page(0, 100, 100, []), prev, curr)


def test_populate_component_images_groups_propagated_captions():
    a = Table(0, 0, BBox(0, 0, 10, 10), [], 'Таблица 1', '1', image_path='a.png')
    b = Table(1, 0, BBox(0, 0, 10, 10), [], 'Продолжение таблицы 1', '1', True, image_path='b.png')
    c = Table(2, 0, BBox(0, 0, 10, 10), [], 'Таблица 2', '2', image_path='c.png')
    doc = _doc([a, b, c])
    stitch_tables(doc)
    populate_component_images(doc)
    assert a.component_images == ['a.png', 'b.png']
    assert b.component_images == ['a.png', 'b.png']
    assert c.component_images is None


def test_same_caption_unrelated_tables_do_not_share_images():
    first = Table(0, 0, BBox(0, 0, 10, 10), [], 'Таблица 1', '1', image_path='a.png')
    second = Table(2, 0, BBox(0, 0, 10, 10), [], 'Таблица 1', '1', image_path='b.png')
    doc = _doc([first, second])
    stitch_tables(doc)
    populate_component_images(doc)
    assert first.component_images is None
    assert second.component_images is None


def test_stitching_reads_threshold_and_number_match_config(tmp_path, monkeypatch):
    (tmp_path / 'rag_config.yaml').write_text(
        'table_stitching:\n  bottom_threshold_ratio: 0.5\n  require_table_num_match: false\n',
        encoding='utf-8',
    )
    monkeypatch.chdir(tmp_path)
    head = Table(0, 0, BBox(0, 60, 100, 70), [Cell(0, 0, 1, 1, '')], 'Таблица 1', '1')
    continuation = Table(1, 0, BBox(0, 0, 100, 20), [Cell(0, 0, 1, 1, '')], 'Продолжение таблицы 2', '2', True)
    doc = _doc([head, continuation])
    stitch_tables(doc)
    assert continuation.caption == head.caption
    assert continuation.stitch_group_id == head.stitch_group_id


def test_stitching_uses_configured_geometry_threshold(tmp_path, monkeypatch):
    (tmp_path / 'rag_config.yaml').write_text(
        'table_stitching:\n  bottom_threshold_ratio: 0.5\n', encoding='utf-8'
    )
    monkeypatch.chdir(tmp_path)
    head = Table(0, 0, BBox(0, 60, 100, 65), [Cell(0, 0, 1, 1, '')], 'Таблица 1', '1')
    continuation = Table(1, 0, BBox(0, 0, 100, 20), [Cell(0, 0, 1, 1, '')])
    doc = _doc([head, continuation])
    stitch_tables(doc)
    assert continuation.caption == head.caption


def test_count_columns_uses_span():
    assert count_columns(Table(0, 0, BBox(0, 0, 1, 1), [Cell(0, 2, 1, 3, '')])) == 5


def test_captionless_geometry_different_page_geometry_uses_prev_page_height():
    prev = Table(0, 0, BBox(0, 0, 100, 80), [Cell(0, 0, 1, 1, '')])
    curr = Table(1, 0, BBox(0, 0, 100, 10), [Cell(0, 0, 1, 1, '')])
    assert geometry_says_same_table(Page(0, 100, 100, []), prev, curr)
    prev.bbox = BBox(0, 0, 100, 70)
    assert not geometry_says_same_table(Page(0, 100, 100, []), prev, curr)
    prev.bbox = BBox(0, 0, 100, 80)
    assert not geometry_says_same_table(Page(0, 200, 200, []), prev, curr)
    