import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import create_markdown


def test_parse_args_exposes_json_native_flag():
    args = create_markdown.parse_args(["-i", "document.pdf", "--json-native"])
    assert args.json_native is True


def test_parse_args_keeps_json_native_disabled_by_default():
    args = create_markdown.parse_args(["-i", "document.pdf"])
    assert args.json_native is False


def test_process_file_accepts_json_native_switch(monkeypatch, tmp_path):
    calls = []
    pages = [{"result": {"textAnnotation": {"width": 100, "height": 100}}}]
    monkeypatch.setattr(create_markdown, "send_to_yandex_ocr", lambda *args: pages)
    monkeypatch.setattr(create_markdown, "parse_yandex_json_to_model", lambda value: object())
    monkeypatch.setattr(create_markdown, "stitch_tables", lambda doc: calls.append("stitch"))
    monkeypatch.setattr(create_markdown, "extract_table_images_from_model", lambda *args: calls.append("tables"))
    monkeypatch.setattr(create_markdown, "extract_pictures_from_model", lambda *args: calls.append("pictures"))
    monkeypatch.setattr(create_markdown, "populate_component_images", lambda doc: calls.append("populate"))
    monkeypatch.setattr(create_markdown, "render_document_to_md", lambda doc: "native markdown")
    monkeypatch.setattr(create_markdown, "merge_tables_by_model", lambda text, doc: text)
    monkeypatch.setattr(create_markdown, "run_script_postprocess", lambda text, img, **kwargs: text)

    assert create_markdown.process_file(
        str(tmp_path / "input.pdf"), False, {}, "key", "folder",
        str(tmp_path / "Markdown"), str(tmp_path / "tmp"),
        use_json_native=True,
    ) is True
    assert calls == ["stitch", "tables", "pictures", "populate"]
    assert (tmp_path / "Markdown" / "input" / "input.md").read_text() == "native markdown"
    assert not (tmp_path / "tmp" / "input" / "raw.md").exists()


def test_native_path_does_not_run_legacy_parser(monkeypatch, tmp_path):
    pages = [{"result": {"textAnnotation": {"width": 100, "height": 100}}}]
    monkeypatch.setattr(create_markdown, "send_to_yandex_ocr", lambda *args: pages)
    monkeypatch.setattr(create_markdown, "parse_yandex_json_to_model", lambda value: SimpleNamespace(tables=[]))
    monkeypatch.setattr(create_markdown, "stitch_tables", lambda doc: None)
    monkeypatch.setattr(create_markdown, "extract_table_images_from_model", lambda *args: None)
    monkeypatch.setattr(create_markdown, "extract_pictures_from_model", lambda *args: None)
    monkeypatch.setattr(create_markdown, "populate_component_images", lambda doc: None)
    monkeypatch.setattr(create_markdown, "render_document_to_md", lambda doc: "native markdown")
    monkeypatch.setattr(create_markdown, "merge_tables_by_model", lambda text, doc: text)
    monkeypatch.setattr(create_markdown, "run_script_postprocess", lambda text, img, **kwargs: text)
    monkeypatch.setattr(create_markdown, "parse_yandex_json_to_md", lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy")))

    assert create_markdown.process_file(
        str(tmp_path / "input.pdf"), False, {}, "key", "folder",
        str(tmp_path / "Markdown"), str(tmp_path / "tmp"),
        use_json_native=True,
    ) is True


def test_native_ai_uses_model_vision_loader(monkeypatch, tmp_path):
    pages = [{"result": {"textAnnotation": {"width": 100, "height": 100}}}]
    calls = []
    monkeypatch.setattr(create_markdown, "send_to_yandex_ocr", lambda *args: pages)
    monkeypatch.setattr(create_markdown, "parse_yandex_json_to_model", lambda value: SimpleNamespace(tables=[]))
    monkeypatch.setattr(create_markdown, "stitch_tables", lambda doc: None)
    monkeypatch.setattr(create_markdown, "extract_table_images_from_model", lambda *args: None)
    monkeypatch.setattr(create_markdown, "extract_pictures_from_model", lambda *args: None)
    monkeypatch.setattr(create_markdown, "populate_component_images", lambda doc: None)
    monkeypatch.setattr(create_markdown, "render_document_to_md", lambda doc: "native markdown")
    monkeypatch.setattr(create_markdown, "merge_tables_by_model", lambda text, doc: text)
    monkeypatch.setattr(create_markdown, "run_script_postprocess", lambda text, img, **kwargs: text)
    monkeypatch.setattr(create_markdown, "load_vision_tables_from_model", lambda doc, tmp: calls.append("load") or [])
    monkeypatch.setattr(create_markdown, "ai_postprocess_json_native", lambda text, cfg, label, vision: calls.append("ai") or text)

    assert create_markdown.process_file(
        str(tmp_path / "input.pdf"), True, {"ai_postprocess": {"prompt": "p"}}, "key", "folder",
        str(tmp_path / "Markdown"), str(tmp_path / "tmp"),
        use_json_native=True,
    ) is True
    assert calls == ["load", "ai"]


def test_native_flag_is_forwarded_by_main(monkeypatch, tmp_path):
    input_path = tmp_path / "input.md"
    input_path.write_text("x", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(create_markdown, "process_file", lambda *args, **kwargs: captured.update(kwargs) or True)
    monkeypatch.setattr(create_markdown, "load_config", lambda path: {})
    monkeypatch.setattr(create_markdown, "find_input_files", lambda path: [input_path])
    monkeypatch.setattr(create_markdown, "setup_logging", lambda path: None)
    monkeypatch.setattr(create_markdown, "load_env", lambda path: None)
    monkeypatch.setattr(create_markdown, "parse_args", lambda: type("Args", (), {
        "input": str(input_path), "ai": False, "json_native": True, "rag": False,
        "reg": False, "config": "config", "rag_config": "rag",
    })())
    create_markdown.main()
    assert captured["use_json_native"] is True
