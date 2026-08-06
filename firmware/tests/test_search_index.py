"""
Тесты для firmware/src/create_index.py и firmware/src/search.py.

Запуск (из корня репозитория):
    python -m pytest firmware/tests/test_search_index.py -v

Не требуют сети/Qdrant: API-вызовы мокаются через monkeypatch,
чистые функции тестируются напрямую.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

# Тесты не должны зависеть от окружения запуска: фиксируем базовый URL
# ДО импорта скриптов, чтобы модульные константы указывали на публичный API.
os.environ.setdefault("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn")

SRC = Path(__file__).resolve().parents[1] / "src"

def _load(name):
    spec = importlib.util.spec_from_file_location(name, SRC / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot load {name}.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

create_index = _load("create_index")
search = _load("search")


# ─── create_index: validate_data ──────────────────────────────────────

def _chunk(cid, text="текст", doc_id="doc1", assets=None):
    return {
        "chunk_id": cid,
        "document_id": doc_id,
        "title": "ГОСТ X",
        "text": text,
        "assets": assets or [],
    }


def test_validate_data_ok():
    chunks = [_chunk("c1"), _chunk("c2")]
    assets_by_id = {}
    doc_meta = {"document_id": "doc1"}
    assert create_index.validate_data(chunks, assets_by_id, doc_meta) == []


def test_validate_data_duplicate_chunk_id():
    chunks = [_chunk("c1"), _chunk("c1")]
    errors = create_index.validate_data(chunks, {}, {"document_id": "doc1"})
    assert any("дубликат chunk_id='c1'" in e for e in errors)


def test_validate_data_empty_text():
    chunks = [_chunk("c1", text="   ")]
    errors = create_index.validate_data(chunks, {}, {"document_id": "doc1"})
    assert any("пустой text" in e for e in errors)


def test_validate_data_document_id_mismatch():
    chunks = [_chunk("c1", doc_id="other")]
    errors = create_index.validate_data(chunks, {}, {"document_id": "doc1"})
    assert any("не совпадает" in e for e in errors)


def test_validate_data_missing_asset_ref():
    chunks = [_chunk("c1", assets=["t_missing"])]
    errors = create_index.validate_data(chunks, {}, {"document_id": "doc1"})
    assert any("несуществующий asset_id='t_missing'" in e for e in errors)


def test_validate_data_orphan_asset():
    chunks = [_chunk("c1")]
    assets_by_id = {"t1": {"asset_id": "t1", "chunk_ids": ["c9"]}}
    errors = create_index.validate_data(chunks, assets_by_id, {"document_id": "doc1"})
    assert any("осиротевший" in e or "несуществующий chunk_id='c9'" in e for e in errors)


# ─── create_index: build_embed_text ───────────────────────────────────

def test_build_embed_text_heading_path_and_doc_line():
    chunk = {
        "chunk_id": "c1",
        "document_id": "doc1",
        "title": "ГОСТ X",
        "text": "Текст пункта",
        "assets": [],
        "heading_texts": {"chapter": "Глава 1", "section": "Зоны", "clause": "1.2"},
    }
    doc_meta = {"domain": "electrical"}
    text = create_index.build_embed_text(chunk, {}, doc_meta)
    assert "doc1. ГОСТ X (electrical)" in text
    assert "Глава 1 → Зоны → 1.2" in text
    assert "Текст пункта" in text


def test_build_embed_text_asset_captions():
    chunk = {
        "chunk_id": "c1",
        "document_id": "doc1",
        "title": "ГОСТ X",
        "text": "Текст",
        "assets": ["t1", "i1"],
    }
    assets_by_id = {
        "t1": {"asset_id": "t1", "asset_type": "table", "caption": "Сечения"},
        "i1": {"asset_id": "i1", "asset_type": "image", "caption": "Схема"},
    }
    text = create_index.build_embed_text(chunk, assets_by_id, {})
    assert "Таблица: Сечения" in text
    assert "Рисунок: Схема" in text


# ─── create_index: stable_point_id ────────────────────────────────────

def test_stable_point_id_deterministic_and_unique():
    a = create_index.stable_point_id(_chunk("c1"), 0)
    b = create_index.stable_point_id(_chunk("c1"), 0)
    c = create_index.stable_point_id(_chunk("c1"), 1)
    assert a == b
    assert a != c


# ─── create_index: build_payload ──────────────────────────────────────

def test_build_payload_structure():
    chunk = {
        "chunk_id": "c1",
        "document_id": "doc1",
        "title": "ГОСТ X",
        "status": "active",
        "chapter": "Гл",
        "section": "Разд",
        "clause": "1",
        "section_path": "Гл / Разд / 1",
        "heading_texts": {"chapter": "Гл"},
        "text": "текст",
        "references": ["ref"],
        "assets": ["t1"],
        "chunk_tokens": 5,
    }
    assets_by_id = {"t1": {"asset_id": "t1", "asset_type": "table", "caption": "C"}}
    doc_meta = {"document_type": "standard", "domain": "electrical"}
    payload = create_index.build_payload(chunk, doc_meta, assets_by_id, row_index=3)
    assert payload["chunk_id"] == "c1"
    assert payload["document_id"] == "doc1"
    assert payload["document_type"] == "standard"
    assert payload["domain"] == "electrical"
    assert payload["title"] == "ГОСТ X"
    assert payload["status"] == "active"
    assert payload["chapter"] == "Гл"
    assert payload["section"] == "Разд"
    assert payload["clause"] == "1"
    assert payload["section_path"] == "Гл / Разд / 1"
    assert payload["heading_texts"] == {"chapter": "Гл"}
    assert payload["text"] == "текст"
    assert payload["references"] == ["ref"]
    assert payload["assets"] == [{"asset_id": "t1", "asset_type": "table", "caption": "C"}]
    assert payload["chunk_tokens"] == 5
    assert payload["row_index"] == 3


# ─── create_index: embed_texts_siliconflow (mock requests) ────────────

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_embed_texts_siliconflow_request_and_parse(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse({
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        })

    monkeypatch.setattr(create_index.requests, "post", fake_post)
    vecs = create_index.embed_texts_siliconflow(["a", "b"], api_key="k-123")

    assert captured["url"] == create_index.EMBED_API_URL
    assert captured["url"].endswith("/v1/embeddings")
    assert captured["json"]["model"] == "Qwen/Qwen3-Embedding-8B"
    assert captured["json"]["input"] == ["a", "b"]
    assert captured["json"]["encoding_format"] == "float"
    assert captured["headers"]["Authorization"] == "Bearer k-123"
    # сортировка по index: [1,0] -> [0,1]
    assert vecs == [[1.0, 0.0], [0.0, 1.0]]


def test_embed_texts_siliconflow_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def failing_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        raise ValueError("boom")

    monkeypatch.setattr(create_index.requests, "post", failing_post)
    with pytest.raises(RuntimeError, match="SiliconFlow embedding failed"):
        create_index.embed_texts_siliconflow(["a"], api_key="k")
    assert calls["n"] == create_index.EMBED_RETRIES


# ─── search: rerank_siliconflow (mock requests) ───────────────────────

def test_rerank_siliconflow_request_and_parse(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse({
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.3},
                {"index": 2, "relevance_score": 0.5},
            ]
        })

    monkeypatch.setattr(search.requests, "post", fake_post)
    scores = search.rerank_siliconflow("запрос", ["doc1", "doc2", "doc3"], api_key="k", top_n=3)

    assert captured["url"] == search.RERANK_API_URL
    assert captured["url"].endswith("/v1/rerank")
    assert captured["json"]["model"] == "Qwen/Qwen3-Reranker-8B"
    assert captured["json"]["query"] == "запрос"
    assert captured["json"]["documents"] == ["doc1", "doc2", "doc3"]
    assert captured["json"]["top_n"] == 3
    # скоры выровнены по исходному порядку documents
    assert scores == [0.3, 0.9, 0.5]


def test_embed_query_uses_instruction(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return FakeResponse({"data": [{"index": 0, "embedding": [0.5, 0.5]}]})

    monkeypatch.setattr(search.requests, "post", fake_post)
    vec = search.embed_query_siliconflow("высота молниеотвода", api_key="k")
    assert "Instruct:" in captured["json"]["input"][0]
    assert "Query: высота молниеотвода" in captured["json"]["input"][0]
    assert vec == [0.5, 0.5]


# ─── search: фильтры и метки ──────────────────────────────────────────

class FakePoint:
    def __init__(self, score, payload=None):
        self.score = score
        self.payload = payload or {"text": "x"}


def test_filter_by_rrf_score_keeps_strong_drops_weak():
    cands = [FakePoint(0.5), FakePoint(0.2), FakePoint(0.1), FakePoint(0.05)]
    kept = search.filter_by_rrf_score(cands)
    assert [c.score for c in kept] == [0.5, 0.2]


def test_filter_by_rrf_score_fallback_when_all_dropped():
    cands = [FakePoint(0.05), FakePoint(0.01)]
    kept = search.filter_by_rrf_score(cands)
    # fallback на сырые данные
    assert kept == cands


def test_build_payload_filter_none_without_args():
    assert search.build_payload_filter(None, None, None) is None


def test_build_payload_filter_conditions():
    f = search.build_payload_filter("cable", "standard", "std-002")
    keys = {c.key for c in f.must}
    assert keys == {"domain", "document_type", "document_id"}


def test_quality_label_thresholds():
    assert search.quality_label(0.9) == "точное"
    assert search.quality_label(0.75) == "хорошее"
    assert search.quality_label(0.4) == "среднее"


def test_result_to_dict_fields():
    point = FakePoint(0.42, {
        "chunk_id": "c1",
        "document_id": "d1",
        "document_type": "standard",
        "domain": "electrical",
        "title": "ГОСТ",
        "status": "active",
        "section_path": "A / B",
        "heading_texts": {"chapter": "A"},
        "text": "текст",
        "references": [],
        "assets": [{"asset_type": "table", "caption": "C", "image_path": "t.png"}],
    })
    d = search.result_to_dict(point, score=0.42, rank=1)
    assert d["rank"] == 1
    assert d["score"] == 0.42
    assert d["rrf_score"] == 0.42
    assert d["quality"] == "среднее"
    assert d["chunk_id"] == "c1"
    assert d["assets"][0]["caption"] == "C"


# ─── CLI-поведение без сети: --help ───────────────────────────────────

def test_create_index_help():
    import subprocess
    r = subprocess.run(
        [sys.executable, str(SRC / "create_index.py"), "--help"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "--chunks" in r.stdout


def test_search_help():
    import subprocess
    r = subprocess.run(
        [sys.executable, str(SRC / "search.py"), "--help"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "--list-collections" in r.stdout
    assert "--sources" in r.stdout
