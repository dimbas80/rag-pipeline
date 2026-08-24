#!/usr/bin/env python3
"""Тесты fix_latex_caret_spaces: пробелы вокруг ^ в LaTeX-формулах ($...$ и $$...$$).

Задача t_af228810: скриптовая постобработка, дублирующая AI-промпт
config_ai.yaml («в latex добавлять пробелы перед и после знака степени ^»),
но работающая без --ai.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import create_markdown


# ── Прямые тесты fix_latex_caret_spaces ──────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Примеры из задачи
    ("$x^2$", "$x ^ 2$"),
    ("$a^{bc}$", "$a ^ {bc}$"),   # правило: пробел перед и после ^; лишний пробел после { из примера не воспроизводим
    ("$x ^2$", "$x ^ 2$"),
    ("$x^ 2$", "$x ^ 2$"),
    ("$$\\frac{a^2}{b}$$", "$$\\frac{a ^ 2}{b}$$"),
    ("обычный текст^не формула", "обычный текст^не формула"),
    # Уже ок — не удваиваем
    ("$x ^ 2$", "$x ^ 2$"),
    # Несколько формул
    ("$x^2$ и $y^3$", "$x ^ 2$ и $y ^ 3$"),
    ("текст $a^2+b^2$ текст", "текст $a ^ 2+b ^ 2$ текст"),
    # $$...$$ многострочная (DOTALL)
    ("$$\\begin{aligned}x^2&\\\\y^3\\end{aligned}$$",
     "$$\\begin{aligned}x ^ 2&\\\\y ^ 3\\end{aligned}$$"),
    # Экранированный \\^ — не трогаем
    ("$90^{\\circ}$", "$90 ^ {\\circ}$"),
    ("$\\^a$", "$\\^a$"),
    # Без формул — без изменений
    ("просто текст", "просто текст"),
    ("| A | B |\n|---|---|\n| 1 | 2 |", "| A | B |\n|---|---|\n| 1 | 2 |"),
])
def test_fix_latex_caret_spaces(text, expected):
    assert create_markdown.fix_latex_caret_spaces(text) == expected


def test_escaped_caret_untouched():
    """\\^ (литеральный циркумфлекс) вне ^-оператора не трогаем."""
    assert create_markdown.fix_latex_caret_spaces("$x\\^2$") == "$x\\^2$"
    assert create_markdown.fix_latex_caret_spaces("$\\hat{x}^2$") == "$\\hat{x} ^ 2$"


def test_unicode_text_untouched():
    """Кириллический текст с ^ вне формул не меняется."""
    text = "Формула $x^2$ внутри, а тут^не формула"
    assert create_markdown.fix_latex_caret_spaces(text) == "Формула $x ^ 2$ внутри, а тут^не формула"


# ── Проверка интеграции в run_script_postprocess ─────────────────────────────

def test_run_script_postprocess_includes_step():
    """Этап 9 (пробелы вокруг ^) есть в run_script_postprocess с log.info."""
    src = Path(create_markdown.__file__).read_text(encoding="utf-8")
    assert "fix_latex_caret_spaces(md_text)" in src
    assert 'log.info("  9. LaTeX: пробелы вокруг ^")' in src


def test_run_script_postprocess_applies_caret_spacing(tmp_path):
    """Сквозной прогон: md с формулой проходит постобработку с пробелами вокруг ^."""
    out = create_markdown.run_script_postprocess("Степень $x^2$ готова.", tmp_path)
    assert "Степень $x ^ 2$ готова." == out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
