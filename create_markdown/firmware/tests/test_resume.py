#!/usr/bin/env python3
"""Тесты режима возобновления пайплайна из кэшированных артефактов.

Архитектура: workflows/t_d2337df8/architecture-report.md (вариант A):
  - валидный tmp/<stem>/yandex_result.json -> pages из кэша, без
    send_to_yandex_ocr и без требования YANDEX_API_KEY;
  - валидные table_<idx>.md (первая непустая строка — маркер
    _TABLE_ID_MARKER_RE) -> vision пропускается, недостающие дозапускаются
    подмножеством через recognize_tables_vision;
  - локальные PyMuPDF-стадии (вырезка картинок/таблиц) перезапускаются
    детерминированно — итог побайтово равен полному прогону.

Кейсы 1-10 из §8 отчёта:
  1. полный кэш (yandex_result.json + все table_*.md) -> OCR/vision не
     вызываются, AI вызывается, итоговый .md создан, в логе «из кэша»
  2. нет yandex_result.json -> полный прогон OCR (send_to_yandex_ocr вызван)
  3. битый кэш (пустой файл, {}, [], не-JSON) -> полный прогон OCR
  4. частичные table_*.md -> recognize_tables_vision вызван ТОЛЬКО с
     недостающими таблицами (по table_idx)
  5. table_N.md без маркера -> считается нераспознанным -> дозапуск
  6. документ без таблиц -> vision-блок не выполняется
  7. без Яндекс-ключей + валидный кэш + --ai -> успех, OCR не вызывается
  8. без Яндекс-ключей + без кэша -> process_file возвращает False
  9. детерминизм: возобновлённый прогон побайтово равен полному прогону
 10. регрессия: .md + --ai не изменился (--rag-only покрыт остальным пакетом)

Сетевые функции замоканы: send_to_yandex_ocr, recognize_tables_vision,
_call_ai_api. parse_yandex_json_to_md НЕ мокается — при возобновлении md_text
строится повторным парсингом кэша (как в утверждённой архитектуре).
"""
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import create_markdown


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures и помощники
# ═══════════════════════════════════════════════════════════════════════════


def _block(text, x=0, y=0, layout_type="LAYOUT_TYPE_TEXT", width=1240):
    """Блок в формате Yandex OCR JSON."""
    return {
        "boundingBox": {
            "vertices": [
                {"x": x, "y": y},
                {"x": x + width, "y": y},
                {"x": x + width, "y": y + 20},
                {"x": x, "y": y + 20},
            ]
        },
        "lines": [{"text": text}],
        "layoutType": layout_type,
    }


def _pages(text="Привет мир"):
    """Минимальная страница OCR, детерминированно парсящаяся в Markdown."""
    return [{
        "result": {
            "textAnnotation": {
                "width": 1240, "height": 1754,
                "blocks": [_block(text)],
                "tables": [],
                "pictures": [],
            }
        }
    }]


@pytest.fixture
def fake_env(tmp_path):
    """Минимальные файлы: входной pdf + tmp-каталог для кэша."""
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out_base = tmp_path / "out"
    tmp_base = tmp_path / "tmp"
    file_tmp_dir = tmp_base / "input"
    file_tmp_dir.mkdir(parents=True)
    return {
        "pdf": str(pdf),
        "out_base": str(out_base),
        "tmp_base": str(tmp_base),
        "file_tmp_dir": str(file_tmp_dir),
    }


@pytest.fixture(autouse=True)
def _clean_ai_checkpoints():
    """Чекпойнты Этапа 6+7 пишутся в tmp/.ai_checkpoints относительно CWD —
    чистим до и после каждого теста, чтобы не было помех от прошлых прогонов."""
    ckpt_root = Path("tmp") / ".ai_checkpoints"

    def _wipe():
        if ckpt_root.exists():
            for p in ckpt_root.glob("*.json"):
                p.unlink()

    _wipe()
    yield
    _wipe()


def _write_cache(fake_env, pages=None) -> Path:
    """Записать валидный yandex_result.json в tmp/<stem>/."""
    pages = _pages() if pages is None else pages
    path = Path(fake_env["file_tmp_dir"]) / "yandex_result.json"
    path.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _table_images():
    """Две таблицы, как возвращает extract_table_images (ключ — table_idx)."""
    return [
        {"table_idx": 1, "id": "t_p1_0", "path": "table_1.png", "page": 0},
        {"table_idx": 2, "id": "t_p1_1", "path": "table_2.png", "page": 0},
    ]


