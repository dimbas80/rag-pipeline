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

def write_env(path, values):
    path = Path(path); current = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); current[k] = v
            elif line: current[line] = None
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    current.update({k: v for k, v in values.items() if v})
    write_yaml(path.with_suffix(path.suffix + ".tmp.yaml"), current)
    tmp = path.with_suffix(path.suffix + ".tmp.yaml")
    text = "\n".join(f"{k}={v}" for k, v in current.items() if v is not None) + "\n"
    tmp.write_text(text, encoding="utf-8"); os.replace(tmp, path)
