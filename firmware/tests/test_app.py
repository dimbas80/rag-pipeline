from pathlib import Path

from firmware.src import app
from fastapi.testclient import TestClient


def test_app_routes_include_security_endpoints():
    paths = {r.path for r in app.app.routes}
    assert '/api/documents/{sid}/index' in paths
    assert '/api/images/{doc_dir}/{rel_path:path}' in paths
    assert '/api/settings/providers/roles' in paths
    assert '/api/settings/providers/refresh' in paths


def test_traversal_routes_reject_unsafe_paths():
    client = TestClient(app.app)
    assert client.get('/api/files/markdown/../secret').status_code in (404, 307)
    assert client.get('/api/images/../secret.png').status_code in (404, 307)


def test_settings_response_does_not_expose_env_values(monkeypatch):
    monkeypatch.setattr(app.config_ui, 'read_env', lambda _: {'SERVICE_KEY': '••••1234'})
    response = app.get_env_config()
    assert response['SERVICE_KEY'] == '••••1234'
    assert 'secret-value' not in str(response)


def test_index_requires_markdown_registration_and_write_permission(monkeypatch, tmp_path):
    class Config:
        write_enabled = True
    sid = "gating-test"
    monkeypatch.setattr(app, "cfg", Config())
    monkeypatch.setitem(app.sessions, sid, {"md": tmp_path / "doc.md", "reg": tmp_path / "doc_reg.yaml"})
    client = TestClient(app.app)
    response = client.post(f"/api/documents/{sid}/index")
    assert response.status_code == 400


def test_settings_status_reads_single_providers_file(monkeypatch):
    monkeypatch.setattr(app.config_ui, "read_yaml", lambda _: {
        "roles": {"build_search_index": {"embedding": {"provider": "p"}}}
    })
    result = app.settings_status()
    assert result["roles"]["build_search_index.embedding"] is True
    assert result["roles"]["create_markdown.table_vision"] is False


def _fake_cfg(tmp_path):
    class Cfg:
        config_dir = tmp_path
        providers_path = tmp_path / "providers.yaml"
        create_markdown_config_path = tmp_path / "create_markdown_config.yaml"
        search_config_path = tmp_path / "search_config.yaml"
        create_markdown_dir = tmp_path / "cm"
        build_search_index_dir = tmp_path / "bsi"
        qdrant_path = tmp_path / "qdrant_data"
        collection = "technical_standard"
        write_enabled = True
        env_file = tmp_path / ".env"
    return Cfg()


def _fake_providers():
    return {
        "providers": {"p": {"base_url": "https://x/v1", "api_key_env": "K", "models": {"m": "chat"}}},
        "roles": {"create_markdown": {
            "table_vision": {"provider": "p", "model": "m"},
            "ai_postprocess": {"provider": "p", "model": "m"},
        }},
    }


def test_convert_argv_uses_config_dir(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    cfg.env_file.write_text("K=v\n", encoding="utf-8")
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app, "_session", lambda sid: {"source": tmp_path / "doc.pdf"})
    monkeypatch.setattr(app.config_ui, "read_yaml", lambda _: _fake_providers())
    captured = {}
    def fake_start(argv, cwd=None, env=None, kind="convert"):
        captured["argv"] = list(argv)
        captured["env"] = env
        return type("J", (), {"__dict__": {"id": "j1"}})()
    monkeypatch.setattr(app.runner, "start", fake_start)
    app.convert("x")
    argv = captured["argv"]
    assert "--config" in argv and str(cfg.create_markdown_config_path) in argv
    assert "--providers-config" in argv and str(cfg.providers_path) in argv
    assert captured["env"].get("K") == "v"


def test_index_argv_uses_config_dir_and_providers_config(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    (tmp_path / "doc.md").write_text("x", encoding="utf-8")
    (tmp_path / "doc_reg.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app, "_session", lambda sid: {"md": tmp_path / "doc.md", "reg": tmp_path / "doc_reg.yaml"})
    captured = {}
    def fake_start_sequence(phases, cwd=None, env=None, kind="index"):
        captured["phases"] = [list(p) for p in phases]
        return type("J", (), {"__dict__": {"id": "j2"}})()
    monkeypatch.setattr(app.runner, "start_sequence", fake_start_sequence)
    app.index_document("x")
    rag, index = captured["phases"]
    assert "--config" in rag and str(cfg.create_markdown_config_path) in rag
    assert "--providers-config" in rag and str(cfg.providers_path) in rag
    assert "--providers_config" in index and str(cfg.providers_path) in index
    assert "--qdrant-path" in index and "--strict" in index


def test_provider_roles_roundtrip_single_file(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    cfg.providers_path.write_text(
        "providers: {p: {base_url: https://x/v1, api_key_env: K, models: {m: chat}}}\nroles: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app, "cfg", cfg)
    app.update_provider_roles({"kind": "chat", "spec": {"provider": "p", "model": "m"}})
    written = app.config_ui.read_yaml(cfg.providers_path)
    assert written["roles"]["create_markdown"]["ai_postprocess"]["model"] == "m"
    assert written["roles"]["build_search_index"]["query_processing"]["model"] == "m"


def _env_client(monkeypatch, tmp_path, content):
    cfg = _fake_cfg(tmp_path)
    cfg.env_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(app, "cfg", cfg)
    return TestClient(app.app)


def test_put_env_ignores_masked_values(monkeypatch, tmp_path):
    client = _env_client(monkeypatch, tmp_path, "SECRET=realkey\n")
    response = client.put("/api/settings/env", json={"values": {"SECRET": "••••lkey"}, "delete": []})
    assert response.status_code == 200
    assert app.config_ui.read_env_raw(tmp_path / ".env")["SECRET"] == "realkey"


def test_put_env_writes_new_value_and_deletes(monkeypatch, tmp_path):
    client = _env_client(monkeypatch, tmp_path, "A=one\nB=two\n")
    response = client.put("/api/settings/env", json={"values": {"A": "uno", "B": "••••two"}, "delete": ["B"]})
    assert response.status_code == 200
    raw = app.config_ui.read_env_raw(tmp_path / ".env")
    assert raw["A"] == "uno"
    assert "B" not in raw


def test_state_reports_reg_and_md_existence(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    session = {
        "session_id": "s1",
        "stem": "doc",
        "name": "doc.pdf",
        "source": tmp_path / "doc.pdf",
        "reg": tmp_path / "doc_reg.yaml",
        "md": tmp_path / "doc.md",
    }
    monkeypatch.setitem(app.sessions, "s1", session)
    (tmp_path / "doc.md").write_text("# test", encoding="utf-8")
    response = app.state("s1")
    assert response["reg_exists"] is False
    assert response["md_exists"] is True
    assert response["can_index"] is False
