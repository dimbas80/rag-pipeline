"""Contract tests for the split provider registry and fallback behavior."""
import importlib.util
import os
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
spec = importlib.util.spec_from_file_location("llm_providers", SRC / "llm_providers.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def registry(fallback="{}"):
    return f"""
providers:
  primary:
    base_url: https://primary.example/v1/
    api_key_env: PRIMARY_KEY
    models: {{chat-model: chat}}
  backup:
    base_url: https://backup.example/v1
    api_key_env: BACKUP_KEY
    models: {{backup-model: chat}}
roles:
  pipe:
    chat: {{provider: primary, model: chat-model, fallback: {fallback}}}
"""


def write(tmp_path, text):
    path = tmp_path / "providers.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_providers_valid_and_missing_fallback(tmp_path):
    cfg = module.load_providers(write(tmp_path, registry()))
    assert isinstance(cfg, dict)
    assert "primary" in cfg["providers"]


@pytest.mark.parametrize("text", ["providers: {}", "roles: {}", "not: [valid"])
def test_load_providers_rejects_invalid_structure(tmp_path, text):
    with pytest.raises(module.ProviderConfigError):
        module.load_providers(write(tmp_path, text))


def test_load_providers_missing_file_raises(tmp_path):
    with pytest.raises(module.ProviderConfigError):
        module.load_providers(str(tmp_path / "missing.yaml"))


def test_load_providers_provider_fields_and_models_are_required(tmp_path):
    for fragment in ("base_url:", "api_key_env:", "models: {}"):
        text = registry().replace("base_url: https://primary.example/v1/", fragment) if fragment == "base_url:" else registry().replace("api_key_env: PRIMARY_KEY", fragment) if fragment == "api_key_env:" else registry().replace("models: {chat-model: chat}", fragment)
        with pytest.raises(module.ProviderConfigError):
            module.load_providers(write(tmp_path, text))


def test_resolve_subrole_and_empty_fallback(tmp_path):
    cfg = module.load_providers(write(tmp_path, registry()))
    resolved = module.resolve_subrole(cfg, "pipe", "chat")
    assert resolved["provider"] == "primary"
    assert resolved["fallback"] is None


def test_resolve_subrole_validates_fallback(tmp_path):
    cfg = module.load_providers(write(tmp_path, registry("{provider: backup, model: backup-model}")))
    assert module.resolve_subrole(cfg, "pipe", "chat")["fallback"]["provider"] == "backup"
    bad = module.load_providers(write(tmp_path, registry("{provider: backup}")))
    with pytest.raises(module.ProviderConfigError):
        module.resolve_subrole(bad, "pipe", "chat")


def test_resolve_subrole_unknown_role_provider_or_model_raises(tmp_path):
    cfg = module.load_providers(write(tmp_path, registry()))
    for args in (("missing", "chat"), ("pipe", "missing")):
        with pytest.raises(module.ProviderConfigError):
            module.resolve_subrole(cfg, *args)
    cfg["roles"]["pipe"]["chat"]["provider"] = "missing"
    with pytest.raises(module.ProviderConfigError):
        module.resolve_subrole(cfg, "pipe", "chat")


def test_get_endpoint_maps_capabilities_and_strips_slash(tmp_path):
    cfg = module.load_providers(write(tmp_path, registry()))
    assert module.get_endpoint(cfg, "primary", "chat").endswith("/chat/completions")
    assert module.get_endpoint(cfg, "primary", "embedding").endswith("/embeddings")
    assert module.get_endpoint(cfg, "primary", "rerank").endswith("/rerank")


def test_get_api_key_override_environment_and_dotenv(tmp_path, monkeypatch):
    cfg = module.load_providers(write(tmp_path, registry()))
    monkeypatch.setenv("PRIMARY_KEY", "from-env")
    assert module.get_api_key(cfg, "primary", "override") == "override"
    assert module.get_api_key(cfg, "primary") == "from-env"
    monkeypatch.delenv("PRIMARY_KEY")
    with pytest.raises(module.ProviderConfigError, match="PRIMARY_KEY"):
        module.get_api_key(cfg, "primary")


def test_run_with_fallback_primary_success_does_not_call_backup():
    calls = []
    spec = {"provider": "primary", "model": "one", "fallback": {"provider": "backup", "model": "two"}}
    assert module.run_with_fallback(spec, lambda target: calls.append(target) or "ok", "chat") == "ok"
    assert len(calls) == 1


def test_run_with_fallback_calls_backup_after_primary_failure():
    calls = []
    def attempt(target):
        calls.append(target["provider"])
        if target["provider"] == "primary":
            raise module.ProviderUnavailableError("down")
        return "backup-ok"
    spec = {"provider": "primary", "model": "one", "fallback": {"provider": "backup", "model": "two"}}
    assert module.run_with_fallback(spec, attempt, "chat") == "backup-ok"
    assert calls == ["primary", "backup"]


def test_run_with_fallback_without_fallback_has_actionable_error():
    spec = {"provider": "primary", "model": "one", "fallback": None}
    with pytest.raises(module.ProviderUnavailableError, match="невозможно продолжить"):
        module.run_with_fallback(spec, lambda _: (_ for _ in ()).throw(module.ProviderUnavailableError("down")), "chat")


def test_run_with_fallback_both_fail():
    spec = {"provider": "primary", "model": "one", "fallback": {"provider": "backup", "model": "two"}}
    with pytest.raises(module.ProviderUnavailableError):
        module.run_with_fallback(spec, lambda _: (_ for _ in ()).throw(module.ProviderUnavailableError("down")), "chat")


def test_run_with_fallback_does_not_swallow_config_error():
    error = module.ProviderConfigError("bad config")
    with pytest.raises(module.ProviderConfigError):
        module.run_with_fallback({"provider": "p", "model": "m"}, lambda _: (_ for _ in ()).throw(error), "chat")
