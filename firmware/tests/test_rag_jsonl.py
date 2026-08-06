#!/usr/bin/env python3
"""Тесты для RAG JSONL Converter (ADR-9, секция 12 pipeline.py).

Проверяют 9 функций модуля md_to_rag_jsonl:
  - load_rag_config()
  - _extract_heading_number()
  - _build_ancestors()
  - parse_md_structure()
  - extract_clause_text()
  - extract_references()
  - _get_page_for_heading()
  - _split_oversized_clause()
  - build_rag_jsonl()
  + _find_doc_key() и интеграцию в process_file() (--rag)
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

# Тестовый JSON ГОСТ СО153-34.21.122-2003 (как в test_headings.py)
TEST_JSON = "/mnt/sdb/!База_ГОСТ/tmp/СО153-34_21_122-2003 Молниезащита/yandex_result.json"


@pytest.fixture(scope="module")
def rag_config():
    return pipeline.load_rag_config(RAG_CONFIG)


@pytest.fixture(scope="module")
def so153_md():
    """Структурированный MD в формате ГОСТ: ## глава, ### раздел, #### пункт."""
    return (
        "## СОДЕРЖАНИЕ\n"
        "1. Введение\n"
        "### 3.2. Внешняя молниезащитная система\n"
        "### 3.2.1. Молниеприемники\n"
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
        "\n"
        "##### 3.2.1.1. Общие соображения\n"
        "Подпункт с деталями по размещению молниеприемников.\n"
    )


# ═══════════════════════════════════════════════════════════════════════════
# load_rag_config
# ═══════════════════════════════════════════════════════════════════════════


def test_load_rag_config_ok(rag_config):
    """Загрузка реального rag_config.yaml: секции defaults/references/documents."""
    assert "defaults" in rag_config
    assert "references" in rag_config
    assert "documents" in rag_config
    assert "so153_molniezashita" in rag_config["documents"]
    # v2: токен-лимит вместо char-лимита, Qwen3 tokenizer, статус документа
    assert rag_config["defaults"]["max_chunk_tokens"] == 7000
    assert rag_config["defaults"]["tokenizer"] == "Qwen/Qwen3-Embedding-8B"
    assert rag_config["defaults"]["tokenizer_revision"] == "main"
    assert rag_config["defaults"]["allow_degraded_fallback"] is False
    assert rag_config["defaults"]["default_status"] == "active"
    assert len(rag_config["references"]["patterns"]) >= 5
    doc = rag_config["documents"]["so153_molniezashita"]
    assert doc["document_id"] == "СО 153-34.21.122-2003"
    assert doc["status"] == "active"
    assert doc["status_reason"] is None
    assert doc["replaced_by_document_id"] is None
    assert "Содержание" in doc["ignore_sections"]


def test_load_rag_config_inactive_document(rag_config):
    """Недействующий документ: официальный номер в replaced_by_document_id, не slug."""
    doc = rag_config["documents"]["old_snip"]
    assert doc["status"] == "inactive"
    assert doc["status_reason"] == "Заменён на СП 60.13330.2012"
    # Официальный номер документа-преемника (не doc_key/slug)
    assert doc["replaced_by_document_id"] == "СП 60.13330.2012"
    # Ключ каталога — отдельное опциональное поле
    assert doc["replaced_by_doc_key"] == "sp60_otoplenie"


