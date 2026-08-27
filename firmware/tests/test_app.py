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
        upload_base_dir = tmp_path / "upload"
        upload_max_mb = 500
        base_markdown = tmp_path / "Markdown"
        qdrant_path_default = tmp_path / "qdrant_data"
        collection = "technical_standard"
        write_enabled = True
        env_file = tmp_path / ".env"
        prompts = {}

        # Повторяют поведение DeployConfig (решение №29): override читается
        # лениво из .env, чтобы settings_qdrant() видел свежезаписанный путь.
        @property
        def qdrant_path_override(self):
            return app.config_ui.read_env_raw(self.env_file).get("QDRANT_PATH") or None

        @property
        def qdrant_path(self):
            return Path(self.qdrant_path_override) if self.qdrant_path_override else self.qdrant_path_default
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
    monkeypatch.setattr(app, "_session", lambda sid: {"name": "doc.pdf", "source": tmp_path / "doc.pdf"})
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


# --- решение №28: prefill из _reg.yaml без OCR ---

def _prefill_session(tmp_path, sid="s1"):
    return {
        "session_id": sid,
        "stem": "doc",
        "name": "doc.pdf",
        "source": tmp_path / "doc.pdf",
        "reg": tmp_path / "doc_reg.yaml",
        "md": tmp_path / "doc.md",
    }


