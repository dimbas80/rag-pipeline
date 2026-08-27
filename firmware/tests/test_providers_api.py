import pytest
import yaml
from firmware.src import providers_api

def test_tag_model():
    assert providers_api.tag_model('text-embedding-3-small')=='embedding'
    assert providers_api.tag_model('qwen-vl')=='vision'

def test_scan_models(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {'data':[{'id':'x'}]}
    monkeypatch.setattr(providers_api.requests,'get',lambda *a,**k:R())
    assert providers_api.scan_models('https://x','k')==['x']


def _write_providers(path):
    path.write_text(yaml.safe_dump({"providers": {
        "a": {"base_url": "https://a/v1", "api_key_env": "A_KEY", "models": {}},
        "b": {"base_url": "https://b/v1", "api_key_env": "B_KEY", "models": {}},
        "c": {"base_url": "https://c/v1", "api_key_env": "C_KEY", "models": {}},
    }}), encoding="utf-8")


def test_refresh_all_models_updates_and_isolates_errors(monkeypatch, tmp_path):
    path = tmp_path / "providers.yaml"
    _write_providers(path)

    def fake_scan(base_url, api_key, timeout=20):
        if base_url == "https://b/v1":
            raise RuntimeError("boom")
        return [{"name": "x-model", "tag": "chat"}]
    monkeypatch.setattr(providers_api, "scan_and_tag_models", fake_scan)

    result = providers_api.refresh_all_models(path, {"A_KEY": "a", "B_KEY": "b", "C_KEY": "c"})
    assert result["a"]["ok"] is True
    assert result["b"]["ok"] is False
    assert "boom" in result["b"]["error"]
    assert result["c"]["ok"] is True
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["providers"]["a"]["models"] == {"x-model": "chat"}
    assert written["providers"]["b"]["models"] == {}
    assert written["providers"]["c"]["models"] == {"x-model": "chat"}


def test_refresh_skips_provider_without_key(monkeypatch, tmp_path):
    path = tmp_path / "providers.yaml"
    _write_providers(path)
    result = providers_api.refresh_all_models(path, {})
    assert result["a"] == {"ok": False, "error": "нет ключа"}
    assert result["b"] == {"ok": False, "error": "нет ключа"}
    assert result["c"] == {"ok": False, "error": "нет ключа"}