def _write_table_md(fake_env, table_idx, marker=None, content="| a | b |\n| - | - |\n"):
    """Записать table_<idx>.md; маркер по умолчанию согласован с _table_images()."""
    if marker is None:
        marker = f"t_p1_{table_idx - 1}"
    path = Path(fake_env["file_tmp_dir"]) / f"table_{table_idx}.md"
    path.write_text(f"<!-- {marker} -->\n{content}", encoding="utf-8")
    return path


def _ai_config():
    return {
        "ai_postprocess": {"prompt": "Объединённый промпт."},
        "table_vision": {"api_key_env": "PROVOD_API_KEY", "prompt": "Vision промпт."},
    }


def _call_process_file(fake_env, *, use_ai, api_key="key", folder_id="folder",
                       config=None, table_images=None, ai_result="AI RESULT"):
    """Вызвать process_file с замоканными сетевыми/тяжёлыми зависимостями.

    parse_yandex_json_to_md НЕ мокается (реальный повторный парсинг кэша).
    Возвращает (ok, mocks), где mocks — dict замоканных функций:
    send_ocr, ximg, tab, rtv, post, ai.
    """
    if config is None:
        config = _ai_config()
    if table_images is None:
        table_images = []
    with patch("create_markdown.send_to_yandex_ocr", return_value=_pages()) as m_ocr, \
         patch("create_markdown.extract_images_from_pdf", return_value=[]) as m_ximg, \
         patch("create_markdown.extract_table_images", return_value=table_images) as m_tab, \
         patch("create_markdown.recognize_tables_vision",
               return_value=len(table_images)) as m_rtv, \
         patch("create_markdown.run_script_postprocess",
               side_effect=lambda md, img, **kw: md) as m_post, \
         patch("create_markdown._call_ai_api",
               side_effect=lambda *a, **k: ai_result) as m_ai:
        ok = create_markdown.process_file(
            fake_env["pdf"], use_ai, config,
            api_key=api_key, folder_id=folder_id,
            output_base=fake_env["out_base"], tmp_base=fake_env["tmp_base"],
        )
    return ok, {
        "send_ocr": m_ocr, "ximg": m_ximg, "tab": m_tab,
        "rtv": m_rtv, "post": m_post, "ai": m_ai,
    }


def _final_md(fake_env) -> Path:
    return Path(fake_env["out_base"]) / "input" / "input.md"


