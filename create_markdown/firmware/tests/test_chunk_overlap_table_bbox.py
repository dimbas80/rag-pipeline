#!/usr/bin/env python3
"""Тесты: overlap 3 строки между чанками AI-постобработки + расширение boundingBox таблиц вверх на 64pt."""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from create_markdown import ai_postprocess, extract_table_images, _chunk_text, SECTION_BOUNDARY_RE  # noqa: E402

CTX_START = "[Контекст из предыдущего чанка]"
CTX_END = "[Конец контекста]"


# ═══════════════════════════════════════════════════════════════════════════
# ai_postprocess: overlap чанков
# ═══════════════════════════════════════════════════════════════════════════


def _fake_ai_strips_context(calls):
    """Симулирует корректную AI: контекст — справочная информация, в результат не попадает."""

    def fake_ai(text, config, context=""):
        calls.append(text)  # сырой вход, как его получила AI (с контекстом)
        if text.startswith(CTX_START):
            end = text.index(f"{CTX_END}\n\n")
            return text[end + len(f"{CTX_END}\n\n"):]
        return text

    return fake_ai


def test_ai_postprocess_single_chunk_no_overlap(monkeypatch, tmp_path):
    """Один чанк → overlap не добавляется, контекстных маркеров нет."""
    monkeypatch.chdir(tmp_path)
    md_text = "# Заголовок\n\nОбычный текст."
    calls = []

    with patch("create_markdown._call_ai_api", side_effect=_fake_ai_strips_context(calls)):
        result = ai_postprocess(md_text, {}, file_label="test")

    assert len(calls) == 1
    assert calls[0] == md_text
    assert CTX_START not in calls[0]
    assert result == md_text


def test_ai_postprocess_multi_chunk_overlap_last_3_lines(monkeypatch, tmp_path):
    """Несколько чанков: в начало каждого следующего добавляются последние 3 строки предыдущего."""
    monkeypatch.chdir(tmp_path)
    md_text = (
        "```txt\n"
        "alpha\n"
        "beta\n"
        "gamma\n"
        "delta\n"
        "epsilon\n"
        "```\n"
        "\n"
        "```txt\n"
        "second\n"
        "block\n"
        "```\n"
    )
    chunks = _chunk_text(md_text)
    assert len(chunks) == 2, f"Ожидалось 2 чанка, получено {len(chunks)}: {chunks!r}"

    calls = []
    with patch("create_markdown._call_ai_api", side_effect=_fake_ai_strips_context(calls)):
        result = ai_postprocess(md_text, {}, file_label="test")

    assert len(calls) == 2

    # Первый чанк — без контекста
    assert calls[0] == chunks[0]
    assert CTX_START not in calls[0]

    # Второй чанк — с контекстом из последних 3 строк первого
    prev_lines = chunks[0].strip().split("\n")
    context = "\n".join(prev_lines[-3:])
    expected = f"{CTX_START}\n{context}\n{CTX_END}\n\n{chunks[1]}"
    assert calls[1] == expected
    assert result == "\n\n".join(chunks)


def test_ai_postprocess_multi_chunk_overlap_short_prev(monkeypatch, tmp_path):
    """Предыдущий чанк короче 3 строк → контекст — весь предыдущий чанк целиком."""
    monkeypatch.chdir(tmp_path)
    md_text = "Параграф\n\n```\nsecond\nblock\n```\n"
    chunks = _chunk_text(md_text)
    assert len(chunks) == 2
    assert len(chunks[0].strip().split("\n")) < 3

    calls = []
    with patch("create_markdown._call_ai_api", side_effect=_fake_ai_strips_context(calls)):
        ai_postprocess(md_text, {}, file_label="test")

    expected = f"{CTX_START}\n{chunks[0].strip()}\n{CTX_END}\n\n{chunks[1]}"
    assert calls[1] == expected


# ═══════════════════════════════════════════════════════════════════════════
# extract_table_images: вырезка таблиц (верх включает подпись, иначе 64pt)
# ═══════════════════════════════════════════════════════════════════════════


def _make_pdf(tmp_path):
    import fitz

    pdf_path = tmp_path / "test.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _page_with_table(min_y):
    return [{
        "result": {
            "textAnnotation": {
                "width": 1240,
                "height": 1754,
                "tables": [{
                    "boundingBox": {
                        "vertices": [
                            {"x": 100, "y": min_y},
                            {"x": 1140, "y": min_y},
                            {"x": 1140, "y": 600},
                            {"x": 100, "y": 600},
                        ]
                    },
                }],
            }
        }
    }]


