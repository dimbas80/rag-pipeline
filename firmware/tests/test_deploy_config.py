import os
from pathlib import Path
import pytest
from firmware.src.deploy_config import load


def _config(dev_env_file=None, prod_env_file=None):
    dev = f"  env_file: {dev_env_file}\n" if dev_env_file else ""
    prod = f"  env_file: {prod_env_file}\n" if prod_env_file else ""
    return (
        "active: dev\n"
        "dev:\n"
        "  host: localhost\n"
        "  port: 8081\n"
        "  config_dir: /d\n"
        "  upload: {base_dir: /u, max_mb: 1}\n"
        "  pipelines: {create_markdown_dir: /c, build_search_index_dir: /b}\n"
        "  qdrant: {path: /q, collection: c, write_enabled: false}\n"
        + dev +
        "  base_markdown: /m\n"
        "prod:\n"
        "  host: p\n"
        "  port: 80\n"
        "  config_dir: /pd\n"
        "  upload: {base_dir: /pu, max_mb: 2}\n"
        "  pipelines: {create_markdown_dir: /pc, build_search_index_dir: /pb}\n"
        "  qdrant: {path: /pq, collection: pc, write_enabled: true}\n"
        + prod +
        "  base_markdown: /pm\n"
    )


def test_load_dev_and_env_override(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config(), encoding="utf-8")
    assert load(cfg).port == 8081
    assert load(cfg).config_dir == Path("/d")
    os.environ["INTERFACE_RAG_ENV"] = "prod"
    try:
        loaded = load(cfg)
        assert loaded.write_enabled is True
        assert loaded.config_dir == Path("/pd")
    finally:
        os.environ.pop("INTERFACE_RAG_ENV", None)


def test_env_file_defaults_to_config_dir(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config(), encoding="utf-8")
    assert load(cfg).env_file == Path("/d/.env")
    cfg.write_text(_config(dev_env_file="/custom/.env"), encoding="utf-8")
    assert load(cfg).env_file == Path("/custom/.env")


def test_derived_config_paths_point_to_config_dir(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config(), encoding="utf-8")
    loaded = load(cfg)
    assert loaded.providers_path == Path("/d/providers.yaml")
    assert loaded.create_markdown_config_path == Path("/d/create_markdown_config.yaml")
    assert loaded.search_config_path == Path("/d/search_config.yaml")


