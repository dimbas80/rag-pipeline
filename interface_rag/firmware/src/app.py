from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
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
    from firmware.src import config_ui, registration, providers_api, fs_perms, bot_service
    from firmware.src.chat_api import ChatSession
    from firmware.src import qdrant_api
except ImportError:  # pragma: no cover - only for direct `uvicorn app:app`
    from deploy_config import load
    from jobs import JobRunner, build_env
    import config_ui, registration, providers_api, fs_perms, bot_service
    from chat_api import ChatSession
    import qdrant_api

cfg = load()
app = FastAPI(title="interface_RAG")
runner = JobRunner()
_SENTINEL = object()  # маркер «генератор исчерпан» для потоковой отдачи чата
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

class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    # Решение №52: теги модели — «chat», «chat+vision» или список тегов.
    models: dict[str, list[str] | str] | None = None

class EnvUpdate(BaseModel):
    values: dict[str, str] = {}
    delete: list[str] = []

class DocumentTypeAdd(BaseModel):
    type: str

class MarkdownUpdate(BaseModel):
    content: str

class QdrantPathUpdate(BaseModel):
    path: str = ""

class BaseDirUpdate(BaseModel):
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
    suffix = Path(name).suffix.lower()
    if name != file.filename or suffix not in {".pdf", ".docx", ".doc", ".md"}:
        raise HTTPException(400, "Недопустимый файл")
    if len(name) > 255:
        raise HTTPException(400, "Слишком длинное имя")
    limit = cfg.upload_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "Файл превышает допустимый размер")
    stem = Path(name).stem
    md_path = cfg.base_markdown / stem / f"{stem}.md"
    if suffix == ".md":
        # Решение 39: .md кладём сразу в канонический Markdown/<stem>/<stem>.md,
        # НЕ в корень upload_base_dir (не оставляем <stem>.md/<stem>_ai.md в корне базы).
        # Если файл уже в базе — НЕ пересохраняем (переиндексация существующего).
        if md_path.exists():
            source = md_path          # существующий .md — индексируем его как есть
        else:
            fs_perms.ensure_dir(md_path.parent)          # 0777 (решение №22)
            fs_perms.write_bytes(md_path, data, 0o666)   # 0666 (решение №22)
            source = md_path
    else:
        fs_perms.ensure_dir(cfg.upload_base_dir)
        source = cfg.upload_base_dir / name
        fs_perms.write_bytes(source, data, 0o666)  # файлы базы — 0666 (решение №22)
    sid = uuid.uuid4().hex
    sessions[sid] = {"session_id": sid, "name": name, "stem": stem, "source": source,
                     "reg": cfg.base_markdown / stem / f"{stem}_reg.yaml", "md": md_path}
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
    slug = str(payload.fields.get("slug") or "")
    if slug and not re.fullmatch(r"[A-Za-z0-9_]+", slug):
        raise HTTPException(400, "Слаг может содержать только латинские буквы, цифры и знак подчёркивания")
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
    fields["slug"] = registration.make_slug(fields.get("document_id")) if fields.get("document_id") else None
    return {"fields": fields, "source": "vision", "exists": False, "image_available": bool(image_bytes)}


@app.post("/api/documents/{sid}/convert")
def convert(sid):
    s = _session(sid)
    if Path(s["name"]).suffix.lower() == ".md":
        # Решение 39: .md уже лежит в Markdown/<stem>/<stem>.md (загружен на шаге upload).
        # OCR/AI-постобработка не требуется; пересохранение не выполняется. Возвращаем
        # мгновенно-завершённую job (единая инфраструктура SSE/лога), затем index.
        return runner.start(
            [sys.executable, "-c",
             "print('md уже готов — конвертация не требуется (переиндексация без пересохранения)')"],
            kind="convert").__dict__
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


_STEM_RE = re.compile(r"^[\w.а-яА-ЯёЁ -]+$")


def _valid_stem(stem: str) -> bool:
    """stem валиден, если проходит regex, не начинается с '.' (скрытые каталоги)
    и не содержит хода вверх по дереву (§13.1)."""
    return (bool(stem) and not stem.startswith(".")
            and ".." not in stem and "/" not in stem and "\\" not in stem
            and bool(_STEM_RE.fullmatch(stem)))


