import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pipeline import (
    _CONTINUATION_CAPTION_RE,
    _find_table_caption_block,
    _normalize_inline_latex_delimiters,
    _restore_invalid_ai_tables,
    _stitch_continuation_tables,
)


def _block(text, y, layout="LAYOUT_TYPE_TEXT"):
    return {
        "layoutType": layout,
        "boundingBox": {"vertices": [{"x": 1, "y": y}, {"x": 10, "y": y}]},
        "lines": [{"text": text}],
    }


def _table(y):
    return {"boundingBox": {"vertices": [{"x": 1, "y": y}, {"x": 10, "y": y + 10}]}}


def test_continuation_caption_is_found_for_text_block():
    table = _table(100)
    blocks = [_block("Продолжение таблицы 4.1", 50)]
    found = _find_table_caption_block(blocks, table, [table])
    assert found == blocks[0]
    assert _CONTINUATION_CAPTION_RE.search("Продолжение таблицы 4.1")


def test_page_context_does_not_stitch_unrelated_captionless_table():
    md = (
        "Таблица 5\n| H | V |\n|---|---|\n| 1 | a |\n\n"
        "| H | V |\n|---|---|\n| 9 | z |\n"
    )
    result = _stitch_continuation_tables(
        md,
        page_boundaries=[(0, 4), (4, 8)],
        page_has_continuation=[False, True],
    )
    assert result.count("| H | V |") == 2
    assert "| 1 | a |" in result
    assert "| 9 | z |" in result


def test_invalid_ai_table_is_restored_when_rows_are_lost(caplog):
    original = "<!-- t_p1_0 -->\nТаблица\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    processed = "<!-- t_p1_0 -->\nТаблица\n| A | B |\n|---|---|\n| 1 | 2 |"
    result = _restore_invalid_ai_tables(original, processed)
    assert result == original
    assert "t_p1_0" in caplog.text


def test_invalid_ai_table_is_restored_when_columns_change():
    original = "<!-- t_p1_0 -->\n| A | B |\n|---|---|\n| 1 | 2 |"
    processed = "<!-- t_p1_0 -->\n| A |\n|---|\n| 1 |"
    assert _restore_invalid_ai_tables(original, processed) == original


def test_valid_ai_table_is_kept():
    original = "<!-- t_p1_0 -->\n| A | B |\n|---|---|\n| 1 | 2 |"
    processed = "<!-- t_p1_0 -->\n| A | B |\n|---|---|\n| 1 | improved |"
    assert _restore_invalid_ai_tables(original, processed) == processed


def test_invalid_ai_table_is_restored_when_marker_is_removed():
    original = "<!-- t_p1_0 -->\n| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
    processed = "| A | B |\n|---|---|\n| 1 | 2 |"
    result = _restore_invalid_ai_tables(original, processed)
    assert "| 3 | 4 |" in result


def test_inline_latex_delimiter_is_normalized():
    assert _normalize_inline_latex_delimiters(r"x \(a + b\) y") == "x $a + b$ y"