def test_missing_config_dir_fails(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "active: dev\n"
        "dev: {upload: {base_dir: /u, max_mb: 1}, pipelines: {create_markdown_dir: /c, build_search_index_dir: /b}, qdrant: {path: /q, collection: c, write_enabled: false}, base_markdown: /m}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="config_dir"):
        load(cfg)


def test_missing_group_fails(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("active: dev\ndev: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="отсутствуют"):
        load(cfg)


# --- решение №29: qdrant_path как свойство (env > .env > config.yaml) ---

def test_qdrant_path_default_when_no_override(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config(), encoding="utf-8")
    loaded = load(cfg)
    assert loaded.qdrant_path == Path("/q")
    assert loaded.qdrant_path_default == Path("/q")
    assert loaded.qdrant_path_override is None


def test_qdrant_path_override_from_process_env(tmp_path, monkeypatch):
    # process-env QDRANT_PATH БЕЗ записи в .env интерфейса → override отсутствует,
    # путь = дефолт. process-env больше НЕ создаёт override (фикс решения №29).
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config(), encoding="utf-8")
    monkeypatch.setenv("QDRANT_PATH", "/env/qdrant")
    loaded = load(cfg)
    assert loaded.qdrant_path_override is None
    assert loaded.qdrant_path == Path("/q")
    assert loaded.qdrant_path == loaded.qdrant_path_default


def test_qdrant_path_override_from_env_file(tmp_path):
    cfg = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text("QDRANT_PATH=/dotenv/qdrant\n", encoding="utf-8")
    cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    assert loaded.qdrant_path == Path("/dotenv/qdrant")
    assert loaded.qdrant_path_override == "/dotenv/qdrant"


def test_qdrant_path_env_file_beats_process_env(tmp_path, monkeypatch):
    # Инвертированный приоритет (фикс решения №29): и .env интерфейса с QDRANT_PATH,
    # и QDRANT_PATH в process-env (заражение пайплайна) → побеждает .env интерфейса.
    cfg = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text("QDRANT_PATH=/dotenv/qdrant\n", encoding="utf-8")
    cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    monkeypatch.setenv("QDRANT_PATH", "/proc/qdrant")
    loaded = load(cfg)
    assert loaded.qdrant_path_override == "/dotenv/qdrant"
    assert loaded.qdrant_path == Path("/dotenv/qdrant")


def test_qdrant_path_ignores_process_env_pollution(tmp_path, monkeypatch):
    # Ровно сценарий бага: пайплайн голым load_dotenv() положил QDRANT_PATH из СВОЕГО
    # .env в os.environ процесса → интерфейс обязан продолжать использовать .env
    # интерфейса (не прод-путь пайплайна).
    cfg = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text("QDRANT_PATH=/interface/qdrant\n", encoding="utf-8")
    cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    # «Пайплайн загрязнил» process-env: load_dotenv положил QDRANT_PATH из СВОЕГО .env
    monkeypatch.setenv("QDRANT_PATH", "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data")
    assert loaded.qdrant_path_override == "/interface/qdrant"   # .env интерфейса победил
    assert loaded.qdrant_path == Path("/interface/qdrant")      # НЕ прод-путь


def test_qdrant_path_survives_load_dotenv(tmp_path, monkeypatch):
    # Интеграция: НАСТОЯЩИЙ load_dotenv (то, что реально мутирует os.environ в
    # get_api_key() пайплайна) против фикстуры .env пайплайна с QDRANT_PATH=/mnt/sdb/…
    # → cfg.qdrant_path не сменился на прод-путь (см. §5.3(а) архитектуры).
    from dotenv import load_dotenv
    env_file = tmp_path / ".env"
    env_file.write_text("QDRANT_PATH=/interface/qdrant\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    pipeline_env = tmp_path / "pipeline" / ".env"
    pipeline_env.parent.mkdir(parents=True)
    pipeline_env.write_text(
        "QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data\nDEEPSEEK_API_KEY=sk-x\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("QDRANT_PATH", raising=False)  # чистый старт, как на dev-сервере
    load_dotenv(dotenv_path=pipeline_env)             # эмулирует get_api_key() пайплайна
    assert loaded.qdrant_path_override == "/interface/qdrant"
    assert loaded.qdrant_path == Path("/interface/qdrant")


def test_qdrant_path_empty_env_file_value_falls_back(tmp_path):
    cfg = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text("QDRANT_PATH=\n", encoding="utf-8")
    cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    assert loaded.qdrant_path_override is None
    assert loaded.qdrant_path == Path("/q")


def test_env_injection_does_not_change_qdrant_path(tmp_path, monkeypatch):
    # Инъекция ключей config/.env (t_a7d01588) ортогональна QDRANT_PATH-фиксу:
    # даже если config/.env содержит QDRANT_PATH и инъекция кладёт его в os.environ,
    # резолвер обязан читать .env (read_env_raw), а не process-env; результат тот же.
    from firmware.src.chat_api import _inject_env_file
    env_file = tmp_path / ".env"; env_file.write_text("QDRANT_PATH=/interface/qdrant\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"; cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    _inject_env_file(env_file)   # кладёт QDRANT_PATH=/interface/qdrant в os.environ
    assert loaded.qdrant_path_override == "/interface/qdrant"
    assert loaded.qdrant_path == Path("/interface/qdrant")
    monkeypatch.delenv("QDRANT_PATH", raising=False)   # не оставлять загрязнение
