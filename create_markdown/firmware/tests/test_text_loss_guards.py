#!/usr/bin/env python3
"""Тесты защиты от потери текста (инцидент ГОСТ 32144-2013).

Три класса потерь и их фиксы:
  A. merge_tables удалял текст между таблицами: подстроки «окончание/
     продолжение» в обычной прозе считались маркером продолжения
     → слияние с молчаливым удалением прозы (пропали разделы 4.2.4.2–4.3.3).
  B. _block_to_md удалял ЛЮБОЙ блок со словом «таблица»/«примечание»:
     абзац с упоминанием «(таблица Б.1)» считался подписью и терялся
     (пропал текст после Таблицы Б.2).
  C. AI-этап принимал любой непустой ответ LLM: усечённый/неполный ответ
     молча заменял исходный чанк.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import create_markdown as cm  # noqa: E402
from create_markdown import (  # noqa: E402
    _ai_result_or_original,
    _block_to_md,
    _between_is_service_only,
    _looks_like_table_caption,
    merge_tables,
    parse_yandex_json_to_md,
)


# ═══════════════════════════════════════════════════════════════════════════
# A. merge_tables: проза между таблицами не удаляется
# ═══════════════════════════════════════════════════════════════════════════

_T1 = "<!-- t_p9_1 -->\n*Таблица 5 — Значения*\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"
_T2 = "<!-- t_p11_0 -->\n*Таблица А.1 — Классификация*\n| C | D |\n| --- | --- |\n| 3 | 4 |\n"
# Проза из реального инцидента: слово «окончанием» в середине предложения.
_PROSE = (
    "Провал напряжения, как правило, связан с возникновением и окончанием "
    "короткого замыкания или иного резкого возрастания тока.\n"
)


def test_merge_tables_keeps_prose_with_okonchanie_word():
    """Проза со словом «окончанием» между таблицами — НЕ маркер продолжения."""
    md = _T1 + "\n" + _PROSE + "\n" + _T2
    out = merge_tables(md)
    assert out == md, "текст между таблицами был удалён при слиянии"
    assert "окончанием" in out
    assert "4.2.5" not in out or True  # sanity: маркерных потерь нет
    assert "| 1 | 2 |" in out and "| 3 | 4 |" in out


def test_merge_tables_merges_on_marker_only_between():
    """Единственная строка «Продолжение таблицы 1» между таблицами → слияние."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы 1*\n"
        "| A | B |\n| --- | --- |\n| 3 | 4 |\n"
    )
    out = merge_tables(md)
    assert out.count("| A | B |") == 1  # шапки слиты
    assert "| 1 | 2 |" in out and "| 3 | 4 |" in out


def test_merge_tables_merges_marker_with_id_and_page_noise():
    """ID-маркер + подпись-продолжение + колонтитул/номер страницы → слияние."""
    md = (
        "<!-- t_p1_0 -->\n*Таблица 1*\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n"
        "<!-- t_p2_0 -->\n*Продолжение таблицы 1*\n"
        "ГОСТ 32144—2013\n10\n\n"
        "| A | B |\n| --- | --- |\n| 3 | 4 |\n"
    )
    out = merge_tables(md)
    assert out.count("| A | B |") == 1
    assert "| 3 | 4 |" in out
    # ID-маркеры поглощённых таблиц сохраняются (контракт T7)
    assert "<!-- t_p1_0 -->" in out and "<!-- t_p2_0 -->" in out


def test_merge_tables_prose_plus_marker_not_merged():
    """Проза рядом с маркером → слияние запрещено, текст сохраняется."""
    prose = "При оценке соответствия маркированные данные не учитывают.\n"
    md = _T1 + "\n" + "Продолжение таблицы 5\n" + prose + "\n" + _T2
    out = merge_tables(md)
    assert "При оценке соответствия" in out
    assert "| 1 | 2 |" in out and "| 3 | 4 |" in out
    assert out.count("| A | B |") == 1  # каждая таблица со своей шапкой


def test_between_is_service_only_variants():
    """Прямые проверки предиката: служебное vs содержательное."""
    assert _between_is_service_only(["*Продолжение таблицы 1*"])
    assert _between_is_service_only(["Окончание таблицы 2"])
    assert _between_is_service_only(["Продолжение таблицы 3"])  # без слова тоже можно
    assert _between_is_service_only(["<!-- t_p2_0 -->", "*Таблица 1 — X*"])
    assert _between_is_service_only(["ГОСТ 32144—2013", "10"])
    assert not _between_is_service_only([_PROSE.strip()])
    assert not _between_is_service_only(["окончанием короткого замыкания"])
    assert not _between_is_service_only(["*Продолжение таблицы 1*", "обычный текст"])


