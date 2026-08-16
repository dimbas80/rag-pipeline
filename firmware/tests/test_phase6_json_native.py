import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pipeline


def test_parse_args_exposes_json_native_flag():
    args = pipeline.parse_args(["-i", "document.pdf", "--json-native"])
    assert args.json_native is True


def test_parse_args_keeps_json_native_disabled_by_default():
    args = pipeline.parse_args(["-i", "document.pdf"])
    assert args.json_native is False


def test_process_file_accepts_json_native_switch(monkeypatch, tmp_path):
    calls = []
    pages = [{"result": {"textAnnotation": {"width": 100, "height": 100}}}]
    monkeypatch.setattr(pipeline, "send_to_yandex_ocr", lambda *args: pages)
    monkeypatch.setattr(pipeline, "parse_yandex_json_to_model", lambda value: object())
    monkeypatch.setattr(pipeline, "stitch_tables", lambda doc: calls.append("stitch"))
    monkeypatch.setattr(pipeline, "extract_table_images_from_model", lambda *args: calls.append("tables"))
    monkeypatch.setattr(pipeline, "extract_pictures_from_model", lambda *args: calls.append("pictures"))
    monkeypatch.setattr(pipeline, "populate_component_images", lambda doc: calls.append("populate"))
    monkeypatch.setattr(pipeline, "render_document_to_md", lambda doc: "native markdown")
    monkeypatch.setattr(pipeline, "merge_tables_by_model", lambda text, doc: text)
    monkeypatch.setattr(pipeline, "run_script_postprocess", lambda text, img, **kwargs: text)

    assert pipeline.process_file(
        str(tmp_path / "input.pdf"), False, {}, "key", "folder",
        str(tmp_path / "Markdown"), str(tmp_path / "tmp"),
        use_json_native=True,
    ) is True
    assert calls == ["stitch", "tables", "pictures", "populate"]
    assert (tmp_path / "Markdown" / "input" / "input.md").read_text() == "native markdown"
    assert not (tmp_path / "tmp" / "input" / "raw.md").exists()


def test_native_path_does_not_run_legacy_parser(monkeypatch, tmp_path):
    pages = [{"result": {"textAnnotation": {"width": 100, "height": 100}}}]
    monkeypatch.setattr(pipeline, "send_to_yandex_ocr", lambda *args: pages)
    monkeypatch.setattr(pipeline, "parse_yandex_json_to_model", lambda value: SimpleNamespace(tables=[]))
    monkeypatch.setattr(pipeline, "stitch_tables", lambda doc: None)
    monkeypatch.setattr(pipeline, "extract_table_images_from_model", lambda *args: None)
    monkeypatch.setattr(pipeline, "extract_pictures_from_model", lambda *args: None)
    monkeypatch.setattr(pipeline, "populate_component_images", lambda doc: None)
    monkeypatch.setattr(pipeline, "render_document_to_md", lambda doc: "native markdown")
    monkeypatch.setattr(pipeline, "merge_tables_by_model", lambda text, doc: text)
    monkeypatch.setattr(pipeline, "run_script_postprocess", lambda text, img, **kwargs: text)
    monkeypatch.setattr(pipeline, "parse_yandex_json_to_md", lambda **kwargs: (_ for _ in ()).throw(AssertionError("legacy")))

    assert pipeline.process_file(
        str(tmp_path / "input.pdf"), False, {}, "key", "folder",
        str(tmp_path / "Markdown"), str(tmp_path / "tmp"),
        use_json_native=True,
    ) is True


def test_native_ai_uses_model_vision_loader(monkeypatch, tmp_path):
    pages = [{"result": {"textAnnotation": {"width": 100, "height": 100}}}]
    calls = []
    monkeypatch.setattr(pipeline, "send_to_yandex_ocr", lambda *args: pages)
    monkeypatch.setattr(pipeline, "parse_yandex_json_to_model", lambda value: SimpleNamespace(tables=[]))
    monkeypatch.setattr(pipeline, "stitch_tables", lambda doc: None)
    monkeypatch.setattr(pipeline, "extract_table_images_from_model", lambda *args: None)
    monkeypatch.setattr(pipeline, "extract_pictures_from_model", lambda *args: None)
    monkeypatch.setattr(pipeline, "populate_component_images", lambda doc: None)
    monkeypatch.setattr(pipeline, "render_document_to_md", lambda doc: "native markdown")
    monkeypatch.setattr(pipeline, "merge_tables_by_model", lambda text, doc: text)
    monkeypatch.setattr(pipeline, "run_script_postprocess", lambda text, img, **kwargs: text)
    monkeypatch.setattr(pipeline, "load_vision_tables_from_model", lambda doc, tmp: calls.append("load") or [])
    monkeypatch.setattr(pipeline, "ai_postprocess_json_native", lambda text, cfg, label, vision: calls.append("ai") or text)

    assert pipeline.process_file(
        str(tmp_path / "input.pdf"), True, {"ai_postprocess": {"prompt": "p"}}, "key", "folder",
        str(tmp_path / "Markdown"), str(tmp_path / "tmp"),
        use_json_native=True,
    ) is True
    assert calls == ["load", "ai"]


def test_native_flag_is_forwarded_by_main(monkeypatch, tmp_path):
    input_path = tmp_path / "input.md"
    input_path.write_text("x", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(pipeline, "process_file", lambda *args, **kwargs: captured.update(kwargs) or True)
    monkeypatch.setattr(pipeline, "load_config", lambda path: {})
    monkeypatch.setattr(pipeline, "find_input_files", lambda path: [input_path])
    monkeypatch.setattr(pipeline, "setup_logging", lambda path: None)
    monkeypatch.setattr(pipeline, "load_env", lambda path: None)
    monkeypatch.setattr(pipeline, "parse_args", lambda: type("Args", (), {
        "input": str(input_path), "ai": False, "json_native": True, "rag": False,
        "reg": False, "config": "config", "rag_config": "rag",
    })())
    pipeline.main()
    assert captured["use_json_native"] is True
