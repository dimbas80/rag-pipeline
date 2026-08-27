import os
from pathlib import Path
import pytest
from firmware.src.deploy_config import load

def test_load_dev_and_env_override(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("active: dev\ndev: {host: localhost, port: 8081, upload: {base_dir: /u, max_mb: 1}, pipelines: {create_markdown_dir: /c, build_search_index_dir: /b}, qdrant: {path: /q, collection: c, write_enabled: false}, env_file: /e, base_markdown: /m}\nprod: {host: p, port: 80, upload: {base_dir: /pu, max_mb: 2}, pipelines: {create_markdown_dir: /pc, build_search_index_dir: /pb}, qdrant: {path: /pq, collection: pc, write_enabled: true}, env_file: /pe, base_markdown: /pm}\n", encoding="utf-8")
    assert load(cfg).port == 8081
    os.environ["INTERFACE_RAG_ENV"] = "prod"
    try:
        assert load(cfg).write_enabled is True
    finally:
        os.environ.pop("INTERFACE_RAG_ENV", None)

def test_missing_group_fails(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("active: dev\ndev: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="отсутствуют"):
        load(cfg)