def _delete_result(cfg, stem: str) -> dict:
    """Удалить промежуточные артефакты конвертации текущего документа (решение №40).

    Удаляются ровно: <base_dir>/tmp/<stem>/, <base_markdown>/<stem>/image/,
    <base_markdown>/<stem>/table_images.json. Сохраняются .md, _reg.yaml,
    _chunks.jsonl, _assets.json и исходник. Каждый путь resolve()'ится и
    проверяется на префикс разрешённого корня до удаления.
    """
    if not _valid_stem(stem):
        raise ValueError("Некорректное имя документа")
    deleted: list[str] = []
    removed = 0

    def _under(base: Path, path: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False

    tmp_root = (cfg.base_dir / "tmp").resolve()
    md_root = Path(cfg.base_markdown).resolve()
    targets = [
        ("dir", cfg.base_dir / "tmp" / stem, tmp_root),
        ("dir", Path(cfg.base_markdown) / stem / "image", md_root),
        ("file", Path(cfg.base_markdown) / stem / "table_images.json", md_root),
    ]
    for kind, raw, root in targets:
        path = raw.resolve()
        if not _under(root, path) or path == root:
            raise ValueError(f"Путь вне разрешённого каталога: {path}")
        if kind == "dir" and path.is_dir():
            shutil.rmtree(path)
            deleted.append(str(path))
            removed += 1
        elif kind == "file" and path.is_file():
            path.unlink()
            deleted.append(str(path))
            removed += 1
    doc_dir = Path(cfg.base_markdown) / stem
    kept = {
        "md": str(doc_dir / f"{stem}.md") if (doc_dir / f"{stem}.md").is_file() else None,
        "reg": str(doc_dir / f"{stem}_reg.yaml") if (doc_dir / f"{stem}_reg.yaml").is_file() else None,
        "chunks": str(doc_dir / f"{stem}_chunks.jsonl") if (doc_dir / f"{stem}_chunks.jsonl").is_file() else None,
        "assets": str(doc_dir / f"{stem}_assets.json") if (doc_dir / f"{stem}_assets.json").is_file() else None,
    }
    message = f"Удалено объектов: {removed}" if removed else "Нечего удалять — промежуточных артефактов нет"
    return {"deleted": deleted, "removed": removed, "kept": kept, "message": message}


@app.post("/api/documents/{sid}/delete-result")
def delete_result(sid):
    s = _session(sid)
    try:
        return _delete_result(cfg, s["stem"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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


def _markdown_path(stem: str) -> Path:
    """Валидированный путь `<base_markdown>/<stem>/<stem>.md` (GET и PUT, §13.1)."""
    if ".." in stem or "/" in stem or not re.fullmatch(r"[\w.а-яА-ЯёЁ -]+", stem):
        raise HTTPException(404)
    path = (cfg.base_markdown / stem / f"{stem}.md").resolve()
    if cfg.base_markdown.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404)
    return path


@app.get("/api/files/markdown/{stem}")
def markdown(stem):
    return FileResponse(_markdown_path(stem))


@app.put("/api/files/markdown/{stem}")
def update_markdown(stem, payload: MarkdownUpdate):
    """Сохранить отредактированный .md из текстового редактора UI (решение №51).

    Права 0666 — как у файлов базы; после правки требуется переиндексация
    (клиент показывает напоминание).
    """
    path = _markdown_path(stem)
    fs_perms.write_text_atomic(path, payload.content, 0o666)
    return {"ok": True, "bytes": len(payload.content.encode("utf-8"))}


@app.get("/api/documents-in-base")
def documents_in_base():
    try:
        return qdrant_api.distinct_documents(cfg.qdrant_path, cfg.collection)
    except Exception as exc:
        raise HTTPException(503, f"Qdrant недоступен: {exc}") from exc


@app.delete("/api/documents-in-base/{document_id}")
def delete_document_from_base(document_id: str):
    """Удалить все чанки документа из Qdrant (вкладка «Документы в базе»).

    Файловые артефакты (.md, _reg.yaml) сохраняются — документ можно
    переиндексировать. Запись в Qdrant gated тем же write_enabled, что и
    индексация.
    """
    if not cfg.write_enabled:
        raise HTTPException(409, "Запись в Qdrant запрещена настройкой (qdrant.write_enabled)")
    try:
        deleted = qdrant_api.delete_document(cfg.qdrant_path, cfg.collection, document_id)
    except Exception as exc:
        raise HTTPException(503, f"Qdrant недоступен: {exc}") from exc
    return {"document_id": document_id, "deleted": deleted}


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

# --- Провайдеры: per-provider операции (итерация 5, решения 42–45). ---
# PUT/DELETE /roles и /refresh зарегистрированы выше → приоритет над {name}.

@app.put("/api/settings/providers/{name}")
def update_provider_settings(name: str, payload: ProviderUpdate):
    try:
        return providers_api.update_provider(cfg.providers_path, name, payload.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(404, "Провайдер не найден")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.delete("/api/settings/providers/{name}")
def delete_provider_settings(name: str):
    try:
        providers_api.delete_provider(cfg.providers_path, name)
    except KeyError:
        raise HTTPException(404, "Провайдер не найден")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": name}

@app.post("/api/settings/providers/{name}/refresh")
def refresh_provider_settings(name: str):
    try:
        return providers_api.refresh_provider(cfg.providers_path, name, build_env(cfg.env_file))
    except KeyError:
        raise HTTPException(404, "Провайдер не найден")

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
    if path:
        # Нормализация после проверки абсолютности: /tmp/../etc → /etc, чтобы
        # путь с компонентами «..» не сохранялся в .env как есть.
        path = str(Path(path).resolve())
    config_ui.write_env(cfg.env_file, {"QDRANT_PATH": path})
    return settings_qdrant()

@app.get("/api/settings/base-dir")
def settings_base_dir():
    """Корневая папка документов (BASE_DIR): эффективный путь, дефолт, override."""
    return {
        "path": str(cfg.base_dir),
        "default": str(cfg.upload_base_dir),
        "overridden": cfg.base_dir_override is not None,
    }

@app.put("/api/settings/base-dir")
def update_base_dir(payload: BaseDirUpdate):
    """Задать корневую папку документов персистентно (BASE_DIR в .env).

    Пустая строка = сбросить override (вернуться к дефолту из config.yaml).
    Запись идёт через config_ui.write_env → .env остаётся 0600.
    """
    path = (payload.path or "").strip()
    if path and not Path(path).is_absolute():
        raise HTTPException(400, "Путь должен быть абсолютным")
    if path:
        path = str(Path(path).resolve())
    config_ui.write_env(cfg.env_file, {"BASE_DIR": path})
    return settings_base_dir()

@app.get("/api/fs/dirs")
def fs_dirs(path: str = "/"):
    """Список подкаталогов сервера для выбора папок в UI (машина интерфейса,
    не машина браузера). Отдаются только имена каталогов, скрытые (с точкой)
    не показываются; содержимое файлов не читается."""
    target = (path or "/").strip() or "/"
    if not Path(target).is_absolute():
        raise HTTPException(400, "Путь должен быть абсолютным")
    resolved = Path(target).resolve()
    if not resolved.exists():
        raise HTTPException(404, "Каталог не существует")
    if not resolved.is_dir():
        raise HTTPException(400, "Это не каталог")
    entries = [
        {"name": child.name, "path": str(child)}
        for child in sorted(resolved.iterdir(), key=lambda p: p.name)
        if child.is_dir() and not child.name.startswith(".")
    ]
    parent = str(resolved.parent) if resolved.parent != resolved else None
    return {"path": str(resolved), "parent": parent, "entries": entries}

def _tmp_root() -> Path:
    return (cfg.base_dir / "tmp").resolve()

def _dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

@app.get("/api/settings/tmp")
def tmp_settings():
    """Текущий размер папки временных файлов <BASE_DIR>/tmp (для раздела «Папки»)."""
    root = _tmp_root()
    return {"path": str(root), "size_bytes": _dir_size_bytes(root)}

@app.post("/api/settings/tmp/clear")
def tmp_clear():
    """Удалить всё содержимое <BASE_DIR>/tmp (сама папка остаётся).

    Удаляются только дети tmp-каталога — соседние файлы и сам корень не трогаются.
    """
    root = _tmp_root()
    freed = _dir_size_bytes(root)
    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return {"path": str(root), "freed_bytes": freed, "size_bytes": 0}

# --- Типы документов (решение №50): datalist регистрации, пользовательские дополнения. ---

def _read_document_types() -> list[str]:
    data = config_ui.read_yaml(cfg.document_types_path)
    types = data.get("types") if isinstance(data, dict) else None
    return [str(t) for t in types] if isinstance(types, list) else []


@app.get("/api/settings/document-types")
def get_document_types():
    return {"types": _read_document_types()}


@app.post("/api/settings/document-types")
def add_document_type(payload: DocumentTypeAdd):
    value = (payload.type or "").strip()
    if not value or len(value) > 40:
        raise HTTPException(400, "Некорректный тип документа")
    types = _read_document_types()
    if value not in types:
        types.append(value)
        config_ui.write_yaml(cfg.document_types_path, {"types": types})
    return {"types": types}


@app.get("/api/settings/status")
def settings_status():
    providers = config_ui.read_yaml(cfg.providers_path)
    roles = providers.get("roles", {})
    required = (("create_markdown", "table_vision"), ("create_markdown", "ai_postprocess"), ("create_markdown", "registration_vision"), ("build_search_index", "query_processing"), ("build_search_index", "embedding"), ("build_search_index", "rerank"))
    return {"environment": cfg.environment, "write_enabled": cfg.write_enabled, "roles": {f"{p}.{r}": bool(roles.get(p, {}).get(r)) for p, r in required}}


def _bot_token() -> str | None:
    return config_ui.read_env_raw(cfg.env_file).get("TELEGRAM_BOT_TOKEN") or None


@app.get("/api/settings/telegram/bot")
def telegram_bot_status():
    """Статус interface-rag-bot.service + связность с Telegram API (getMe)."""
    return bot_service.status(_bot_token())


@app.post("/api/settings/telegram/bot/restart")
def restart_telegram_bot():
    """Перезапустить сервис бота; свежий статус — в ответе."""
    try:
        result = bot_service.restart(_bot_token())
    except Exception as exc:
        raise HTTPException(502, f"Не удалось перезапустить бота: {exc}") from exc
    if not result.get("available"):
        raise HTTPException(503, result.get("reason") or "systemd-юнит бота недоступен")
    return result

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
            await _stream_query(websocket, session, text)
    except Exception:
        await websocket.close()


async def _stream_query(websocket: WebSocket, session: ChatSession, text: str) -> None:
    """Потоковая отдача node-событий клиенту по мере выполнения графа.

    session.stream(text) — ленивый sync-генератор LangGraph: каждый next()
    выполняет ровно один узел (LLM-вызов / поиск Qdrant — блокирующий),
    поэтому он крутится в executor-потоке. asyncio.Queue НЕ потокобезопасна,
    значит чанки маршалируются в event-loop через
    loop.call_soon_threadsafe(q.put_nowait, ...), а единственный потребитель —
    await q.get() на loop-потоке (вариант A, архитектура t_b551380a §3-4).

    Семантика сохранена: `__interrupt__` → clarification (финальный ответ не
    шлётся); иначе в конце шлётся session._format(state) с накопленным state.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()   # трогается ТОЛЬКО loop-потоком
    stop = threading.Event()             # «клиент ушёл» → producer прекращает тянуть

    def produce() -> None:
        gen = session.stream(text)       # ленивый sync-генератор LangGraph
        try:
            for update in gen:
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(q.put_nowait, update)
        except Exception as exc:         # узел графа бросил → item-исключение в очередь
            loop.call_soon_threadsafe(q.put_nowait, exc)
        finally:
            gen.close()                  # корректно оборвать генератор (finally-цепочку)
            loop.call_soon_threadsafe(q.put_nowait, _SENTINEL)

    producer = loop.run_in_executor(None, produce)

    state: dict = {}
    interrupted = False
    try:
        while True:
            item = await q.get()
            if item is _SENTINEL:
                break
            if isinstance(item, Exception):
                # (A) ошибка графа: сообщить клиенту (снять «Думаю…»), затем пробросить
                try:
                    await websocket.send_json({"type": "error", "text": str(item)})
                except Exception:
                    pass
                raise item
            for node, values in item.items():
                if node == "__interrupt__":
                    await websocket.send_json(
                        {"type": "clarification",
                         "text": str(getattr(values[0], "value", values[0]))})
                    interrupted = True
                    continue
                await websocket.send_json({"type": "node", "node": node})
                if isinstance(values, dict):
                    state.update(values)
        if not interrupted:
            await websocket.send_json(session._format(state))
    except Exception:
        # (C) send_json бросил (клиент ушёл) / проброшенная ошибка графа:
        # оборвать поток, не ждать producer (LLM-вызов может идти долго)
        stop.set()
        producer.cancel()
        raise
    finally:
        if not stop.is_set():
            await producer  # нормальное завершение — джойн потока (уже вернулся)
