#!/usr/bin/env python3
"""Тесты gap-filling (в составе --ai): берёт prompt из секции ai_table конфига."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import pipeline


@pytest.fixture
def fake_env(tmp_path):
    """Минимальные файлы: входной pdf, tmp-каталог с table_1.md."""
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out_base = tmp_path / "out"
    tmp_base = tmp_path / "tmp"
    (tmp_base / "input").mkdir(parents=True)
    (tmp_base / "input" / "table_1.md").write_text(
        "| A | B |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8"
    )
    return {
        "pdf": str(pdf),
        "out_base": str(out_base),
        "tmp_base": str(tmp_base),
        "table_text": (tmp_base / "input" / "table_1.md").read_text(encoding="utf-8"),
    }


def _run_process_file(fake_env, config, use_ai=True,
                      md_text="# md\n", order=None):
    """Прогнать process_file с замоканными тяжёлыми зависимостями.

    md_text: текст, который вернёт parse_yandex_json_to_md (влияет на размер gap_input).
    order:   опциональный список; в него записывается порядок вызовов
             ("gap_filling" → "_call_ai_api", "ai_postprocess" → ai_postprocess).
    """
    pages = [{"result": {"textAnnotation": {"width": 10, "height": 10,
                                             "blocks": [], "tables": [], "pictures": []}}}]

    def record(name):
        if order is not None:
            order.append(name)

    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md",
               return_value=(md_text, [], [])), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.extract_table_images", return_value=[]), \
         patch("pipeline.recognize_tables_vision", return_value=0), \
         patch("pipeline.run_script_postprocess",
               side_effect=lambda md, img, **kw: md), \
         patch("pipeline.ai_postprocess",
               side_effect=lambda md, cfg, label: (record("ai_postprocess"), md)[1]), \
         patch("pipeline._call_ai_api",
               side_effect=lambda *a, **k: (record("gap_filling"), "GAP RESULT")[1]) as mock_call:
        ok = pipeline.process_file(
            fake_env["pdf"],
            use_ai,
            config,
            api_key="key",
            folder_id="folder",
            output_base=fake_env["out_base"],
            tmp_base=fake_env["tmp_base"],
        )
        return ok, mock_call


def test_gap_filling_uses_ai_table_prompt(fake_env):
    """Gap-filling собирает gap_input из prompt секции ai_table и вызывает API."""
    cfg = {
        "ai_table": {
            "provider": "deepseek",
            "model": "m",
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "prompt": "Конфиг-промт для gap-filling.",
        }
    }
    ok, mock_call = _run_process_file(fake_env, cfg)
    assert ok is True
    assert mock_call.call_count == 1
    gap_input = mock_call.call_args.args[0]
    # Промт из конфига стоит в начале, без хардкод-инструкции
    assert gap_input.startswith("Конфиг-промт для gap-filling.\n\n")
    assert "=== Markdown-файл ===" in gap_input
    assert "=== Файлы таблиц (vision-распознавание) ===" in gap_input
    assert fake_env["table_text"] in gap_input
    assert "Проверь все таблицы в Markdown-файле ниже" not in gap_input
    # В _call_ai_api передаётся секция ai_table (не ai_postprocess)
    assert mock_call.call_args.args[1] is cfg["ai_table"]


def test_gap_filling_missing_prompt_exits(fake_env):
    """ai_table без prompt → sys.exit(1) с сообщением."""
    cfg = {"ai_table": {"provider": "deepseek", "model": "m"}}
    with pytest.raises(SystemExit) as exc:
        _run_process_file(fake_env, cfg)
    assert exc.value.code == 1


def test_gap_filling_no_ai_table_section_exits(fake_env):
    """Конфиг вообще без секции ai_table → sys.exit(1)."""
    with pytest.raises(SystemExit) as exc:
        _run_process_file(fake_env, {"ai_postprocess": {"prompt": "x"}})
    assert exc.value.code == 1


def test_gap_filling_no_table_files_skips(fake_env):
    """Нет table_*.md → gap-filling не запускается, файл обрабатывается без него."""
    (Path(fake_env["tmp_base"]) / "input" / "table_1.md").unlink()
    cfg = {"ai_table": {"prompt": "p"}}
    ok, mock_call = _run_process_file(fake_env, cfg)
    assert ok is True
    assert mock_call.call_count == 0


def test_gap_filling_single_call_context(fake_env):
    """Маленький gap_input → один вызов _call_ai_api с контекстом = file_stem."""
    cfg = {"ai_table": {"prompt": "p"}}
    ok, mock_call = _run_process_file(fake_env, cfg)
    assert ok is True
    assert mock_call.call_count == 1
    assert mock_call.call_args.args[2] == "input"  # file_stem от input.pdf


def test_gap_filling_chunks_large_input(fake_env):
    """Большой gap_input (> AI_MAX_CHARS) разбивается на чанки, результаты склеиваются."""
    big_md = "\n\n".join(f"Параграф {i} " + "x" * 500 for i in range(200))
    assert len(big_md) > pipeline.AI_MAX_CHARS
    cfg = {"ai_table": {"prompt": "p"}}
    ok, mock_call = _run_process_file(fake_env, cfg, md_text=big_md)
    assert ok is True
    # Больше одного вызова — чанкинг сработал
    assert mock_call.call_count > 1
    # Каждый вызов — свой чанк с номером в контексте, ни один не превышает лимит
    all_chunks = ""
    for i, call in enumerate(mock_call.call_args_list):
        assert call.args[2] == f"input [gap ч.{i + 1}]"
        assert len(call.args[0]) <= pipeline.AI_MAX_CHARS
        all_chunks += call.args[0]
    # Содержимое не теряется: все параграфы и заголовки секций есть в чанках
    assert "=== Markdown-файл ===" in all_chunks
    assert "=== Файлы таблиц (vision-распознавание) ===" in all_chunks
    for i in range(200):
        assert f"Параграф {i}" in all_chunks
    assert fake_env["table_text"].strip() in all_chunks
    # Результаты склеены через \n\n — итоговый файл содержит результат каждого чанка
    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")
    assert content.count("GAP RESULT") == mock_call.call_count


def test_gap_filling_chunk_partial_results(fake_env):
    """Если модель ответила не на все чанки — склеиваются только успешные."""
    big_md = "\n\n".join(f"Параграф {i} " + "x" * 500 for i in range(200))

    counter = [0]

    def fake_gap(*args, **kwargs):
        # Отвечаем только на нечётные вызовы
        n = counter[0]
        counter[0] += 1
        return f"RESULT {n}" if n % 2 == 0 else None

    pages = [{"result": {"textAnnotation": {"width": 10, "height": 10,
                                             "blocks": [], "tables": [], "pictures": []}}}]
    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", return_value=(big_md, [], [])), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.extract_table_images", return_value=[]), \
         patch("pipeline.recognize_tables_vision", return_value=0), \
         patch("pipeline.run_script_postprocess", side_effect=lambda md, img, **kw: md), \
         patch("pipeline.ai_postprocess", side_effect=lambda md, cfg, label: md), \
         patch("pipeline._call_ai_api", side_effect=fake_gap) as mock_call:
        ok = pipeline.process_file(
            fake_env["pdf"], True,
            {"ai_table": {"prompt": "p"}},
            api_key="key", folder_id="folder",
            output_base=fake_env["out_base"], tmp_base=fake_env["tmp_base"],
        )
    assert ok is True
    assert mock_call.call_count > 1
    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")
    # Только успешные ответы попали в итог
    assert content.count("RESULT ") == mock_call.call_count // 2 + mock_call.call_count % 2


def test_gap_filling_runs_before_ai_postprocess(fake_env):
    """Новый порядок: gap-filling (Этап 6) идёт ДО ai_postprocess (Этап 7)."""
    order = []
    cfg = {"ai_table": {"prompt": "p"}}
    ok, _ = _run_process_file(fake_env, cfg, order=order)
    assert ok is True
    assert order == ["gap_filling", "ai_postprocess"]


def test_gap_filling_output_feeds_ai_postprocess(fake_env):
    """ai_postprocess получает на вход результат gap-filling (не исходный md)."""
    captured = {}

    def fake_post(md, cfg, label):
        captured["md"] = md
        return md

    pages = [{"result": {"textAnnotation": {"width": 10, "height": 10,
                                             "blocks": [], "tables": [], "pictures": []}}}]
    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", return_value=("# md\n", [], [])), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.extract_table_images", return_value=[]), \
         patch("pipeline.recognize_tables_vision", return_value=0), \
         patch("pipeline.run_script_postprocess", side_effect=lambda md, img, **kw: md), \
         patch("pipeline.ai_postprocess", side_effect=fake_post), \
         patch("pipeline._call_ai_api", return_value="GAP RESULT"):
        ok = pipeline.process_file(
            fake_env["pdf"], True,
            {"ai_table": {"prompt": "p"}},
            api_key="key", folder_id="folder",
            output_base=fake_env["out_base"], tmp_base=fake_env["tmp_base"],
        )
    assert ok is True
    assert captured.get("md") == "GAP RESULT"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
