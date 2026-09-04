from __future__ import annotations
import json, os, re, subprocess, tempfile
from datetime import date
from pathlib import Path
import yaml

try:  # supports both documented package and legacy module invocation
    from firmware.src import fs_perms
except ImportError:  # pragma: no cover - direct module usage from firmware/src
    import fs_perms

FIELDS = ["document_id", "document_id_alt", "document_type", "domain", "title", "edition", "date_enacted", "date_amended", "amended_by", "source_file", "status", "status_reason", "replaced_by_document_id", "replaced_by_doc_key", "ignore_sections"]
_TRANSLIT = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    "А":"A","Б":"B","В":"V","Г":"G","Д":"D","Е":"E","Ё":"E","Ж":"Zh","З":"Z","И":"I","Й":"Y","К":"K","Л":"L","М":"M","Н":"N","О":"O","П":"P","Р":"R","С":"S","Т":"T","У":"U","Ф":"F","Х":"H","Ц":"C","Ч":"Ch","Ш":"Sh","Щ":"Sch","Ъ":"","Ы":"Y","Ь":"","Э":"E","Ю":"Yu","Я":"Ya"
})
def make_slug(document_id, existing=()):
    """Слаг-ключ из обозначения документа (решение №47): транслитерация
    обозначения целиком. Пример: «ГОСТ 18410—73» → `GOST_18410_73`;
    «СП 297.1325800.2017» → `SP_297_1325800_2017`."""
    text = (document_id or "").translate(_TRANSLIT)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    if not slug:
        slug = "DOC"
    base = slug; i = 2
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

_DATE_RE = re.compile(
    r"(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})"
    r"|(?P<d2>\d{1,2})\.(?P<mo2>\d{1,2})\.(?P<y2>\d{4})"
    r"|(?P<d3>\d{1,2})\.(?P<mo3>\d{1,2})\.(?P<yy>\d{2})")

def normalize_date(value):
    """Распознанную дату → строка `ГГГГ-ММ-ДД` (для input[type=date]) или None.

    Понимает `ГГГГ-ММ-ДД`, `ДД.ММ.ГГГГ` и `ДД.ММ.ГГ` (ГГ<50 → 20xx, иначе 19xx);
    дату ищет внутри произвольного текста (титул редко даёт чистый формат).
    Некорректная дата (например 32.13.2020) → None (решение №48).
    """
    if not value:
        return None
    m = _DATE_RE.search(str(value))
    if not m:
        return None
    try:
        if m.group("y"):
            y, mo, d = int(m.group("y")), int(m.group("mo")), int(m.group("d"))
        elif m.group("y2"):
            d, mo, y = int(m.group("d2")), int(m.group("mo2")), int(m.group("y2"))
        else:
            d, mo, yy = int(m.group("d3")), int(m.group("mo3")), int(m.group("yy"))
            y = 2000 + yy if yy < 50 else 1900 + yy
        date(y, mo, d)  # валидация календарной даты
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"

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
        prompt = (config or {}).get("registration_vision", "Извлеки JSON: document_id, title, domain_hint, document_type, date_enacted (дата введения в действие). Если неизвестно — null.")
        raw = llm_client.vision_completion(spec, image_bytes, prompt, key)
        match = re.search(r"\{.*\}", raw, re.S)
        result = json.loads(match.group(0) if match else raw)
        fields = {key: result.get(key) for key in ("document_id", "title", "document_type", "domain_hint", "date_enacted")}
        fields["date_enacted"] = normalize_date(fields.get("date_enacted"))
        return fields
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
        slug = make_slug(fields.get("document_id"), docs)
    record = {key: fields.get(key) for key in FIELDS}
    record["source_file"] = fields.get("source_file", reg_path.stem.removesuffix("_reg"))
    record["status"] = fields.get("status", "active")
    record["ignore_sections"] = fields.get("ignore_sections", ["Предисловие", "Содержание"])
    data["documents"] = {slug: record}  # ровно одна запись на документ
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    yaml.safe_load(text)  # валидация перед атомарной записью (как раньше)
    fs_perms.write_text_atomic(reg_path, text, 0o666)
    return slug
