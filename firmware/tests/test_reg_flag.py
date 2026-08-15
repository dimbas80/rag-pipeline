#!/usr/bin/env python3
"""Тесты --reg: интерактивная регистрация документа в rag_config.yaml.

Контракт: docs/architecture/rag-register-flag.md (тест-план §11).

Покрывают:
  1. Slug: префикс-карта, первая группа цифр, транслит, коллизия → _2,
     ручной slug с недопустимыми символами / коллизией → переспрос.
  2. edition: 73→1973, 16→2016, 2016→2016, мусор→None.
  3. Извлечение: головы .md разного форматирования; LLM-JSON валидный /
     в ```json fences / битый → regex-fallback; пустой prompt не вызывает API.
  4. Запись: append на копии настоящего rag_config.yaml → safe_load равен
     ожиданию, исходные записи/комментарии не изменены (байтовый префикс);
     гейт отклоняет дубль slug; null-fill меняет только целевые поля.
  5. Идемпотентность: повторный прогон — полный skip без единого input().
  6. Диалог: Enter = принять авто; ввод = замена; Enter на опц. = null;
     invalid status/date → переспрос; финальное n → None, файл не изменён.
  7. Не-TTY: isatty=False → None, error, конфиг не изменён (байтово).
  8. Регресс: --ai без --reg — run_registration не вызывается; main() c
     битым rag_config при --reg → exit 1.
"""
import builtins
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import pipeline

RAG_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "rag_config.yaml")


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures и хелперы
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def tty(monkeypatch):
    """stdin.isatty() → True (интерактивный терминал)."""
    monkeypatch.setattr(pipeline.sys.stdin, "isatty", lambda: True)


@pytest.fixture()
def non_tty(monkeypatch):
    """stdin.isatty() → False (cron/бот/</dev/null)."""
    monkeypatch.setattr(pipeline.sys.stdin, "isatty", lambda: False)


