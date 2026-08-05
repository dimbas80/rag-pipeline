#!/usr/bin/env python3
"""Тесты для извлечения заголовков разделов из Yandex JSON (ADR-8).

Проверяют:
  - _extract_headings_from_json() — 5-правил алгоритм (regex, длина, rel_x,
    один блок на строке, уровень по глубине номера)
  - _apply_headings_to_md() — замена **жирных** заголовков на ##/###/####
  - Интеграцию в process_file() (Этап 3b)
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import pipeline

# Путь к тестовому JSON ГОСТ СО153-34.21.122-2003 (29 стр., 1022 блока)
TEST_JSON = "/mnt/sdb/!База_ГОСТ/tmp/СО153-34_21_122-2003 Молниезащита/yandex_result.json"


def _block(text, x, y, layout_type="LAYOUT_TYPE_SECTION_HEADER", width=500):
    """Собрать блок в формате Yandex OCR JSON."""
    x, y = float(x), float(y)
    return {
        "boundingBox": {
            "vertices": [
                {"x": x, "y": y},
                {"x": x + width, "y": y},
                {"x": x + width, "y": y + 33},
                {"x": x, "y": y + 33},
            ]
        },
        "lines": [{"text": text}],
        "layoutType": layout_type,
    }


def _page(blocks, width=2481, height=3508):
    """Собрать страницу в формате Yandex OCR JSON."""
    return {
        "result": {
            "textAnnotation": {
                "width": width,
                "height": height,
                "blocks": blocks,
                "tables": [],
                "pictures": [],
            }
        }
    }


# ═══════════════════════════════════════════════════════════════════════════
# _extract_headings_from_json — 5 правил
# ═══════════════════════════════════════════════════════════════════════════


def test_extract_headings_simple_heading():
    """Правило 1+5: «3. ЗАЩИТА...» → level 1, number 3."""
    pages = [_page([_block("3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ", x=100, y=100)])]
    headings = pipeline._extract_headings_from_json(pages)
    assert len(headings) == 1
    h = headings[0]
    assert h["level"] == 1
    assert h["number"] == "3"
    assert h["page"] == 0
    assert h["text"] == "ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ"
    assert h["full_text"] == "3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ"


def test_extract_headings_deep_levels():
    """Правило 5: глубина номера → уровень (3.2.1.1 → level 4)."""
    pages = [_page([_block("3.2.1.1. Общие соображения", x=200, y=100)])]
    headings = pipeline._extract_headings_from_json(pages)
    assert len(headings) == 1
    h = headings[0]
    assert h["number"] == "3.2.1.1"
    assert h["level"] == 4
    assert h["text"] == "Общие соображения"


def test_extract_headings_regex_number_requires_dot():
    """Правило 1b: «200 кА» без точки — однобуквенный номер > 2 цифр, НЕ заголовок."""
    pages = [_page([_block("200 кА", x=100, y=100)])]
    assert pipeline._extract_headings_from_json(pages) == []


def test_extract_headings_regex_number_with_trailing_dot():
    """Правило 1: «3 ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ» — точка после номера не обязательна."""
    pages = [_page([_block("3 ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ", x=100, y=100)])]
    found = pipeline._extract_headings_from_json(pages)
    assert len(found) == 1
    assert found[0]["number"] == "3"
    assert found[0]["text"] == "ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ"


def test_extract_headings_length_rule():
    """Правило 2: длинный текст (>= 100 символов) — НЕ заголовок."""
    long_text = "1. " + "очень длинный заголовок " * 10  # ~210 символов
    pages = [_page([_block(long_text, x=100, y=100)])]
    assert pipeline._extract_headings_from_json(pages) == []


def test_extract_headings_word_count_rule():
    """Правило 2: > 10 слов — НЕ заголовок."""
    many_words = "1. слово " * 12  # 24 слова
    pages = [_page([_block(many_words, x=100, y=100)])]
    assert pipeline._extract_headings_from_json(pages) == []


def test_extract_headings_rel_x_rule():
    """Правило 3: x_left / width >= 0.50 — НЕ заголовок (центр/право)."""
    # Страница шириной 1000, блок на x=600 → rel_x = 0.60
    pages = [_page([_block("3. ЗАЩИТА", x=600, y=100)], width=1000)]
    assert pipeline._extract_headings_from_json(pages) == []

    # x=450 → rel_x = 0.45 < 0.50 → заголовок
    pages = [_page([_block("3. ЗАЩИТА", x=450, y=100)], width=1000)]
    assert len(pipeline._extract_headings_from_json(pages)) == 1


def test_extract_headings_one_block_per_line_rule():
    """Правило 4: на той же Y (±15px) есть другой блок — НЕ заголовок."""
    pages = [_page([
        _block("1. Входящие линии", x=100, y=200),
        _block("2. Антенны", x=400, y=210),  # в пределах 15px по Y
    ])]
    assert pipeline._extract_headings_from_json(pages) == []


def test_extract_headings_y_tolerance_boundary():
    """Правило 4: блоки на Y-разнице > 15px — оба считаются заголовками."""
    pages = [_page([
        _block("1. Один", x=100, y=200),
        _block("2. Два", x=100, y=250),  # разница 50px > 15px
    ])]
    headings = pipeline._extract_headings_from_json(pages)
    assert len(headings) == 2


def test_extract_headings_empty_pages():
    """pages=None или [] → []."""
    assert pipeline._extract_headings_from_json(None) == []
    assert pipeline._extract_headings_from_json([]) == []


def test_extract_headings_no_blocks():
    """Страница без blocks[] → []."""
    pages = [_page([])]
    assert pipeline._extract_headings_from_json(pages) == []


def test_extract_headings_real_json_finds_68_plus():
    """Приёмка: на тестовом JSON (29 стр.) находится 68+ заголовков."""
    if not os.path.exists(TEST_JSON):
        pytest.skip(f"Тестовый JSON не найден: {TEST_JSON}")
    with open(TEST_JSON, encoding="utf-8") as f:
        pages = json.load(f)
    headings = pipeline._extract_headings_from_json(pages)
    assert len(headings) >= 68
    # Глубина до 4 уровней
    assert max(h["level"] for h in headings) >= 4


# ═══════════════════════════════════════════════════════════════════════════
# _apply_headings_to_md
# ═══════════════════════════════════════════════════════════════════════════


def test_apply_headings_basic():
    """**3. ЗАЩИТА** → ## 3. ЗАЩИТА (level 1 → ##)."""
    md = "Текст до\n\n**3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**\n\nТекст после"
    headings = [{"level": 1, "full_text": "3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ"}]
    result = pipeline._apply_headings_to_md(md, headings)
    assert "## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ" in result
    assert "**3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**" not in result


