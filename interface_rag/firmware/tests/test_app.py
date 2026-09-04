from pathlib import Path

import pytest
import yaml

from firmware.src import app
from fastapi import HTTPException
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
        document_types_path = tmp_path / "document_types.yaml"
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

        # BASE_DIR: та же схема — override из .env, иначе upload_base_dir.
        @property
        def base_dir_override(self):
            return app.config_ui.read_env_raw(self.env_file).get("BASE_DIR") or None

        @property
        def base_dir(self):
            return Path(self.base_dir_override) if self.base_dir_override else self.upload_base_dir

        @property
        def base_markdown(self):
            return self.base_dir / "Markdown" if self.base_dir_override else tmp_path / "Markdown"
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


# --- корневая папка документов через /api/settings/base-dir ---

def test_get_base_dir_settings_reports_path_default_and_override(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    response = app.settings_base_dir()
    assert response["path"] == str(tmp_path / "upload")
    assert response["default"] == str(tmp_path / "upload")
    assert response["overridden"] is False


def test_put_base_dir_settings_writes_env(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    response = client.put("/api/settings/base-dir", json={"path": "/mnt/sdb/!База_ГОСТ"})
    assert response.status_code == 200
    assert app.config_ui.read_env_raw(tmp_path / ".env")["BASE_DIR"] == "/mnt/sdb/!База_ГОСТ"
    assert response.json()["overridden"] is True
    assert response.json()["path"] == "/mnt/sdb/!База_ГОСТ"


def test_put_base_dir_settings_empty_resets_override(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    (tmp_path / ".env").write_text("BASE_DIR=/old/base\n", encoding="utf-8")
    client = TestClient(app.app)
    response = client.put("/api/settings/base-dir", json={"path": ""})
    assert response.status_code == 200
    assert "BASE_DIR" not in app.config_ui.read_env_raw(tmp_path / ".env")


def test_put_base_dir_settings_rejects_relative_path(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    assert client.put("/api/settings/base-dir", json={"path": "relative/path"}).status_code == 400


def test_put_base_dir_settings_normalizes_dotdot_path(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    client = TestClient(app.app)
    response = client.put("/api/settings/base-dir", json={"path": "/tmp/../base"})
    assert response.status_code == 200
    assert app.config_ui.read_env_raw(tmp_path / ".env")["BASE_DIR"] == str(Path("/base"))
    assert response.json()["path"] == str(Path("/base"))


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


# --- итерация 5: «Удалить результат» (решение №40) ---

def _delete_result_fixture(tmp_path):
    cfg = _fake_cfg(tmp_path)
    stem = "ГОСТ 12.3.456-2020"
    tmp_base = cfg.upload_base_dir / "tmp" / stem
    (tmp_base / "pages").mkdir(parents=True)
    (tmp_base / "pages" / "p1.json").write_text("{}", encoding="utf-8")
    md_dir = cfg.base_markdown / stem
    (md_dir / "image").mkdir(parents=True)
    (md_dir / "image" / "p1_i1.png").write_bytes(b"png")
    (md_dir / "table_images.json").write_text("{}", encoding="utf-8")
    (md_dir / f"{stem}.md").write_text("# doc", encoding="utf-8")
    (md_dir / f"{stem}_reg.yaml").write_text("{}", encoding="utf-8")
    (md_dir / f"{stem}_chunks.jsonl").write_text("{}", encoding="utf-8")
    (md_dir / f"{stem}_assets.json").write_text("{}", encoding="utf-8")
    return cfg, stem


def test_delete_result_removes_only_intermediate_artifacts(tmp_path):
    cfg, stem = _delete_result_fixture(tmp_path)
    result = app._delete_result(cfg, stem)
    assert result["removed"] == 3
    assert not (cfg.upload_base_dir / "tmp" / stem).exists()
    md_dir = cfg.base_markdown / stem
    assert not (md_dir / "image").exists()
    assert not (md_dir / "table_images.json").exists()
    # сохраняется всё ценное (требование AC: .md, регистрация, чанки, исходник)
    assert (md_dir / f"{stem}.md").is_file()
    assert (md_dir / f"{stem}_reg.yaml").is_file()
    assert (md_dir / f"{stem}_chunks.jsonl").is_file()
    assert (md_dir / f"{stem}_assets.json").is_file()
    assert set(result["kept"]) == {"md", "reg", "chunks", "assets"}
    assert all(v is not None for v in result["kept"].values())
    # повторный вызов — идемпотентен, «нечего удалять»
    again = app._delete_result(cfg, stem)
    assert again["removed"] == 0


def test_delete_result_rejects_unsafe_stem(tmp_path):
    cfg = _fake_cfg(tmp_path)
    for bad in ("../evil", "a/b", "..", "", "x\\y", "на../з", ".hidden"):
        try:
            app._delete_result(cfg, bad)
            assert False, f"ожидался ValueError для stem={bad!r}"
        except ValueError:
            pass
    assert app._valid_stem("ГОСТ 12.3.456-2020") is True


def test_delete_result_endpoint_uses_session_stem(monkeypatch, tmp_path):
    cfg, stem = _delete_result_fixture(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setitem(app.sessions, "s-dr", {"stem": stem})
    client = TestClient(app.app)
    response = client.post("/api/documents/s-dr/delete-result")
    assert response.status_code == 200
    body = response.json()
    assert body["removed"] == 3
    assert (cfg.base_markdown / stem / f"{stem}.md").is_file()
    # чужой sid -> 404
    assert client.post("/api/documents/nope/delete-result").status_code == 404


# --- итерация 5: PUT/DELETE/per-provider refresh маршруты (решения 41–45) ---

def _providers_route_client(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    cfg.providers_path.write_text(yaml.safe_dump({
        "providers": {"old": {"base_url": "https://o/v1", "api_key_env": "O_KEY",
                               "models": {"m1": "chat"}}},
        "roles": {"create_markdown": {"ai_postprocess": {"provider": "old", "model": "m1"}}},
    }), encoding="utf-8")
    monkeypatch.setattr(app, "cfg", cfg)
    return TestClient(app.app), cfg


def test_put_provider_route_rename(tmp_path, monkeypatch):
    client, cfg = _providers_route_client(monkeypatch, tmp_path)
    response = client.put("/api/settings/providers/old", json={"name": "new"})
    assert response.status_code == 200
    written = yaml.safe_load(cfg.providers_path.read_text(encoding="utf-8"))
    assert "new" in written["providers"]
    assert written["roles"]["create_markdown"]["ai_postprocess"]["provider"] == "new"


def test_delete_provider_route_409_when_referenced(tmp_path, monkeypatch):
    client, _ = _providers_route_client(monkeypatch, tmp_path)
    response = client.delete("/api/settings/providers/old")
    assert response.status_code == 409
    assert "create_markdown.ai_postprocess" in response.json()["detail"]
    assert client.delete("/api/settings/providers/ghost").status_code == 404


def test_put_roles_route_not_shadowed_by_name_route(tmp_path, monkeypatch):
    # /providers/roles (PUT) зарегистрирован раньше /providers/{name} —
    # регрессия на приоритет маршрутов FastAPI.
    client, cfg = _providers_route_client(monkeypatch, tmp_path)
    response = client.put("/api/settings/providers/roles",
                          json={"kind": "chat", "spec": {"provider": "old", "model": "m1"}})
    assert response.status_code == 200


def test_refresh_provider_route(monkeypatch, tmp_path):
    client, cfg = _providers_route_client(monkeypatch, tmp_path)
    monkeypatch.setattr(app.providers_api, "scan_and_tag_models",
                        lambda base_url, api_key, timeout=20: [{"name": "m2", "tags": ["chat", "vision"]}])
    cfg.env_file.write_text("O_KEY=secret\n", encoding="utf-8")
    response = client.post("/api/settings/providers/old/refresh")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    # решение №53: merge — существующая m1 сохраняется, m2 добавляется
    assert response.json()["models"] == {"m1": ["chat"], "m2": ["chat", "vision"]}
    assert client.post("/api/settings/providers/ghost/refresh").status_code == 404


def test_register_uses_explicit_slug_from_form(monkeypatch, tmp_path):
    reg_path = tmp_path / "doc_reg.yaml"
    monkeypatch.setattr(app, "_session", lambda sid: {"name": "doc.pdf", "source": tmp_path / "doc.pdf",
                                                      "reg": reg_path})
    response = app.register("x", app.Registration(fields={
        "document_id": "ГОСТ 123", "document_type": "ГОСТ", "domain": "Кабели",
        "slug": "GOST_123_my_custom_key",
    }))
    assert response == {"slug": "GOST_123_my_custom_key"}
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    assert "GOST_123_my_custom_key" in data["documents"]


def test_register_rejects_invalid_slug(monkeypatch, tmp_path):
    reg_path = tmp_path / "doc_reg.yaml"
    monkeypatch.setattr(app, "_session", lambda sid: {"name": "doc.pdf", "source": tmp_path / "doc.pdf",
                                                      "reg": reg_path})
    for bad_slug in ("ГОСТ 123", "my slug", "ключ/1"):
        with pytest.raises(HTTPException) as exc:
            app.register("x", app.Registration(fields={
                "document_id": "ГОСТ 123", "document_type": "ГОСТ", "domain": "Кабели",
                "slug": bad_slug,
            }))
        assert exc.value.status_code == 400
    assert not reg_path.exists()


# --- статус/перезапуск Телеграм-бота (interface-rag-bot.service, раздел «Телеграм») ---

def _fake_bot_info(**overrides):
    info = {"available": True, "unit": "interface-rag-bot.service", "active": True,
            "sub_state": "running", "restarts": 2, "started_at": "Mon 2026-08-31 22:00:52 +07",
            "token_configured": True, "telegram_ok": True}
    info.update(overrides)
    return info


def test_telegram_bot_status_returns_service_info_without_token(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    cfg.env_file.write_text("TELEGRAM_BOT_TOKEN=secret-token-123\n", encoding="utf-8")
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app.bot_service, "status", lambda token: _fake_bot_info(token_configured=bool(token)))
    response = app.telegram_bot_status()
    assert response["available"] is True
    assert response["active"] is True
    assert response["telegram_ok"] is True
    # токен никогда не попадает в ответ
    assert "secret-token-123" not in str(response)


def test_telegram_bot_status_passes_env_token_to_service(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    cfg.env_file.write_text("TELEGRAM_BOT_TOKEN=tok-42\n", encoding="utf-8")
    monkeypatch.setattr(app, "cfg", cfg)
    captured = {}
    monkeypatch.setattr(app.bot_service, "status", lambda token: captured.setdefault("token", token) or _fake_bot_info())
    app.telegram_bot_status()
    assert captured["token"] == "tok-42"


def test_telegram_bot_restart_returns_fresh_status(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app.bot_service, "restart", lambda token: _fake_bot_info(restarts=3))
    response = app.restart_telegram_bot()
    assert response["available"] is True
    assert response["restarts"] == 3


def test_telegram_bot_restart_unavailable_unit_raises_503(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app.bot_service, "restart", lambda token: {"available": False, "unit": "interface-rag-bot.service"})
    with pytest.raises(HTTPException) as exc:
        app.restart_telegram_bot()
    assert exc.value.status_code == 503


def test_telegram_bot_restart_systemctl_failure_raises_502(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    def boom(token):
        raise RuntimeError("systemctl failed")
    monkeypatch.setattr(app.bot_service, "restart", boom)
    with pytest.raises(HTTPException) as exc:
        app.restart_telegram_bot()
    assert exc.value.status_code == 502


# --- решение №50: пользовательские типы документов ---

def test_document_types_roundtrip(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    assert app.get_document_types() == {"types": []}
    result = app.add_document_type(app.DocumentTypeAdd(type="  ГОСТ Р  "))
    assert result["types"] == ["ГОСТ Р"]
    # идемпотентность + второй тип
    app.add_document_type(app.DocumentTypeAdd(type="ГОСТ Р"))
    result = app.add_document_type(app.DocumentTypeAdd(type="СТО"))
    assert result["types"] == ["ГОСТ Р", "СТО"]
    with pytest.raises(HTTPException) as exc:
        app.add_document_type(app.DocumentTypeAdd(type="   "))
    assert exc.value.status_code == 400
    assert app.get_document_types() == {"types": ["ГОСТ Р", "СТО"]}


# --- решение №51: текстовый редактор итогового md ---

def test_markdown_put_updates_file(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    doc_dir = tmp_path / "Markdown" / "doc"
    doc_dir.mkdir(parents=True)
    (doc_dir / "doc.md").write_text("старый текст", encoding="utf-8")
    client = TestClient(app.app)
    response = client.put("/api/files/markdown/doc", json={"content": "# Новый текст\n"})
    assert response.status_code == 200
    assert (doc_dir / "doc.md").read_text(encoding="utf-8") == "# Новый текст\n"
    # GET отдаёт обновлённый файл
    assert "Новый текст" in client.get("/api/files/markdown/doc").text
    # неизвестный stem → 404, traversal не проходит
    assert client.put("/api/files/markdown/nope", json={"content": "x"}).status_code == 404
    assert client.put("/api/files/markdown/../etc", json={"content": "x"}).status_code in (404, 307)


# --- обзор каталогов сервера для выбора папок в UI ---

def test_fs_dirs_lists_only_visible_directories(monkeypatch, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    client = TestClient(app.app)
    response = client.get("/api/fs/dirs", params={"path": str(tmp_path)})
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(tmp_path)
    assert body["parent"] == str(tmp_path.parent)
    names = [e["name"] for e in body["entries"]]
    assert names == sorted(["alpha", "beta"])
    assert all(e["path"] == str(tmp_path / n) for n, e in
               zip(names, body["entries"]))


def test_fs_dirs_root_has_no_parent(monkeypatch):
    client = TestClient(app.app)
    response = client.get("/api/fs/dirs")
    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "/"
    assert body["parent"] is None


def test_fs_dirs_unknown_path_404(monkeypatch):
    client = TestClient(app.app)
    response = client.get("/api/fs/dirs", params={"path": "/nonexistent-xyz-123"})
    assert response.status_code == 404


def test_fs_dirs_rejects_relative_path(monkeypatch):
    client = TestClient(app.app)
    assert client.get("/api/fs/dirs", params={"path": "relative/path"}).status_code == 400


# --- очистка временных файлов tmp/ (раздел «Папки») ---

def test_get_tmp_size_reports_path_and_bytes(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    tmp_dir = cfg.base_dir / "tmp"
    (tmp_dir / "sub").mkdir(parents=True)
    (tmp_dir / "sub" / "f.bin").write_bytes(b"x" * 100)
    (tmp_dir / "g.bin").write_bytes(b"y" * 30)
    response = app.tmp_settings()
    assert response["path"] == str(tmp_dir)
    assert response["size_bytes"] == 130


def test_get_tmp_size_missing_dir_is_zero(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    response = app.tmp_settings()
    assert response["size_bytes"] == 0


def test_post_tmp_clear_removes_contents_keeps_dir(monkeypatch, tmp_path):
    cfg = _fake_cfg(tmp_path)
    monkeypatch.setattr(app, "cfg", cfg)
    tmp_dir = cfg.base_dir / "tmp"
    (tmp_dir / "sub").mkdir(parents=True)
    (tmp_dir / "sub" / "f.bin").write_bytes(b"x" * 100)
    (cfg.base_dir / "keep.txt").write_text("важный файл вне tmp", encoding="utf-8")
    client = TestClient(app.app)
    response = client.post("/api/settings/tmp/clear")
    assert response.status_code == 200
    body = response.json()
    assert body["size_bytes"] == 0
    assert body["freed_bytes"] == 100
    assert tmp_dir.is_dir()
    assert list(tmp_dir.iterdir()) == []
    assert (tmp_dir.parent / "keep.txt").read_text(encoding="utf-8") == "важный файл вне tmp"
