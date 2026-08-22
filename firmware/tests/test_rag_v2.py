#!/usr/bin/env python3
"""Тесты RAG v2 (ADR-010): Qwen3 tokenizer, JSONL v2, assets, безопасный --rag.

Покрывают:
  - _init_tokenizer(): Qwen3 доступен/недоступен, явный degraded fallback
  - _split_oversized_clause_tokens(): токен-лимит, таблицы/код атомарны
  - validate_rag_config(): status/replaced_by_document_id
  - build_rag_jsonl_v2(): JSONL v2 схема, нет source.page, chunk_id, assets
  - Asset Registry: _extract_tables_from_md/_extract_images_from_md/
    _build_asset_registry/_link_assets_to_chunks/write_rag_assets
  - _classify_input(), run_rag_pipeline(), _run_rag_only()
  - .md --rag не модифицирует .md/image; атомарная перезапись
  - PDF без --ai вырезает таблицы; --ai включает vision дополнительно
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import pipeline

RAG_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "rag_config.yaml")


@pytest.fixture(scope="module")
def rag_config():
    return pipeline.load_rag_config(RAG_CONFIG)


def _tok(text: str) -> int:
    """Детерминированный токенизатор для тестов: 1 токен на 3 символа."""
    return max(1, len(text) // 3)


# ═══════════════════════════════════════════════════════════════════════════
# _init_tokenizer — Qwen3 / degraded fallback
# ═══════════════════════════════════════════════════════════════════════════


class _FakeTokenizer:
    def encode(self, text):
        return list(range(len(text) // 2))


class TestInitTokenizer:
    def test_init_qwen3_available(self, monkeypatch):
        """При доступном transformers.AutoTokenizer → (callable, 'qwen3')."""
        import transformers

        monkeypatch.setattr(
            transformers.AutoTokenizer,
            "from_pretrained",
            staticmethod(lambda *a, **k: _FakeTokenizer()),
        )
        fn, method = pipeline._init_tokenizer({
            "defaults": {"tokenizer": "Qwen/Qwen3-Embedding-8B", "tokenizer_revision": "main"},
        })
        assert method == "qwen3"
        assert callable(fn)
        assert fn("Привет мир") > 0

    def test_init_qwen3_tokenize_russian(self, monkeypatch):
        """Токенизация русского текста через fake tokenizer."""
        import transformers

        monkeypatch.setattr(
            transformers.AutoTokenizer,
            "from_pretrained",
            staticmethod(lambda *a, **k: _FakeTokenizer()),
        )
        fn, _ = pipeline._init_tokenizer({"defaults": {}})
        assert fn("Привет мир") == len("Привет мир") // 2

    def test_init_qwen3_tokenize_empty(self, monkeypatch):
        import transformers

        monkeypatch.setattr(
            transformers.AutoTokenizer,
            "from_pretrained",
            staticmethod(lambda *a, **k: _FakeTokenizer()),
        )
        fn, _ = pipeline._init_tokenizer({"defaults": {}})
        assert fn("") == 0

    def test_init_no_transformers_no_fallback_raises(self, monkeypatch):
        """Без transformers и allow_degraded_fallback=false → RuntimeError."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "transformers", None)
        with pytest.raises(RuntimeError):
            pipeline._init_tokenizer({"defaults": {"allow_degraded_fallback": False}})

    def test_init_no_transformers_with_fallback(self, monkeypatch):
        """Без transformers, но allow_degraded_fallback=true → degraded mode."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "transformers", None)
        fn, method = pipeline._init_tokenizer({
            "defaults": {
                "allow_degraded_fallback": True,
                "tokenizer_fallback_ratio": 3.5,
            },
        })
        assert method == "degraded_chars_per_token"
        assert callable(fn)
        # "Привет мир" = 10 символов / 3.5 → 2 токена
        assert fn("Привет мир") == max(1, int(10 / 3.5))

    def test_init_transformers_import_error(self, monkeypatch):
        """ImportError при загрузке transformers → degraded или ошибка."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "transformers", None)
        with pytest.raises(RuntimeError):
            pipeline._init_tokenizer({"defaults": {}})

    def test_init_load_failure_no_fallback_raises(self, monkeypatch):
        """Ошибка загрузки (сеть/HF) при запрещённом fallback → RuntimeError."""
        import transformers

        def _boom(*a, **k):
            raise OSError("HF недоступен")

        monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", staticmethod(_boom))
        with pytest.raises(RuntimeError):
            pipeline._init_tokenizer({"defaults": {"allow_degraded_fallback": False}})

    def test_init_load_failure_with_fallback(self, monkeypatch):
        """Ошибка загрузки при разрешённом fallback → degraded mode."""
        import transformers

        def _boom(*a, **k):
            raise OSError("HF недоступен")

        monkeypatch.setattr(transformers.AutoTokenizer, "from_pretrained", staticmethod(_boom))
        fn, method = pipeline._init_tokenizer({
            "defaults": {"allow_degraded_fallback": True, "tokenizer_fallback_ratio": 4.0},
        })
        assert method == "degraded_chars_per_token"
        assert fn("abcdefgh") == 2  # 8 / 4


# ═══════════════════════════════════════════════════════════════════════════
# _split_oversized_clause_tokens
# ═══════════════════════════════════════════════════════════════════════════


class TestSplitOversizedClauseTokens:
    def test_small_text(self):
        assert pipeline._split_oversized_clause_tokens("маленький", 7000, _tok) == ["маленький"]

    def test_groups_paragraphs(self):
        paras = [f"Параграф номер {i} " * 3 for i in range(5)]
        text = "\n\n".join(paras)
        parts = pipeline._split_oversized_clause_tokens(text, 30, _tok)
        assert len(parts) >= 2
        assert "\n\n".join(parts) == text

    def test_table_atomic(self):
        table = "| a | b |\n| 1 | 2 |\n| 3 | 4 |"
        text = "Текст.\n\n" + table + "\n\nЕщё текст"
        parts = pipeline._split_oversized_clause_tokens(text, 6, _tok)
        assert any(table in p for p in parts)

    def test_giant_table_as_is(self):
        giant = "| " + "x" * 500 + " |"
        text = "До\n\n" + giant + "\n\nПосле"
        parts = pipeline._split_oversized_clause_tokens(text, 20, _tok)
        assert giant in parts

    def test_code_block_atomic(self):
        code = "```\nline\n\n" * 10 + "```"
        parts = pipeline._split_oversized_clause_tokens(code, 15, _tok)
        assert len(parts) >= 1
        assert all("```" in p for p in parts)

    def test_empty(self):
        assert pipeline._split_oversized_clause_tokens("", 10, _tok) == [""]