def queue_input(monkeypatch, answers):
    """Подменить builtins.input очередью ответов; лишний вызов — ошибка.

    Returns: список промптов (для проверки, что не спрашивалось).
    """
    it = iter(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(
                f"input() вызван больше раз, чем ответов; промпт: {prompt!r}"
            )

    monkeypatch.setattr(builtins, "input", fake_input)
    return prompts


def real_config_text() -> str:
    return Path(RAG_CONFIG).read_text(encoding="utf-8")


def write_config(tmp_path, text):
    p = tmp_path / "rag_config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def base_fields(**kw):
    """Полный канонический набор полей записи (§3)."""
    f = {
        "document_id": "ГОСТ 839—80",
        "document_id_alt": None,
        "document_type": "ГОСТ",
        "domain": "Кабели",
        "title": "КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ",
        "edition": "1980",
        "date_enacted": "1980-01-01",
        "date_amended": None,
        "amended_by": None,
        "source_file": "ГОСТ 839-80 Кабели.pdf",
        "status": "active",
        "status_reason": None,
        "replaced_by_document_id": None,
        "replaced_by_doc_key": None,
        "ignore_sections": ["Предисловие", "Содержание"],
    }
    f.update(kw)
    return f


# Полный диалог новой записи: 13 промптов (см. §7) — все авто приняты,
# domain и date_enacted заполнены (для комплектности §5.3), запись подтверждена.
NEW_RECORD_QUEUE = [
    "",           # 1.  document_id: принять авто
    "",           # 2.  document_id_alt: пропустить → null
    "",           # 3.  document_type: принять авто
    "Кабели",     # 4.  domain
    "",           # 5.  title: принять авто
    "",           # 6.  edition: принять авто
    "1980-01-01", # 7.  date_enacted
    "",           # 8.  date_amended: пропустить
    "",           # 9.  amended_by: пропустить
    "",           # 10. status: active
    "",           # 11. ignore_sections: принять дефолт
    "",           # 12. slug: принять кандидата
    "y",          # 13. финальный гейт записи
]


# ═══════════════════════════════════════════════════════════════════════════
# 1. Slug (§5.1)
# ═══════════════════════════════════════════════════════════════════════════


class TestSlug:
    def test_prefix_map_uppercase(self):
        """Префикс — ВЕРХНИЙ регистр (правка Orchestrator: §5.1 п.4)."""
        assert pipeline._reg_make_slug("ГОСТ 18410—73", "ГОСТ", "Кабели", set()) == "GOST_18410_kabel"
        assert pipeline._reg_make_slug("СП 89.13330.2016", "СП", "Котельные", set()) == "SP_89_kotelnye"
        assert pipeline._reg_make_slug("СО 153-34.21.122-2003", "СО", "Молниезащита", set()) == "SO_153_molniezashita"
        assert pipeline._reg_make_slug("СНиП 2.04.05-86", "СНиП", "Отопление", set()) == "SNIP_2_otoplenie"
        assert pipeline._reg_make_slug("ПУЭ 7", "ПУЭ", None, set()) == "PUE_7"

    def test_first_digit_group(self):
        """Номер — первая группа цифр document_id (§5.1 п.2)."""
        assert pipeline._reg_make_slug("ГОСТ 31996—2012", "ГОСТ", "Кабели", set()) == "GOST_31996_kabel"
        assert pipeline._reg_make_slug("ГОСТ 18410—73", "ГОСТ", None, set()) == "GOST_18410"

    def test_no_number_omits_part(self):
        """«ПУЭ» без номера → PUE (§5.1 п.2)."""
        assert pipeline._reg_make_slug("ПУЭ", "ПУЭ", None, set()) == "PUE"

    def test_translit_contract_examples(self):
        """§5.1 п.3: Кабели→kabel, Молниезащита→molniezashita."""
        assert pipeline._reg_translit("Кабели") == "kabel"
        assert pipeline._reg_translit("Молниезащита") == "molniezashita"
        assert pipeline._reg_translit("Котельные") == "kotelnye"
        assert pipeline._reg_translit("Отопление") == "otoplenie"

    def test_unknown_type_defaults_to_doc(self):
        """Тип не распознан → префикс doc (§5.1 п.1)."""
        assert pipeline._reg_make_slug("ФО 12-345", None, "Кабели", set()) == "doc_12_kabel"

    def test_collision_suffix(self):
        """Коллизия с существующим ключом → _2, _3 (§5.1 п.5)."""
        existing = {"GOST_18410_kabel"}
        assert pipeline._reg_make_slug("ГОСТ 18410—73", "ГОСТ", "Кабели", existing) == "GOST_18410_kabel_2"
        existing2 = {"GOST_18410_kabel", "GOST_18410_kabel_2"}
        assert pipeline._reg_make_slug("ГОСТ 18410—73", "ГОСТ", "Кабели", existing2) == "GOST_18410_kabel_3"

    def test_manual_slug_invalid_chars_reprompt(self, monkeypatch):
        """Ручной slug с недопустимыми символами → переспрос (§5.1)."""
        extracted = {"document_id": "ГОСТ 839—80", "document_type": "ГОСТ",
                     "title": "X", "edition": "1980", "domain_hint": None}
        queue_input(monkeypatch, [
            "", "", "", "Кабели", "", "", "1980-01-01", "", "", "", "",
            "bad-slug!",  # не [A-Za-z0-9_]+ → переспрос
            "good_slug",
        ])
        result = pipeline._reg_interactive_fill(extracted, "x.pdf", set(), None, None)
        assert result is not None
        assert result[0] == "good_slug"

    def test_manual_slug_collision_reprompt(self, monkeypatch):
        """Ручной slug с коллизией (регистронезависимо) → переспрос (§5.1)."""
        extracted = {"document_id": "ГОСТ 839—80", "document_type": "ГОСТ",
                     "title": "X", "edition": "1980", "domain_hint": None}
        queue_input(monkeypatch, [
            "", "", "", "Кабели", "", "", "1980-01-01", "", "", "", "",
            "gost_18410_kabel",  # коллизия (регистр не важен)
            "my_slug",
        ])
        result = pipeline._reg_interactive_fill(
            extracted, "x.pdf", {"GOST_18410_kabel"}, None, None,
        )
        assert result is not None
        assert result[0] == "my_slug"


# ═══════════════════════════════════════════════════════════════════════════
# 2. edition (§5.2)
# ═══════════════════════════════════════════════════════════════════════════


class TestEdition:
    def test_two_digit_19xx(self):
        assert pipeline._reg_edition_from_id("ГОСТ 18410—73") == "1973"
        assert pipeline._reg_edition_from_id("СНиП 2.04.05-86") == "1986"

    def test_two_digit_20xx(self):
        assert pipeline._reg_edition_from_id("ГОСТ 123—16") == "2016"

    def test_four_digit(self):
        assert pipeline._reg_edition_from_id("СП 89.13330.2016") == "2016"
        assert pipeline._reg_edition_from_id("СО 153-34.21.122-2003") == "2003"

    def test_garbage(self):
        assert pipeline._reg_edition_from_id("ПУЭ 7") is None
        assert pipeline._reg_edition_from_id("мусор") is None
        assert pipeline._reg_edition_from_id("") is None
        assert pipeline._reg_edition_from_id(None) is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Извлечение (§4)
# ═══════════════════════════════════════════════════════════════════════════


class TestExtraction:
    def test_head_with_em_dash_and_caps_title(self):
        """Обозначение с —, КАПС-заголовок."""
        head = "КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ\nГОСТ 18410—73\n"
        r = pipeline._reg_extract_fields(head, None)
        assert r["document_id"] == "ГОСТ 18410—73"
        assert r["document_type"] == "ГОСТ"
        assert r["edition"] == "1973"
        assert r["title"] == "КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ"

    def test_head_with_hash_title(self):
        """#-заголовок берётся как title."""
        head = "# Котельные установки\n\nСП 89.13330.2016\n"
        r = pipeline._reg_extract_fields(head, None)
        assert r["document_id"] == "СП 89.13330.2016"
        assert r["document_type"] == "СП"
        assert r["title"] == "Котельные установки"

    def test_head_with_en_dash_snip(self):
        """Обозначение с – (en dash)."""
        head = "СНиП 2.04.05–86\nОтопление, вентиляция и кондиционирование\n"
        r = pipeline._reg_extract_fields(head, None)
        assert r["document_id"] == "СНиП 2.04.05–86"
        assert r["document_type"] == "СНиП"
        assert r["edition"] == "1986"

    def test_head_with_minus_dash(self):
        """Обозначение с обычным дефисом."""
        head = "СО 153-34.21.122-2003\nИнструкция по молниезащите\n"
        r = pipeline._reg_extract_fields(head, None)
        assert r["document_id"] == "СО 153-34.21.122-2003"
        assert r["document_type"] == "СО"
        assert r["edition"] == "2003"

    def test_no_designation(self):
        """Обозначение не извлеклось → document_id None, без падения."""
        head = "# Просто документ\nТекст без обозначений\n"
        r = pipeline._reg_extract_fields(head, None)
        assert r["document_id"] is None
        assert r["title"] == "Просто документ"

    def test_llm_valid_json(self, monkeypatch):
        head = "КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        with patch(
            "pipeline._call_ai_api",
            return_value=(
                '{"document_id": "ГОСТ 18410—73", '
                '"title": "КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ", '
                '"domain_hint": "Кабели"}'
            ),
        ):
            r = pipeline._reg_extract_fields(head, {"reg_extract": {"prompt": "p"}})
        assert r["document_id"] == "ГОСТ 18410—73"
        assert r["title"] == "КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ"
        assert r["domain_hint"] == "Кабели"

    def test_llm_json_in_fences(self, monkeypatch):
        """JSON в ```json fences — парсится (re.search {..})."""
        head = "КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        with patch(
            "pipeline._call_ai_api",
            return_value='```json\n{"document_id": "ГОСТ 18410—73"}\n```',
        ):
            r = pipeline._reg_extract_fields(head, {"reg_extract": {"prompt": "p"}})
        assert r["document_id"] == "ГОСТ 18410—73"

    def test_llm_broken_falls_back_to_regex(self, monkeypatch):
        """LLM вернул не-JSON → regex-слой."""
        head = "КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        with patch("pipeline._call_ai_api", return_value="не понял"):
            r = pipeline._reg_extract_fields(head, {"reg_extract": {"prompt": "p"}})
        assert r["document_id"] == "ГОСТ 18410—73"

    def test_llm_override_recomputes_edition(self, monkeypatch):
        """LLM заменил document_id → edition пересчитан по новому id."""
        head = "КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        with patch("pipeline._call_ai_api", return_value='{"document_id": "ГОСТ 839—80"}'):
            r = pipeline._reg_extract_fields(head, {"reg_extract": {"prompt": "p"}})
        assert r["document_id"] == "ГОСТ 839—80"
        assert r["edition"] == "1980"

    def test_empty_prompt_guard(self, monkeypatch):
        """Пустой prompt в reg_extract НЕ вызывает _call_ai_api (иначе sys.exit)."""
        called = []

        def boom(text, cfg, ctx=""):
            called.append(1)
            raise SystemExit(1)

        head = "КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        with patch("pipeline._call_ai_api", side_effect=boom):
            r = pipeline._reg_extract_fields(head, {"reg_extract": {"prompt": ""}})
        assert called == []
        assert r["document_id"] == "ГОСТ 18410—73"

    def test_no_reg_extract_section(self, monkeypatch):
        """Секции reg_extract нет → regex-слой, без вызова API."""
        head = "КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        with patch(
            "pipeline._call_ai_api",
            side_effect=AssertionError("не должен вызываться"),
        ):
            r = pipeline._reg_extract_fields(head, {})
        assert r["document_id"] == "ГОСТ 18410—73"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Безопасная запись YAML (§6)
