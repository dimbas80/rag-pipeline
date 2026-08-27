from __future__ import annotations
import re, shutil, tempfile
from pathlib import Path
import yaml

FIELDS = ["document_id", "document_id_alt", "document_type", "domain", "title", "edition", "date_enacted", "date_amended", "amended_by", "source_file", "status", "status_reason", "replaced_by_document_id", "replaced_by_doc_key", "ignore_sections"]
PREFIX = {"ГОСТ":"GOST", "СП":"SP", "СО":"SO", "СНиП":"SNIP", "ПУЭ":"PUE"}
def make_slug(document_id, document_type, domain, existing=()):
    prefix = PREFIX.get(document_type or "", re.sub(r"\W+", "_", document_type or "DOC"))
    num = re.search(r"\d+", document_id or "")
    tail = re.sub(r"[^a-z0-9]+", "_", (domain or "").lower().replace("кабели", "kabel")).strip("_")
    base = f"{prefix}_{num.group(0) if num else 'DOC'}" + (f"_{tail}" if tail else "")
    slug = base; i = 2
    while slug in existing: slug = f"{base}_{i}"; i += 1
    return slug

def extract_first_page(source_file):
    try:
        import fitz
        with fitz.open(source_file) as doc:
            if not doc: return None
            return doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png")
    except Exception: return None

def write_reg_yaml(reg_path, fields):
    reg_path = Path(reg_path); reg_path.parent.mkdir(parents=True, exist_ok=True)
    if reg_path.exists(): shutil.copy2(reg_path, reg_path.with_name(reg_path.name + ".bak"))
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8")) if reg_path.exists() else {}
    data = data if isinstance(data, dict) else {}; docs = data.setdefault("documents", {})
    slug = fields.get("slug") or make_slug(fields.get("document_id"), fields.get("document_type"), fields.get("domain"), docs)
    record = {key: fields.get(key) for key in FIELDS}; record["source_file"] = fields.get("source_file", reg_path.stem.removesuffix("_reg")); record["status"] = fields.get("status", "active"); record["ignore_sections"] = fields.get("ignore_sections", ["Предисловие", "Содержание"])
    docs[slug] = record
    tmp = reg_path.with_suffix(reg_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"); yaml.safe_load(tmp.read_text(encoding="utf-8")); tmp.replace(reg_path)
    return slug
