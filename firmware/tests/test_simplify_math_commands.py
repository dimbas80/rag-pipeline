#!/usr/bin/env python3
"""Тесты _simplify_math_commands: \\f-регулярка не должна съедать \\frac, \\flat и др.

Задача t_57f53413: регулярка r"\\\\f\\s*" матчила \\f внутри валидных
LaTeX-команд (\\frac, \\flat, \\forall ...). Исправление — negative lookahead
(?![a-zA-Z]): удалять \\f только если за ним НЕ идёт буква.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import pipeline


# ── Прямые тесты _simplify_math_commands ─────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    # Таблица из задачи
    (r"\frac {a}{b}", r"\frac {a}{b}"),      # не трогаем
    (r"\flat", r"\flat"),                    # не трогаем
    (r"\f", ""),                             # OCR-мусор — удаляем
    (r"\f 123", "123"),                      # удаляем, пробелы тоже
    # Другие валидные команды на \\f — не трогаем
    (r"\forall x \in A", r"\forall x \in A"),
    (r"\footnote{abc}", r"\footnote{abc}"),
    (r"\fbox{abc}", r"\fbox{abc}"),
    (r"\framebox{abc}", r"\framebox{abc}"),
    (r"\frac{a}{b} + \flat", r"\frac{a}{b} + \flat"),
    # \\f перед не-буквой — удаляем (с пробелами)
    (r"x \f y", "x y"),
    (r"\f,", ","),
    # Нет \\f — без изменений
    (r"просто текст", "просто текст"),
    (r"\alpha \beta", r"\alpha \beta"),
])
def test_simplify_math_commands(text, expected):
    assert pipeline._simplify_math_commands(text) == expected


def test_simplify_math_f_inside_word_not_matched():
    """\\f матчится только после backslash, не внутри слова (textfoo)."""
    assert pipeline._simplify_math_commands(r"\textfoo") == r"\textfoo"


# ── Интеграция: полная формула из бага не должна ломаться ───────────────────

def test_clean_formula_full_bug_formula_keeps_frac():
    """Формула из описания бага: \\frac не должен превратиться в 'rac'."""
    full = (r"R _ {G} = 27 + 24 \lg \left "
            r"(\frac {L _ {v}} {L _ {v _ {0}} ^ {0,9}} \right)")
    out = pipeline._clean_formula(full)
    assert r"\frac {L _ {v}}" in out
    # \\frac выжил целиком (с backslash); раньше регулярка съедала \\f -> 'rac'
    assert r"\frac" in out


def test_clean_extra_braces_keeps_frac_braces():
    """Без \\f _clean_extra_braces видит protected-команду \\frac и не ест скобки."""
    out = pipeline._clean_extra_braces(r"\frac{a}{b}")
    assert out == r"\frac{a}{b}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
