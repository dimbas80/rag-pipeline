from __future__ import annotations
import re
import requests
try:
    from firmware.src.config_ui import read_yaml, write_yaml
except ImportError:
    from config_ui import read_yaml, write_yaml

def tag_model(name, rules=None):
    n = name.lower(); rules = rules or {"embedding": ["embedding", "embed"], "rerank": ["rerank", "reranker"], "vision": ["vision", "-vl", "vl-", "4.5v", "4.6v", "4v", "-v-flash", "llava", "gpt-4o"]}
    for tag, patterns in rules.items():
        if any(p in n for p in patterns) or (tag == "vision" and (re.search(r"qwen.*-vl|gemini.*vision|claude.*vision", n))): return tag
    return "chat"

def scan_models(base_url, api_key, timeout=20):
    url = base_url.rstrip("/") + "/models"
    response = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    response.raise_for_status()
    return [item.get("id", "") for item in response.json().get("data", []) if item.get("id")]


def scan_and_tag_models(base_url, api_key, timeout=20):
    return [{"name": name, "tag": tag_model(name)} for name in scan_models(base_url, api_key, timeout)]

def add_provider(providers_path, name, base_url, api_key_env, models):
    if not re.match(r"^https?://[^\s]+$", base_url): raise ValueError("Некорректный base_url")
    data = read_yaml(providers_path); providers = data.setdefault("providers", {})
    if name in providers: raise ValueError("Провайдер уже существует")
    if any(item.get("api_key_env") == api_key_env for item in providers.values()): raise ValueError("api_key_env уже используется")
    providers[name] = {"base_url": base_url.rstrip("/"), "api_key_env": api_key_env, "models": {m: tag_model(m) for m in models}}
    write_yaml(providers_path, data); return providers[name]


def refresh_all_models(providers_path, env=None):
    """«Обновить» (решение №11): /v1/models для каждого провайдера с ключом.

    Обновляет models (теги по имени) в едином providers.yaml. Ошибка по одному
    провайдеру не роняет остальных — возвращает {provider: {ok, models|error}}.
    """
    env = dict(env or {})
    data = read_yaml(providers_path)
    providers = data.setdefault("providers", {})
    result = {}
    changed = False
    for name, provider in list(providers.items()):
        api_key_env = provider.get("api_key_env") if isinstance(provider, dict) else None
        api_key = env.get(api_key_env) if api_key_env else None
        if not api_key:
            result[name] = {"ok": False, "error": "нет ключа"}
            continue
        try:
            models = scan_and_tag_models(provider["base_url"], api_key)
            provider["models"] = {item["name"]: item["tag"] for item in models}
            changed = True
            result[name] = {"ok": True, "models": provider["models"]}
        except Exception as exc:
            result[name] = {"ok": False, "error": str(exc)}
    if changed:
        write_yaml(providers_path, data)
    return result
