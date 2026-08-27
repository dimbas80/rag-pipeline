from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:  # supports both documented package and legacy module invocation
    from firmware.src.deploy_config import load
    from firmware.src.jobs import JobRunner, build_env
    from firmware.src import config_ui, registration, providers_api, fs_perms
    from firmware.src.chat_api import ChatSession
    from firmware.src import qdrant_api
except ImportError:  # pragma: no cover - only for direct `uvicorn app:app`
    from deploy_config import load
    from jobs import JobRunner, build_env
    import config_ui, registration, providers_api, fs_perms
    from chat_api import ChatSession
    import qdrant_api

cfg = load()
app = FastAPI(title="interface_RAG")
runner = JobRunner()
sessions: dict[str, dict] = {}
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class Registration(BaseModel):
    fields: dict

class IndexRequest(BaseModel):
    pass

class ProviderScan(BaseModel):
    base_url: str
    api_key: str

class ProviderModel(BaseModel):
    name: str
    tag: str | None = None

class ProviderAdd(BaseModel):
    name: str
    base_url: str
    api_key_env: str
    models: list[str] | list[ProviderModel]

class EnvUpdate(BaseModel):
    values: dict[str, str] = {}
    delete: list[str] = []

class QdrantPathUpdate(BaseModel):
    path: str = ""


def _session(sid: str) -> dict:
    value = sessions.get(sid)
    if value is None:
        raise HTTPException(404, "Сессия не найдена")
    return value


@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(Path(__file__).parent / "static/index.html")


@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    if name != file.filename or Path(name).suffix.lower() not in {".pdf", ".docx", ".doc", ".md"}:
        raise HTTPException(400, "Недопустимый файл")
    if len(name) > 255:
        raise HTTPException(400, "Слишком длинное имя")
    limit = cfg.upload_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "Файл превышает допустимый размер")
    fs_perms.ensure_dir(cfg.upload_base_dir)
    target = cfg.upload_base_dir / name
    fs_perms.write_bytes(target, data, 0o666)  # файлы базы — 0666 (решение №22)
    sid, stem = uuid.uuid4().hex, Path(name).stem
    sessions[sid] = {"session_id": sid, "name": name, "stem": stem, "source": target,
                     "reg": cfg.base_markdown / stem / f"{stem}_reg.yaml",
                     "md": cfg.base_markdown / stem / f"{stem}.md"}
    return {"session_id": sid, "stem": stem}


@app.get("/api/documents/{sid}")
def state(sid):
    s = _session(sid)
    return {**s, "source": str(s["source"]), "reg": str(s["reg"]), "md": str(s["md"]),
            "reg_exists": s["reg"].exists(), "md_exists": s["md"].exists(),
            "can_index": s["reg"].exists() and s["md"].exists() and cfg.write_enabled}


@app.post("/api/documents/{sid}/register")
def register(sid, payload: Registration):
    s = _session(sid)
    fs_perms.ensure_dir(s["reg"].parent)  # каталог базы — 0777 (решение №22)
    slug = registration.write_reg_yaml(s["reg"], {**payload.fields, "source_file": s["name"]})
    return {"slug": slug}

@app.get("/api/documents/{sid}/reg")
def registration_state(sid):
    s = _session(sid)
    if not s["reg"].is_file():
        return {"fields": {}, "exists": False}
    return {"fields": config_ui.read_yaml(s["reg"]), "exists": True}

@app.post("/api/documents/{sid}/register/prefill")
def registration_prefill(sid):
    """Prefill формы регистрации (решение №28).

    Если `<stem>_reg.yaml` уже существует — вернуть его поля БЕЗ распознавания
    (`source="reg_yaml"`); иначе — vision-prefill первой страницы (`source="vision"`).
    """
    s = _session(sid)
    record = registration.read_reg_record(s["reg"])
    if record is not None:
        fields = dict(record)
        fields["slug"] = registration.read_reg_slug(s["reg"])
        return {"fields": fields, "source": "reg_yaml", "exists": True,
                "image_available": bool(s["source"].is_file())}
    image_bytes = registration.extract_first_page(s["source"])
    fields = registration.vision_prefill(image_bytes, config=cfg.prompts, providers_path=cfg.providers_path, env=build_env(cfg.env_file)) if image_bytes else {}
    fields.setdefault("domain", fields.get("domain_hint"))
    fields["slug"] = registration.make_slug(fields.get("document_id"), fields.get("document_type"), fields.get("domain"), ()) if fields.get("document_id") else None
    return {"fields": fields, "source": "vision", "exists": False, "image_available": bool(image_bytes)}