def test_apply_headings_level_mapping():
    """Уровни: 1→##, 2→###, 3→####, 4→#####."""
    md = (
        "**1. ВВЕДЕНИЕ**\n"
        "**3.2. Внешняя молниезащитная система**\n"
        "**3.2.1. Молниеприемники**\n"
        "**3.2.1.1. Общие соображения**\n"
    )
    headings = [
        {"level": 1, "full_text": "1. ВВЕДЕНИЕ"},
        {"level": 2, "full_text": "3.2. Внешняя молниезащитная система"},
        {"level": 3, "full_text": "3.2.1. Молниеприемники"},
        {"level": 4, "full_text": "3.2.1.1. Общие соображения"},
    ]
    result = pipeline._apply_headings_to_md(md, headings)
    assert "## 1. ВВЕДЕНИЕ" in result
    assert "### 3.2. Внешняя молниезащитная система" in result
    assert "#### 3.2.1. Молниеприемники" in result
    assert "##### 3.2.1.1. Общие соображения" in result


def test_apply_headings_count_one_keeps_duplicates():
    """count=1: дубликат в оглавлении остаётся жирным."""
    md = (
        "Оглавление: **3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**\n\n"
        "Раздел: **3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**\n"
    )
    headings = [{"level": 1, "full_text": "3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ"}]
    result = pipeline._apply_headings_to_md(md, headings)
    # Первое вхождение (оглавление) заменено
    assert "## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ" in result
    # Второе (в тексте) осталось жирным
    assert "**3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**" in result


def test_apply_headings_special_chars_escaped():
    """full_text со спецсимволами — re.escape() защищает."""
    md = "**3.2. Формула a+b (важно!)**\n"
    headings = [{"level": 2, "full_text": "3.2. Формула a+b (важно!)"}]
    result = pipeline._apply_headings_to_md(md, headings)
    assert "### 3.2. Формула a+b (важно!)" in result


def test_apply_headings_empty_list():
    """headings пуст → md без изменений."""
    md = "**3. ЗАЩИТА**\n"
    assert pipeline._apply_headings_to_md(md, []) == md
    assert pipeline._apply_headings_to_md(md, None) == md


def test_apply_headings_no_match_no_change():
    """full_text не найден в md — текст не меняется."""
    md = "Просто текст\n"
    headings = [{"level": 1, "full_text": "9. НЕТ ТАКОГО ЗАГОЛОВКА"}]
    assert pipeline._apply_headings_to_md(md, headings) == md


# ═══════════════════════════════════════════════════════════════════════════
# Интеграция в process_file()
# ═══════════════════════════════════════════════════════════════════════════


def _page_with_heading(text, x=100, y=100):
    """Страница с одним заголовком-блоком."""
    return _page([_block(text, x=x, y=y)])


def test_process_file_applies_headings(tmp_path):
    """process_file(): Этап 3b применяет заголовки до run_script_postprocess()."""
    pages = [_page_with_heading("3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ")]
    md_from_parser = "**3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ**\n\nТекст раздела"
    seen_md = {}

    def fake_parse(pages=None, json_path=None):
        return md_from_parser, [], []

    def fake_postprocess(md, img_dir, **kwargs):
        seen_md["post"] = md
        return md

    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out_base = tmp_path / "out"
    tmp_base = tmp_path / "tmp"

    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", side_effect=fake_parse), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.run_script_postprocess", side_effect=fake_postprocess):
        ok = pipeline.process_file(
            str(pdf), use_ai=False, config={},
            api_key="key", folder_id="folder",
            output_base=str(out_base), tmp_base=str(tmp_base),
        )

    assert ok is True
    # run_script_postprocess получил md уже с ##-заголовком
    assert "## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ" in seen_md["post"]
    # Итоговый .md тоже содержит заголовок
    out_md = (out_base / "input" / "input.md").read_text(encoding="utf-8")
    assert "## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ" in out_md


def test_process_file_no_headings_md_unchanged(tmp_path):
    """process_file(): нет заголовков — md не меняется на Этапе 3b."""
    pages = [_page([])]
    md_from_parser = "Обычный текст без заголовков"
    seen_md = {}

    def fake_parse(pages=None, json_path=None):
        return md_from_parser, [], []

    def fake_postprocess(md, img_dir, **kwargs):
        seen_md["post"] = md
        return md

    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out_base = tmp_path / "out"
    tmp_base = tmp_path / "tmp"

    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", side_effect=fake_parse), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.run_script_postprocess", side_effect=fake_postprocess):
        ok = pipeline.process_file(
            str(pdf), use_ai=False, config={},
            api_key="key", folder_id="folder",
            output_base=str(out_base), tmp_base=str(tmp_base),
        )

    assert ok is True
    assert seen_md["post"] == md_from_parser
