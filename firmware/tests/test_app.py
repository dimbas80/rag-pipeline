from firmware.src import app
from fastapi.testclient import TestClient

def test_app_routes_include_security_endpoints():
    paths={r.path for r in app.app.routes}
    assert '/api/documents/{sid}/index' in paths
    assert '/api/images/{doc_dir}/{rel_path:path}' in paths
    assert '/api/settings/providers/roles' in paths

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

def test_settings_status_reads_combined_pipeline_roles(monkeypatch):
    monkeypatch.setattr(app, "_combined_providers", lambda: {
        "roles": {"build_search_index": {"embedding": {"provider": "p"}}}
    })
    result = app.settings_status()
    assert result["roles"]["build_search_index.embedding"] is True