@app.post("/api/documents/{sid}/convert")
def convert(sid):
    s = _session(sid)
    providers = config_ui.read_yaml(cfg.providers_path)
    env = build_env(cfg.env_file)
    missing = []
    for role in ("table_vision", "ai_postprocess"):
        spec = providers.get("roles", {}).get("create_markdown", {}).get(role)
        if not isinstance(spec, dict) or not spec.get("provider") or not spec.get("model"):
            missing.append(role)
            continue
        provider = providers.get("providers", {}).get(spec["provider"], {})
        if not provider.get("api_key_env") or not env.get(provider["api_key_env"]):
            missing.append(role)
    if missing:
        raise HTTPException(400, "Настройте vision и чат-модель и добавьте API-ключи")
    src = cfg.create_markdown_dir / "create_markdown.py"
    argv = [sys.executable, str(src), "-i", str(s["source"]), "--ai",
            "--config", str(cfg.create_markdown_config_path),
            "--providers-config", str(cfg.providers_path)]
    return runner.start(argv, cwd=str(cfg.create_markdown_dir), env=env, kind="convert").__dict__

@app.post("/api/documents/{sid}/index")
def index_document(sid, _payload: IndexRequest | None = None):
    s = _session(sid)
    if not (s["md"].is_file() and s["reg"].is_file() and cfg.write_enabled):
        raise HTTPException(400, "Завершите предыдущие шаги: требуются .md, _reg.yaml и разрешённая запись в Qdrant")
    rag = [sys.executable, str(cfg.create_markdown_dir / "create_markdown.py"), "-i", str(s["md"]), "--rag",
           "--config", str(cfg.create_markdown_config_path),
           "--providers-config", str(cfg.providers_path)]
    index = [sys.executable, str(cfg.build_search_index_dir / "create_index.py"), str(s["md"].parent),
             "--qdrant-path", str(cfg.qdrant_path.resolve()), "--collection", cfg.collection, "--strict",
             "--providers_config", str(cfg.providers_path)]
    env = build_env(cfg.env_file)
    first = runner.start_sequence([rag, index], cwd=str(cfg.create_markdown_dir), env=env, kind="index")
    return first.__dict__


@app.get("/api/jobs/{job_id}")
def job(job_id):
    try:
        return runner.get(job_id).__dict__
    except KeyError:
        raise HTTPException(404, "Задача не найдена")


@app.post("/api/jobs/{job_id}/stop")
def stop(job_id):
    try:
        return runner.stop(job_id).__dict__
    except KeyError:
        raise HTTPException(404, "Задача не найдена")


@app.get("/api/jobs/{job_id}/events")
def events(job_id):
    q = runner.subscribe(job_id)
    async def stream():
        while True:
            try:
                yield f"data: {json.dumps(q.get(timeout=.5), ensure_ascii=False)}\n\n"
            except Exception:
                if runner.get(job_id).status in {"done", "error", "stopped"}:
                    break
                await asyncio.sleep(.1)
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/files/markdown/{stem}")
def markdown(stem):
    if ".." in stem or "/" in stem or not re.fullmatch(r"[\w.а-яА-ЯёЁ -]+", stem):
        raise HTTPException(404)
    path = (cfg.base_markdown / stem / f"{stem}.md").resolve()
    if cfg.base_markdown.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/documents-in-base")
def documents_in_base():
    try:
        return qdrant_api.distinct_documents(cfg.qdrant_path, cfg.collection)
    except Exception as exc:
        raise HTTPException(503, f"Qdrant недоступен: {exc}") from exc


@app.get("/api/images/{doc_dir}/{rel_path:path}")
def image(doc_dir: str, rel_path: str):
    if Path(doc_dir).name != doc_dir or Path(rel_path).is_absolute():
        raise HTTPException(404)
    base = cfg.base_markdown.resolve()
    path = (base / doc_dir / rel_path).resolve()
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or base not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


# Единый общий конфиг: интерфейс читает/пишет ТОЛЬКО файлы из cfg.config_dir
# (providers.yaml, create_markdown_config.yaml, search_config.yaml, .env). Никаких
# копий в каталогах пайплайнов; пайплайнам конфиги передаются CLI-ключами (§4.9).

@app.get("/api/settings/providers")
def settings_providers():
    return config_ui.read_yaml(cfg.providers_path)

@app.put("/api/settings/providers")
def update_providers(payload: dict):
    config_ui.validate_providers(payload)
    config_ui.write_yaml(cfg.providers_path, payload)
    return payload

@app.put("/api/settings/providers/roles")
def update_provider_roles(payload: dict):
    data = config_ui.read_yaml(cfg.providers_path)
    kind = payload.get("kind")
    config_ui.sync_role_models(data, payload.get("spec", {}), kind)
    config_ui.validate_providers(data)
    config_ui.write_yaml(cfg.providers_path, data)
    return data