# ═══════════════════════════════════════════════════════════════════════════
# Юнит-тесты новых функций
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadCachedPages:
    def test_no_file_returns_none(self, tmp_path):
        assert create_markdown._load_cached_pages(tmp_path) is None

    def test_empty_file_returns_none(self, tmp_path):
        (tmp_path / "yandex_result.json").write_text("", encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) is None

    def test_object_json_returns_none(self, tmp_path):
        (tmp_path / "yandex_result.json").write_text("{}", encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) is None

    def test_empty_list_returns_none(self, tmp_path):
        (tmp_path / "yandex_result.json").write_text("[]", encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) is None

    def test_non_json_returns_none(self, tmp_path):
        (tmp_path / "yandex_result.json").write_text("not json", encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) is None

    def test_list_without_textannotation_returns_none(self, tmp_path):
        (tmp_path / "yandex_result.json").write_text(
            json.dumps([{"result": {}}]), encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) is None

    def test_valid_pages_returns_list(self, tmp_path):
        pages = _pages()
        (tmp_path / "yandex_result.json").write_text(
            json.dumps(pages, ensure_ascii=False), encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) == pages

    def test_broken_json_logs_warning(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        (tmp_path / "yandex_result.json").write_text("not json", encoding="utf-8")
        assert create_markdown._load_cached_pages(tmp_path) is None
        assert "Кэш yandex_result.json повреждён" in caplog.text


class TestVisionTableMissing:
    def test_no_files_all_missing(self, tmp_path):
        missing = create_markdown._vision_table_missing(_table_images(), tmp_path)
        assert [t["table_idx"] for t in missing] == [1, 2]

    def test_valid_marker_not_missing(self, tmp_path):
        (tmp_path / "table_1.md").write_text("<!-- t_p1_0 -->\n| a |\n", encoding="utf-8")
        (tmp_path / "table_2.md").write_text("<!-- t_p1_1 -->\n| b |\n", encoding="utf-8")
        missing = create_markdown._vision_table_missing(_table_images(), tmp_path)
        assert missing == []

    def test_leading_blank_lines_still_valid(self, tmp_path):
        # Валидность — по ПЕРВОЙ НЕПУСТОЙ строке (пустые строки в начале допустимы)
        (tmp_path / "table_1.md").write_text("\n\n<!-- t_p1_0 -->\n| a |\n",
                                             encoding="utf-8")
        missing = create_markdown._vision_table_missing(_table_images()[:1], tmp_path)
        assert missing == []

    def test_no_marker_means_missing(self, tmp_path):
        (tmp_path / "table_1.md").write_text("текст без маркера\n", encoding="utf-8")
        missing = create_markdown._vision_table_missing(_table_images()[:1], tmp_path)
        assert [t["table_idx"] for t in missing] == [1]

    def test_marker_not_first_line_means_missing(self, tmp_path):
        (tmp_path / "table_1.md").write_text("текст\n<!-- t_p1_0 -->\n", encoding="utf-8")
        missing = create_markdown._vision_table_missing(_table_images()[:1], tmp_path)
        assert [t["table_idx"] for t in missing] == [1]

    def test_entry_without_table_idx_means_missing(self, tmp_path):
        # legacy/битый вход без table_idx не может иметь валидного кэша
        entry = {"path": "table_1.png"}
        missing = create_markdown._vision_table_missing([entry], tmp_path)
        assert missing == [entry]


# ═══════════════════════════════════════════════════════════════════════════
# Кейс 1: полный кэш
# ═══════════════════════════════════════════════════════════════════════════


def test_full_cache_skips_ocr_and_vision(fake_env, caplog):
    """Полный кэш: OCR и vision не вызываются, AI вызывается, лог «из кэша»."""
    caplog.set_level(logging.INFO)
    _write_cache(fake_env)
    for ti in _table_images():
        _write_table_md(fake_env, ti["table_idx"])

    ok, mocks = _call_process_file(
        fake_env, use_ai=True, table_images=_table_images(),
    )

    assert ok is True
    mocks["send_ocr"].assert_not_called()
    mocks["rtv"].assert_not_called()
    mocks["ai"].assert_called_once()
    assert _final_md(fake_env).exists()
    assert "Данные Яндекса получены из кэша" in caplog.text
    assert "Распознанные таблицы: 2 из 2 (из кэша)" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Кейсы 2-3: нет/битый кэш -> полный прогон OCR
# ═══════════════════════════════════════════════════════════════════════════


def test_no_cache_full_ocr_run(fake_env, caplog):
    """Нет yandex_result.json -> send_to_yandex_ocr вызван (полный прогон)."""
    caplog.set_level(logging.INFO)
    ok, mocks = _call_process_file(fake_env, use_ai=False, table_images=[])

    assert ok is True
    mocks["send_ocr"].assert_called_once()
    # Полный прогон пишет кэш для будущих возобновлений
    assert (Path(fake_env["file_tmp_dir"]) / "yandex_result.json").exists()
    assert "Полный прогон: кэш yandex_result.json отсутствует" in caplog.text


@pytest.mark.parametrize("broken", [
    "",
    "{}",
    "[]",
    "not json at all",
])
def test_broken_cache_full_ocr_run(fake_env, broken, caplog):
    """Битый кэш (пустой, {}, [], не-JSON) -> полный прогон OCR."""
    caplog.set_level(logging.INFO)
    (Path(fake_env["file_tmp_dir"]) / "yandex_result.json").write_text(
        broken, encoding="utf-8")
    ok, mocks = _call_process_file(fake_env, use_ai=False, table_images=[])

    assert ok is True
    mocks["send_ocr"].assert_called_once()
    assert "Полный прогон: кэш yandex_result.json отсутствует" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Кейсы 4-5: частичный vision-кэш -> дозапуск подмножества
# ═══════════════════════════════════════════════════════════════════════════


def test_partial_table_cache_reruns_only_missing(fake_env, caplog):
    """Частичное table_*.md: vision дозапускается ТОЛЬКО по недостающим."""
    caplog.set_level(logging.INFO)
    _write_cache(fake_env)
    _write_table_md(fake_env, 1)  # таблица 1 в кэше, таблицы 2 нет

    ok, mocks = _call_process_file(
        fake_env, use_ai=True, table_images=_table_images(),
    )

    assert ok is True
    mocks["send_ocr"].assert_not_called()
    mocks["rtv"].assert_called_once()
    rerun = mocks["rtv"].call_args.args[0]
    assert [t["table_idx"] for t in rerun] == [2]
    assert "Распознавание таблиц: не хватает 1 из 2 — дозапуск vision" in caplog.text


def test_table_md_without_marker_rerun(fake_env):
    """table_N.md без маркера считается нераспознанным и дозапускается."""
    _write_cache(fake_env)
    # Файл есть, но первая строка — не маркер
    (Path(fake_env["file_tmp_dir"]) / "table_1.md").write_text(
        "пустой результат vision\n", encoding="utf-8")

    ok, mocks = _call_process_file(
        fake_env, use_ai=True, table_images=_table_images()[:1],
    )

    assert ok is True
    mocks["rtv"].assert_called_once()
    rerun = mocks["rtv"].call_args.args[0]
    assert [t["table_idx"] for t in rerun] == [1]


# ═══════════════════════════════════════════════════════════════════════════
# Кейс 6: документ без таблиц
# ═══════════════════════════════════════════════════════════════════════════


def test_document_without_tables_skips_vision(fake_env):
    """Документ без таблиц: vision-блок не выполняется, .md создаётся."""
    _write_cache(fake_env)
    ok, mocks = _call_process_file(fake_env, use_ai=True, table_images=[])

    assert ok is True
    mocks["rtv"].assert_not_called()
    assert _final_md(fake_env).exists()
    # AI-этап всё равно работает (md_text без табличных маркеров)
    mocks["ai"].assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# Кейсы 7-8: Яндекс-ключи
# ═══════════════════════════════════════════════════════════════════════════


def test_resume_without_yandex_keys_succeeds(fake_env, caplog):
    """Без Яндекс-ключей + валидный кэш + --ai -> успех без OCR."""
    caplog.set_level(logging.INFO)
    _write_cache(fake_env)
    for ti in _table_images():
        _write_table_md(fake_env, ti["table_idx"])

    ok, mocks = _call_process_file(
        fake_env, use_ai=True, api_key="", folder_id="",
        table_images=_table_images(),
    )

    assert ok is True
    mocks["send_ocr"].assert_not_called()
    mocks["rtv"].assert_not_called()
    assert "Данные Яндекса получены из кэша" in caplog.text


def test_no_keys_no_cache_returns_false(fake_env, caplog):
    """Без Яндекс-ключей + без кэша -> False и чёткая ошибка."""
    caplog.set_level(logging.INFO)
    ok, mocks = _call_process_file(fake_env, use_ai=False, api_key="", folder_id="")

    assert ok is False
    mocks["send_ocr"].assert_not_called()
    assert "YANDEX_API_KEY и YANDEX_FOLDER_ID должны быть заданы в .env" in caplog.text
    assert "кэш yandex_result.json отсутствует" in caplog.text


# ═══════════════════════════════════════════════════════════════════════════
# Кейс 9: детерминизм возобновлённого прогона vs полного
# ═══════════════════════════════════════════════════════════════════════════


def test_resume_output_equals_full_run(fake_env):
    """Возобновлённый прогон побайтово равен полному (эквивалентность
    локальной перевырезки + повторного парсинга кэша)."""
    # Полный прогон: OCR замокан, кэша нет -> send_to_yandex_ocr вызывается,
    # yandex_result.json пишется на диск.
    ok1, m1 = _call_process_file(
        fake_env, use_ai=True, table_images=_table_images(),
    )
    assert ok1 is True
    m1["send_ocr"].assert_called_once()
    full_md = _final_md(fake_env).read_bytes()
    assert full_md

    # Возобновление: тот же вход, кэш уже есть -> OCR не вызывается
    ok2, m2 = _call_process_file(
        fake_env, use_ai=True, table_images=_table_images(),
    )
    assert ok2 is True
    m2["send_ocr"].assert_not_called()
    resume_md = _final_md(fake_env).read_bytes()

    assert resume_md == full_md


# ═══════════════════════════════════════════════════════════════════════════
# Кейс 10: регрессия .md + --ai (--rag-only покрыт остальным пакетом)
# ═══════════════════════════════════════════════════════════════════════════


def test_regression_md_ai_unchanged(tmp_path):
    """.md + --ai: контракт не изменился — AI-постобработка готового Markdown."""
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Заголовок\n\nТекст.\n", encoding="utf-8")

    with patch("create_markdown.ai_postprocess",
               side_effect=lambda md, cfg, label: md + "\n\nДобавлено AI") as m_ai:
        ok = create_markdown.process_file(
            str(md_file), use_ai=True,
            config={"ai_postprocess": {"prompt": "x"}},
            api_key="", folder_id="",
            output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
        )

    assert ok is True
    m_ai.assert_called_once()
    out = tmp_path / "doc_ai.md"
    assert out.exists()
    assert "Добавлено AI" in out.read_text(encoding="utf-8")


def test_regression_md_without_flags_fails(tmp_path):
    """.md без --ai/--rag -> ошибка (существующее поведение не изменилось)."""
    md_file = tmp_path / "doc.md"
    md_file.write_text("текст", encoding="utf-8")
    ok = create_markdown.process_file(
        str(md_file), use_ai=False, config={},
        api_key="", folder_id="",
        output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
    )
    assert ok is False