# ═══════════════════════════════════════════════════════════════════════════


class TestWrite:
    def test_append_on_real_config_copy(self):
        """Append на копии настоящего rag_config.yaml: safe_load равен ожиданию,
        исходные записи/комментарии не изменены (байтовый префикс)."""
        text = real_config_text()
        new_text = pipeline._reg_append_record(text, "GOST_839_kabel", base_fields())
        assert new_text is not None
        parsed = yaml.safe_load(new_text)
        doc = parsed["documents"]["GOST_839_kabel"]
        assert doc["document_id"] == "ГОСТ 839—80"
        assert doc["document_type"] == "ГОСТ"
        assert doc["domain"] == "Кабели"
        assert doc["edition"] == "1980"
        assert doc["status"] == "active"
        assert doc["ignore_sections"] == ["Предисловие", "Содержание"]
        # Исходные байты не изменены (префикс до точки врезки — как был)
        assert new_text[: len(text.rstrip("\n"))] == text.rstrip("\n")
        # Комментарии каталога сохранены
        assert "# ═══" in new_text
        assert "so153_molniezashita" in new_text

    def test_append_block_format(self):
        """§6.3: 2 пробела slug, 4 — поля, двойные кавычки, status без кавычек."""
        block = pipeline._reg_render_record_block("GOST_839_kabel", base_fields())
        assert "  # ── ГОСТ 839—80 — добавлено --reg" in block
        assert "  GOST_839_kabel:" in block
        assert '    document_id: "ГОСТ 839—80"' in block
        assert '    domain: "Кабели"' in block
        assert "    status: active" in block
        assert "    status_reason: null" in block
        assert '      - "Предисловие"' in block
        assert '      - "Содержание"' in block

    def test_gate_rejects_duplicate_slug(self, tmp_path):
        """Гейт: дубль slug → None, файл не изменён (§6.1)."""
        text = real_config_text()
        cfg = write_config(tmp_path, text)
        new_text = pipeline._reg_append_record(text, "GOST_18410_kabel", base_fields())
        assert new_text is None
        assert cfg.read_text(encoding="utf-8") == text

    def test_null_fill_changes_only_target_fields(self):
        """null-fill меняет только строки целевой записи (§6.1)."""
        text = real_config_text()
        new_text = pipeline._reg_fill_nulls(
            text, "so153_molniezashita",
            {"document_type": "СО", "domain": "Молниезащита"},
        )
        assert new_text is not None
        parsed = yaml.safe_load(new_text)
        doc = parsed["documents"]["so153_molniezashita"]
        assert doc["document_type"] == "СО"
        assert doc["domain"] == "Молниезащита"
        # не-заполненные поля не тронуты
        assert doc["title"] == "Инструкция по устройству молниезащиты зданий, сооружений и промышленных коммуникаций"
        assert doc["date_amended"] is None
        # другие записи не изменились
        assert parsed["documents"]["sp89_kotelnye"]["document_type"] is None
        # байтово изменились ровно две строки
        old_lines = text.splitlines()
        new_lines = new_text.splitlines()
        diffs = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        assert len(diffs) == 2, diffs

    def test_yaml_escaping(self):
        """Кавычки и бэкслеш в значениях экранируются (§6.3)."""
        f = base_fields(title='Скажи "привет" \\ ок')
        new_text = pipeline._reg_append_record(real_config_text(), "GOST_esc_test", f)
        assert new_text is not None
        parsed = yaml.safe_load(new_text)
        assert parsed["documents"]["GOST_esc_test"]["title"] == 'Скажи "привет" \\ ок'


