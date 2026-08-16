import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import pipeline


def test_ai_postprocess_json_native_binds_vision_by_caption(monkeypatch):
    calls = []

    def fake_call(text, config, context=""):
        calls.append(text)
        return "processed"

    monkeypatch.setattr(pipeline, "_call_ai_api", fake_call)
    result = pipeline.ai_postprocess_json_native(
        "Таблица 1.2\n| a | b |", {}, "doc", [{"caption": "Таблица 1.2", "table_num": "1.2", "markdown": "VISION"}]
    )
    assert result == "processed"
    assert "VISION" in calls[0]
    assert "=== Эталонные таблицы ===" in calls[0]


def test_ai_postprocess_json_native_binds_vision_by_number(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "_call_ai_api", lambda text, config, context="": calls.append(text) or text)
    pipeline.ai_postprocess_json_native(
        "Таблица 4.1\ntext", {}, "doc", [{"caption": "other", "table_num": "4.1", "markdown": "VISION-41"}]
    )
    assert "VISION-41" in calls[0]


def test_ai_postprocess_json_native_without_match_passes_chunk_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "_call_ai_api", lambda text, config, context="": calls.append(text) or "result")
    source = "ordinary text"
    assert pipeline.ai_postprocess_json_native(source, {}, "doc", [{"caption": "Table 9", "table_num": "9", "markdown": "VISION"}]) == "result"
    assert calls == [source]


def test_ai_postprocess_json_native_empty_vision_tables(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "_call_ai_api", lambda text, config, context="": calls.append(text) or None)
    source = "ordinary text"
    assert pipeline.ai_postprocess_json_native(source, {}, "doc", []) == source
    assert calls == [source]


def test_load_vision_tables_from_model_maps_image_crop(tmp_path):
    (tmp_path / "table_2.md").write_text("vision markdown", encoding="utf-8")
    doc = SimpleNamespace(tables=[
        SimpleNamespace(image_path=str(tmp_path / "table_1.png"), caption="missing", table_num="1"),
        SimpleNamespace(image_path=str(tmp_path / "table_2.png"), caption="Таблица 2", table_num="2"),
    ])
    assert pipeline.load_vision_tables_from_model(doc, tmp_path) == [
        {"caption": "Таблица 2", "table_num": "2", "markdown": "vision markdown"}
    ]

def test_load_vision_tables_from_model_ignores_tables_without_image_or_crop(tmp_path):
    doc = SimpleNamespace(tables=[SimpleNamespace(image_path=None, caption="x", table_num="1")])
    assert pipeline.load_vision_tables_from_model(doc, tmp_path) == []


def test_native_ai_preserves_fallback_when_api_returns_none(monkeypatch):
    monkeypatch.setattr(pipeline, "_call_ai_api", lambda *args, **kwargs: None)
    source = "Таблица 1\ntext"
    assert pipeline.ai_postprocess_json_native(source, {}, "doc", []) == source


# Keep the model classes imported in production covered by a realistic object shape.
def test_table_shape_is_accepted():
    assert hasattr(pipeline, "Table")
    assert hasattr(pipeline, "Document")


_UNUSED = SimpleNamespace
