"""fs_perms: точечные права базы — каталоги 0777, файлы 0666 (решение №22)."""
import os
import stat

from firmware.src.fs_perms import (
    ensure_dir,
    write_bytes,
    write_text,
    write_text_atomic,
    chmod_copy,
)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_ensure_dir_creates_with_0777(tmp_path):
    target = tmp_path / "a" / "b"
    ensure_dir(target)
    assert target.is_dir()
    assert _mode(target) == 0o777


def test_ensure_dir_chmods_existing_directory(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    os.chmod(target, 0o755)
    ensure_dir(target)
    assert _mode(target) == 0o777


def test_write_bytes_sets_0666(tmp_path):
    path = tmp_path / "doc.bin"
    write_bytes(path, b"data")
    assert path.read_bytes() == b"data"
    assert _mode(path) == 0o666


def test_write_text_sets_0666(tmp_path):
    path = tmp_path / "doc.txt"
    write_text(path, "hello")
    assert path.read_text(encoding="utf-8") == "hello"
    assert _mode(path) == 0o666


def test_write_text_atomic_sets_0666_and_cleans_tmp(tmp_path):
    path = tmp_path / "doc.yaml"
    write_text_atomic(path, "a: 1\n")
    assert _mode(path) == 0o666
    assert list(tmp_path.glob("doc.yaml.*")) == []  # временный файл удалён


def test_chmod_copy_sets_0666(tmp_path):
    src = tmp_path / "doc.yaml"
    src.write_text("x", encoding="utf-8")
    os.chmod(src, 0o644)
    dst = tmp_path / "doc.yaml.bak"
    chmod_copy(src, dst)
    assert dst.read_text(encoding="utf-8") == "x"
    assert _mode(dst) == 0o666
