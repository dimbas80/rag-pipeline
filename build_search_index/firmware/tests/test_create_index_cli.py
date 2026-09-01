"""
Unit-тесты нового CLI-поведения firmware/src/create_index.py:

- авто-поиск файлов в папке документа (discover_input_files);
- выбор входных путей по аргументам CLI (resolve_input_paths);
- авто-выбор коллекции Qdrant (select_collection).

Сеть/Qdrant не требуются: клиент мокается фейком с get_collections().

Запуск (из корня репозитория):
    python -m pytest firmware/tests/test_create_index_cli.py -v
"""
import importlib.util
import os
import subprocess
import sys
import types
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


# ─── discover_input_files ─────────────────────────────────────────────

def _make_doc_dir(tmp_path, files=(), dirs=()):
    d = tmp_path / "doc"
    d.mkdir()
    for name in files:
        (d / name).write_text("{}", encoding="utf-8")
    for name in dirs:
        (d / name).mkdir()
    return d


def test_discover_ok(tmp_path):
    d = _make_doc_dir(tmp_path, files=["doc_chunks.jsonl", "doc_assets.json"])
    chunks, assets = create_index.discover_input_files(d)
    assert chunks == d / "doc_chunks.jsonl"
    assert assets == d / "doc_assets.json"


def test_discover_two_chunks_raises_with_listing(tmp_path):
    d = _make_doc_dir(tmp_path, files=["a_chunks.jsonl", "b_chunks.jsonl", "doc_assets.json"])
    with pytest.raises(ValueError) as ei:
        create_index.discover_input_files(d)
    msg = str(ei.value)
    assert "a_chunks.jsonl" in msg and "b_chunks.jsonl" in msg
    assert "--chunks" in msg and "--assets" in msg


def test_discover_missing_assets_raises(tmp_path):
    d = _make_doc_dir(tmp_path, files=["doc_chunks.jsonl"])
    with pytest.raises(ValueError, match="assets"):
        create_index.discover_input_files(d)


def test_discover_missing_chunks_raises(tmp_path):
    d = _make_doc_dir(tmp_path, files=["doc_assets.json"])
    with pytest.raises(ValueError, match="chunks"):
        create_index.discover_input_files(d)


def test_discover_ignores_bak_md_and_image(tmp_path):
    d = _make_doc_dir(
        tmp_path,
        files=[
            "doc_chunks.jsonl",
            "doc_assets.json",
            "doc.md",
            "doc_assets.json.bak-2245",
            "doc_chunks.jsonl.bak-2245",
        ],
        dirs=["image", "adir_chunks.jsonl"],  # папка с суффиксом chunks — не файл
    )
    chunks, assets = create_index.discover_input_files(d)
    assert chunks == d / "doc_chunks.jsonl"
    assert assets == d / "doc_assets.json"


def test_discover_bak_not_counted_as_assets(tmp_path):
    # единственный кандидат в assets — .bak: он не подходит под паттерн
    d = _make_doc_dir(tmp_path, files=["doc_chunks.jsonl", "doc_assets.json.bak-2245"])
    with pytest.raises(ValueError, match="assets"):
        create_index.discover_input_files(d)


def test_discover_not_a_dir_raises(tmp_path):
    with pytest.raises(ValueError, match="Папка не найдена"):
        create_index.discover_input_files(tmp_path / "nope")


# ─── select_collection ────────────────────────────────────────────────

class FakeCollectionsResponse:
    def __init__(self, names):
        self.collections = [types.SimpleNamespace(name=n) for n in names]


class FakeClient:
    """get_collections() возвращает заданный список имён (или падает)."""

    def __init__(self, names, fail=False):
        self._names = list(names)
        self._fail = fail
        self.get_collections_calls = 0

    def get_collections(self):
        self.get_collections_calls += 1
        if self._fail:
            raise RuntimeError("storage locked")
        return FakeCollectionsResponse(self._names)


def test_select_collection_requested_used_without_client():
    # клиент при этом вообще не должен трогаться
    client = FakeClient(["a", "b"], fail=True)
    assert create_index.select_collection(client, requested="custom") == "custom"
    assert client.get_collections_calls == 0


def test_select_collection_zero_returns_default(capsys):
    assert create_index.select_collection(FakeClient([]), None) == create_index.DEFAULT_COLLECTION
    out = capsys.readouterr().out
    assert create_index.DEFAULT_COLLECTION in out


def test_select_collection_single_uses_it(capsys):
    assert create_index.select_collection(FakeClient(["alpha"]), None) == "alpha"
    out = capsys.readouterr().out
    assert "единственная" in out
    assert "alpha" in out