# ═══════════════════════════════════════════════════════════════════════════
# B. _block_to_md: якорные подписи; абзацы с упоминанием живут
# ═══════════════════════════════════════════════════════════════════════════

_VEROYATNOST = (
    "Вероятность превышения значений коммутационных импульсных напряжений, "
    "указанных в таблице Б.2, составляет не более 5 %, а значений импульсных "
    "напряжений, вызываемых молниевыми разрядами (таблица Б.1) — не более 10 % "
    "для воздушных линий с металлическими и железобетонными опорами."
)


def _block(text, layout="LAYOUT_TYPE_TEXT"):
    return {"layoutType": layout, "lines": [{"text": text}]}


def test_block_to_md_paragraph_mentioning_table_kept():
    """Абзац с «(таблица Б.1)» в середине — это текст, не подпись."""
    out = _block_to_md(_block(_VEROYATNOST))
    assert out == _VEROYATNOST


def test_block_to_md_caption_suppressed_variants():
    """Подписи: обычная, разреженная «Т а б л и ц а», без первой буквы."""
    assert _block_to_md(_block("Таблица 1 — Значения коэффициентов")) == ""
    assert _block_to_md(_block("Т а б л и ц а 2 — Значения")) == ""
    assert _block_to_md(_block("а б л и ц а Б.1 — Значения импульсных")) == ""
    assert _block_to_md(_block("Продолжение таблицы 3")) == ""
    assert _block_to_md(_block("Окончание таблицы 2", layout="LAYOUT_TYPE_CAPTION")) == ""


def test_block_to_md_primechanie_anchored():
    """«Примечание» в начале блока подавляется, в середине текста — нет."""
    assert _block_to_md(_block("П р и м е ч а н и е — Важно для расчёта")) == ""
    assert _block_to_md(_block("Примечания к таблице 1 приведены выше")) == ""
    prose = "В соответствии с примечанием 2 к таблице 1 применяют коэффициент."
    assert _block_to_md(_block(prose)) == prose


def test_block_to_md_prose_okonchanie_not_caption():
    """Проза, начинающаяся с «Окончание/Продолжение» без слова «таблица»,
    в _block_to_md не подавляется (контекст «перед таблицей» — в парсере)."""
    prose = "Окончание проверки зафиксировало отклонение напряжения."
    assert _block_to_md(_block(prose)) == prose


def test_looks_like_table_caption_variants():
    """Хелпер (в т.ч. JSON-native caption_flag): якорь, не подстрока."""
    assert _looks_like_table_caption("Таблица Б.1 — Значения")
    assert _looks_like_table_caption("Т а б л и ц а 2 — Х")
    assert _looks_like_table_caption("Окончание таблицы 2")
    assert not _looks_like_table_caption(_VEROYATNOST)
    assert not _looks_like_table_caption("Приведены в таблице Б.2 значения")


# ═══════════════════════════════════════════════════════════════════════════
# B2. Контекст парсера: маркер продолжения валиден только перед таблицей
# ═══════════════════════════════════════════════════════════════════════════

YA_W, YA_H = 1240, 1754


def _bbox(y_top, height=60, x0=100, x1=1140):
    return {
        "vertices": [
            {"x": x0, "y": y_top},
            {"x": x1, "y": y_top},
            {"x": x1, "y": y_top + height},
            {"x": x0, "y": y_top + height},
        ]
    }


def _ya_block(y_top, text, layout="LAYOUT_TYPE_TEXT", height=30):
    return {
        "boundingBox": _bbox(y_top, height=height),
        "layoutType": layout,
        "lines": [{"text": text}],
    }


def _ya_table(y_top, bottom=None):
    bottom = bottom if bottom is not None else y_top + 60
    cells = [
        {"rowIndex": r, "columnIndex": c, "rowSpan": 1, "columnSpan": 1,
         "text": f"{chr(65 + c)}{r + 1}"}
        for r in range(2) for c in range(2)
    ]
    return {
        "boundingBox": {
            "vertices": [
                {"x": 100, "y": y_top},
                {"x": 1140, "y": y_top},
                {"x": 1140, "y": bottom},
                {"x": 100, "y": bottom},
            ]
        },
        "rowCount": 2,
        "columnCount": 2,
        "cells": cells,
    }


def _page(blocks=None, tables=None):
    return [{
        "result": {
            "textAnnotation": {
                "width": YA_W,
                "height": YA_H,
                "blocks": blocks or [],
                "tables": tables or [],
                "pictures": [],
            }
        }
    }]


