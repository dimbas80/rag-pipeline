"""BASE_DIR: корень документов для main() — --base-dir > env BASE_DIR > входной файл.

Интерфейс передаёт корень через переменную окружения BASE_DIR (общий .env),
бот и установщик — тем же механизмом. Без корня сохраняется прежнее поведение:
папки определяются от расположения входного файла.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import create_markdown  # noqa: E402


def _run_main(monkeypatch, tmp_path, argv, env=None):
    """Вызвать main() с подменённым process_file и вернуть (output_base, tmp_base)."""
    captured = {}

    def fake_process_file(input_path, *args, **kwargs):
        captured["output_base"] = args[4]
        captured["tmp_base"] = args[5]
        return True

    monkeypatch.setattr(create_markdown, "process_file", fake_process_file)
    if env is None:
        monkeypatch.delenv("BASE_DIR", raising=False)
    else:
        monkeypatch.setenv("BASE_DIR", env)
    monkeypatch.chdir(tmp_path)
    with patch.object(sys, "argv", ["create_markdown.py"] + argv):
        create_markdown.main()
    return captured


class TestBaseDir:
    def test_parse_args_base_dir_default_empty(self):
        ns = create_markdown.parse_args(["-i", "x.pdf"])
        assert ns.base_dir == ""

    def test_parse_args_base_dir_flag(self):
        ns = create_markdown.parse_args(["-i", "x.pdf", "--base-dir", "/base"])
        assert ns.base_dir == "/base"

    def test_flag_sets_output_and_tmp(self, monkeypatch, tmp_path):
        """--base-dir: итог в <base>/Markdown/<stem>/, временные в <base>/tmp/<stem>/."""
        src = tmp_path / "incoming" / "doc.md"
        src.parent.mkdir(parents=True)
        src.write_text("# ТЕКСТ\n", encoding="utf-8")
        base = tmp_path / "base"
        captured = _run_main(monkeypatch, tmp_path,
                             ["-i", str(src), "--base-dir", str(base)])
        assert Path(captured["output_base"]) == base / "Markdown"
        assert Path(captured["tmp_base"]) == base / "tmp"

    def test_env_base_dir_used_without_flag(self, monkeypatch, tmp_path):
        """Без флага, но с BASE_DIR в окружении (общий .env интерфейса → build_env)."""
        src = tmp_path / "incoming" / "doc.md"
        src.parent.mkdir(parents=True)
        src.write_text("# ТЕКСТ\n", encoding="utf-8")
        base = tmp_path / "base"
        captured = _run_main(monkeypatch, tmp_path, ["-i", str(src)],
                             env=str(base))
        assert Path(captured["output_base"]) == base / "Markdown"
        assert Path(captured["tmp_base"]) == base / "tmp"

    def test_flag_beats_env(self, monkeypatch, tmp_path):
        src = tmp_path / "doc.md"
        src.write_text("# ТЕКСТ\n", encoding="utf-8")
        captured = _run_main(monkeypatch, tmp_path,
                             ["-i", str(src), "--base-dir", str(tmp_path / "b1")],
                             env=str(tmp_path / "b2"))
        assert Path(captured["output_base"]) == tmp_path / "b1" / "Markdown"

    def test_no_flag_no_env_falls_back_to_input_dir(self, monkeypatch, tmp_path):
        """Обратная совместимость: без корня папки — от входного файла."""
        src = tmp_path / "incoming" / "doc.md"
        src.parent.mkdir(parents=True)
        src.write_text("# ТЕКСТ\n", encoding="utf-8")
        captured = _run_main(monkeypatch, tmp_path, ["-i", str(src)])
        assert Path(captured["output_base"]) == src.parent / "Markdown"
        assert Path(captured["tmp_base"]) == src.parent / "tmp"

    def test_md_inside_markdown_without_base_tmp_beside(self, monkeypatch, tmp_path):
        """Прежнее правило: .md внутри Markdown/ без корня → tmp рядом с Markdown/."""
        root = tmp_path / "base"
        md = root / "Markdown" / "doc" / "doc.md"
        md.parent.mkdir(parents=True)
        md.write_text("# ТЕКСТ\n", encoding="utf-8")
        captured = _run_main(monkeypatch, tmp_path, ["-i", str(md), "--rag"])
        assert Path(captured["output_base"]) == md.parent / "Markdown"
        assert Path(captured["tmp_base"]) == root / "tmp"

    def test_md_inside_markdown_with_base_no_rewalk(self, monkeypatch, tmp_path):
        """С корнем --rag для .md из другого дерева Markdown/ не уводит tmp наружу."""
        src_root = tmp_path / "elsewhere"
        md = src_root / "Markdown" / "doc" / "doc.md"
        md.parent.mkdir(parents=True)
        md.write_text("# ТЕКСТ\n", encoding="utf-8")
        base = tmp_path / "base"
        captured = _run_main(monkeypatch, tmp_path,
                             ["-i", str(md), "--rag", "--base-dir", str(base)])
        assert Path(captured["tmp_base"]) == base / "tmp"