def test_select_collection_multiple_interactive_choice(capsys):
    client = FakeClient(["a", "b", "c"])
    chosen = create_index.select_collection(
        client, None,
        input_fn=lambda prompt: "2",
        isatty_fn=lambda: True,
    )
    assert chosen == "b"
    out = capsys.readouterr().out
    assert "3. c" in out          # нумерованный список 1..N
    assert "выбрана пользователем" in out


def test_select_collection_multiple_invalid_3_times_raises():
    replies = iter(["x", "0", "99"])   # все три — невалидные
    client = FakeClient(["a", "b"])
    with pytest.raises(ValueError, match="после 3 попыток"):
        create_index.select_collection(
            client, None,
            input_fn=lambda prompt: next(replies),
            isatty_fn=lambda: True,
        )


def test_select_collection_multiple_valid_after_invalid():
    # невалидный ввод, затем валидный — выбирается вторая из трёх
    replies = iter(["abc", "3"])
    client = FakeClient(["a", "b", "c"])
    chosen = create_index.select_collection(
        client, None,
        input_fn=lambda prompt: next(replies),
        isatty_fn=lambda: True,
    )
    assert chosen == "c"


def test_select_collection_non_tty_pipe_choice():
    # echo "2" | python3 ... : stdin не tty, но строка из пайпа читается
    client = FakeClient(["a", "b", "c"])
    chosen = create_index.select_collection(
        client, None,
        input_fn=lambda prompt: "2",
        isatty_fn=lambda: False,
    )
    assert chosen == "b"


def test_select_collection_non_tty_eof_raises_with_listing():
    def eof(prompt):
        raise EOFError

    client = FakeClient(["a", "b"])
    with pytest.raises(ValueError) as ei:
        create_index.select_collection(
            client, None,
            input_fn=eof,
            isatty_fn=lambda: False,
        )
    msg = str(ei.value)
    assert "Укажите --collection" in msg
    assert "a" in msg and "b" in msg


def test_select_collection_get_collections_error():
    with pytest.raises(RuntimeError, match="список коллекций"):
        create_index.select_collection(FakeClient([], fail=True), None)


# ─── resolve_input_paths / комбинации флагов ─────────────────────────

def test_resolve_old_flags_both(tmp_path):
    c = tmp_path / "c.jsonl"
    a = tmp_path / "a.json"
    assert create_index.resolve_input_paths(None, c, a) == (c, a)


def test_resolve_dir_uses_discovery(tmp_path):
    d = _make_doc_dir(tmp_path, files=["doc_chunks.jsonl", "doc_assets.json"])
    chunks, assets = create_index.resolve_input_paths(d, None, None)
    assert chunks.name == "doc_chunks.jsonl"
    assert assets.name == "doc_assets.json"


def test_resolve_dir_plus_flags_raises(tmp_path):
    d = _make_doc_dir(tmp_path, files=["doc_chunks.jsonl", "doc_assets.json"])
    with pytest.raises(ValueError, match="не вместе"):
        create_index.resolve_input_paths(d, tmp_path / "c.jsonl", None)
    with pytest.raises(ValueError, match="не вместе"):
        create_index.resolve_input_paths(d, None, tmp_path / "a.json")


def test_resolve_only_one_flag_raises():
    with pytest.raises(ValueError, match="оба флага"):
        create_index.resolve_input_paths(None, Path("c.jsonl"), None)
    with pytest.raises(ValueError, match="оба флага"):
        create_index.resolve_input_paths(None, None, Path("a.json"))
    with pytest.raises(ValueError, match="оба флага"):
        create_index.resolve_input_paths(None, None, None)


# ─── CLI: argparse / коды выхода (без сети) ──────────────────────────

def _run(*args):
    return subprocess.run(
        [sys.executable, str(SRC / "create_index.py"), *args],
        capture_output=True, text=True, timeout=60,
    )


def test_cli_help_lists_input_dir_and_legacy_flags():
    r = _run("--help")
    assert r.returncode == 0
    assert "input_dir" in r.stdout
    assert "--chunks" in r.stdout
    assert "--assets" in r.stdout


def test_cli_no_args_exits_with_hint():
    r = _run()
    assert r.returncode == 1
    assert "input_dir" in (r.stderr + r.stdout)


def test_cli_dir_plus_chunks_exits_error(tmp_path):
    d = _make_doc_dir(tmp_path, files=["doc_chunks.jsonl", "doc_assets.json"])
    r = _run(str(d), "--chunks", str(tmp_path / "c.jsonl"))
    assert r.returncode == 1
    assert "[ERROR]" in (r.stderr + r.stdout)


def test_cli_only_chunks_flag_exits_error():
    r = _run("--chunks", "c.jsonl")
    assert r.returncode == 1
    assert "[ERROR]" in (r.stderr + r.stdout)