# ═══════════════════════════════════════════════════════════════════════════
# 5. Идемпотентность (§5.3)
# ═══════════════════════════════════════════════════════════════════════════


class TestIdempotence:
    def test_second_run_skips_without_prompts(self, tmp_path, monkeypatch, tty):
        """После записи повторный прогон — полный skip, ни одного input()."""
        cfg = write_config(tmp_path, real_config_text())
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ\nГОСТ 839—80\n"

        queue_input(monkeypatch, list(NEW_RECORD_QUEUE))
        res = pipeline.run_registration(
            "ГОСТ 839-80 Кабели.pdf", md, cfg, rag, None,
        )
        assert res is not None
        assert "GOST_839_kabel" in res["documents"]

        # Повторный прогон: input не должен вызываться вовсе
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt="": (_ for _ in ()).throw(
                AssertionError("не должно быть промптов")
            ),
        )
        res2 = pipeline.run_registration(
            "ГОСТ 839-80 Кабели.pdf", md, cfg, pipeline.load_rag_config(cfg), None,
        )
        assert res2 is not None
        # Дубликата нет
        docs = yaml.safe_load(cfg.read_text(encoding="utf-8"))["documents"]
        assert list(docs).count("GOST_839_kabel") == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. Диалог (§7)
# ═══════════════════════════════════════════════════════════════════════════


