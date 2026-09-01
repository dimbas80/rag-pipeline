#!/usr/bin/env python3
"""Тесты объединённого Этапа 6+7 (AI-коррекция таблиц + постобработка) в составе --ai.

С коммита d5bdebe отдельного этапа «gap-filling» больше нет: Этап 6 и Этап 7
объединены в один проход внутри process_file (см. create_markdown.py,
область ~строки 6688-6825). Секция конфига ai_table удалена полностью.

Тесты проверяют контракт объединённого этапа:
  (а) системный промпт берётся из ai_postprocess.prompt, fallback — table_vision.prompt;
      настройки провайдера — ai_postprocess, fallback — postprocess;
  (б) нет промпта нигде → SystemExit(1);
  (в) нет table_*.md → этап работает без сверки таблиц;
      vision-таблицы из tmp/<stem>/table_*.md подставляются в чанк по ID-маркеру;
  (г) чанкование большого md_text (каждый чанк <= AI_MAX_CHARS, контекст
      "<stem> [ч. N]", содержимое не теряется, результаты склеены);
  (е) частичные ответы (часть чанков None) — в итог попадают успешные,
      неуспешные сохраняют исходный чанк;
  (ж) результат единого прохода записывается в итоговый .md.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import create_markdown


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_env(tmp_path):
    """Минимальные файлы: входной pdf + tmp-каталог для vision-таблиц."""
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


def _ai_pages():
    """Минимальная страница OCR для process_file с --ai (без таблиц)."""
    return [{
        "result": {
            "textAnnotation": {
                "width": 1240, "height": 1754,
                "blocks": [], "tables": [], "pictures": [],
            }
        }
    }]


def _run_process_file(fake_env, config, md_text="# md\n", api_side_effect=None):
    """Прогнать process_file(--ai) с замоканными тяжёлыми зависимостями.

    Возвращает (ok, mock_call), где mock_call — замоканный
    create_markdown._call_ai_api (позволяет проверять аргументы вызовов).
    """
    if api_side_effect is None:
        api_side_effect = lambda *a, **k: "AI RESULT"

    with patch("create_markdown.send_to_yandex_ocr", return_value=_ai_pages()), \
         patch("create_markdown.parse_yandex_json_to_md", return_value=(md_text, [], [])), \
         patch("create_markdown.extract_table_images", return_value=[]), \
         patch("create_markdown.run_script_postprocess", side_effect=lambda md, img, **kw: md), \
         patch("create_markdown._call_ai_api", side_effect=api_side_effect) as mock_call:
        ok = create_markdown.process_file(
            fake_env["pdf"], True, config,
            api_key="key", folder_id="folder",
            output_base=fake_env["out_base"], tmp_base=fake_env["tmp_base"],
        )
    return ok, mock_call


def _big_md_text():
    """Большой md_text (> AI_MAX_CHARS) из секций с заголовками."""
    sections = "\n\n".join(
        f"## {i}. Раздел\n\nПараграф {i} " + "x" * 300 for i in range(300)
    )
    assert len(sections) > create_markdown.AI_MAX_CHARS
    return sections


# ═══════════════════════════════════════════════════════════════════════════
# (а) Промпт: ai_postprocess → fallback table_vision; провайдер: postprocess
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_prompt_from_ai_postprocess(fake_env):
    """(а) Промпт берётся из ai_postprocess; настройки провайдера — оттуда же."""
    cfg = {
        "ai_postprocess": {
            "provider": "deepseek",
            "model": "m",
            "base_url": "https://api.deepseek.com/v1",
            "prompt": "Промпт ai_postprocess.",
        },
        "table_vision": {"prompt": "Промпт table_vision."},
    }
    ok, mock_call = _run_process_file(fake_env, cfg)
    assert ok is True
    assert mock_call.call_count == 1
    ai_cfg = mock_call.call_args.args[1]
    # Промпт подменён на объединённый — из ai_postprocess (table_vision игнорируется)
    assert ai_cfg["prompt"] == "Промпт ai_postprocess."
    # Настройки провайдера — из секции ai_postprocess
    assert ai_cfg["provider"] == "deepseek"
    assert ai_cfg["model"] == "m"


def test_unified_prompt_fallback_to_table_vision(fake_env):
    """(а) Нет ai_postprocess.prompt → fallback на table_vision.prompt."""
    cfg = {
        "ai_postprocess": {"provider": "deepseek", "model": "m"},
        "table_vision": {"prompt": "Промпт table_vision."},
    }
    ok, mock_call = _run_process_file(fake_env, cfg)
    assert ok is True
    assert mock_call.call_count == 1
    ai_cfg = mock_call.call_args.args[1]
    assert ai_cfg["prompt"] == "Промпт table_vision."


def test_unified_provider_config_fallback_to_postprocess(fake_env):
    """Настройки провайдера: нет ai_postprocess → берутся из postprocess,
    промпт всё равно объединённый (table_vision)."""
    cfg = {
        "postprocess": {"provider": "provod", "model": "p", "prompt": "P"},
        "table_vision": {"prompt": "Промпт table_vision."},
    }
    ok, mock_call = _run_process_file(fake_env, cfg)
    assert ok is True
    ai_cfg = mock_call.call_args.args[1]
    assert ai_cfg["provider"] == "provod"
    assert ai_cfg["model"] == "p"
    # Промпт — объединённый: из table_vision, а не из postprocess.prompt
    assert ai_cfg["prompt"] == "Промпт table_vision."


# ═══════════════════════════════════════════════════════════════════════════
# (б) Нет промпта нигде → SystemExit(1)
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_missing_prompt_exits(fake_env):
    """(б) Нет prompt ни в ai_postprocess, ни в table_vision → SystemExit(1)."""
    with pytest.raises(SystemExit) as exc:
        _run_process_file(fake_env, {"ai_postprocess": {"provider": "deepseek", "model": "m"}})
    assert exc.value.code == 1


# ═══════════════════════════════════════════════════════════════════════════
# (в) Vision-таблицы: нет файлов → работаем без сверки; есть → подставляем
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_no_table_files_still_runs(fake_env):
    """(в) Нет table_*.md → этап работает без сверки таблиц."""
    ok, mock_call = _run_process_file(fake_env, {"ai_postprocess": {"prompt": "p"}})
    assert ok is True
    assert mock_call.call_count == 1
    # Без vision-таблиц в чанке нет секции «Эталонные таблицы»
    chunk_input = mock_call.call_args.args[0]
    assert "Эталонные таблицы" not in chunk_input
    # Итоговый .md всё равно записан
    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    assert out_md.exists()


def test_unified_attaches_matching_vision_table(fake_env):
    """Vision-таблицы из tmp/<stem>/table_*.md подставляются в чанк по ID-маркеру."""
    table_text = "| A | B |\n|---|---|\n| 1 | 2 |"
    (Path(fake_env["file_tmp_dir"]) / "table_1.md").write_text(
        "<!-- t_p1_0 -->\n" + table_text + "\n", encoding="utf-8"
    )
    md_text = "## 1. Раздел\n\nТаблица:\n\n<!-- t_p1_0 -->\n"
    ok, mock_call = _run_process_file(
        fake_env, {"ai_postprocess": {"prompt": "p"}}, md_text=md_text
    )
    assert ok is True
    assert mock_call.call_count == 1
    chunk_input = mock_call.call_args.args[0]
    # Маркер в md_text → эталонная таблица добавлена в чанк
    assert "=== Эталонные таблицы ===" in chunk_input
    assert table_text in chunk_input


# ═══════════════════════════════════════════════════════════════════════════
# (г) Чанкование большого md_text
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_chunks_large_md_text(fake_env):
    """(г) Большой md_text чанкуется: чанк <= AI_MAX_CHARS, контекст
    "<stem> [ч. N]", содержимое не теряется, результаты склеены.

    Фейковый ответ сопоставим с чанком по длине: гард полноты
    (_ai_result_or_original) отбраковывает усечённые ответы, поэтому
    «короткая заглушка» в итог не попадает.
    """
    big_md = _big_md_text()
    calls = []

    def fake_ai(text, cfg, context=""):
        n = len(calls)
        calls.append((text, context))
        return text + f" [AI_RESULT_{n}]"

    ok, mock_call = _run_process_file(
        fake_env, {"ai_postprocess": {"prompt": "p"}},
        md_text=big_md, api_side_effect=fake_ai,
    )
    assert ok is True
    assert mock_call.call_count > 1
    # Контекст каждого чанка — "<stem> [ч.{i + 1}]", ни один чанк не превышает лимит
    all_input = ""
    for i, (text, context) in enumerate(calls):
        assert context == f"input [ч.{i + 1}]"
        assert len(text) <= create_markdown.AI_MAX_CHARS
        all_input += text
    # Содержимое не теряется: все параграфы есть в чанках
    for i in range(300):
        assert f"Параграф {i}" in all_input
    # Результаты склеены в итоговый .md
    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    content = out_md.read_text(encoding="utf-8")
    for i in range(len(calls)):
        assert f"AI_RESULT_{i}" in content


# ═══════════════════════════════════════════════════════════════════════════
# (е) Частичные ответы (часть чанков None)
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_partial_results_keep_chunk(fake_env):
    """(е) Часть чанков вернула None → в итог попадают только успешные,
    неуспешные сохраняют исходный чанк (содержимое не теряется)."""
    big_md = _big_md_text()
    expected_chunks = create_markdown._chunk_text(big_md, create_markdown.AI_MAX_CHARS)
    assert len(expected_chunks) > 1

    counter = [0]

    def fake_ai(text, cfg, context=""):
        n = counter[0]
        counter[0] += 1
        # Успешный ответ сопоставим с чанком по длине (гард полноты отбраковывает
        # усечённые ответы), неуспешный — None (исходный чанк сохраняется).
        return text + f" [AI_OK_{n}]" if n % 2 == 0 else None

    ok, mock_call = _run_process_file(
        fake_env, {"ai_postprocess": {"prompt": "p"}},
        md_text=big_md, api_side_effect=fake_ai,
    )
    assert ok is True
    assert mock_call.call_count == len(expected_chunks)
    assert mock_call.call_count > 1

    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    content = out_md.read_text(encoding="utf-8")
    # Успешные ответы в итоге, «None» не попадает
    for n in range(0, counter[0], 2):
        assert f"AI_OK_{n}" in content
    assert "None" not in content
    # Неуспешные чанки (нечётные вызовы) сохранили исходное содержимое
    for n in range(1, len(expected_chunks), 2):
        assert expected_chunks[n].strip() in content


# ═══════════════════════════════════════════════════════════════════════════
# (ж) Результат единого прохода → итоговый .md
# ═══════════════════════════════════════════════════════════════════════════


def test_unified_single_pass_writes_final_md(fake_env):
    """(ж) Результат единого прохода записывается в итоговый .md."""
    ok, mock_call = _run_process_file(fake_env, {"ai_postprocess": {"prompt": "p"}})
    assert ok is True
    assert mock_call.call_count == 1
    # Контекст единственного чанка — "<stem> [ч.1]"
    assert mock_call.call_args.args[2] == "input [ч.1]"
    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    assert out_md.exists()
    assert out_md.read_text(encoding="utf-8") == "AI RESULT"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
