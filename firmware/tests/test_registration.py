import os
import stat

import yaml

from firmware.src.registration import (
    make_slug,
    write_reg_yaml,
    read_reg_record,
    read_reg_slug,
)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _fields(**overrides):
    base = {
        "document_id": "ГОСТ 18410-73",
        "document_type": "ГОСТ",
        "domain": "Кабели",
        "title": "Кабели силовые",
        "edition": 1973,
        "date_enacted": "1973-01-01",
        "source_file": "ГОСТ 18410-73.pdf",
    }
    base.update(overrides)
    return base


def test_registration_slug_transliterates():
    assert make_slug('ГОСТ 123', 'ГОСТ', 'Электрика').startswith('GOST_123_')


def test_write_reg_yaml_upsert_keeps_slug_and_single_record(tmp_path):
    reg_path = tmp_path / "doc_reg.yaml"
    first = write_reg_yaml(reg_path, _fields())
    second = write_reg_yaml(reg_path, _fields(title="Новый заголовок"))
    assert first == second  # повторная регистрация не плодит новый slug
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert list(data["documents"].keys()) == [first]  # ровно одна запись
    assert data["documents"][first]["title"] == "Новый заголовок"


def test_write_reg_yaml_cleans_historical_duplicates(tmp_path):
    reg_path = tmp_path / "doc_reg.yaml"
    write_reg_yaml(reg_path, _fields())
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    slug = next(iter(data["documents"]))
    data["documents"]["GOST_18410_73_kab_2"] = dict(data["documents"][slug])
    reg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    write_reg_yaml(reg_path, _fields(title="После чистки"))
    result = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert len(result["documents"]) == 1  # исторические дубли схлопнуты


def test_write_reg_yaml_uses_explicit_slug_field(tmp_path):
    reg_path = tmp_path / "doc_reg.yaml"
    slug = write_reg_yaml(reg_path, _fields(slug="GOST_18410_73_kab"))
    assert slug == "GOST_18410_73_kab"
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert list(data["documents"].keys()) == ["GOST_18410_73_kab"]


def test_write_reg_yaml_sets_0666_perms_and_0777_parent(tmp_path):
    reg_path = tmp_path / "deep" / "doc_reg.yaml"
    write_reg_yaml(reg_path, _fields())
    write_reg_yaml(reg_path, _fields(title="v2"))  # вторая запись → .bak
    assert _mode(reg_path) == 0o666
    assert _mode(reg_path.parent) == 0o777
    bak = tmp_path / "deep" / "doc_reg.yaml.bak"
    assert bak.exists()
    assert _mode(bak) == 0o666


def test_read_reg_record_returns_flat_fields(tmp_path):
    reg_path = tmp_path / "doc_reg.yaml"
    write_reg_yaml(reg_path, _fields(title="Читаем"))
    record = read_reg_record(reg_path)
    assert record is not None
    assert record["title"] == "Читаем"
    assert record["document_id"] == "ГОСТ 18410-73"
    assert record["status"] == "active"
    assert record["ignore_sections"] == ["Предисловие", "Содержание"]
    assert "slug" not in record  # плоская запись без ключа-слага
    assert read_reg_slug(reg_path) is not None


def test_read_reg_record_missing_or_empty_returns_none(tmp_path):
    assert read_reg_record(tmp_path / "missing.yaml") is None
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert read_reg_record(empty) is None
    empty.write_text("documents: {}\n", encoding="utf-8")
    assert read_reg_record(empty) is None
    empty.write_text("не: [валидный", encoding="utf-8")
    assert read_reg_record(empty) is None
