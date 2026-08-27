from pathlib import Path
import yaml
import pytest
from firmware.src.config_ui import (
    read_yaml, read_env,
    validate_providers, validate_search_config, validate_reg_record,
    write_env, read_env_raw, sync_role_models,
)


def test_read_yaml_missing_returns_empty(tmp_path):
    assert read_yaml(tmp_path / "missing.yaml") == {}


def test_write_env_creates_parent_dir(tmp_path):
    path = tmp_path / "deep" / "dir" / ".env"
    write_env(path, {"A": "one"})
    assert read_env_raw(path) == {"A": "one"}
    assert read_env(path) == {"A": "••••one"}


def test_validate_configs_and_sync_roles(tmp_path):
    providers = {"providers": {"p": {"base_url": "https://x/v1", "api_key_env": "X_KEY", "models": {"chat": "chat", "vision": "vision"}}}, "roles": {}}
    assert validate_providers(providers) == providers
    search = {"nodes": {"answer": {"temperature": 0.2, "max_tokens": 10}}}
    assert validate_search_config(search) == search
    assert validate_reg_record({"document_id": "ГОСТ 1", "title": "T", "document_type": "ГОСТ", "domain": "D", "edition": 2024, "date_enacted": "2024-01-01", "source_file": "a.pdf"})
    sync_role_models(providers, {"provider": "p", "model": "chat", "fallback": {"provider": "p", "model": "chat"}}, "chat")
    assert providers["roles"]["create_markdown"]["ai_postprocess"]["model"] == "chat"
    assert providers["roles"]["build_search_index"]["query_processing"]["model"] == "chat"


def test_write_env_empty_value_deletes_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=one\nB=two\n", encoding="utf-8")
    write_env(path, {"A": "", "B": "three", "C": "new"})
    assert read_env_raw(path) == {"B": "three", "C": "new"}
    assert (tmp_path / ".env.bak").exists()


def test_invalid_configs_rejected():
    with pytest.raises(ValueError): validate_providers({"providers": {"p": {}}})
    with pytest.raises(ValueError): validate_search_config({"nodes": {"x": {"temperature": "bad", "max_tokens": 1}}})
    with pytest.raises(ValueError): validate_reg_record({"document_id": "x"})
