"""Tests for the provider registry and role resolver."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
import pytest
import create_markdown


def registry():
    return {
        "providers": {
            "one": {"base_url": "https://one/v1", "api_key_env": "ONE_KEY", "models": {"m": "chat"}},
            "two": {"base_url": "https://two/v1", "api_key_env": "TWO_KEY", "models": {"fallback": "chat"}},
        }
    }


def test_resolve_role_injects_provider_fields_and_fallback():
    result = create_markdown.resolve_role(
        {"provider": "one", "model": "m", "fallback": {"provider": "two", "model": "fallback"}},
        registry(),
    )
    assert result == {
        "provider": "one", "model": "m", "api_key_env": "ONE_KEY", "base_url": "https://one/v1",
        "fallback": {"provider": "two", "model": "fallback", "api_key_env": "TWO_KEY", "base_url": "https://two/v1"},
    }


def test_resolve_role_rejects_unknown_provider_and_model():
    with pytest.raises(ValueError, match="Неизвестный провайдер"):
        create_markdown.resolve_role({"provider": "missing", "model": "m"}, registry())
    with pytest.raises(ValueError, match="Модель не в списке"):
        create_markdown.resolve_role({"provider": "one", "model": "missing"}, registry())


def test_load_providers_config_rejects_duplicate_api_key_env(tmp_path):
    path = tmp_path / "providers.yaml"
    path.write_text("providers:\n  a: {api_key_env: SAME, models: {m: chat}}\n  b: {api_key_env: SAME, models: {m: chat}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Дублирующийся api_key_env"):
        create_markdown.load_providers_config(path)


def test_load_providers_config_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="не найден"):
        create_markdown.load_providers_config(tmp_path / "missing.yaml")