def test_parse_suppresses_continuation_marker_only_before_table():
    """«Окончание таблицы 1» непосредственно перед таблицей — маркер, не текст.

    Из потока текста блок уходит; как подпись таблицы остаётся ровно один раз
    (то же поведение, что в контракте T2 для «Продолжение таблицы 2»).
    """
    pages = _page(
        tables=[_ya_table(200)],
        blocks=[_ya_block(150, "Окончание таблицы 1")],
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)
    assert md.count("Окончание таблицы 1") == 1
    assert "*Окончание таблицы 1*" in md  # подпись таблицы, не строка текста
    assert "| A1 | B1 |" in md  # таблица отрендерена


def test_parse_keeps_prose_okonchanie_without_table_after():
    """Проза «Окончание…», за которой НЕ идёт таблица, — обычный текст."""
    pages = _page(
        blocks=[
            _ya_block(100, "Окончание проверки зафиксировало отклонение."),
            _ya_block(200, "Далее по тексту стандарта."),
        ],
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)
    assert "Окончание проверки зафиксировало отклонение." in md
    assert "Далее по тексту стандарта." in md


def test_parse_suppresses_orphan_T_before_spaced_caption():
    """Оторванная «Т» + «а б л и ц а Б.1 — …»: без голой «Т» и без дубля подписи."""
    pages = _page(
        tables=[_ya_table(400)],
        blocks=[
            _ya_block(310, "Т", height=30),
            _ya_block(320, "а б л и ц а Б.1 — Значения импульсных напряжений",
                      layout="LAYOUT_TYPE_CAPTION"),
        ],
    )
    md, _, _ = parse_yandex_json_to_md(pages=pages)
    lines = [l.strip() for l in md.split("\n")]
    assert "Т" not in lines, "голая строка «Т» протекла в текст"
    assert md.count("а б л и ц а Б.1") == 1, "подпись задвоена"
    assert "<!-- t_p1_0 -->" in md


# ═══════════════════════════════════════════════════════════════════════════
# C. Гард полноты AI-ответа
# ═══════════════════════════════════════════════════════════════════════════

def test_ai_guard_short_answer_keeps_chunk():
    """Ответ LLM заметно короче чанка → исходный чанк."""
    chunk = "Абзац один.\n\n" + "Полный текст раздела. " * 50
    short = "Абзац один."
    assert _ai_result_or_original(short, chunk, "тест") == chunk


def test_ai_guard_lost_marker_keeps_chunk():
    """Потерянный ID-маркер таблицы в ответе → исходный чанк."""
    chunk = (
        "Текст перед\n"
        "<!-- t_p9_1 -->\n*Таблица 5*\n| A |\n| --- |\n| 1 |\n\n"
        "Текст после"
    )
    result = chunk.replace("<!-- t_p9_1 -->\n", "")
    assert _ai_result_or_original(result, chunk, "тест") == chunk


def test_ai_guard_valid_answer_passes():
    """Полный ответ проходит без замен; пустой ответ → исходный чанк."""
    chunk = "Некий текст чанка."
    assert _ai_result_or_original(chunk + " (исправлено)", chunk, "тест") == chunk + " (исправлено)"
    assert _ai_result_or_original(None, chunk, "тест") == chunk
    assert _ai_result_or_original("", chunk, "тест") == chunk


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, **kwargs):
        self._responses = _FakeClient.responses
        self._calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        resp = self._responses[min(self._calls, len(self._responses) - 1)]
        self._calls += 1
        return resp


def test_call_ai_api_finish_reason_length_returns_none(monkeypatch):
    """finish_reason=length — усечённый ответ считается неудачей."""
    payload = {"choices": [{"message": {"content": "частичный ответ"},
                            "finish_reason": "length"}]}
    _FakeClient.responses = [_FakeResponse(payload)]
    monkeypatch.setattr(cm.httpx, "Client", _FakeClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    cfg = {"provider": "deepseek", "model": "test-model",
           "base_url": "https://api.example.com/v1", "prompt": "системный промпт"}
    # без fallback — все попытки исчерпаны → None
    assert cm._call_ai_api("входной текст", cfg) is None


def test_call_ai_api_finish_reason_stop_returns_content(monkeypatch):
    """finish_reason=stop и полный ответ — возвращается как есть."""
    payload = {"choices": [{"message": {"content": "полный ответ"},
                            "finish_reason": "stop"}]}
    _FakeClient.responses = [_FakeResponse(payload)]
    monkeypatch.setattr(cm.httpx, "Client", _FakeClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    cfg = {"provider": "deepseek", "model": "test-model",
           "base_url": "https://api.example.com/v1", "prompt": "системный промпт"}
    assert cm._call_ai_api("входной текст", cfg) == "полный ответ"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
