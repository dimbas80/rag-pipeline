import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from pipeline import (
    _CONTINUATION_CAPTION_RE,
    _find_table_caption_block,
    _normalize_inline_latex_delimiters,
    _extract_warn_tables,
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


def test_extract_warn_tables_single():
    assert _extract_warn_tables("<!-- t_p27_0 -->\n<!-- WARN_7.1 -->\n| table |") == ["7.1"]


def test_extract_warn_tables_multiple_dedup():
    text = "<!-- WARN_7.1 -->\n<!-- WARN_4.1 -->\n<!-- WARN_7.1 -->"
    assert _extract_warn_tables(text) == ["7.1", "4.1"]


def test_extract_warn_tables_appendix_number():
    assert _extract_warn_tables("<!-- WARN_Б.1 -->") == ["Б.1"]


def test_extract_warn_tables_none():
    assert _extract_warn_tables("обычный Markdown без маркеров") == []


def test_inline_latex_delimiter_is_normalized():
    assert _normalize_inline_latex_delimiters(r"x \(a + b\) y") == "x $a + b$ y"
