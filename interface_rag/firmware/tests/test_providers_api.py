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


# --- итерация 5 (решения 41–45): rename с каскадом, delete с защитой, per-provider refresh ---

def _providers_with_roles(path):
    path.write_text(yaml.safe_dump({
        "providers": {
            "old": {"base_url": "https://o/v1", "api_key_env": "O_KEY", "models": {"m1": "chat"}},
            "free": {"base_url": "https://f/v1", "api_key_env": "F_KEY", "models": {}},
        },
        "roles": {
            "create_markdown": {
                "ai_postprocess": {"provider": "old", "model": "m1",
                                    "fallback": {"provider": "old", "model": "m1"}},
                "table_vision": {"provider": "old", "model": "t"},
            },
        },
    }), encoding="utf-8")


def test_update_provider_rename_updates_role_refs(tmp_path):
    path = tmp_path / "providers.yaml"
    _providers_with_roles(path)
    data = providers_api.update_provider(path, "old", {"name": "new"})
    assert "old" not in data["providers"]
    assert "new" in data["providers"]
    spec = data["roles"]["create_markdown"]["ai_postprocess"]
    assert spec["provider"] == "new"
    assert spec["fallback"]["provider"] == "new"
    # запись на диске совпадает
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == data


def test_update_provider_rename_conflict_and_bad_base_url(tmp_path):
    path = tmp_path / "providers.yaml"
    _providers_with_roles(path)
    with pytest.raises(ValueError):
        providers_api.update_provider(path, "old", {"name": "free"})
    with pytest.raises(ValueError):
        providers_api.update_provider(path, "old", {"base_url": "ftp://x"})
    with pytest.raises(ValueError):
        providers_api.update_provider(path, "old", {"name": "   "})
    with pytest.raises(KeyError):
        providers_api.update_provider(path, "nope", {"name": "x"})


def test_update_provider_models_and_base_url(tmp_path):
    path = tmp_path / "providers.yaml"
    _providers_with_roles(path)
    data = providers_api.update_provider(path, "free", {
        "base_url": "https://f2/v1/",
        "models": {"emb-1": "embedding", "bad-tag-model": "weird"},
    })
    provider = data["providers"]["free"]
    assert provider["base_url"] == "https://f2/v1"
    assert provider["models"]["emb-1"] == "embedding"
    # неизвестный тег -> авто-тегирование по имени
    assert provider["models"]["bad-tag-model"] == "chat"


def test_delete_provider_guarded_by_roles(tmp_path):
    path = tmp_path / "providers.yaml"
    _providers_with_roles(path)
    with pytest.raises(ValueError) as exc:
        providers_api.delete_provider(path, "old")
    assert "create_markdown.ai_postprocess" in str(exc.value)
    assert "old" in yaml.safe_load(path.read_text(encoding="utf-8"))["providers"]
    data = providers_api.delete_provider(path, "free")
    assert "free" not in data["providers"]
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["providers"].keys() == {"old"}
    with pytest.raises(KeyError):
        providers_api.delete_provider(path, "free")


def test_refresh_provider_single_isolates_error(monkeypatch, tmp_path):
    path = tmp_path / "providers.yaml"
    _providers_with_roles(path)

    def fake_scan(base_url, api_key, timeout=20):
        raise RuntimeError("net down")
    monkeypatch.setattr(providers_api, "scan_and_tag_models", fake_scan)
    result = providers_api.refresh_provider(path, "old", {"O_KEY": "k"})
    assert result == {"ok": False, "error": "net down"}
    # без ключа
    assert providers_api.refresh_provider(path, "old", {}) == {"ok": False, "error": "нет ключа"}
    with pytest.raises(KeyError):
        providers_api.refresh_provider(path, "missing", {})

    monkeypatch.setattr(providers_api, "scan_and_tag_models",
                        lambda base_url, api_key, timeout=20: [{"name": "m2", "tag": "vision"}])
    result = providers_api.refresh_provider(path, "old", {"O_KEY": "k"})
    assert result["ok"] is True
    assert result["models"] == {"m2": "vision"}
    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert written["providers"]["old"]["models"] == {"m2": "vision"}
