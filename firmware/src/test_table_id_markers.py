#!/usr/bin/env python3
"""Тесты сквозной ID-маркировки таблиц (t_pN_M) для AI-сопоставления OCR ↔ vision."""

from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import (
    _inject_table_ids,
    _table_id_sort_key,
    run_script_postprocess,
)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _table_id_sort_key
# ═══════════════════════════════════════════════════════════════════════════


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
# Tests: _inject_table_ids
# ═══════════════════════════════════════════════════════════════════════════


MD_TWO_PAGES = """\
Заголовок документа

*Таблица 1 — Тест*
| Колонка A | Колонка B |
| :---: | :---: |
| 1 | 2 |

Текст между таблицами

*Таблица 2 — Ещё*
| X | Y |
| --- | --- |
| a | b |
"""


def _boundaries_for(md: str, split_line: int) -> list[tuple[int, int]]:
    """Построить page_boundaries: страница 1 — строки [0, split_line),
    страница 2 — [split_line, end)."""
    total = len(md.split("\n"))
    return [(0, split_line), (split_line, total)]


def test_inject_ids_basic_two_pages():
    """Маркеры перед названиями; индекс сбрасывается на новой странице."""
    lines = MD_TWO_PAGES.split("\n")
    # страница 1: до 'Текст между таблицами', страница 2: остальное
    split_line = next(i for i, l in enumerate(lines) if l.startswith("Текст между"))
    boundaries = _boundaries_for(MD_TWO_PAGES, split_line)

    out = _inject_table_ids(MD_TWO_PAGES, boundaries, [{"id": "t_p1_0"}, {"id": "t_p2_0"}])

    assert "<!-- t_p1_0 -->\n*Таблица 1" in out
    assert "<!-- t_p2_0 -->\n*Таблица 2" in out
    assert out.count("<!-- t_p") == 2
    # строки таблиц не потеряны
    assert "| 1 | 2 |" in out
    assert "| a | b |" in out


def test_inject_ids_two_tables_same_page():
    """Две таблицы на одной странице: индексы 0 и 1."""
    md = (
        "*Таблица A*\n"
        "| A |\n"
        "|---|\n"
        "| 1 |\n"
        "\n"
        "*Таблица B*\n"
        "| B |\n"
        "|---|\n"
        "| 2 |\n"
    )
    boundaries = _boundaries_for(md, len(md.split("\n")))

    out = _inject_table_ids(md, boundaries, [{"id": "t_p1_0"}, {"id": "t_p1_1"}])

    assert "<!-- t_p1_0 -->\n*Таблица A*" in out
    assert "<!-- t_p1_1 -->\n*Таблица B*" in out
    assert out.count("<!-- t_p") == 2


def test_inject_ids_table_without_name_at_start():
    """Таблица в начале документа без строки-названия — маркер в начало."""
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    boundaries = _boundaries_for(md, len(md.split("\n")))

    out = _inject_table_ids(md, boundaries, [{"id": "t_p1_0"}])

    assert out.startswith("<!-- t_p1_0 -->\n| A | B |")
    assert "| 1 | 2 |" in out


def test_inject_ids_blank_line_between_row_groups_single_table():
    """Группы |-строк, разделённые пустой строкой, — одна таблица (один маркер)."""
    md = (
        "*Таблица*\n"
        "| A |\n"
        "|---|\n"
        "| 1 |\n"
        "\n"
        "| 2 |\n"
    )
    boundaries = _boundaries_for(md, len(md.split("\n")))

    out = _inject_table_ids(md, boundaries, [{"id": "t_p1_0"}])

    assert out.count("<!-- t_p") == 1
    assert "<!-- t_p1_0 -->\n*Таблица*" in out
    assert "| 2 |" in out


def test_inject_ids_no_boundaries_returns_unchanged():
    """Без page_boundaries — текст не меняется."""
    out = _inject_table_ids(MD_TWO_PAGES, None, [{"id": "t_p1_0"}])
    assert out == MD_TWO_PAGES


def test_inject_ids_no_tables_returns_unchanged():
    """Без |-таблиц — текст не меняется."""
    md = "Просто текст\nбез таблиц\n"
    boundaries = _boundaries_for(md, len(md.split("\n")))
    out = _inject_table_ids(md, boundaries, [{"id": "t_p1_0"}])
    assert out == md


def test_inject_ids_does_not_touch_non_table_pipes():
    """Строки с | внутри текста, но не начинающиеся с |, не считаются таблицей."""
    md = "Пример | внутри текста\n\n*Таблица*\n| A |\n|---|\n| 1 |\n"
    boundaries = _boundaries_for(md, len(md.split("\n")))

    out = _inject_table_ids(md, boundaries, [{"id": "t_p1_0"}])

    assert out.count("<!-- t_p") == 1
    assert "<!-- t_p1_0 -->\n*Таблица*" in out


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run_script_postprocess (интеграция)
# ═══════════════════════════════════════════════════════════════════════════


def test_run_script_postprocess_injects_ids(tmp_path):
    """С page_boundaries и table_images — маркеры вставляются."""
    md = (
        "Заголовок\n\n"
        "*Таблица 1*\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
    )
    total = len(md.split("\n"))
    boundaries = [(0, total)]

    out = run_script_postprocess(
        md, tmp_path,
        page_boundaries=boundaries,
        table_images=[{"id": "t_p1_0"}],
    )
    assert "<!-- t_p1_0 -->" in out
    assert "<!-- t_p1_0 -->\n*Таблица 1*" in out


def test_run_script_postprocess_without_ids_backward_compatible(tmp_path):
    """Без новых параметров — поведение как раньше (нет маркеров)."""
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
    """page_boundaries есть, table_images нет → маркеры НЕ вставляются."""
    md = (
        "*Таблица 1*\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
    )
    total = len(md.split("\n"))
    out = run_script_postprocess(
        md, tmp_path,
        page_boundaries=[(0, total)],
        table_images=None,
    )
    assert "<!-- t_p" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