def test_registration_prefill_reads_reg_yaml_without_ocr(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    session = _prefill_session(tmp_path)
    monkeypatch.setitem(app.sessions, session["session_id"], session)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-fake")
    app.registration.write_reg_yaml(
        tmp_path / "doc_reg.yaml",
        {"document_id": "ГОСТ 18410-73", "document_type": "ГОСТ", "domain": "Кабели",
         "title": "T", "edition": 1973, "date_enacted": "1973-01-01", "source_file": "doc.pdf",
         "status": "active", "ignore_sections": ["Предисловие"]},
    )
    calls = []
    monkeypatch.setattr(app.registration, "extract_first_page", lambda *a, **k: calls.append("ocr") or b"png")
    monkeypatch.setattr(app.registration, "vision_prefill", lambda *a, **k: calls.append("vision") or {})
    response = app.registration_prefill("s1")
    assert response["source"] == "reg_yaml"
    assert response["exists"] is True
    assert response["fields"]["document_id"] == "ГОСТ 18410-73"
    assert response["fields"]["ignore_sections"] == ["Предисловие"]
    assert response["fields"]["slug"] is not None
    assert not calls  # OCR/vision не вызывались


def test_registration_prefill_falls_back_to_vision(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    session = _prefill_session(tmp_path)
    monkeypatch.setitem(app.sessions, session["session_id"], session)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr(app.registration, "extract_first_page", lambda src: b"png")
    monkeypatch.setattr(
        app.registration, "vision_prefill",
        lambda image, **k: {"document_id": "ГОСТ 1", "title": "T", "domain_hint": "Д", "document_type": "ГОСТ"},
    )
    response = app.registration_prefill("s1")
    assert response["source"] == "vision"
    assert response["exists"] is False
    assert response["fields"]["document_id"] == "ГОСТ 1"
    assert response["fields"]["domain"] == "Д"


def test_registration_prefill_vision_skipped_without_image(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    session = _prefill_session(tmp_path)
    monkeypatch.setitem(app.sessions, session["session_id"], session)
    monkeypatch.setattr(app.registration, "extract_first_page", lambda src: None)
    calls = []
    monkeypatch.setattr(app.registration, "vision_prefill", lambda *a, **k: calls.append(1) or {})
    response = app.registration_prefill("s1")
    assert response["source"] == "vision"
    assert response["image_available"] is False
    assert not calls


# --- решение №29: папка Qdrant через /api/settings/qdrant ---

def test_get_qdrant_settings_reports_path_default_and_override(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    response = app.settings_qdrant()
    assert response["path"] == str(tmp_path / "qdrant_data")
    assert response["default"] == str(tmp_path / "qdrant_data")
    assert response["overridden"] is False


def test_put_qdrant_settings_writes_env(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    response = client.put("/api/settings/qdrant", json={"path": "/mnt/sdb/qdrant_data"})
    assert response.status_code == 200
    assert app.config_ui.read_env_raw(tmp_path / ".env")["QDRANT_PATH"] == "/mnt/sdb/qdrant_data"
    assert response.json()["overridden"] is True
    assert response.json()["path"] == "/mnt/sdb/qdrant_data"


def test_put_qdrant_settings_empty_resets_override(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    (tmp_path / ".env").write_text("QDRANT_PATH=/old/path\n", encoding="utf-8")
    client = TestClient(app.app)
    response = client.put("/api/settings/qdrant", json={"path": ""})
    assert response.status_code == 200
    assert "QDRANT_PATH" not in app.config_ui.read_env_raw(tmp_path / ".env")


def test_put_qdrant_settings_rejects_relative_path(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    assert client.put("/api/settings/qdrant", json={"path": "relative/path"}).status_code == 400


def test_put_qdrant_settings_normalizes_dotdot_path(monkeypatch, tmp_path):
    # LOW из ревью: абсолютный путь с компонентами «..» нормализуется (resolve),
    # чтобы /tmp/../etc не сохранялся в .env как есть.
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    response = client.put("/api/settings/qdrant", json={"path": "/tmp/../etc"})
    assert response.status_code == 200
    assert app.config_ui.read_env_raw(tmp_path / ".env")["QDRANT_PATH"] == str(Path("/etc"))
    assert response.json()["path"] == str(Path("/etc"))


# --- решение 39: .md → канонический Markdown/<stem>/<stem>.md при upload ---

def test_upload_md_places_into_canonical_markdown(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    response = client.post("/api/documents",
                           files={"file": ("ГОСТ 1.md", b"# content", "text/markdown")})
    assert response.status_code == 200
    session = app.sessions[response.json()["session_id"]]
    canonical = tmp_path / "Markdown" / "ГОСТ 1" / "ГОСТ 1.md"
    assert session["source"] == canonical
    assert session["md"] == canonical
    assert canonical.is_file()
    assert canonical.read_bytes() == b"# content"
    # в корне upload_base_dir файл НЕ остаётся (не <stem>.md и не <stem>_ai.md)
    assert not (tmp_path / "upload" / "ГОСТ 1.md").exists()


def test_upload_md_does_not_overwrite_existing(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    canonical = tmp_path / "Markdown" / "doc" / "doc.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("OLD CONTENT", encoding="utf-8")
    client = TestClient(app.app)
    response = client.post("/api/documents",
                           files={"file": ("doc.md", b"NEW CONTENT", "text/markdown")})
    assert response.status_code == 200
    assert canonical.read_text(encoding="utf-8") == "OLD CONTENT"  # переиндексация без пересохранения
    session = app.sessions[response.json()["session_id"]]
    assert session["source"] == canonical


def test_upload_pdf_still_uses_upload_base_dir(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    response = client.post("/api/documents",
                           files={"file": ("doc.pdf", b"%PDF-fake", "application/pdf")})
    assert response.status_code == 200
    session = app.sessions[response.json()["session_id"]]
    target = tmp_path / "upload" / "doc.pdf"
    assert session["source"] == target
    assert target.is_file()
    assert session["md"] == tmp_path / "Markdown" / "doc" / "doc.md"  # канон для index


def test_convert_md_is_noop_instant_job(monkeypatch, tmp_path):
    import sys
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app, "_session", lambda sid: {"name": "doc.md"})
    def boom(*a, **k):
        raise AssertionError("read_yaml не должен вызываться для .md (проверка провайдеров пропускается)")
    monkeypatch.setattr(app.config_ui, "read_yaml", boom)
    captured = {}
    def fake_start(argv, cwd=None, env=None, kind="convert"):
        captured["argv"] = list(argv)
        captured["kind"] = kind
        return type("J", (), {"__dict__": {"id": "j-md"}})()
    monkeypatch.setattr(app.runner, "start", fake_start)
    result = app.convert("x")
    assert result["id"] == "j-md"
    assert captured["kind"] == "convert"
    assert captured["argv"][0] == sys.executable
    assert "-c" in captured["argv"]  # мгновенно-завершённая job без OCR/AI