def test_load_rag_config_missing_file():
    """Отсутствующий файл → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        pipeline.load_rag_config("/nonexistent/rag_config.yaml")


def test_load_rag_config_invalid_yaml(tmp_path):
    """Битый YAML → yaml.YAMLError."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("defaults: [unclosed\n", encoding="utf-8")
    with pytest.raises(Exception):
        pipeline.load_rag_config(bad)


# ═══════════════════════════════════════════════════════════════════════════
# _extract_heading_number
# ═══════════════════════════════════════════════════════════════════════════


def test_extract_heading_number_basic():
    """'3.2.1. Молниеприемники' → '3.2.1'."""
    assert pipeline._extract_heading_number("3.2.1. Молниеприемники") == "3.2.1"


def test_extract_heading_number_chapter():
    """'3. ЗАЩИТА' → '3'."""
    assert pipeline._extract_heading_number("3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ") == "3"


def test_extract_heading_number_deep():
    """'3.2.1.1. Общие соображения' → '3.2.1.1'."""
    assert pipeline._extract_heading_number("3.2.1.1. Общие соображения") == "3.2.1.1"


def test_extract_heading_number_no_number():
    """Заголовок без номера → None."""
    assert pipeline._extract_heading_number("Введение") is None


def test_extract_heading_number_requires_trailing_dot():
    """'3.2.1 Молниеприемники' (без точки после номера) → None."""
    assert pipeline._extract_heading_number("3.2.1 Молниеприемники") is None


def test_extract_heading_number_empty():
    assert pipeline._extract_heading_number("") is None
    assert pipeline._extract_heading_number(None) is None


# ═══════════════════════════════════════════════════════════════════════════
# parse_md_structure + extract_clause_text
# ═══════════════════════════════════════════════════════════════════════════


def test_parse_md_structure_levels_and_numbers(so153_md):
    """Уровни ##..#####, номера и границы (next_line_num)."""
    struct = pipeline.parse_md_structure(so153_md)
    # ## СОДЕРЖАНИЕ (level 2, number None), ## 3. (2), ### (3), #### (4), ##### (5)
    levels = [h["level"] for h in struct]
    assert levels == [2, 3, 3, 2, 3, 4, 5]
    assert struct[0]["number"] is None
    assert struct[0]["heading_text"] == "СОДЕРЖАНИЕ"
    assert struct[3]["number"] == "3"
    assert struct[4]["number"] == "3.2"
    assert struct[5]["number"] == "3.2.1"
    assert struct[6]["number"] == "3.2.1.1"
    # Границы: СОДЕРЖАНИЕ до строки '### 3.2.' и т.д.
    assert struct[0]["next_line_num"] == struct[1]["line_num"]
    assert struct[5]["next_line_num"] == struct[6]["line_num"]
    # Последний heading → до конца файла
    assert struct[-1]["next_line_num"] == len(so153_md.splitlines())


def test_parse_md_structure_ignores_non_headings():
    """Строки без '#' не распознаются как заголовки."""
    md = "Просто текст\n## 2. ЗАГОЛОВОК\nещё текст\n# Один hash — не заголовок\n###### 6 хэшей — не заголовок\n"
    struct = pipeline.parse_md_structure(md)
    assert len(struct) == 1
    assert struct[0]["number"] == "2"


def test_parse_md_structure_empty():
    assert pipeline.parse_md_structure("") == []
    assert pipeline.parse_md_structure("текст без заголовков") == []


def test_extract_clause_text_between_headings(so153_md):
    """Текст от заголовка до следующего заголовка."""
    struct = pipeline.parse_md_structure(so153_md)
    # Пункт #### 3.2.1. — текст 'Молниеприемники могут быть...'
    i = 5
    text = pipeline.extract_clause_text(so153_md, struct[i]["line_num"], struct[i]["next_line_num"])
    assert text == "Молниеприемники могут быть естественными или искусственными."
    # Глава ## 3. — вводный абзац
    i = 3
    text = pipeline.extract_clause_text(so153_md, struct[i]["line_num"], struct[i]["next_line_num"])
    assert text.startswith("Вводный абзац главы")


def test_extract_clause_text_empty():
    """Заголовок без текста → пустая строка."""
    md = "## A\n## B\nтекст B"
    struct = pipeline.parse_md_structure(md)
    text = pipeline.extract_clause_text(md, struct[0]["line_num"], struct[0]["next_line_num"])
    assert text == ""


# ═══════════════════════════════════════════════════════════════════════════
# _build_ancestors
# ═══════════════════════════════════════════════════════════════════════════


def _struct(md):
    return pipeline.parse_md_structure(md)


def test_build_ancestors_adr_table():
    """Таблица ADR-9a: уровни → chapter/section/clause."""
    md = (
        "## 3. ЗАЩИТА\n"
        "### 3.2. Внешняя молниезащитная система\n"
        "#### 3.2.1. Молниеприемники\n"
        "##### 3.2.1.1. Общие соображения\n"
    )
    s = _struct(md)
    assert pipeline._build_ancestors(s, 0) == {"chapter": "3", "section": None, "clause": None}
    assert pipeline._build_ancestors(s, 1) == {"chapter": "3", "section": "3.2", "clause": None}
    assert pipeline._build_ancestors(s, 2) == {"chapter": "3", "section": "3.2", "clause": "3.2.1"}
    assert pipeline._build_ancestors(s, 3) == {"chapter": "3", "section": "3.2", "clause": "3.2.1.1"}


def test_build_ancestors_no_parents():
    """Первый заголовок без предков."""
    md = "#### 3.2.1. Молниеприемники\n"
    s = _struct(md)
    assert pipeline._build_ancestors(s, 0) == {"chapter": None, "section": None, "clause": "3.2.1"}


def test_build_ancestors_across_chapters():
    """Раздел наследует главу, но не предыдущий раздел другой главы."""
    md = (
        "## 2. ТЕРМИНЫ\n"
        "### 2.1. Определения\n"
        "## 3. ЗАЩИТА\n"
        "### 3.2. Внешняя молниезащитная система\n"
    )
    s = _struct(md)
    # ### 3.2. → глава 3 (не 2), раздел 3.2
    assert pipeline._build_ancestors(s, 3) == {"chapter": "3", "section": "3.2", "clause": None}


def test_build_ancestors_unnumbered_subclause():
    """Ненумерованный ##### наследует clause предыдущего ####."""
    md = (
        "## 3. ЗАЩИТА\n"
        "### 3.2. Внешняя МЗС\n"
        "#### 3.2.1. Молниеприемники\n"
        "##### Общие соображения\n"
    )
    s = _struct(md)
    assert pipeline._build_ancestors(s, 3) == {"chapter": "3", "section": "3.2", "clause": "3.2.1"}


def test_build_ancestors_new_chapter_resets_section_clause():
    """Регрессия: новый top-level ## не наследует section/clause из предыдущей главы.

    В СО153 после главы «4. ...» идёт раздел «### 4.7. ...», затем повторный
    top-level «## 1/2/3» (рекомендации). Раньше «## 1» получал чужой section 4.7.
    """
    md = (
        "## 4. ЗАЩИТА ОТ ВТОРИЧНЫХ ВОЗДЕЙСТВИЙ МОЛНИИ\n"
        "### 4.7. Защита оборудования в существующих зданиях\n"
        "## 1. Разработка эксплуатационно-технической документации\n"
    )
    s = _struct(md)
    # Второй top-level ## 1: section/clause сброшены, глава — своя
    assert pipeline._build_ancestors(s, 2) == {"chapter": "1", "section": None, "clause": None}


def test_build_ancestors_new_chapter_after_clause_resets():
    """Новый ## сбрасывает даже наследованный clause от предыдущего ####."""
    md = (
        "## 3. ЗАЩИТА\n"
        "### 3.2. Внешняя МЗС\n"
        "#### 3.2.1. Молниеприемники\n"
        "## 2. Порядок приемки\n"
    )
    s = _struct(md)
    assert pipeline._build_ancestors(s, 3) == {"chapter": "2", "section": None, "clause": None}


def test_build_ancestors_section_bounded_by_chapter():
    """Регрессия: #### в новой главе без ### не наследует section из предыдущей главы."""
    md = (
        "## 3. ЗАЩИТА\n"
        "### 3.2. Внешняя молниезащитная система\n"
        "## 2. ПОРЯДОК ПРИЕМКИ\n"
        "#### 2.1. Оформление\n"
    )
    s = _struct(md)
    assert pipeline._build_ancestors(s, 3) == {"chapter": "2", "section": None, "clause": "2.1"}


def test_build_heading_texts_new_chapter_resets():
    """Регрессия: heading_texts для нового ## не наследует section/clause тексты."""
    md = (
        "## 4. ЗАЩИТА ОТ ВТОРИЧНЫХ ВОЗДЕЙСТВИЙ МОЛНИИ\n"
        "### 4.7. Защита оборудования в существующих зданиях\n"
        "## 1. Разработка эксплуатационно-технической документации\n"
    )
    s = _struct(md)
    assert pipeline._build_heading_texts(s, 2) == {
        "chapter": "1. Разработка эксплуатационно-технической документации",
        "section": None,
        "clause": None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# _build_embedding_text
# ═══════════════════════════════════════════════════════════════════════════


def test_build_embedding_text_full():
    """Все три заголовка + текст; порядок chapter → section → clause."""
    ht = {
        "chapter": "2. ОБЩИЕ ПОЛОЖЕНИЯ",
        "section": "2.3. Параметры токов молнии",
        "clause": "2.3.1. Амплитуда",
    }
    text = "Исходный текст."
    assert pipeline._build_embedding_text(ht, text) == (
        "Заголовок главы: 2. ОБЩИЕ ПОЛОЖЕНИЯ\n"
        "Заголовок раздела: 2.3. Параметры токов молнии\n"
        "Заголовок пункта: 2.3.1. Амплитуда\n"
        "\n"
        "Исходный текст."
    )


def test_build_embedding_text_short_section_only():
    """Короткий раздел: глава + раздел + текст (без пустых строк/None)."""
    ht = {
        "chapter": "2. ОБЩИЕ ПОЛОЖЕНИЯ",
        "section": "2.3. Параметры токов молнии",
        "clause": None,
    }
    text = "Молния представляет собой импульс тока."
    emb = pipeline._build_embedding_text(ht, text)
    assert emb == (
        "Заголовок главы: 2. ОБЩИЕ ПОЛОЖЕНИЯ\n"
        "Заголовок раздела: 2.3. Параметры токов молнии\n"
        "\n"
        "Молния представляет собой импульс тока."
    )
    # Исходный текст присутствует целиком
    assert emb.endswith(text)


def test_build_embedding_text_chapter_only():
    """Только глава (top-level): один заголовок + текст."""
    ht = {"chapter": "1. ВВЕДЕНИЕ", "section": None, "clause": None}
    assert pipeline._build_embedding_text(ht, "Текст.") == (
        "Заголовок главы: 1. ВВЕДЕНИЕ\n"
        "\n"
        "Текст."
    )


def test_build_embedding_text_no_headings():
    """Нет заголовков (или None) → только исходный текст."""
    assert pipeline._build_embedding_text({}, "text") == "text"
    assert pipeline._build_embedding_text(None, "text") == "text"
    assert pipeline._build_embedding_text(
        {"chapter": None, "section": None, "clause": None}, "text"
    ) == "text"


# ═══════════════════════════════════════════════════════════════════════════
# extract_references
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def patterns(rag_config):
    return rag_config["references"]["patterns"]


def test_extract_references_acceptance(patterns):
    """Приёмка плана: 'см. п. 3.2.1, табл. 3.1' → ['п. 3.2.1', 'табл. 3.1']."""
    refs = pipeline.extract_references("см. п. 3.2.1, табл. 3.1", patterns)
    assert refs == ["п. 3.2.1", "табл. 3.1"]


def test_extract_references_all_patterns(patterns):
    """Все 5 паттернов из spec."""
    text = (
        "см. п. 3.2.1; согласно п. 1.2; по пункту 4.5; "
        "таблица 3.1 и табл. 2.2; разд. 5; гл. 1 и глава 7"
    )
    refs = pipeline.extract_references(text, patterns)
    assert "п. 3.2.1" in refs
    assert "п. 1.2" in refs
    assert "п. 4.5" in refs
    assert "табл. 3.1" in refs
    assert "табл. 2.2" in refs
    assert "разд. 5" in refs
    assert "гл. 1" in refs
    assert "гл. 7" in refs


def test_extract_references_dedup(patterns):
    """Дедупликация одинаковых ссылок."""
    refs = pipeline.extract_references("см. п. 3.2.1 и снова см. п. 3.2.1", patterns)
    assert refs == ["п. 3.2.1"]


def test_extract_references_empty():
    """Нет совпадений / пустой текст / пустые паттерны → []."""
    assert pipeline.extract_references("просто текст", ["см\\.\\s*п\\.\\s*(\\d+)"]) == []
    assert pipeline.extract_references("", ["см\\.\\s*п\\.\\s*(\\d+)"]) == []
    assert pipeline.extract_references("см. п. 3.2.1", []) == []
    assert pipeline.extract_references("см. п. 3.2.1", None) == []


def test_extract_references_bad_pattern(patterns):
    """Некорректный regexp не роняет вызов."""
    refs = pipeline.extract_references("см. п. 3.2.1", patterns + ["("])
    assert "п. 3.2.1" in refs


# ═══════════════════════════════════════════════════════════════════════════
# _get_page_for_heading
# ═══════════════════════════════════════════════════════════════════════════


def test_get_page_for_heading():
    jh = [
        {"page": 0, "number": "1"},
        {"page": 7, "number": "3.2.1"},
        {"page": 9, "number": "3.2.1"},  # дубликат — берётся первое
    ]
    assert pipeline._get_page_for_heading("3.2.1", jh) == 7


def test_get_page_for_heading_not_found():
    jh = [{"page": 0, "number": "1"}]
    assert pipeline._get_page_for_heading("9.9", jh) is None


def test_get_page_for_heading_empty_inputs():
    assert pipeline._get_page_for_heading("1", None) is None
    assert pipeline._get_page_for_heading("1", []) is None
    assert pipeline._get_page_for_heading(None, [{"page": 0, "number": "1"}]) is None


# ═══════════════════════════════════════════════════════════════════════════
# _split_oversized_clause
# ═══════════════════════════════════════════════════════════════════════════


def test_split_oversized_small_text():
    """len(text) <= max_chars → [text]."""
    assert pipeline._split_oversized_clause("маленький", 1500) == ["маленький"]


def test_split_oversized_groups_paragraphs():
    """Параграфы группируются в чанки ≤ max_chars, порядок сохраняется."""
    paras = [f"Параграф номер {i} " * 3 for i in range(5)]  # ~51 символ
    text = "\n\n".join(paras)
    parts = pipeline._split_oversized_clause(text, 120)
    assert len(parts) == 3
    assert all(len(p) <= 120 for p in parts)
    # Склейка сохраняет исходный текст (с точностью до разделителей)
    assert "\n\n".join(parts) == text


def test_split_oversized_table_atomic():
    """Таблица (| строки) не разрывается."""
    table = "| a | b |\n| 1 | 2 |\n| 3 | 4 |"
    text = "Текст.\n\n" + table + "\n\nЕщё текст"
    parts = pipeline._split_oversized_clause(text, 10)
    assert any(table in p for p in parts)


def test_split_oversized_giant_table_as_is():
    """Одиночный блок > max_chars публикуется как есть (ADR-9b п.5)."""
    giant = "| " + "x" * 500 + " |"
    text = "До\n\n" + giant + "\n\nПосле"
    parts = pipeline._split_oversized_clause(text, 100)
    assert giant in parts


def test_split_oversized_code_block_atomic():
    """Кодовый блок (```...```) не разрывается, даже с пустыми строками."""
    code = "```\nline\n\n" * 10 + "```"
    parts = pipeline._split_oversized_clause(code, 40)
    assert len(parts) >= 1
    assert all("```" in p for p in parts)


def test_split_oversized_empty():
    assert pipeline._split_oversized_clause("", 10) == [""]


# ═══════════════════════════════════════════════════════════════════════════
# _find_doc_key
# ═══════════════════════════════════════════════════════════════════════════


def test_find_doc_key_exact(rag_config):
    assert pipeline._find_doc_key("СО153-34_21_122-2003 Молниезащита.pdf", rag_config) == "so153_molniezashita"


def test_find_doc_key_normalized(rag_config):
    """Дефисы/подчёркивания и регистр не мешают сопоставлению."""
    assert pipeline._find_doc_key("со153-34-21-122-2003 молниезащита.PDF", rag_config) == "so153_molniezashita"


def test_find_doc_key_stem_match(rag_config):
    """Совпадение по stem (например, .docx вместо .pdf)."""
    assert pipeline._find_doc_key("СО153-34_21_122-2003 Молниезащита.docx", rag_config) == "so153_molniezashita"


def test_find_doc_key_not_found(rag_config):
    assert pipeline._find_doc_key("other_document.pdf", rag_config) is None
    assert pipeline._find_doc_key("other_document.pdf", None) is None


# ═══════════════════════════════════════════════════════════════════════════
# build_rag_jsonl
# ═══════════════════════════════════════════════════════════════════════════


def test_build_rag_jsonl_basic(so153_md, rag_config):
    """Приёмка: корректные поля document_id/title/chapter/section/clause/text/source/references."""
    jh = [
        {"page": 7, "number": "3"},
        {"page": 8, "number": "3.2"},
        {"page": 9, "number": "3.2.1"},
        {"page": 10, "number": "3.2.1.1"},
    ]
    jsonl = pipeline.build_rag_jsonl(so153_md, jh, rag_config, "so153_molniezashita")
    rows = [json.loads(line) for line in jsonl.splitlines() if line.strip()]

    # СОДЕРЖАНИЕ + подразделы пропущены → остаются 4 clause
    assert len(rows) == 4

    r0 = rows[0]  # ## 3.
    assert r0["document_id"] == "СО 153-34.21.122-2003"
    assert r0["title"] == "Инструкция по устройству молниезащиты зданий, сооружений и промышленных коммуникаций"
    assert r0["chapter"] == "3"
    assert r0["section"] is None
    assert r0["clause"] is None
    assert r0["source"] == {"file": "СО153-34_21_122-2003 Молниезащита.pdf", "page": 7}
    assert r0["references"] == []
    assert "Вводный абзац главы" in r0["text"]

    r2 = rows[2]  # #### 3.2.1.
    assert r2["chapter"] == "3"
    assert r2["section"] == "3.2"
    assert r2["clause"] == "3.2.1"
    assert r2["source"]["page"] == 9

    r1 = rows[1]  # ### 3.2. — references из текста
    assert r1["references"] == ["п. 3.2.1", "табл. 3.1"]

    r3 = rows[3]  # ##### 3.2.1.1.
    assert r3["clause"] == "3.2.1.1"
    assert r3["source"]["page"] == 10


def test_build_rag_jsonl_every_line_valid(so153_md, rag_config):
    """Каждая строка JSONL парсится json.loads()."""
    jsonl = pipeline.build_rag_jsonl(so153_md, None, rag_config, "so153_molniezashita")
    for line in jsonl.splitlines():
        if line.strip():
            obj = json.loads(line)
            assert set(obj.keys()) >= {
                "document_id", "title", "chapter", "section", "clause",
                "text", "source", "references",
            }


def test_build_rag_jsonl_ignore_sections(so153_md, rag_config):
    """ignore_sections=['Содержание'] → секция и подразделы пропущены."""
    jsonl = pipeline.build_rag_jsonl(so153_md, None, rag_config, "so153_molniezashita")
    assert "Текст оглавления" not in jsonl
    rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
    # Ни один heading из оглавления не попал в ancestors: раздел 3.2.1.1
    # имеет section='3.2' из активного ###, а не из оглавления
    assert all("оглавления" not in r["text"] for r in rows)


def test_build_rag_jsonl_source_page_null_without_json(so153_md, rag_config):
    """Без Yandex JSON → source.page = null (режим .md + --rag)."""
    jsonl = pipeline.build_rag_jsonl(so153_md, None, rag_config, "so153_molniezashita")
    rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
    assert all(r["source"]["page"] is None for r in rows)


def test_build_rag_jsonl_oversized_splits(rag_config):
    """Oversized clause → подчанки с повторением метаданных и «(ч. N)»."""
    cfg = dict(rag_config)
    cfg["defaults"] = dict(rag_config["defaults"], max_chunk_chars=60)
    paras = [f"Абзац {i} с детальным описанием молниеприемника и токоотвода. " * 2 for i in range(4)]
    md = (
        "## 3. ЗАЩИТА\n"
        "\n"
        "### 3.2. Внешняя молниезащитная система\n"
        "\n"
        "#### 3.2.1. Молниеприемники\n"
        + "\n\n".join(paras)
    )
    jsonl = pipeline.build_rag_jsonl(md, [{"page": 5, "number": "3.2.1"}], cfg, "so153_molniezashita")
    rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
    assert len(rows) >= 2
    clauses = [r["clause"] for r in rows]
    assert any("(ч. 1)" in c for c in clauses)
    assert any("(ч. 2)" in c for c in clauses)
    # Метаданные повторяются в каждом подчанке
    for r in rows:
        assert r["chapter"] == "3"
        assert r["section"] == "3.2"
        assert r["source"]["page"] == 5
        assert r["document_id"] == "СО 153-34.21.122-2003"


def test_build_rag_jsonl_unknown_doc_key(so153_md, rag_config):
    """Неизвестный doc_key → ValueError."""
    with pytest.raises(ValueError):
        pipeline.build_rag_jsonl(so153_md, None, rag_config, "unknown_doc")


def test_build_rag_jsonl_empty_md(rag_config):
    """Пустой MD → пустая JSONL-строка."""
    jsonl = pipeline.build_rag_jsonl("", None, rag_config, "so153_molniezashita")
    assert jsonl == ""


def test_build_rag_jsonl_skips_empty_clauses(rag_config):
    """Заголовок без текста → clause пропущен."""
    md = "## 1. ПУСТАЯ ГЛАВА\n## 2. ГЛАВА С ТЕКСТОМ\nТекст второй главы."
    jsonl = pipeline.build_rag_jsonl(md, None, rag_config, "so153_molniezashita")
    rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["chapter"] == "2"


# ═══════════════════════════════════════════════════════════════════════════
# Реальные данные: yandex_result.json СО153 (приёмка ADR-9c)
# ═══════════════════════════════════════════════════════════════════════════


def test_build_rag_jsonl_real_json(rag_config):
    """Приёмка: на реальном JSON (29 стр.) JSONL валиден, есть page и ignore_sections."""
    if not os.path.exists(TEST_JSON):
        pytest.skip(f"Тестовый JSON не найден: {TEST_JSON}")
    with open(TEST_JSON, encoding="utf-8") as f:
        pages = json.load(f)
    md_text, _, _ = pipeline.parse_yandex_json_to_md(pages=pages)
    headings = pipeline._extract_headings_from_json(pages)
    md_text = pipeline._apply_headings_to_md(md_text, headings)
    assert len(headings) >= 68

    jsonl = pipeline.build_rag_jsonl(md_text, headings, rag_config, "so153_molniezashita")
    rows = [json.loads(l) for l in jsonl.splitlines() if l.strip()]
    assert len(rows) > 0
    # Ни один clause не содержит оглавление (ignore_sections)
    for r in rows:
        if r["clause"]:
            assert "содержание" not in r["text"].lower()
    # Хотя бы один clause имеет страницу из Yandex JSON
    assert any(r["source"]["page"] is not None for r in rows)


# ═══════════════════════════════════════════════════════════════════════════
# Интеграция в process_file() (--rag)
# ═══════════════════════════════════════════════════════════════════════════


def _fake_pdf_env(tmp_path, name="input.pdf"):
    pdf = tmp_path / name
    pdf.write_bytes(b"%PDF-1.4 fake")
    return str(pdf), str(tmp_path / "out"), str(tmp_path / "tmp")


def test_process_file_rag_writes_jsonl(tmp_path, rag_config):
    """process_file(use_rag=True) создаёт Markdown/<file>/rag_chunks.jsonl + assets."""
    pdf, out_base, tmp_base = _fake_pdf_env(tmp_path, "СО153-34_21_122-2003 Молниезащита.pdf")
    pages = [{"result": {"textAnnotation": {"width": 10, "height": 10,
                                             "blocks": [], "tables": [], "pictures": []}}}]
    md = "## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ\nТекст главы.\n"

    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", return_value=(md, [], [])), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline._init_tokenizer",
               return_value=(lambda t: len(t) // 3, "qwen3")), \
         patch("pipeline.run_script_postprocess", side_effect=lambda m, i, **kw: m):
        ok = pipeline.process_file(
            pdf, use_ai=False, config={},
            api_key="key", folder_id="folder",
            output_base=out_base, tmp_base=tmp_base,
            use_rag=True, rag_config=rag_config,
        )

    assert ok is True
    rag_path = Path(out_base) / "СО153-34_21_122-2003 Молниезащита" / "СО153-34_21_122-2003 Молниезащита_chunks.jsonl"
    assert rag_path.exists()
    rows = [json.loads(l) for l in rag_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["chapter"] == "3"
    assert rows[0]["document_id"] == "СО 153-34.21.122-2003"
    # v2: нет source.page; есть assets и chunking_method
    assert "page" not in rows[0]["source"]
    assert rows[0]["chunking_method"] == "qwen3"
    assert "chunk_id" in rows[0]
    # rag_assets.json создаётся
    assets_path = Path(out_base) / "СО153-34_21_122-2003 Молниезащита" / "СО153-34_21_122-2003 Молниезащита_assets.json"
    assert assets_path.exists()


def test_process_file_rag_no_doc_key(tmp_path, rag_config):
    """Файл не найден в конфиге → warning, но обработка успешна."""
    pdf, out_base, tmp_base = _fake_pdf_env(tmp_path, "unknown.pdf")
    pages = [{"result": {"textAnnotation": {"width": 10, "height": 10,
                                             "blocks": [], "tables": [], "pictures": []}}}]
    md = "## 3. ТЕКСТ\nТекст.\n"

    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", return_value=(md, [], [])), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.run_script_postprocess", side_effect=lambda m, i, **kw: m):
        ok = pipeline.process_file(
            pdf, use_ai=False, config={},
            api_key="key", folder_id="folder",
            output_base=out_base, tmp_base=tmp_base,
            use_rag=True, rag_config=rag_config,
        )
    assert ok is True
    # rag_chunks.jsonl не создан (doc_key не найден)
    assert not (Path(out_base) / "unknown" / "unknown_chunks.jsonl").exists()


def test_process_file_without_rag_no_jsonl(tmp_path, rag_config):
    """Без --rag JSONL не создаётся."""
    pdf, out_base, tmp_base = _fake_pdf_env(tmp_path)
    pages = [{"result": {"textAnnotation": {"width": 10, "height": 10,
                                             "blocks": [], "tables": [], "pictures": []}}}]
    md = "## 3. ТЕКСТ\nТекст.\n"
    with patch("pipeline.send_to_yandex_ocr", return_value=pages), \
         patch("pipeline.parse_yandex_json_to_md", return_value=(md, [], [])), \
         patch("pipeline.extract_images_from_pdf", return_value=[]), \
         patch("pipeline.run_script_postprocess", side_effect=lambda m, i, **kw: m):
        ok = pipeline.process_file(
            pdf, use_ai=False, config={},
            api_key="key", folder_id="folder",
            output_base=out_base, tmp_base=tmp_base,
        )
    assert ok is True
    assert not (Path(out_base) / "input" / "input_chunks.jsonl").exists()


def test_process_file_md_rag(tmp_path, rag_config):
    """Режим .md + --rag (без --ai): JSONL из готового MD, source.page = null."""
    md_file = tmp_path / "СО153-34_21_122-2003 Молниезащита.md"
    md_file.write_text("## 3. ЗАЩИТА ОТ ПРЯМЫХ УДАРОВ МОЛНИИ\nТекст главы.\n", encoding="utf-8")
    out_base = str(tmp_path / "out")
    tmp_base = str(tmp_path / "tmp")

    with patch("pipeline._init_tokenizer",
               return_value=(lambda t: len(t) // 3, "qwen3")):
        ok = pipeline.process_file(
            str(md_file), use_ai=False, config={},
            api_key="", folder_id="",
            output_base=out_base, tmp_base=tmp_base,
            use_rag=True, rag_config=rag_config,
        )
    assert ok is True
    rag_path = Path(out_base) / "СО153-34_21_122-2003 Молниезащита" / "СО153-34_21_122-2003 Молниезащита_chunks.jsonl"
    assert rag_path.exists()
    rows = [json.loads(l) for l in rag_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    # v2: source.page удалён из публичной схемы; остаётся _source_page = null
    assert "page" not in rows[0]["source"]
    assert rows[0]["_source_page"] is None
    assert rows[0]["status"] == "active"
    assert rows[0]["chunking_method"] in ("qwen3", "degraded_chars_per_token")


def test_process_file_md_without_ai_rag_fails(tmp_path, rag_config):
    """.md без --ai и без --rag → ошибка (существующее поведение)."""
    md_file = tmp_path / "doc.md"
    md_file.write_text("текст", encoding="utf-8")
    ok = pipeline.process_file(
        str(md_file), use_ai=False, config={},
        api_key="", folder_id="",
        output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
    )
    assert ok is False


def test_process_file_md_ai_rag(tmp_path, rag_config):
    """.md + --ai + --rag: RAG строится из AI-результата."""
    md_file = tmp_path / "СО153-34_21_122-2003 Молниезащита.md"
    md_file.write_text("## 3. ЗАЩИТА\nТекст главы.\n", encoding="utf-8")
    out_base = str(tmp_path / "out")
    tmp_base = str(tmp_path / "tmp")

    with patch("pipeline.ai_postprocess",
               side_effect=lambda md, cfg, label: md + "\n\n## 4. ДОБАВЛЕНО AI\nТекст.\n"), \
         patch("pipeline._init_tokenizer",
               return_value=(lambda t: len(t) // 3, "qwen3")):
        ok = pipeline.process_file(
            str(md_file), use_ai=True, config={"ai_postprocess": {"prompt": "x"}},
            api_key="", folder_id="",
            output_base=out_base, tmp_base=tmp_base,
            use_rag=True, rag_config=rag_config,
        )
    assert ok is True
    rag_path = Path(out_base) / "СО153-34_21_122-2003 Молниезащита" / "СО153-34_21_122-2003 Молниезащита_chunks.jsonl"
    assert rag_path.exists()
    rows = [json.loads(l) for l in rag_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    chapters = [r["chapter"] for r in rows]
    assert "4" in chapters  # AI-добавленная глава попала в JSONL


# ═══════════════════════════════════════════════════════════════════════════
# CLI: parse_args
# ═══════════════════════════════════════════════════════════════════════════


def test_parse_args_rag_flags():
    args = pipeline.parse_args(["-i", "file.pdf", "--rag", "--rag-config", "my_rag.yaml"])
    assert args.rag is True
    assert args.rag_config == "my_rag.yaml"


def test_parse_args_rag_defaults():
    args = pipeline.parse_args(["-i", "file.pdf"])
    assert args.rag is False
    assert args.rag_config == "./rag_config.yaml"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
