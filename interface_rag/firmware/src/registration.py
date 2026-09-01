from __future__ import annotations
import json, os, re, subprocess, tempfile
from pathlib import Path
import yaml

try:  # supports both documented package and legacy module invocation
    from firmware.src import fs_perms
except ImportError:  # pragma: no cover - direct module usage from firmware/src
    import fs_perms

FIELDS = ["document_id", "document_id_alt", "document_type", "domain", "title", "edition", "date_enacted", "date_amended", "amended_by", "source_file", "status", "status_reason", "replaced_by_document_id", "replaced_by_doc_key", "ignore_sections"]
PREFIX = {"ГОСТ":"GOST", "СП":"SP", "СО":"SO", "СНиП":"SNIP", "ПУЭ":"PUE"}
_TRANSLIT = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"
})
def make_slug(document_id, document_type, domain, existing=()):
    prefix = PREFIX.get(document_type or "", re.sub(r"\W+", "_", document_type or "DOC"))
    num = re.search(r"\d+", document_id or "")
    tail = re.sub(r"[^a-z0-9]+", "_", (domain or "").lower().translate(_TRANSLIT)).strip("_")
    base = f"{prefix}_{num.group(0) if num else 'DOC'}" + (f"_{tail}" if tail else "")
    slug = base; i = 2
    while slug in existing: slug = f"{base}_{i}"; i += 1
    return slug

def extract_first_page(source_file):
    source_file = Path(source_file)
    if source_file.suffix.lower() in {".doc", ".docx"}:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source_file)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                converted = Path(tmp) / (source_file.stem + ".pdf")
                return extract_first_page(converted)
        except Exception:
            return None
    try:
        import fitz
        with fitz.open(source_file) as doc:
            if not doc: return None
            return doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png")
    except Exception: return None

def vision_prefill(image_bytes, *, config=None, providers_path=None, env=None):
    """Extract registration fields through the configured registration vision role."""
    try:
        from firmware.src import config_ui, llm_client
    except ImportError:
        import config_ui, llm_client
    try:
        providers = config_ui.read_yaml(providers_path)
        spec = llm_client.resolve_role(providers, "create_markdown", "registration_vision")
        key = (env or os.environ).get(spec.get("api_key_env", ""), "")
        prompt = (config or {}).get("registration_vision", "Извлеки JSON: document_id, title, domain_hint, document_type. Если неизвестно — null.")
        raw = llm_client.vision_completion(spec, image_bytes, prompt, key)
        match = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(match.group(0) if match else raw)
        return {key: result.get(key) for key in ("document_id", "title", "document_type", "domain_hint")}
    except Exception:
        return {}

def _load_reg_docs(reg_path):
    """Прочитать `<stem>_reg.yaml` → (slug, record) единственной записи.

    Возвращает (None, None), если файла нет/пусто/структура не та.
    """
    reg_path = Path(reg_path)
    if not reg_path.is_file():
        return None, None
    try:
        data = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    docs = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(docs, dict) or not docs:
        return None, None
    slug = next(iter(docs))
    record = docs[slug]
    if not isinstance(record, dict):
        return None, None
    return slug, record


def read_reg_record(reg_path) -> dict | None:
    """Плоская запись documents.<first-slug> или None (файла нет/пусто).

    Используется prefill'ом (решение №28): если `_reg.yaml` уже существует —
    вернуть его поля БЕЗ распознавания первой страницы.
    """
    _, record = _load_reg_docs(reg_path)
    return dict(record) if record is not None else None


def read_reg_slug(reg_path) -> str | None:
    """Ключ-слаг единственной записи `_reg.yaml` (или None)."""
    slug, _ = _load_reg_docs(reg_path)
    return slug


def write_reg_yaml(reg_path, fields):
    """Upsert ровно одной записи документа в `<stem>_reg.yaml` (решение №24).

    - ключ-слаг СОХРАНЯЕТСЯ при повторной регистрации (стабильность chunk_id);
    - в файле всегда ровно одна запись `documents.<slug>` (исторические дубли
      схлопываются в первый ключ);
    - каталог — 0777, файл и `.bak` — 0666 (решение №22, через fs_perms).
    """
    reg_path = Path(reg_path)
    fs_perms.ensure_dir(reg_path.parent)
    if reg_path.exists():
        fs_perms.chmod_copy(reg_path, reg_path.with_name(reg_path.name + ".bak"), 0o666)
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}
    data = data if isinstance(data, dict) else {}
    docs = data.get("documents")
    docs = docs if isinstance(docs, dict) else {}
    # Upsert: существующий slug (из формы/префилла или первый ключ файла)
    # важнее, чем заново вычисленный — иначе ломаются chunk_id в Qdrant.
    if fields.get("slug"):
        slug = str(fields["slug"])
    elif docs:
        slug = next(iter(docs))
    else:
        slug = make_slug(fields.get("document_id"), fields.get("document_type"), fields.get("domain"), docs)
    record = {key: fields.get(key) for key in FIELDS}
    record["source_file"] = fields.get("source_file", reg_path.stem.removesuffix("_reg"))
    record["status"] = fields.get("status", "active")
    record["ignore_sections"] = fields.get("ignore_sections", ["Предисловие", "Содержание"])
    data["documents"] = {slug: record}  # ровно одна запись на документ
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    yaml.safe_load(text)  # валидация перед атомарной записью (как раньше)
    fs_perms.write_text_atomic(reg_path, text, 0o666)
    return slug