@app.post("/api/settings/providers/scan")
def scan_provider(payload: ProviderScan):
    try:
        return {"models": providers_api.scan_and_tag_models(payload.base_url, payload.api_key)}
    except Exception as exc:
        raise HTTPException(502, f"Не удалось получить список моделей: {exc}") from exc

@app.post("/api/settings/providers/add")
def add_provider(payload: ProviderAdd):
    models = [item if isinstance(item, str) else item.name for item in payload.models]
    try:
        return providers_api.add_provider(cfg.providers_path, payload.name, payload.base_url, payload.api_key_env, models)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/api/settings/providers/refresh")
def refresh_providers():
    try:
        return providers_api.refresh_all_models(cfg.providers_path, build_env(cfg.env_file))
    except Exception as exc:
        raise HTTPException(502, f"Не удалось обновить модели: {exc}") from exc

def _config_path(kind):
    return cfg.search_config_path if kind == "search" else cfg.create_markdown_config_path

@app.get("/api/settings/search-config")
def get_search_config(): return config_ui.read_yaml(_config_path("search"))

@app.put("/api/settings/search-config")
def put_search_config(payload: dict):
    config_ui.validate_search_config(payload); config_ui.write_yaml(_config_path("search"), payload); return payload

@app.get("/api/settings/create-markdown-config")
def get_markdown_config(): return config_ui.read_yaml(_config_path("markdown"))

@app.put("/api/settings/create-markdown-config")
def put_markdown_config(payload: dict):
    config_ui.write_yaml(_config_path("markdown"), payload); return payload

@app.get("/api/settings/env")
def get_env_config(): return config_ui.read_env(cfg.env_file)

@app.put("/api/settings/env")
def put_env_config(payload: EnvUpdate):
    # Значения, которые выглядят как маска (••••…), приходят из UI без изменений —
    # их нельзя записывать, иначе реальный ключ затрётся маской (§9.5, решение №15).
    values = {k: v for k, v in payload.values.items() if v and not v.startswith("••••")}
    values.update({key: "" for key in payload.delete})
    config_ui.write_env(cfg.env_file, values)
    return config_ui.read_env(cfg.env_file)

@app.get("/api/settings/collections")
def settings_collections(): return {"collections": qdrant_api.list_collections(cfg.qdrant_path)}

@app.get("/api/settings/qdrant")
def settings_qdrant():
    """Текущая папка Qdrant (решение №29): эффективный путь, дефолт, override."""
    return {
        "path": str(cfg.qdrant_path),
        "default": str(cfg.qdrant_path_default),
        "overridden": cfg.qdrant_path_override is not None,
    }

@app.put("/api/settings/qdrant")
def update_qdrant(payload: QdrantPathUpdate):
    """Задать папку Qdrant персистентно (QDRANT_PATH в .env).

    Пустая строка = сбросить override (вернуться к дефолту из config.yaml).
    Запись идёт через config_ui.write_env → .env остаётся 0600.
    """
    path = (payload.path or "").strip()
    if path and not Path(path).is_absolute():
        raise HTTPException(400, "Путь должен быть абсолютным")
    config_ui.write_env(cfg.env_file, {"QDRANT_PATH": path})
    return settings_qdrant()

@app.get("/api/settings/status")
def settings_status():
    providers = config_ui.read_yaml(cfg.providers_path)
    roles = providers.get("roles", {})
    required = (("create_markdown", "table_vision"), ("create_markdown", "ai_postprocess"), ("create_markdown", "registration_vision"), ("build_search_index", "query_processing"), ("build_search_index", "embedding"), ("build_search_index", "rerank"))
    return {"environment": cfg.environment, "write_enabled": cfg.write_enabled, "roles": {f"{p}.{r}": bool(roles.get(p, {}).get(r)) for p, r in required}}

@app.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    await websocket.accept()
    session = ChatSession(cfg)
    try:
        while True:
            message = await websocket.receive_json()
            kind, text = message.get("type"), str(message.get("text", "")).strip()
            if kind not in {"query", "reply"} or not text:
                await websocket.send_json({"type": "error", "text": "Ожидается непустой текст"})
                continue
            if kind == "reply":
                result = await asyncio.to_thread(session.resume, text)
                await websocket.send_json(result)
                continue
            updates = await asyncio.to_thread(lambda: list(session.stream(text)))
            state = {}
            interrupted = False
            for update in updates:
                for node, values in update.items():
                    if node == "__interrupt__":
                        interrupts = values
                        value = getattr(interrupts[0], "value", interrupts[0])
                        await websocket.send_json({"type": "clarification", "text": str(value)})
                        interrupted = True
                        continue
                    await websocket.send_json({"type": "node", "node": node})
                    if isinstance(values, dict):
                        state.update(values)
            if not interrupted:
                result = session._format(state)
                await websocket.send_json(result)
    except Exception:
        await websocket.close()