# ═══════════════════════════════════════════════════════════════════════════
# validate_rag_config
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateRagConfig:
    def test_ok(self, rag_config):
        assert pipeline.validate_rag_config(rag_config) == []

    def test_max_tokens_invalid(self):
        errs = pipeline.validate_rag_config({"defaults": {"max_chunk_tokens": 5}})
        assert any("max_chunk_tokens" in e for e in errs)

    def test_status_missing(self):
        cfg = {"defaults": {}, "documents": {"d": {"status": None}}}
        errs = pipeline.validate_rag_config(cfg)
        assert any("status" in e for e in errs)

    def test_status_invalid(self):
        cfg = {"defaults": {}, "documents": {"d": {"status": "draft"}}}
        errs = pipeline.validate_rag_config(cfg)
        assert any("draft" in e for e in errs)

    def test_active_with_replaced_by_document_id(self):
        """status=active, но replaced_by_document_id не null → ошибка."""
        cfg = {
            "defaults": {"max_chunk_tokens": 7000},
            "documents": {"d": {"status": "active", "replaced_by_document_id": "СП 60"}},
        }
        errs = pipeline.validate_rag_config(cfg)
        assert any("replaced_by_document_id" in e for e in errs)

    def test_active_with_replaced_by_doc_key(self):
        cfg = {
            "defaults": {"max_chunk_tokens": 7000},
            "documents": {"d": {"status": "active", "replaced_by_doc_key": "sp60"}},
        }
        errs = pipeline.validate_rag_config(cfg)
        assert any("replaced_by_doc_key" in e for e in errs)

    def test_inactive_with_replacement_ok(self):
        """status=inactive с официальным номером преемника — валидно."""
        cfg = {
            "defaults": {"max_chunk_tokens": 7000},
            "documents": {
                "d": {
                    "status": "inactive",
                    "status_reason": "Заменён",
                    "replaced_by_document_id": "СП 60.13330.2012",
                    "replaced_by_doc_key": "sp60_otoplenie",
                }
            },
        }
        assert pipeline.validate_rag_config(cfg) == []


# ═══════════════════════════════════════════════════════════════════════════
# build_rag_jsonl_v2 — JSONL v2 контракт
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def v2_md():
    return (
        "## СОДЕРЖАНИЕ\n"
        "1. Введение\n"
        "### 3.2. Внешняя молниезащитная система\n"
        "Текст оглавления.\n"
        "\n"
        "## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ\n"
        "Вводный абзац главы о молниезащите.\n"
        "\n"
        "### 3.2. Внешняя молниезащитная система\n"
        "Внешняя МЗС может состоять из молниеприемников, токоотводов и "
        "заземлителей (см. п. 3.2.1, табл. 3.1).\n"
        "\n"
        "#### 3.2.1. Молниеприемники\n"
        "Молниеприемники могут быть естественными или искусственными.\n"
    )


