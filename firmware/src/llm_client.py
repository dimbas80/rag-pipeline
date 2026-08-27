from __future__ import annotations
import base64, json, re
import requests

def resolve_role(providers, pipeline, subrole):
    role = providers.get("roles", {}).get(pipeline, {}).get(subrole)
    if not role: raise ValueError(f"Роль не настроена: {pipeline}.{subrole}")
    provider = providers.get("providers", {}).get(role.get("provider"))
    if not provider or role.get("model") not in provider.get("models", {}): raise ValueError("Провайдер или модель роли не найдены")
    return {**role, "base_url": provider["base_url"], "api_key_env": provider.get("api_key_env")}

def chat_completion(spec, messages, api_key, timeout=120):
    response = requests.post(spec["base_url"].rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={"model": spec["model"], "messages": messages}, timeout=timeout)
    response.raise_for_status(); return response.json()["choices"][0]["message"]["content"]

def vision_completion(spec, image_bytes, prompt, api_key, timeout=120):
    encoded = base64.b64encode(image_bytes).decode()
    return chat_completion(spec, [{"role":"user", "content":[{"type":"text","text":prompt},{"type":"image_url","image_url":{"url":f"data:image/png;base64,{encoded}"}}]}], api_key, timeout)

def run_with_fallback(primary, fallback, call):
    try: return call(primary)
    except Exception:
        if not fallback: raise
        return call(fallback)
