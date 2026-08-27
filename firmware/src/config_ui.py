from __future__ import annotations
import os, re, tempfile, shutil
from pathlib import Path
import yaml

SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.I)
def mask_key(value: str | None) -> str:
    if not value: return ""
    return "••••" + value[-4:]

def read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

def write_yaml(path, data, comments_preserving=False):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists(): shutil.copy2(path, path.with_name(path.name + ".bak"))
    content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        f.write(content); tmp = Path(f.name)
    try:
        yaml.safe_load(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def read_env(path):
    result = {}
    p = Path(path)
    if not p.exists(): return result
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1); result[key.strip()] = mask_key(value.strip().strip('"'))
    return result

def read_env_raw(path):
    """Read .env values for trusted child-process environment injection."""
    result = {}
    p = Path(path)
    if not p.exists(): return result
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result

def write_env(path, values):
    path = Path(path); current = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); current[k] = v
            elif line: current[line] = None
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    # Empty values are an explicit deletion request; omitted keys are retained.
    current.update({str(k): v for k, v in values.items()})
    for key, value in list(current.items()):
        if value == "":
            current.pop(key)

    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    text = "\n".join(f"{k}={v}" for k, v in current.items() if v is not None) + "\n"
    tmp.write_text(text, encoding="utf-8"); os.replace(tmp, path)


def validate_providers(data):
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        raise ValueError("providers обязана быть объектом")
    seen = set()
    for name, provider in data["providers"].items():
        if not isinstance(provider, dict) or not isinstance(provider.get("base_url"), str):
            raise ValueError(f"Некорректный провайдер: {name}")
        if not re.match(r"^https?://[^\s]+$", provider["base_url"]):
            raise ValueError(f"Некорректный base_url: {name}")
        env_name = provider.get("api_key_env")
        if not isinstance(env_name, str) or not env_name:
            raise ValueError(f"Отсутствует api_key_env: {name}")
        if env_name in seen:
            raise ValueError(f"api_key_env уже используется: {env_name}")
        seen.add(env_name)
        if not isinstance(provider.get("models"), dict):
            raise ValueError(f"Некорректные models: {name}")
    return data


def validate_search_config(data):
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("nodes обязана быть непустым объектом")
    for name, node in nodes.items():
        if not isinstance(node, dict) or not isinstance(node.get("temperature"), (int, float)) or isinstance(node.get("temperature"), bool):
            raise ValueError(f"Некорректная temperature: {name}")
        if not isinstance(node.get("max_tokens"), int) or isinstance(node.get("max_tokens"), bool) or node["max_tokens"] <= 0:
            raise ValueError(f"Некорректный max_tokens: {name}")
    return data


def validate_reg_record(record):
    required = ("document_id", "title", "document_type", "domain", "edition", "date_enacted", "source_file")
    if not isinstance(record, dict) or any(not record.get(key) for key in required):
        raise ValueError("Регистрационная запись не содержит обязательные поля")
    return record


def sync_role_models(providers, spec, kind):
    """Apply one UI model selection to both roles sharing that capability."""
    if kind == "chat":
        targets = (("create_markdown", "ai_postprocess"), ("build_search_index", "query_processing"))
    elif kind == "vision":
        targets = (("create_markdown", "table_vision"), ("create_markdown", "registration_vision"))
    elif kind == "embedding":
        targets = (("build_search_index", "embedding"),)
    elif kind == "rerank":
        targets = (("build_search_index", "rerank"),)
    else:
        raise ValueError(f"Неизвестный тип роли: {kind}")
    roles = providers.setdefault("roles", {})
    for pipeline, role in targets:
        roles.setdefault(pipeline, {})[role] = dict(spec)
    return providers
