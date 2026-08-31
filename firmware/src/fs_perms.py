"""Точечные хелперы прав для файлов/каталогов «общей» базы (!База_ГОСТ).

Контракт (решение №22): каталоги под базой получают `0777`, файлы — `0666`.
Механизм — ЯВНЫЙ chmod/mkdir-mode в точках записи, БЕЗ глобального `umask 000`
(иначе утекут права `.env`/конфигов). Хелперы не содержат бизнес-логики и не
используются для записи конфигов интерфейса (`.env`, `*.yaml` в `config_dir`)
— те остаются на ограничительных правах (`0600`) через `config_ui`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def ensure_dir(path, mode: int = 0o777) -> Path:
    """mkdir(parents=True, exist_ok=True) + chmod(mode)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path


def write_bytes(path, data, mode: int = 0o666) -> Path:
    """Записать байты и явно выставить права mode."""
    path = Path(path)
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def write_text(path, text, mode: int = 0o666) -> Path:
    """Записать текст (utf-8) и явно выставить права mode."""
    path = Path(path)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def write_text_atomic(path, text, mode: int = 0o666) -> Path:
    """Атомарная запись текста: temp-файл + os.replace + chmod(mode).

    Права выставляются на temp-файл ДО os.replace, чтобы итоговый файл
    гарантированно получил mode (os.replace переносит inode вместе с правами).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def chmod_copy(src, dst, mode: int = 0o666) -> Path:
    """copy2 + chmod(dst, mode) — для .bak под базой (например `_reg.yaml.bak`)."""
    src = Path(src)
    dst = Path(dst)
    shutil.copy2(src, dst)
    os.chmod(dst, mode)
    return dst