def test_extract_table_images_bbox_no_caption_fallback_64pt(tmp_path):
    """Без подписи верх вырезки = 64pt над таблицей, x0 не изменён."""
    import fitz

    pdf_path = _make_pdf(tmp_path)
    img_dir = tmp_path / "img"
    pages = _page_with_table(min_y=200)

    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    page = fitz.open(str(pdf_path))[0]
    sx = page.rect.width / 1240
    sy = page.rect.height / 1754

    assert mock_pix.call_count == 1
    clip = mock_pix.call_args.kwargs["clip"]
    assert abs(clip.y0 - (200 * sy - 64)) < 1e-6, f"y0={clip.y0}, ожидалось {200 * sy - 64}"
    assert abs(clip.x0 - (100 * sx - 2)) < 1e-6, f"x0={clip.x0}, ожидалось {100 * sx - 2}"
    assert abs(clip.x1 - (1140 * sx + 2)) < 1e-6
    assert abs(clip.y1 - (600 * sy + 2)) < 1e-6

    assert result == [{"page": 0, "table_idx": 1, "path": "table_1.png", "id": "t_p1_0"}]
    mock_pix.return_value.save.assert_called_once_with(str(img_dir / "table_1.png"))


def test_extract_table_images_bbox_clamped_at_page_top(tmp_path):
    """Подъём вверх не выходит за границу страницы: y0 не меньше 0."""
    import fitz

    pdf_path = _make_pdf(tmp_path)
    img_dir = tmp_path / "img"
    # Таблица почти у верхнего края: min(ys)*sy - 64 < 0
    pages = _page_with_table(min_y=10)

    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        extract_table_images(str(pdf_path), pages, img_dir)

    clip = mock_pix.call_args.kwargs["clip"]
    assert clip.y0 == 0, f"y0={clip.y0}, ожидался clamp в 0"


def _page_with_table_and_caption(caption_top_y):
    """Страница с таблицей (top=200) и блоком-подписью выше неё."""
    page = _page_with_table(min_y=200)
    ta = page[0]["result"]["textAnnotation"]
    ta["blocks"] = [{
        "boundingBox": {
            "vertices": [
                {"x": 100, "y": caption_top_y},
                {"x": 1140, "y": caption_top_y},
                {"x": 1140, "y": caption_top_y + 30},
                {"x": 100, "y": caption_top_y + 30},
            ]
        },
        "layoutType": "LAYOUT_TYPE_CAPTION",
        "lines": [{"text": "Таблица 1.2 — Пример"}],
    }]
    return page


def test_extract_table_images_bbox_includes_caption(tmp_path):
    """Найденная подпись → верх вырезки = верх подписи (с -2pt), а не 64pt."""
    import fitz

    pdf_path = _make_pdf(tmp_path)
    img_dir = tmp_path / "img"
    pages = _page_with_table_and_caption(caption_top_y=120)

    with patch("fitz.Page.get_pixmap") as mock_pix:
        mock_pix.return_value.save = MagicMock()
        result = extract_table_images(str(pdf_path), pages, img_dir)

    page = fitz.open(str(pdf_path))[0]
    sy = page.rect.height / 1754

    clip = mock_pix.call_args.kwargs["clip"]
    assert abs(clip.y0 - (120 * sy - 2)) < 1e-6, f"y0={clip.y0}, ожидалось {120 * sy - 2}"
    assert abs(clip.y1 - (600 * sy + 2)) < 1e-6
    # Подпись должна попасть в результат (проверка, что блок найден корректно)
    assert result[0].get("caption") == "Таблица 1.2 — Пример"


# ═══════════════════════════════════════════════════════════════════════════
# _chunk_text: section-aware chunking (ADR-007)
# ═══════════════════════════════════════════════════════════════════════════


def test_section_aware_chunking():
    """Каждый чанк начинается с границы раздела (bold-заголовок с номером или ###)."""
    body1 = "Текст введения. " * 40
    body2 = "Текст терминов. " * 40
    body3 = "Текст защиты. " * 40
    body4 = "Текст заземления. " * 40
    md = (
        "**1. ВВЕДЕНИЕ**\n\n" + body1 + "\n\n"
        "**1.1. Термины и определения**\n\n" + body2 + "\n\n"
        "**2. ЗАЩИТА**\n\n" + body3 + "\n\n"
        "### 2.1. Заземление\n\n" + body4
    )
    chunks = _chunk_text(md, max_chars=1000)
    assert len(chunks) >= 2, f"Ожидалось несколько чанков, получено {len(chunks)}"
    for chunk in chunks:
        first_line = chunk.strip().split("\n")[0]
        assert SECTION_BOUNDARY_RE.match(first_line), f"Чанк начинается не с границы раздела: {first_line!r}"