class TestDialog:
    def test_enters_accept_auto_values(self, tmp_path, monkeypatch, tty):
        """Последовательность Enter'ов = принятые авто-значения; опц. = null."""
        cfg = write_config(tmp_path, real_config_text())
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ\nГОСТ 839—80\n"
        queue_input(monkeypatch, list(NEW_RECORD_QUEUE))
        res = pipeline.run_registration("ГОСТ 839-80 Кабели.pdf", md, cfg, rag, None)
        assert res is not None
        doc = res["documents"]["GOST_839_kabel"]
        assert doc["document_id"] == "ГОСТ 839—80"   # авто принято
        assert doc["document_type"] == "ГОСТ"
        assert doc["title"] == "КАБЕЛИ СИЛОВЫЕ С ПРОПИТАННОЙ БУМАЖНОЙ ИЗОЛЯЦИЕЙ"
        assert doc["edition"] == "1980"
        assert doc["ignore_sections"] == ["Предисловие", "Содержание"]
        assert doc["document_id_alt"] is None        # Enter на опц. = null
        assert doc["status"] == "active"
        assert doc["source_file"] == "ГОСТ 839-80 Кабели.pdf"

    def test_typed_value_replaces_auto(self, tmp_path, monkeypatch, tty):
        """Ввод значения заменяет авто; опциональные заполняются."""
        cfg = write_config(tmp_path, real_config_text())
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ\nГОСТ 18410—73\n"
        queue_input(monkeypatch, [
            "ГОСТ 99999—00",   # document_id: заменить авто
            "СНиП II-35-76",   # document_id_alt
            "",                # document_type: авто ГОСТ
            "Кабели",          # domain
            "",                # title: авто
            "1980",            # edition: заменить (авто было 1973)
            "1980-01-01",      # date_enacted
            "",                # date_amended
            "",                # amended_by
            "",                # status
            "",                # ignore_sections
            "",                # slug
            "y",
        ])
        res = pipeline.run_registration("ГОСТ 18410-73 Кабели.pdf", md, cfg, rag, None)
        assert res is not None
        doc = res["documents"]["GOST_99999_kabel"]
        assert doc["document_id"] == "ГОСТ 99999—00"
        assert doc["document_id_alt"] == "СНиП II-35-76"
        assert doc["edition"] == "1980"

    def test_invalid_status_reprompt(self, tmp_path, monkeypatch, tty):
        """Недопустимый status → переспрос; inactive → обязательный reason."""
        cfg = write_config(tmp_path, real_config_text())
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ\nГОСТ 839—80\n"
        queue_input(monkeypatch, [
            "", "", "", "Кабели", "", "", "1980-01-01", "", "",
            "bogus",           # status: недопустимо → переспрос
            "inactive",        # status
            "Заменён на СП 60.13330.2012",  # status_reason (обязательное)
            "СП 60.13330.2012",             # replaced_by_document_id
            "",                # replaced_by_doc_key: пропустить
            "",                # ignore_sections
            "",                # slug
            "y",
        ])
        res = pipeline.run_registration("ГОСТ 839-80 Кабели.pdf", md, cfg, rag, None)
        assert res is not None
        doc = res["documents"]["GOST_839_kabel"]
        assert doc["status"] == "inactive"
        assert doc["status_reason"] == "Заменён на СП 60.13330.2012"
        assert doc["replaced_by_document_id"] == "СП 60.13330.2012"
        assert doc["replaced_by_doc_key"] is None

    def test_invalid_date_reprompt(self, tmp_path, monkeypatch, tty):
        """Некорректная дата → переспрос до ГГГГ-ММ-ДД."""
        cfg = write_config(tmp_path, real_config_text())
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ\nГОСТ 839—80\n"
        queue_input(monkeypatch, [
            "", "", "", "Кабели", "", "",
            "not-a-date",      # date_enacted: неверный формат → переспрос
            "2020-01-01",      # date_enacted
            "", "", "", "", "", "y",
        ])
        res = pipeline.run_registration("ГОСТ 839-80 Кабели.pdf", md, cfg, rag, None)
        assert res is not None
        doc = res["documents"]["GOST_839_kabel"]
        assert doc["date_enacted"] == "2020-01-01"

    def test_final_no_aborts_without_write(self, tmp_path, monkeypatch, tty):
        """Финальное n → None, конфиг не изменён (§7)."""
        cfg = write_config(tmp_path, real_config_text())
        text_before = cfg.read_text(encoding="utf-8")
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ\nГОСТ 839—80\n"
        q = list(NEW_RECORD_QUEUE)
        q[-1] = "n"
        queue_input(monkeypatch, q)
        res = pipeline.run_registration("ГОСТ 839-80 Кабели.pdf", md, cfg, rag, None)
        assert res is None
        assert cfg.read_text(encoding="utf-8") == text_before

    def test_partial_fill_asks_only_missing(self, tmp_path, monkeypatch, tty):
        """Режим дополнения: существующие значения не переспрашиваются."""
        partial = """documents:
  test_doc:
    document_id: "ГОСТ 123—45"
    document_id_alt: null
    document_type: null
    domain: null
    title: "TEST TITLE"
    edition: "1945"
    date_enacted: null
    date_amended: null
    amended_by: null
    source_file: "ГОСТ 123-45.pdf"
    status: active
    status_reason: null
    replaced_by_document_id: null
    replaced_by_doc_key: null
    ignore_sections:
      - "Предисловие"
      - "Содержание"
"""
        cfg = write_config(tmp_path, partial)
        rag = pipeline.load_rag_config(cfg)
        md = "# ТЕСТ ТАЙТЛ ДОКУМЕНТА НОРМАТИВ\nГОСТ 123—45\n"
        prompts = queue_input(monkeypatch, [
            "",                # document_id_alt: пропустить
            "",                # document_type: принять авто ГОСТ
            "Домены",          # domain
            "",                # date_enacted: пропустить
            "2020-05-05",      # date_amended
            "",                # amended_by: пропустить
            "y",
        ])
        res = pipeline.run_registration("ГОСТ 123-45.pdf", md, cfg, rag, None)
        assert res is not None
        doc = res["documents"]["test_doc"]
        # существующие значения НЕ перезаписаны и НЕ спрашивались
        assert doc["document_id"] == "ГОСТ 123—45"
        assert doc["title"] == "TEST TITLE"
        assert doc["edition"] == "1945"
        # недостающие заполнены
        assert doc["document_type"] == "ГОСТ"
        assert doc["domain"] == "Домены"
        assert doc["date_amended"] == "2020-05-05"
        joined = "\n".join(prompts)
        assert "document_id [" not in joined
        assert "title [" not in joined
        assert "edition [" not in joined
        assert "Статус документа" not in joined
        assert "ignore_sections" not in joined


