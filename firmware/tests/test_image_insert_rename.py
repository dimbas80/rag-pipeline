#!/usr/bin/env python3
"""Тесты вставки изображений (_insert_images_into_md) и переименования (rename_images).

Покрывают два бага:
  1. _insert_images_into_md сопоставлял @@IMAGE_N@@ по индексу в
     отсортированном extracted — при выпадении части вырезок номера fig_N
     сдвигались. Теперь сопоставление по позиции в pictures (page + bbox).
  2. rename_images обновлял alt-текст только для ![image](...), а
     _insert_images_into_md пишет ![fig_N](...) — regex расширен на
     любое значение в [...].
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

from pipeline import _insert_images_into_md, rename_images


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _bbox(x0, y0, x1, y1):
    """bbox в формате Yandex: {'vertices': [{'x':..,'y':..}, ...]} (4 угла)."""
    return {
        "vertices": [
            {"x": x0, "y": y0},
            {"x": x1, "y": y0},
            {"x": x1, "y": y1},
            {"x": x0, "y": y1},
        ]
    }


def _picture(page, bbox):
    return {"page": page, "bbox": bbox}


def _extracted(fig_num, page, bbox, filename=None):
    return {
        "fig_num": fig_num,
        "page": page,
        "filename": filename or f"fig_{fig_num}.png",
        "bbox": bbox,
    }


def _placeholders(n):
    """MD-текст с n плейсхолдерами на отдельных строках."""
    return "\n".join(f"@@IMAGE_{i}@@" for i in range(n))


# ═══════════════════════════════════════════════════════════════════════════
# _insert_images_into_md: сопоставление по pictures (page + bbox)
# ═══════════════════════════════════════════════════════════════════════════

def test_insert_all_images_replaced_in_order():
    """Все картинки извлечены: каждый плейсхолдер получает свой fig_N."""
    b0, b1, b2 = _bbox(10, 10, 100, 100), _bbox(10, 200, 100, 300), _bbox(10, 400, 100, 500)
    pictures = [_picture(0, b0), _picture(0, b1), _picture(0, b2)]
    extracted = [
        _extracted(1, 0, b0),
        _extracted(2, 0, b1),
        _extracted(3, 0, b2),
    ]
    out = _insert_images_into_md(_placeholders(3), extracted, pictures=pictures)
    assert out == (
        "![fig_1](image/fig_1.png)\n"
        "![fig_2](image/fig_2.png)\n"
        "![fig_3](image/fig_3.png)"
    )
    assert "@@IMAGE_" not in out


def test_insert_partial_failure_no_index_shift():
    """БАГ 1: вырезано 3 из 13 (fig_1, fig_12, fig_13) — номера не сдвигаются."""
    pictures = [_picture(0, _bbox(i, 10, i + 50, 60)) for i in range(13)]
    # Извлечены только: picture 0 -> fig_1, picture 11 -> fig_12, picture 12 -> fig_13
    extracted = [
        _extracted(1, 0, pictures[0]["bbox"]),
        _extracted(12, 0, pictures[11]["bbox"]),
        _extracted(13, 0, pictures[12]["bbox"]),
    ]
    out = _insert_images_into_md(_placeholders(13), extracted, pictures=pictures)
    lines = out.split("\n")
    # Правильные ссылки стоят на СВОИХ местах
    assert lines[0] == "![fig_1](image/fig_1.png)"
    assert lines[11] == "![fig_12](image/fig_12.png)"
    assert lines[12] == "![fig_13](image/fig_13.png)"
    # Места с провалившейся вырезкой пусты
    assert lines[1] == ""
    assert lines[10] == ""
    # Ни одного незаменённого плейсхолдера (приёмка: grep '@@IMAGE_' пусто)
    assert "@@IMAGE_" not in out


def test_insert_matches_by_page_and_bbox_not_index():
    """Сопоставление по page+bbox, а не по порядку в extracted."""
    # pictures: страница 0 -> картинка A, страница 1 -> картинка B
    bA = _bbox(10, 10, 100, 100)
    bB = _bbox(10, 10, 100, 100)  # одинаковые координаты, разные страницы
    pictures = [_picture(0, bA), _picture(1, bB)]
    # extracted идёт в обратном порядке (как при выпадении вырезок)
    extracted = [
        _extracted(2, 1, bB),
        _extracted(1, 0, bA),
    ]
    out = _insert_images_into_md(_placeholders(2), extracted, pictures=pictures)
    assert out == (
        "![fig_1](image/fig_1.png)\n"
        "![fig_2](image/fig_2.png)"
    )


def test_insert_bbox_float_rounding():
    """Вершины с плавающей точкой сравниваются с округлением."""
    b = _bbox(10.123456, 20.654321, 100.987654, 200.111111)
    pictures = [_picture(0, b)]
    extracted = [_extracted(7, 0, _bbox(10.12, 20.65, 100.99, 200.11))]
    out = _insert_images_into_md("@@IMAGE_0@@", extracted, pictures=pictures)
    assert out == "![fig_7](image/fig_7.png)"


def test_insert_no_match_clears_placeholder():
    """Вырезка не удалась — плейсхолдер очищается, токен не остаётся."""
    pictures = [_picture(0, _bbox(10, 10, 100, 100))]
    extracted = []  # ничего не извлечено — ранний выход
    assert _insert_images_into_md("@@IMAGE_0@@", extracted, pictures=pictures) == "@@IMAGE_0@@"
    # extracted непуст, но совпадения нет
    extracted = [_extracted(1, 5, _bbox(10, 10, 100, 100))]  # другая страница
    out = _insert_images_into_md("@@IMAGE_0@@", extracted, pictures=pictures)
    assert out == ""
    assert "@@IMAGE_" not in out


def test_insert_without_pictures_falls_back_to_old_order():
    """pictures=None — старое поведение (сортировка по page/Y), совместимость."""
    b0, b1 = _bbox(10, 200, 100, 300), _bbox(10, 10, 100, 100)
    extracted = [
        _extracted(1, 0, b0),
        _extracted(2, 0, b1),
    ]
    out = _insert_images_into_md(_placeholders(2), extracted)
    # Y-сортировка: b1 (y=10) раньше b0 (y=200)
    assert out == (
        "![fig_2](image/fig_2.png)\n"
        "![fig_1](image/fig_1.png)"
    )


def test_insert_empty_extracted_unchanged():
    """extracted пуст — текст не трогаем (ранний выход)."""
    md = "Текст без картинок\n@@IMAGE_0@@"
    assert _insert_images_into_md(md, [], pictures=[_picture(0, _bbox(1, 1, 2, 2))]) == md


# ═══════════════════════════════════════════════════════════════════════════
# rename_images: alt-текст для любого значения в [...]
# ═══════════════════════════════════════════════════════════════════════════

def test_rename_alt_fig_number(tmp_path: Path):
    """БАГ 2: ![fig_12](image/fig_12.png) → ![Рисунок 12](image/fig_12.png)."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    (img_dir / "fig_12.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    md = "См. рисунок: ![fig_12](image/fig_12.png)"
    out, n = rename_images(md, img_dir)
    assert n == 1
    assert out == "См. рисунок: ![Рисунок 1](image/fig_1.png)"
    assert (img_dir / "fig_1.png").exists()
    assert not (img_dir / "fig_12.png").exists()


