from __future__ import annotations
import asyncio, json, re, shutil, sys, uuid
from pathlib import Path
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from .deploy_config import load
from .jobs import JobRunner
from . import config_ui, registration

cfg = load(); app = FastAPI(title="interface_RAG"); runner = JobRunner(); sessions = {}
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
class Registration(BaseModel): fields: dict

@app.get("/", response_class=HTMLResponse)
def index(): return FileResponse(Path(__file__).parent / "static/index.html")
@app.post("/api/documents")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    if name != file.filename or Path(name).suffix.lower() not in {".pdf", ".docx", ".doc", ".md"}: raise HTTPException(400, "Недопустимый файл")
    if len(name) > 255: raise HTTPException(400, "Слишком длинное имя")
    cfg.upload_base_dir.mkdir(parents=True, exist_ok=True); target = cfg.upload_base_dir / name
    target.write_bytes(await file.read()); sid = uuid.uuid4().hex; stem = Path(name).stem
    sessions[sid] = {"session_id":sid,"name":name,"stem":stem,"source":target,"reg":cfg.base_markdown/stem/f"{stem}_reg.yaml","md":cfg.base_markdown/stem/f"{stem}.md"}
    return {"session_id": sid, "stem": stem}
@app.get("/api/documents/{sid}")
def state(sid):
    s = sessions.get(sid) or (_ for _ in ()).throw(HTTPException(404, "Сессия не найдена"))
    return {**s, "source": str(s["source"]), "reg": str(s["reg"]), "md": str(s["md"]), "can_index": s["reg"].exists() and s["md"].exists() and cfg.write_enabled}
@app.post("/api/documents/{sid}/register")
def register(sid, payload: Registration):
    s = sessions.get(sid)
    if not s: raise HTTPException(404, "Сессия не найдена")
    s["reg"].parent.mkdir(parents=True, exist_ok=True); slug = registration.write_reg_yaml(s["reg"], {**payload.fields, "source_file": s["name"]}); return {"slug": slug}
@app.post("/api/documents/{sid}/convert")
def convert(sid):
    s = sessions.get(sid)
    if not s: raise HTTPException(404, "Сессия не найдена")
    src = cfg.create_markdown_dir / "create_markdown.py"; argv=[sys.executable,str(src),"-i",str(s["source"]),"--ai","--config",str(cfg.create_markdown_dir/"create_markdown_config.yaml"),"--providers-config",str(cfg.create_markdown_dir/"providers.yaml")]
    return runner.start(argv, cwd=str(cfg.create_markdown_dir), kind="convert").__dict__
@app.get("/api/jobs/{job_id}")
def job(job_id): return runner.get(job_id).__dict__
@app.post("/api/jobs/{job_id}/stop")
def stop(job_id): return runner.stop(job_id).__dict__
@app.get("/api/jobs/{job_id}/events")
def events(job_id):
    q=runner.subscribe(job_id)
    async def stream():
        while True:
            try: yield f"data: {q.get(timeout=0.5)}\n\n"
            except Exception:
                if runner.get(job_id).status in {"done","error","stopped"}: break
                await asyncio.sleep(.1)
    return StreamingResponse(stream(), media_type="text/event-stream")
@app.get("/api/files/markdown/{stem}")
def markdown(stem):
    if ".." in stem or "/" in stem or not re.fullmatch(r"[\w.а-яА-ЯёЁ -]+", stem): raise HTTPException(404)
    path=(cfg.base_markdown/stem/f"{stem}.md").resolve()
    if cfg.base_markdown.resolve() not in path.parents or not path.is_file(): raise HTTPException(404)
    return FileResponse(path)