def test_section_aware_bold_subsection_headers():
    """Bold-заголовки подразделов **N.N. Title** тоже являются границами разделов."""
    body1 = "Текст введения. " * 40
    body2 = "Текст положений. " * 40
    body3 = "Текст терминов. " * 40
    md = (
        "**1. ВВЕДЕНИЕ**\n\n" + body1 + "\n\n"
        "**1.1. Общие положения**\n\n" + body2 + "\n\n"
        "**1.2. Термины и определения**\n\n" + body3
    )
    chunks = _chunk_text(md, max_chars=1000)
    assert len(chunks) >= 2
    # Подразделы не должны быть «поглощены» предыдущим чанком целиком — каждый
    # новый подраздел открывает чанк (или попадает в чанк со своим заголовком).
    for chunk in chunks:
        first_line = chunk.strip().split("\n")[0]
        assert SECTION_BOUNDARY_RE.match(first_line), f"Чанк начинается не с границы: {first_line!r}"


def test_no_boundaries_fallback():
    """Текст без заголовков разделов → fallback на старую логику (параграфы)."""
    md = "\n\n".join(f"Параграф {i}. " + "текст " * 100 for i in range(10))
    chunks = _chunk_text(md, max_chars=500)
    assert len(chunks) > 1, "Текст без разделов должен разбиться на несколько чанков"
    # Старая логика: каждый параграф — отдельный чанк, содержимое сохраняется
    assert len(chunks) == 10, f"Ожидалось 10 параграфов-чанков, получено {len(chunks)}"
    for i, chunk in enumerate(chunks):
        assert f"Параграф {i}." in chunk, f"Чанк {i} не содержит параграф {i}: {chunk[:60]!r}"


def test_oversized_section_split():
    """Секция > max_chars разбивается по параграфам; таблицы не разрываются."""
    para1 = "Параграф один. " * 200
    para2 = "Параграф два. " * 200
    para3 = "Параграф три. " * 200
    md = (
        "**1. ОГРОМНЫЙ РАЗДЕЛ**\n\n" + para1 + "\n\n" + para2 + "\n\n"
        "| A | B |\n"
        "| --- | --- |\n"
        "| 1 | 2 |\n"
        "| 3 | 4 |\n\n" + para3
    )
    chunks = _chunk_text(md, max_chars=500)
    assert len(chunks) > 1, "Огромная секция должна разбиться на несколько чанков"
    # Таблица целиком в одном чанке (не разрезана между чанками)
    table_rows = ["| A | B |", "| 1 | 2 |", "| 3 | 4 |"]
    for row in table_rows:
        containing = [c for c in chunks if row in c]
        assert len(containing) == 1, f"Строка таблицы {row!r} должна быть ровно в одном чанке"
    # Заголовок раздела остался в начале первого чанка
    assert chunks[0].strip().startswith("**1. ОГРОМНЫЙ РАЗДЕЛ**")


def test_preamble_first_chunk():
    """Текст до первого заголовка — отдельная секция, попадает в первый чанк."""
    body_pre = "Общие сведения о документе. " * 20
    body1 = "Текст введения. " * 20
    md = (
        "ВВЕДЕНИЕ\n\n" + body_pre + "\n\n"
        "**1. ВВЕДЕНИЕ**\n\n" + body1
    )
    chunks = _chunk_text(md, max_chars=2000)
    assert len(chunks) >= 1
    assert "Общие сведения о документе" in chunks[0]
    assert "**1. ВВЕДЕНИЕ**" in chunks[0]
    assert chunks[0].strip().startswith("ВВЕДЕНИЕ")


def test_section_headers_regex():
    """SECTION_BOUNDARY_RE захватывает заголовки разделов и не захватывает таблицы/значения."""
    should_match = [
        "**1. ВВЕДЕНИЕ**",
        "**2.1. Термины и определения**",
        "**3.2.1.1. Общие соображения**",
        "### 4.5. Заземление",
        "#### 4.7.1. Меры защиты",
        "## 4. ЗАЩИТА",
        "##### 4.7.1.1. Дополнительные меры",  # уровень 4 (ADR-8)
        "**СОДЕРЖАНИЕ**",                       # bold-заголовок без номера (TOC)
        "**Примеры классификации объектов**",   # bold-заголовок приложения без номера
        "### Примеры классификации объектов",   # markdown-заголовок без номера
    ]
    should_not_match = [
        "*Таблица 4.3*",                              # подпись таблицы (одна звёздочка)
        "| **200 кА** |",                             # ячейка таблицы
        "Обычный текст без заголовка.",
        "```python",
    ]
    for line in should_match:
        assert SECTION_BOUNDARY_RE.match(line), f"Должен быть границей: {line!r}"
    for line in should_not_match:
        assert not SECTION_BOUNDARY_RE.match(line), f"НЕ должен быть границей: {line!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