def test_rename_alt_plain_image(tmp_path: Path):
    """Старый формат ![image](...) тоже обновляется."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    (img_dir / "abc123.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    md = "![image](image/abc123.png)"
    out, _ = rename_images(md, img_dir)
    assert out == "![Рисунок 1](image/fig_1.png)"


def test_rename_sequential_no_gaps(tmp_path: Path):
    """Файлы fig_1, fig_12, fig_13 → fig_1, fig_2, fig_3; ссылки переписаны."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    for name in ("fig_1.png", "fig_12.png", "fig_13.png"):
        (img_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    md = "\n".join([
        "![fig_1](image/fig_1.png)",
        "![fig_12](image/fig_12.png)",
        "![fig_13](image/fig_13.png)",
    ])
    out, n = rename_images(md, img_dir)
    assert n == 2  # fig_1.png имя не менялось
    assert sorted(p.name for p in img_dir.iterdir()) == [
        "fig_1.png", "fig_2.png", "fig_3.png",
    ]
    # Приёмка: все ссылки вида ![Рисунок N](image/fig_N.png), без смеси alt
    assert out == "\n".join([
        "![Рисунок 1](image/fig_1.png)",
        "![Рисунок 2](image/fig_2.png)",
        "![Рисунок 3](image/fig_3.png)",
    ])


def test_rename_happy_path_normalizes_alt(tmp_path: Path):
    """Все файлы уже fig_1..fig_3 (переименований нет) — alt всё равно
    нормализуется к ![Рисунок N] (единый формат в финальном MD)."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    for name in ("fig_1.png", "fig_2.png", "fig_3.png"):
        (img_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    md = "\n".join([
        "![fig_1](image/fig_1.png)",
        "![fig_2](image/fig_2.png)",
        "![fig_3](image/fig_3.png)",
    ])
    out, n = rename_images(md, img_dir)
    assert n == 0
    assert out == "\n".join([
        "![Рисунок 1](image/fig_1.png)",
        "![Рисунок 2](image/fig_2.png)",
        "![Рисунок 3](image/fig_3.png)",
    ])


def test_rename_no_false_match_on_fig_10(tmp_path: Path):
    """fig_1.png не должен матчить ссылку на fig_10.png."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    (img_dir / "fig_10.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    md = "![fig_10](image/fig_10.png)"
    out, _ = rename_images(md, img_dir)
    assert out == "![Рисунок 1](image/fig_1.png)"
    assert "fig_10" not in out


def test_rename_empty_dir_unchanged(tmp_path: Path):
    """Нет файлов — текст не меняется."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    md = "![fig_1](image/fig_1.png)"
    out, n = rename_images(md, img_dir)
    assert out == md
    assert n == 0


def test_rename_skips_table_images(tmp_path: Path):
    """table_N.png не переименовываются и не трогаются ссылки."""
    img_dir = tmp_path / "image"
    img_dir.mkdir()
    (img_dir / "table_1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (img_dir / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    md = "![image](image/x.png)\n![table_1](image/table_1.png)"
    out, n = rename_images(md, img_dir)
    assert n == 1
    assert out == "![Рисунок 1](image/fig_1.png)\n![table_1](image/table_1.png)"
    assert (img_dir / "table_1.png").exists()
    assert (img_dir / "fig_1.png").exists()