class TestBuildRagJsonlV2:
    def test_marker_comments_are_excluded_from_rag_text(self, rag_config):
        """RAG chunks omit internal table/WARN comments while input remains usable."""
        md = (
            "## 3. Раздел\n"
            "Перед таблицей <!-- t_p2_7 --> <!-- WARN_3.1 -->\n"
            "Полезный текст.\n"
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
        assert rows
        for row in rows:
            assert "t_p2_7" not in row["text"]
            assert "WARN_3.1" not in row["text"]
            assert "t_p2_7" not in row["embedding_text"]
            assert "WARN_3.1" not in row["embedding_text"]

    def test_basic_schema(self, v2_md, rag_config):
        """JSONL v2: обязательные поля, нет source.page, chunk_id стабильный."""
        jh = [
            {"page": 7, "number": "3"},
            {"page": 8, "number": "3.2"},
            {"page": 9, "number": "3.2.1"},
        ]
        jsonl = pipeline.build_rag_jsonl_v2(
            v2_md, jh, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(line) for line in jsonl.splitlines() if line.strip()]

        assert len(rows) == 3  # СОДЕРЖАНИЕ пропущена

        for r in rows:
            # Публичная схема без page; статус и метод проставляются
            assert "page" not in r["source"]
            assert r["status"] == "active"
            assert r["status_reason"] is None
            assert r["replaced_by_document_id"] is None
            assert r["chunking_method"] == "qwen3"
            assert isinstance(r["chunk_tokens"], int) and r["chunk_tokens"] > 0
            assert "section_path" in r
            assert "heading_texts" in r
            assert "assets" in r and isinstance(r["assets"], list)
            assert r["_source_page"] is not None

        r0 = rows[0]  # ## 3.
        assert r0["chapter"] == "3"
        assert r0["chunk_id"] == "so153_molniezashita/3"
        assert r0["section_path"] == "3"
        assert r0["heading_texts"]["chapter"] == "3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ"

        r2 = rows[2]  # #### 3.2.1.
        assert r2["chunk_id"] == "so153_molniezashita/3.2.1"
        assert r2["section_path"] == "3 → 3.2 → 3.2.1"
        assert r2["heading_texts"]["clause"] == "3.2.1. Молниеприемники"

    def test_every_line_valid_required_fields(self, v2_md, rag_config):
        jsonl = pipeline.build_rag_jsonl_v2(
            v2_md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        required = {
            "document_id", "title", "status", "chunk_id",
            "chapter", "section", "clause", "section_path", "heading_texts",
            "text", "embedding_text", "embedding_tokens",
            "source", "_source_page", "assets", "references",
            "chunk_tokens", "chunking_method",
        }
        for line in jsonl.splitlines():
            if line.strip():
                obj = json.loads(line)
                assert required <= set(obj.keys())

    def test_no_source_page_anywhere(self, v2_md, rag_config):
        jsonl = pipeline.build_rag_jsonl_v2(
            v2_md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        for r in rows:
            assert "page" not in r["source"]

    def test_inactive_status(self, v2_md, rag_config):
        """Неактивный документ: статус и номер преемника в каждой строке."""
        jsonl = pipeline.build_rag_jsonl_v2(
            v2_md, None, rag_config, "old_snip", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        assert rows
        for r in rows:
            assert r["status"] == "inactive"
            assert r["status_reason"] == "Заменён на СП 60.13330.2012"
            # Официальный номер, не slug
            assert r["replaced_by_document_id"] == "СП 60.13330.2012"
            assert r["replaced_by_doc_key"] == "sp60_otoplenie"

    def test_oversized_token_splits(self, rag_config):
        """Oversized clause по токенам → подчанки /part_N + «(ч. N)»."""
        cfg = dict(rag_config)
        cfg["defaults"] = dict(rag_config["defaults"], max_chunk_tokens=15)
        paras = [f"Абзац {i} с детальным описанием молниеприемника и токоотвода. " * 2 for i in range(4)]
        md = (
            "## 3. ЗАЩИТА\n"
            "\n"
            "### 3.2. Внешняя молниезащитная система\n"
            "\n"
            "#### 3.2.1. Молниеприемники\n"
            + "\n\n".join(paras)
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, [{"page": 5, "number": "3.2.1"}], cfg, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        assert len(rows) >= 2
        chunk_ids = [r["chunk_id"] for r in rows]
        assert any(c.endswith("/part_1") for c in chunk_ids)
        assert any(c.endswith("/part_2") for c in chunk_ids)
        clauses = [r["clause"] for r in rows]
        assert any("(ч. 1)" in c for c in clauses)
        for r in rows:
            assert r["chapter"] == "3"
            assert r["status"] == "active"
            assert r["chunking_method"] == "qwen3"

    def test_chunking_method_degraded(self, v2_md, rag_config):
        """degraded_chars_per_token проставляется в каждой строке."""
        jsonl = pipeline.build_rag_jsonl_v2(
            v2_md, None, rag_config, "so153_molniezashita", _tok,
            "degraded_chars_per_token",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        assert rows
        for r in rows:
            assert r["chunking_method"] == "degraded_chars_per_token"

    def test_unknown_doc_key(self, v2_md, rag_config):
        with pytest.raises(ValueError):
            pipeline.build_rag_jsonl_v2(v2_md, None, rag_config, "unknown", _tok)

    def test_empty_md(self, rag_config):
        jsonl = pipeline.build_rag_jsonl_v2("", None, rag_config, "so153_molniezashita", _tok)
        assert jsonl == ""

    def test_unnumbered_heading_fallback(self, rag_config):
        """Ненумерованный заголовок → chunk_id вида {doc_slug}/_h{ordinal}."""
        md = (
            "## 3. ЗАЩИТА\n"
            "\n"
            "Вводный текст главы.\n"
            "\n"
            "### Внешняя молниезащитная система\n"
            "Текст раздела без номера.\n"
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        assert rows
        assert rows[0]["chunk_id"] == "so153_molniezashita/3"
        assert rows[1]["chunk_id"] == "so153_molniezashita/_h1"

    def test_chunk_id_unique_with_repeated_headings(self, rag_config):
        """Повторные numbered top-level главы → уникальные chunk_id.

        Первый ID остаётся базовым, повторы получают /occurrence_{N}.
        Регрессия СО153: «## 1/2/3» в разделе рекомендаций дублировали
        introduction «## 1» и «## 2» — последний перезаписывал первый.
        """
        md = (
            "## 1. ВВЕДЕНИЕ\n"
            "Вводный текст.\n"
            "\n"
            "## 2. ОБЩИЕ ПОЛОЖЕНИЯ\n"
            "Текст главы 2.\n"
            "\n"
            "## 1. Разработка эксплуатационно-технической документации\n"
            "Текст рекомендаций 1.\n"
            "\n"
            "## 2. Порядок приемки устройств молниезащиты в эксплуатацию\n"
            "Текст рекомендаций 2.\n"
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        ids = [r["chunk_id"] for r in rows]
        assert len(ids) == len(set(ids))
        # Первый /1 и /2 — базовые; повторы — с occurrence-суффиксом
        assert "so153_molniezashita/1" in ids
        assert "so153_molniezashita/1/occurrence_2" in ids
        assert "so153_molniezashita/2" in ids
        assert "so153_molniezashita/2/occurrence_2" in ids
        # Детерминированность: повторный запуск даёт те же ID
        jsonl2 = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        ids2 = [json.loads(l)["chunk_id"] for l in jsonl2.splitlines() if l.strip()]
        assert ids2 == ids

    def test_chunk_id_occurrence_three(self, rag_config):
        """Третий повтор получает /occurrence_3."""
        md = (
            "## 1. ПЕРВАЯ\n"
            "Текст 1.\n"
            "\n"
            "## 1. ВТОРАЯ\n"
            "Текст 2.\n"
            "\n"
            "## 1. ТРЕТЬЯ\n"
            "Текст 3.\n"
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        ids = [r["chunk_id"] for r in rows]
        assert len(ids) == len(set(ids))
        assert "so153_molniezashita/1" in ids
        assert "so153_molniezashita/1/occurrence_2" in ids
        assert "so153_molniezashita/1/occurrence_3" in ids

    def test_embedding_text_short_section_only(self, rag_config):
        """Короткий section-only чанк: heading context + исходный текст.

        Регрессия СО153: у «2.3» text всего 158 символов; в embedding_text
        обязан попасть контекст главы «2. ...» и раздела «2.3. ...».
        """
        md = (
            "## 2. ОБЩИЕ ПОЛОЖЕНИЯ\n"
            "Текст главы.\n"
            "\n"
            "### 2.3. Параметры токов молнии\n"
            "Молния представляет собой импульс тока."
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        assert rows[1]["chunk_id"] == "so153_molniezashita/2.3"
        r = rows[1]
        # Заголовки главы и раздела + исходный текст
        assert r["embedding_text"].startswith(
            "Заголовок главы: 2. ОБЩИЕ ПОЛОЖЕНИЯ\n"
            "Заголовок раздела: 2.3. Параметры токов молнии\n"
        )
        assert r["embedding_text"].endswith(r["text"])
        assert r["embedding_text"].endswith("Молния представляет собой импульс тока.")
        # embedding_tokens — для фактического embedding_text, chunk_tokens — для text
        assert r["embedding_tokens"] == _tok(r["embedding_text"])
        assert r["chunk_tokens"] == _tok(r["text"])
        assert r["embedding_tokens"] > r["chunk_tokens"]

    def test_embedding_text_oversized_parts(self, rag_config):
        """Oversized чанк: каждая часть получает свои embedding_text/embedding_tokens."""
        cfg = dict(rag_config)
        cfg["defaults"] = dict(rag_config["defaults"], max_chunk_tokens=15)
        paras = [f"Абзац {i} с детальным описанием молниеприемника и токоотвода. " * 2 for i in range(4)]
        md = (
            "## 3. ЗАЩИТА\n"
            "\n"
            "### 3.2. Внешняя молниезащитная система\n"
            "\n"
            "#### 3.2.1. Молниеприемники\n"
            + "\n\n".join(paras)
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, [{"page": 5, "number": "3.2.1"}], cfg, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        assert len(rows) >= 2
        for r in rows:
            # Заголовки повторяются в каждой части, текст — конкретной части
            assert r["embedding_text"].startswith(
                "Заголовок главы: 3. ЗАЩИТА\n"
                "Заголовок раздела: 3.2. Внешняя молниезащитная система\n"
                "Заголовок пункта: 3.2.1. Молниеприемники\n"
            )
            assert r["embedding_text"].endswith(r["text"])
            assert r["embedding_tokens"] == _tok(r["embedding_text"])
            assert r["chunk_tokens"] == _tok(r["text"])
        # Части не пересекаются и в сумме покрывают исходный текст
        # (extract_clause_text() обрезает хвостовые пробелы)
        joined = "\n\n".join(r["text"] for r in rows)
        assert joined == "\n\n".join(paras).strip()

    def test_embedding_text_with_occurrence_id(self, rag_config):
        """Повторный top-level: occurrence-суффикс и заголовки своего блока."""
        md = (
            "## 1. ВВЕДЕНИЕ\n"
            "Вводный текст.\n"
            "\n"
            "## 1. Разработка эксплуатационно-технической документации\n"
            "Текст рекомендаций 1.\n"
        )
        jsonl = pipeline.build_rag_jsonl_v2(
            md, None, rag_config, "so153_molniezashita", _tok, "qwen3",
        )
        rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
        r0, r1 = rows
        assert r0["chunk_id"] == "so153_molniezashita/1"
        assert r0["embedding_text"].startswith("Заголовок главы: 1. ВВЕДЕНИЕ")
        # Повтор: своя глава, без чужого section
        assert r1["chunk_id"] == "so153_molniezashita/1/occurrence_2"
        assert r1["section"] is None
        assert r1["embedding_text"].startswith(
            "Заголовок главы: 1. Разработка эксплуатационно-технической документации"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Asset Registry
# ═══════════════════════════════════════════════════════════════════════════


ASSET_MD = (
    "## 3. ЗАЩИТА\n"
    "\n"
    "Вводный текст с изображением.\n"
    "\n"
    "![Рисунок 1](image/fig_1.png)\n"
    "\n"
    "*Таблица 3.1 — Значения сопротивления заземлителей*\n"
    "\n"
    "| Параметр | Значение |\n"
    "|----------|----------|\n"
    "| R, Ом    | 10       |\n"
    "| U, В     | 220      |\n"
)


class TestAssetRegistry:
    SOURCE_STEM = "СО153-34_21_122-2003 Молниезащита"
    def test_extract_tables_from_md(self):
        tables = pipeline._extract_tables_from_md(ASSET_MD)
        assert len(tables) == 1
        t = tables[0]
        assert t["asset_type"] == "table"
        assert "Таблица 3.1" in t["caption"]
        assert t["md_lines"] == [8, 12]  # start..end (0-индексированные строки)
        assert t["row_count"] == 2
        # Позиционный счётчик больше НЕ источник image_path: без маркера/карты — None
        assert t["image_path"] is None
        assert t["_image_source"] is None
        assert t["_md_block"].startswith("| Параметр")

    def test_extract_tables_from_md_no_caption(self):
        md = "Текст\n\n| a | b |\n|---|----|\n| 1 | 2 |\n"
        tables = pipeline._extract_tables_from_md(md)
        assert len(tables) == 1
        assert tables[0]["caption"] == ""

    def test_extract_tables_from_md_marker_binding(self):
        """ID-маркер перед таблицей + карта id→path → image_path из карты."""
        md = (
            "Текст до.\n\n"
            "<!-- t_p1_0 -->\n"
            "*Таблица 1*\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
        )
        tables = pipeline._extract_tables_from_md(
            md, table_image_map={"t_p1_0": "table_3.png"},
        )
        assert len(tables) == 1
        assert tables[0]["image_path"] == "image/table_3.png"
        assert tables[0]["_image_source"] == "marker"

    def test_extract_tables_from_md_marker_unknown_id(self):
        """Маркер есть, но id нет в карте → image_path None (фолбэк по содержимому)."""
        md = (
            "<!-- t_p9_7 -->\n"
            "| A |\n"
            "|---|\n"
            "| 1 |\n"
        )
        tables = pipeline._extract_tables_from_md(
            md, table_image_map={"t_p1_0": "table_3.png"},
        )
        assert len(tables) == 1
        assert tables[0]["image_path"] is None

    def test_find_caption_refuses_foreign_below_caption(self):
        """Подпись ниже начала таблицы (принадлежит следующей) не переиспользуется.

        Регрессия СП89: table/5 без подписи получала «Таблица Г.2» — подпись
        table/6, расположенную ниже строки начала table/5.
        """
        md = (
            "| Условный диаметр паропровода | 100 - 125 |\n"
            "|---|---|\n"
            "| Условный диаметр кармана | 50 |\n"
            "\n"
            "В миллиметрах\n"
            "\n"
            "*Таблица Г.2*\n"
            "\n"
            "| Условный диаметр паропровода | До 70 включ. |\n"
            "|---|---|\n"
            "| Условный диаметр штуцера | 25 |\n"
        )
        tables = pipeline._extract_tables_from_md(md)
        assert len(tables) == 2
        # Первая таблица подписи не имеет и чужую не подтягивает
        assert tables[0]["caption"] == ""
        # Вторая таблица получает свою подпись
        assert tables[1]["caption"] == "Таблица Г.2"

    def test_find_caption_does_not_reuse_claimed_caption(self):
        """Подпись, привязанная к одной таблице, не привязывается к другой."""
        md = (
            "| A |\n"
            "|---|\n"
            "| 1 |\n"
            "\n"
            "*Таблица 1*\n"
            "\n"
            "| B |\n"
            "|---|\n"
            "| 2 |\n"
        )
        tables = pipeline._extract_tables_from_md(md)
        assert len(tables) == 2
        # Подпись сразу под первой таблицей — её; вторая остаётся без подписи
        assert tables[0]["caption"] == "Таблица 1"
        assert tables[1]["caption"] == ""

    def test_normalize_table_cells(self):
        text = (
            "| A  B | x |\n"
            "|---|---|\n"
            "| *T*_1$ | --- |\n"
            "| а |\n"
        )
        cells = pipeline._normalize_table_cells(text)
        assert "a b" in cells  # пробелы сжаты, регистр lower
        assert "t1" in cells   # сняты *_$
        assert "---" not in cells  # разделители отброшены
        assert "а" not in cells    # длина <=1 отброшена

    def test_match_table_by_content(self):
        md_block = "| X | Y |\n|---|---|\n| Alpha | 1 |\n| Beta | 2 |\n"
        crops = {
            "table_4.png": pipeline._normalize_table_cells(
                "| Alpha | 1 |\n| Beta | 2 |\n"
            ),
            "table_5.png": pipeline._normalize_table_cells(
                "| Gamma | 9 |\n"
            ),
        }
        name, score, _ = pipeline._match_table_by_content(md_block, crops)
        assert name == "table_4.png"
        assert score >= pipeline._TABLE_CONTENT_MATCH_THRESHOLD

    def test_match_table_by_content_no_match(self):
        md_block = "| X | Y |\n|---|---|\n| Alpha | 1 |\n"
        crops = {"table_5.png": pipeline._normalize_table_cells("| Gamma | 9 |\n")}
        name, _, _ = pipeline._match_table_by_content(md_block, crops)
        assert name is None

    def test_build_asset_registry_content_binding(self, tmp_path):
        """--rag без маркеров в MD: привязка по содержимому через tmp_dir.

        tmp/<stem>/table_images.json + table_N.md — карта и OCR-вырезки.
        """
        img_dir = tmp_path / "Markdown" / "doc" / "image"
        img_dir.mkdir(parents=True)
        for n in ("table_3", "table_5"):
            (img_dir / f"{n}.png").write_bytes(b"png")
        tmp_dir = tmp_path / "tmp" / "doc"
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "table_images.json").write_text(
            json.dumps([
                {"id": "t_p1_0", "path": "table_3.png", "page": 0, "table_idx": 3},
                {"id": "t_p1_1", "path": "table_5.png", "page": 0, "table_idx": 5},
            ]),
            encoding="utf-8",
        )
        # OCR-вырезки: первая строка — ID-маркер (как в recognize_tables_vision)
        (tmp_dir / "table_3.md").write_text(
            "<!-- t_p1_0 -->\n| Alpha | 1 |\n| Beta | 2 |\n", encoding="utf-8")
        (tmp_dir / "table_5.md").write_text(
            "<!-- t_p1_1 -->\n| Gamma | 9 |\n", encoding="utf-8")

        md = (
            "## 3. ЗАЩИТА\n"
            "\n"
            "Текст.\n"
            "\n"
            "| X | Y |\n"
            "|---|---|\n"
            "| Alpha | 1 |\n"
            "| Beta | 2 |\n"
        )
        assets = pipeline._build_asset_registry(
            md, "doc", img_dir, tmp_dir=tmp_dir,
        )
        tables = assets["assets"]["tables"]
        assert len(tables) == 1
        assert tables[0]["image_path"] == "image/table_3.png"
        assert tables[0]["_image_source"] == "content"

    def test_build_asset_registry_marker_preferred_over_content(self, tmp_path):
        """Маркер в MD имеет приоритет над привязкой по содержимому."""
        img_dir = tmp_path / "Markdown" / "doc" / "image"
        img_dir.mkdir(parents=True)
        (img_dir / "table_3.png").write_bytes(b"png")
        (img_dir / "table_5.png").write_bytes(b"png")
        tmp_dir = tmp_path / "tmp" / "doc"
        tmp_dir.mkdir(parents=True)
        (tmp_dir / "table_images.json").write_text(
            json.dumps([
                {"id": "t_p1_0", "path": "table_3.png", "page": 0, "table_idx": 3},
                {"id": "t_p1_1", "path": "table_5.png", "page": 0, "table_idx": 5},
            ]),
            encoding="utf-8",
        )
        (tmp_dir / "table_3.md").write_text(
            "<!-- t_p1_0 -->\n| Alpha | 1 |\n", encoding="utf-8")
        (tmp_dir / "table_5.md").write_text(
            "<!-- t_p1_1 -->\n| Gamma | 9 |\n", encoding="utf-8")

        md = (
            "<!-- t_p1_1 -->\n"
            "*Таблица 1*\n\n"
            "| X | Y |\n"
            "|---|---|\n"
            "| Alpha | 1 |\n"
        )
        assets = pipeline._build_asset_registry(
            md, "doc", img_dir, tmp_dir=tmp_dir,
        )
        tables = assets["assets"]["tables"]
        assert len(tables) == 1
        # Маркер t_p1_1 → table_5.png, несмотря на содержимое (table_3 ближе)
        assert tables[0]["image_path"] == "image/table_5.png"
        assert tables[0]["_image_source"] == "marker"


    def test_extract_images_from_md(self):
        images = pipeline._extract_images_from_md(ASSET_MD)
        assert len(images) == 1
        img = images[0]
        assert img["asset_type"] == "image"
        assert img["caption"] == "Рисунок 1"
        assert img["image_path"] == "image/fig_1.png"
        assert isinstance(img["md_line"], int)

    def test_build_asset_registry(self, tmp_path):
        img_dir = tmp_path / "image"
        img_dir.mkdir()
        (img_dir / "fig_1.png").write_bytes(b"png")
        (img_dir / "table_1.png").write_bytes(b"png")
        assets = pipeline._build_asset_registry(
            ASSET_MD, "so153_molniezashita", img_dir, document_id="СО 153-34.21.122-2003",
        )
        assert assets["document_id"] == "СО 153-34.21.122-2003"
        assert assets["doc_slug"] == "so153_molniezashita"
        assert assets["image_dir"] == "image"
        assert len(assets["assets"]["tables"]) == 1
        assert len(assets["assets"]["images"]) == 1
        assert assets["assets"]["tables"][0]["asset_id"] == "so153_molniezashita/table/1"
        assert assets["assets"]["images"][0]["asset_id"] == "so153_molniezashita/fig/1"

    def test_build_asset_registry_missing_image(self, tmp_path):
        """Файл изображения отсутствует → asset пропускается с warning."""
        img_dir = tmp_path / "image"
        img_dir.mkdir()
        (img_dir / "table_1.png").write_bytes(b"png")  # fig_1.png отсутствует
        assets = pipeline._build_asset_registry(ASSET_MD, "so153", img_dir)
        assert len(assets["assets"]["images"]) == 0
        assert len(assets["assets"]["tables"]) == 1

    def test_build_asset_registry_no_img_dir(self, tmp_path):
        """image/ не найден → реестр активов пуст (архитектура §9)."""
        assets = pipeline._build_asset_registry(ASSET_MD, "so153", None)
        assert assets["assets"]["tables"] == []
        assert assets["assets"]["images"] == []

    def test_link_assets_to_chunks(self, tmp_path):
        img_dir = tmp_path / "image"
        img_dir.mkdir()
        (img_dir / "fig_1.png").write_bytes(b"png")
        (img_dir / "table_1.png").write_bytes(b"png")

        assets = pipeline._build_asset_registry(ASSET_MD, "so153_molniezashita", img_dir)
        chunks = [
            {"chunk_id": "so153_molniezashita/3", "text": ASSET_MD, "assets": []},
        ]
        pipeline._link_assets_to_chunks(assets, chunks)

        assert assets["assets"]["tables"][0]["chunk_ids"] == ["so153_molniezashita/3"]
        assert assets["assets"]["images"][0]["chunk_ids"] == ["so153_molniezashita/3"]
        # Обратная связь: чанк получил asset_id
        assert "so153_molniezashita/table/1" in chunks[0]["assets"]
        assert "so153_molniezashita/fig/1" in chunks[0]["assets"]

    def test_write_rag_assets_strips_private_fields(self, tmp_path):
        img_dir = tmp_path / "image"
        img_dir.mkdir()
        (img_dir / "table_1.png").write_bytes(b"png")
        assets = pipeline._build_asset_registry(ASSET_MD, "so153", img_dir)
        out = tmp_path / f"{self.SOURCE_STEM}_assets.json"
        pipeline.write_rag_assets(assets, out)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "_md_block" not in data["assets"]["tables"][0]
        assert "_image_source" not in data["assets"]["tables"][0]


# ═══════════════════════════════════════════════════════════════════════════
# _classify_input
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyInput:
    def test_pdf(self):
        assert pipeline._classify_input(Path("file.pdf")) == "pdf"

    def test_docx(self):
        assert pipeline._classify_input(Path("file.docx")) == "docx"
        assert pipeline._classify_input(Path("file.doc")) == "docx"

    def test_md_standalone(self):
        assert pipeline._classify_input(Path("/tmp/file.md")) == "md_standalone"

    def test_md_rag(self):
        assert pipeline._classify_input(Path("/tmp/Markdown/so153/file.md")) == "md_rag"

    def test_unknown(self):
        assert pipeline._classify_input(Path("file.txt")) == "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# run_rag_pipeline + _run_rag_only — безопасный --rag
# ═══════════════════════════════════════════════════════════════════════════


class TestRunRagOnly:
    SOURCE_STEM = "СО153-34_21_122-2003 Молниезащита"

    def _setup(self, tmp_path):
        doc_dir = tmp_path / "Markdown" / "so153_molniezashita"
        img_dir = doc_dir / "image"
        img_dir.mkdir(parents=True)
        (img_dir / "table_1.png").write_bytes(b"png")
        md = (
            "## 3. ЗАЩИТА\n"
            "\n"
            "Текст с таблицей.\n"
            "\n"
            "*Таблица 3.1 — Значения*\n"
            "\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
        )
        md_path = doc_dir / f"{self.SOURCE_STEM}.md"
        md_path.write_text(md, encoding="utf-8")
        return md_path, md, img_dir

    def test_rag_only_does_not_modify_md(self, tmp_path, rag_config):
        """--rag на .md не изменяет .md и image/ (hash совпадает)."""
        md_path, md, img_dir = self._setup(tmp_path)
        img_hash_before = (img_dir / "table_1.png").read_bytes()

        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            ok = pipeline._run_rag_only(md_path, rag_config)

        assert ok is True
        assert md_path.read_text(encoding="utf-8") == md
        assert (img_dir / "table_1.png").read_bytes() == img_hash_before

        # Производные файлы созданы рядом с MD
        assert (md_path.parent / f"{self.SOURCE_STEM}_chunks.jsonl").exists()
        assert (md_path.parent / f"{self.SOURCE_STEM}_assets.json").exists()

    def test_rag_only_idempotent(self, tmp_path, rag_config):
        """Повторный запуск --rag даёт идентичный результат."""
        md_path, _, _ = self._setup(tmp_path)
        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            assert pipeline._run_rag_only(md_path, rag_config) is True
            first = (md_path.parent / f"{self.SOURCE_STEM}_chunks.jsonl").read_text(encoding="utf-8")
            assert pipeline._run_rag_only(md_path, rag_config) is True
            second = (md_path.parent / f"{self.SOURCE_STEM}_chunks.jsonl").read_text(encoding="utf-8")
        assert first == second

    def test_rag_only_atomic_no_tmp_left(self, tmp_path, rag_config):
        """Атомарная запись: не остаётся временных .tmp файлов."""
        md_path, _, _ = self._setup(tmp_path)
        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            assert pipeline._run_rag_only(md_path, rag_config) is True
        leftovers = list(md_path.parent.glob("*.tmp"))
        assert leftovers == []

    def test_rag_only_no_doc_key(self, tmp_path, rag_config):
        md_path = tmp_path / "Markdown" / "unknown" / "unknown.md"
        md_path.parent.mkdir(parents=True)
        md_path.write_text("## 1. ТЕКСТ\nТекст.\n", encoding="utf-8")
        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            ok = pipeline._run_rag_only(md_path, rag_config)
        assert ok is False
        assert not (md_path.parent / f"{self.SOURCE_STEM}_chunks.jsonl").exists()

    def test_rag_only_no_image_dir(self, tmp_path, rag_config):
        """image/ отсутствует → реестр активов пуст, но JSONL создаётся."""
        doc_dir = tmp_path / "Markdown" / "so153_molniezashita"
        doc_dir.mkdir(parents=True)
        md = "## 3. ЗАЩИТА\nТекст.\n"
        md_path = doc_dir / f"{self.SOURCE_STEM}.md"
        md_path.write_text(md, encoding="utf-8")
        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            ok = pipeline._run_rag_only(md_path, rag_config)
        assert ok is True
        assets = json.loads(
            (md_path.parent / f"{self.SOURCE_STEM}_assets.json").read_text(encoding="utf-8")
        )
        assert assets["assets"]["tables"] == []
        assert assets["assets"]["images"] == []


class TestRunRagPipeline:
    SOURCE_STEM = "СО153-34_21_122-2003 Молниезащита"
    def test_writes_jsonl_and_assets(self, tmp_path, rag_config):
        out_dir = tmp_path / "out"
        img_dir = out_dir / "image"
        img_dir.mkdir(parents=True)
        (img_dir / "table_1.png").write_bytes(b"png")
        md = (
            "## 3. ЗАЩИТА\n"
            "\n"
            "*Таблица 3.1 — Значения*\n"
            "\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |\n"
        )
        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            ok = pipeline.run_rag_pipeline(
                md, None, rag_config, "so153_molniezashita", img_dir, out_dir,
                self.SOURCE_STEM,
            )
        assert ok is True
        jsonl_path = out_dir / f"{self.SOURCE_STEM}_chunks.jsonl"
        assets_path = out_dir / f"{self.SOURCE_STEM}_assets.json"
        assert jsonl_path.exists()
        assert assets_path.exists()

        rows = [json.loads(l) for l in jsonl_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        # Таблица попала в чанк и связана через assets
        assert any("so153_molniezashita/table/1" in r["assets"] for r in rows)

        assets = json.loads(assets_path.read_text(encoding="utf-8"))
        table = assets["assets"]["tables"][0]
        assert table["chunk_ids"] == [rows[0]["chunk_id"]]

    def test_missing_tokenizer_fails(self, tmp_path, rag_config, monkeypatch):
        """Токенизатор недоступен и fallback запрещён → RuntimeError."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "transformers", None)
        with pytest.raises(RuntimeError):
            pipeline.run_rag_pipeline(
                "## 3. ТЕКСТ\nТекст.\n", None, rag_config,
                "so153_molniezashita", None, tmp_path,
                self.SOURCE_STEM,
            )


# ═══════════════════════════════════════════════════════════════════════════
# process_file — таблицы всегда вырезаются, --ai только vision
# ═══════════════════════════════════════════════════════════════════════════


class TestProcessFileTableExtraction:
    SOURCE_STEM = "СО153-34_21_122-2003 Молниезащита"
    def _pages(self):
        return [{
            "result": {
                "textAnnotation": {
                    "width": 10, "height": 10,
                    "blocks": [], "tables": [{"boundingBox": {"vertices": [{"x": 0, "y": 0}] * 4}}],
                    "pictures": [],
                }
            }
        }]

    def test_pdf_without_ai_extracts_table_images(self, tmp_path, rag_config):
        """PDF без --ai: extract_table_images вызывается (локальная вырезка)."""
        pdf = tmp_path / "СО153-34_21_122-2003 Молниезащита.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        md = "## 3. ЗАЩИТА\nТекст.\n"

        with patch("pipeline.send_to_yandex_ocr", return_value=self._pages()), \
             patch("pipeline.parse_yandex_json_to_md", return_value=(md, [], [])), \
             patch("pipeline.extract_images_from_pdf", return_value=[]), \
             patch("pipeline.extract_table_images", return_value=[{"path": "table_1.png"}]) as eti, \
             patch("pipeline.recognize_tables_vision") as rtv, \
             patch("pipeline.run_script_postprocess", side_effect=lambda m, i, **kw: m):
            ok = pipeline.process_file(
                str(pdf), use_ai=False, config={},
                api_key="key", folder_id="folder",
                output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
            )

        assert ok is True
        # Таблицы вырезаны без --ai
        eti.assert_called_once()
        # Vision НЕ запускалась без --ai
        rtv.assert_not_called()

    def test_pdf_persists_table_image_map(self, tmp_path):
        """После вырезки таблиц карта id→path сохраняется в tmp/<stem>/."""
        pdf = tmp_path / "СО153-34_21_122-2003 Молниезащита.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        md = "## 3. ЗАЩИТА\nТекст.\n"
        table_images = [
            {"page": 0, "table_idx": 3, "path": "table_3.png", "id": "t_p1_0"},
            {"page": 0, "table_idx": 5, "path": "table_5.png", "id": "t_p1_1"},
        ]
        with patch("pipeline.send_to_yandex_ocr", return_value=self._pages()), \
             patch("pipeline.parse_yandex_json_to_md", return_value=(md, [], [])), \
             patch("pipeline.extract_images_from_pdf", return_value=[]), \
             patch("pipeline.extract_table_images", return_value=table_images), \
             patch("pipeline.run_script_postprocess", side_effect=lambda m, i, **kw: m):
            ok = pipeline.process_file(
                str(pdf), use_ai=False, config={},
                api_key="key", folder_id="folder",
                output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
            )
        assert ok is True
        map_path = tmp_path / "tmp" / "СО153-34_21_122-2003 Молниезащита" / "table_images.json"
        assert map_path.exists()
        data = json.loads(map_path.read_text(encoding="utf-8"))
        assert data == table_images

    def test_derive_tmp_dir(self):
        assert pipeline._derive_tmp_dir(
            "/x/Markdown/doc", "doc",
        ) == Path("/x/tmp/doc")
        assert pipeline._derive_tmp_dir(
            Path("/x/Markdown/doc"), "doc",
        ) == Path("/x/tmp/doc")
        # Короткий путь без двух уровней родителей — None
        assert pipeline._derive_tmp_dir(Path("/x"), "doc") is None

    def test_pdf_with_ai_triggers_vision_additionally(self, tmp_path):
        """PDF с --ai: вырезка таблиц + vision-распознавание."""
        pdf = tmp_path / "СО153-34_21_122-2003 Молниезащита.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        md = "## 3. ЗАЩИТА\nТекст.\n"
        config = {
            "ai_postprocess": {"prompt": "test prompt"},
            "table_vision": {"api_key_env": "PROVOD_API_KEY"},
        }

        with patch("pipeline.send_to_yandex_ocr", return_value=self._pages()), \
             patch("pipeline.parse_yandex_json_to_md", return_value=(md, [], [])), \
             patch("pipeline.extract_images_from_pdf", return_value=[]), \
             patch("pipeline.extract_table_images", return_value=[{"path": "table_1.png"}]), \
             patch.dict(os.environ, {"PROVOD_API_KEY": "key"}), \
             patch("pipeline.recognize_tables_vision", return_value=1) as rtv, \
             patch("pipeline.run_script_postprocess", side_effect=lambda m, i, **kw: m), \
             patch("pipeline._call_ai_api", return_value=md):
            ok = pipeline.process_file(
                str(pdf), use_ai=True, config=config,
                api_key="key", folder_id="folder",
                output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
            )
        assert ok is True
        rtv.assert_called_once()

    def test_md_inside_markdown_requires_rag(self, tmp_path):
        """.md внутри Markdown/ без --rag → ошибка."""
        md_file = tmp_path / "Markdown" / "so153" / "so153.md"
        md_file.parent.mkdir(parents=True)
        md_file.write_text("## 1. ТЕКСТ\nТекст.\n", encoding="utf-8")
        ok = pipeline.process_file(
            str(md_file), use_ai=False, config={},
            api_key="", folder_id="",
            output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
        )
        assert ok is False

    def test_md_inside_markdown_with_rag_no_nesting(self, tmp_path, rag_config):
        """--rag на .md внутри Markdown/ не создаёт Markdown/<file>/Markdown."""
        doc_dir = tmp_path / "Markdown" / "so153_molniezashita"
        img_dir = doc_dir / "image"
        img_dir.mkdir(parents=True)
        (img_dir / "table_1.png").write_bytes(b"png")
        md = "## 3. ЗАЩИТА\nТекст.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
        md_file = doc_dir / f"{TestRunRagOnly.SOURCE_STEM}.md"
        md_file.write_text(md, encoding="utf-8")

        with patch("pipeline._init_tokenizer",
                   return_value=(lambda t: len(t) // 3, "qwen3")):
            ok = pipeline.process_file(
                str(md_file), use_ai=False, config={},
                api_key="", folder_id="",
                output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
                use_rag=True, rag_config=rag_config,
            )
        assert ok is True
        # Производные файлы рядом с MD, без вложенного Markdown/
        assert (doc_dir / f"{self.SOURCE_STEM}_chunks.jsonl").exists()
        assert (doc_dir / f"{self.SOURCE_STEM}_assets.json").exists()
        assert not (doc_dir / "Markdown").exists()


# ═══════════════════════════════════════════════════════════════════════════
# safe_write — атомарность
# ═══════════════════════════════════════════════════════════════════════════


class TestSafeWriteAtomic:
    def test_writes_content(self, tmp_path):
        p = tmp_path / "a" / "b.txt"
        pipeline.safe_write(p, "hello")
        assert p.read_text(encoding="utf-8") == "hello"

    def test_no_tmp_left(self, tmp_path):
        p = tmp_path / "x.txt"
        pipeline.safe_write(p, "data")
        assert list(tmp_path.glob("*.tmp")) == []
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_overwrite(self, tmp_path):
        p = tmp_path / "x.txt"
        pipeline.safe_write(p, "one")
        pipeline.safe_write(p, "two")
        assert p.read_text(encoding="utf-8") == "two"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