# ═══════════════════════════════════════════════════════════════════════════
# 7. Не-TTY (§8)
# ═══════════════════════════════════════════════════════════════════════════


class TestNonTty:
    def test_no_tty_returns_none_and_does_not_touch_config(self, tmp_path, monkeypatch, non_tty):
        """isatty=False → None, input не вызывается, конфиг не изменён."""
        cfg = write_config(tmp_path, real_config_text())
        text_before = cfg.read_bytes()
        rag = pipeline.load_rag_config(cfg)
        md = "# КАБЕЛИ СИЛОВЫЕ\nГОСТ 839—80\n"
        monkeypatch.setattr(
            builtins, "input",
            lambda prompt="": (_ for _ in ()).throw(
                AssertionError("input не должен вызываться без TTY")
            ),
        )
        res = pipeline.run_registration("ГОСТ 839-80 Кабели.pdf", md, cfg, rag, None)
        assert res is None
        assert cfg.read_bytes() == text_before


# ═══════════════════════════════════════════════════════════════════════════
# 8. Регресс: --ai без --reg, CLI, main()
# ═══════════════════════════════════════════════════════════════════════════


class TestRegression:
    def test_parse_args_reg_flag(self):
        """--reg добавлен без коллизий; --ai без --reg — как раньше."""
        ns = pipeline.parse_args(["-i", "x.pdf", "--ai", "--reg", "--rag"])
        assert ns.reg is True
        assert ns.ai is True
        assert ns.rag is True
        ns2 = pipeline.parse_args(["-i", "x.pdf", "--ai"])
        assert ns2.reg is False
        assert ns2.ai is True

    def test_ai_without_reg_does_not_call_registration(self, tmp_path, monkeypatch):
        """--ai без --reg: run_registration не вызывается (инвариант)."""
        md = tmp_path / "x.md"
        md.write_text("# ТЕКСТ ДОКУМЕНТА\n", encoding="utf-8")
        with patch("pipeline.ai_postprocess", return_value="# ТЕКСТ ДОКУМЕНТА\nОбработано\n"), \
             patch(
                 "pipeline.run_registration",
                 side_effect=AssertionError("--reg не должен вызываться"),
             ):
            ok = pipeline.process_file(
                str(md), use_ai=True,
                config={"ai_postprocess": {"prompt": "x"}},
                api_key="", folder_id="",
                output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
            )
        assert ok is True
        assert (md.parent / "x_ai.md").exists()

    def test_md_standalone_reg_without_ai_or_rag(self, tmp_path, monkeypatch, tty):
        """.md вне Markdown/ + --reg (без --ai/--rag) — валидная регистрация."""
        cfg = write_config(tmp_path, real_config_text())
        md = tmp_path / "doc.md"
        md.write_text("# КАБЕЛИ СИЛОВЫЕ\nГОСТ 839—80\n", encoding="utf-8")
        queue_input(monkeypatch, list(NEW_RECORD_QUEUE))
        ok = pipeline.process_file(
            str(md), use_ai=False, config={}, api_key="", folder_id="",
            output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
            use_reg=True, rag_config=pipeline.load_rag_config(cfg), rag_config_path=str(cfg),
        )
        assert ok is True
        assert "GOST_839_kabel" in yaml.safe_load(cfg.read_text(encoding="utf-8"))["documents"]

    def test_md_rag_requires_flag(self, tmp_path):
        """.md внутри Markdown/ без --rag/--reg → False (гейт расширен §2)."""
        md = tmp_path / "Markdown" / "x" / "x.md"
        md.parent.mkdir(parents=True)
        md.write_text("# ТЕКСТ\n", encoding="utf-8")
        ok = pipeline.process_file(
            str(md), use_ai=False, config={}, api_key="", folder_id="",
            output_base=str(tmp_path / "out"), tmp_base=str(tmp_path / "tmp"),
        )
        assert ok is False

    def test_main_reg_with_bad_config_exits_1(self, monkeypatch, tmp_path):
        """main(): --reg с битым/отсутствующим rag_config → exit 1 (§2)."""
        md = tmp_path / "x.md"
        md.write_text("# ТЕКСТ\n", encoding="utf-8")
        monkeypatch.setattr(
            pipeline.sys, "argv",
            ["pipeline.py", "-i", str(md), "--reg",
             "--rag-config", str(tmp_path / "nope.yaml")],
        )
        with pytest.raises(SystemExit) as e:
            pipeline.main()
        assert e.value.code == 1

    def test_reg_extract_section_in_config_ai(self):
        """config_ai.yaml содержит секцию reg_extract с промптом (§4.2)."""
        cfg_path = Path(pipeline.__file__).parent / "config_ai.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        reg = cfg.get("reg_extract")
        assert isinstance(reg, dict)
        assert bool(reg.get("prompt"))
        assert bool(reg.get("provider"))
        assert bool(reg.get("model"))
        assert bool(reg.get("api_key_env"))
        assert isinstance(reg.get("fallback"), dict)
        # существующие секции не тронуты
        assert isinstance(cfg.get("table_vision"), dict)
        assert isinstance(cfg.get("ai_postprocess"), dict)
