from __future__ import annotations
import re
import requests
try:
    from firmware.src.config_ui import read_yaml, write_yaml
except ImportError:
    from config_ui import read_yaml, write_yaml

ALLOWED_TAGS = ["chat", "vision", "embedding", "rerank"]

def model_tags(name, rules=None):
    """Теги модели по имени — СПИСОК (решение №52): модель может совмещать роли.
    VL/vision-модели умеют и чат → [chat, vision] (gpt-4o, qwen*-vl, llava, …);
    embedding/rerank — специализированные; остальное — [chat]."""
    n = name.lower(); rules = rules or {"embedding": ["embedding", "embed"], "rerank": ["rerank", "reranker"], "vision": ["vision", "-vl", "vl-", "4.5v", "4.6v", "4v", "-v-flash", "llava", "gpt-4o"]}
    tags = []
    for tag, patterns in rules.items():
        if any(p in n for p in patterns) or (tag == "vision" and (re.search(r"qwen.*-vl|gemini.*vision|claude.*vision", n))):
            tags.append(tag)
    if "embedding" in tags or "rerank" in tags:
        return [t for t in ALLOWED_TAGS if t in tags]
    if "vision" in tags:
        return ["chat", "vision"]
    return ["chat"]

def _as_tag_list(value):
    """Значение тега из YAML/запроса («chat», «chat+vision», список) → список
    известных тегов в каноническом порядке; неизвестные отбрасываются."""
    parts = value.split("+") if isinstance(value, str) else (value if isinstance(value, list) else [])
    tags = [str(p).strip().lower() for p in parts]
    return [t for t in ALLOWED_TAGS if t in tags]

def _normalize_models(models):
    """{имя: тег|список} → {имя: [теги]} (решение №52). Пустой/неизвестный
    набор тегов → автоопределение по имени."""
    normalized = {}
    for model_name, value in (models or {}).items():
        model_name = str(model_name).strip()
        if not model_name:
            continue
        normalized[model_name] = _as_tag_list(value) or model_tags(model_name)
    return normalized

def _merge_models(existing, scanned):
    """Merge для «Обновить модели» (решение №53): скан добавляет только НОВЫЕ
    модели; теги уже существующих (в т.ч. вручную поправленные) не меняются."""
    merged = _normalize_models(existing)
    for item in scanned:
        name = str(item.get("name") or "").strip()
        if name and name not in merged:
            merged[name] = item.get("tags") or model_tags(name)
    return merged

def scan_models(base_url, api_key, timeout=20):
    url = base_url.rstrip("/") + "/models"
    response = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    response.raise_for_status()
    return [item.get("id", "") for item in response.json().get("data", []) if item.get("id")]


def scan_and_tag_models(base_url, api_key, timeout=20):
    return [{"name": name, "tags": model_tags(name)} for name in scan_models(base_url, api_key, timeout)]

def add_provider(providers_path, name, base_url, api_key_env, models):
    if not re.match(r"^https?://[^\s]+$", base_url): raise ValueError("Некорректный base_url")
    data = read_yaml(providers_path); providers = data.setdefault("providers", {})
    if name in providers: raise ValueError("Провайдер уже существует")
    if any(item.get("api_key_env") == api_key_env for item in providers.values()): raise ValueError("api_key_env уже используется")
    providers[name] = {"base_url": base_url.rstrip("/"), "api_key_env": api_key_env, "models": {m: model_tags(m) for m in models}}
    write_yaml(providers_path, data); return providers[name]


def _referenced_roles(data, name):
    """Список ролей (строки «pipeline.role»), где провайдер `name` назначен
    основной моделью или fallback (решение №41)."""
    refs = []
    roles = (data or {}).get("roles") or {}
    for pipeline, group in roles.items():
        if not isinstance(group, dict):
            continue
        for role, spec in group.items():
            if not isinstance(spec, dict):
                continue
            if (spec.get("provider") == name) or (isinstance(spec.get("fallback"), dict)
                                                  and spec["fallback"].get("provider") == name):
                refs.append(f"{pipeline}.{role}")
    return refs


def refresh_provider(providers_path, name, env=None):
    """«Обновить модели» одного провайдера (решение №45).

    Скан /models только у целевого провайдера; ошибка скана изолируется и
    возвращается как {"ok": False, "error": ...}, не роняя запрос.
    Запись в providers.yaml — только если список моделей изменился.
    """
    env = dict(env or {})
    data = read_yaml(providers_path)
    providers = data.setdefault("providers", {})
    if name not in providers:
        raise KeyError(name)
    provider = providers[name]
    api_key_env = provider.get("api_key_env") if isinstance(provider, dict) else None
    api_key = env.get(api_key_env) if api_key_env else None
    if not api_key:
        return {"ok": False, "error": "нет ключа"}
    try:
        models = scan_and_tag_models(provider["base_url"], api_key)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    # Решение №53: merge — ручные модели и правки тегов переживают «Обновить модели».
    new_models = _merge_models(provider.get("models"), models)
    if new_models != (provider.get("models") or {}):
        provider["models"] = new_models
        write_yaml(providers_path, data)
    return {"ok": True, "models": new_models}


def update_provider(providers_path, name, patch):
    """Редактирование провайдера (решения №42/№44).

    patch = {name?, base_url?, models?}. Переименование атомарно: ключ в
    `providers` переносится и переписываются все ссылки `roles.*.*.provider`
    и `roles.*.*.fallback.provider`. Возврат — полный реестр.
    """
    data = read_yaml(providers_path)
    providers = data.setdefault("providers", {})
    if name not in providers:
        raise KeyError(name)
    provider = providers[name]
    new_name = (patch.get("name") or name).strip()
    if not new_name:
        raise ValueError("Имя провайдера не может быть пустым")
    if new_name != name and new_name in providers:
        raise ValueError("Провайдер с таким именем уже существует")
    base_url = patch.get("base_url")
    if base_url is not None:
        base_url = base_url.strip()
        if not re.match(r"^https?://[^\s]+$", base_url):
            raise ValueError("Некорректный base_url")
        provider["base_url"] = base_url.rstrip("/")
    if patch.get("models") is not None:
        # Решение №52: значения — «chat», «chat+vision» или список; канонический порядок.
        provider["models"] = _normalize_models(patch["models"])
    if new_name != name:
        # атомарный перенос ключа + переписывание ссылок ролей (№44)
        providers[new_name] = provider
        del providers[name]
        for group in (data.get("roles") or {}).values():
            if not isinstance(group, dict):
                continue
            for spec in group.values():
                if not isinstance(spec, dict):
                    continue
                if spec.get("provider") == name:
                    spec["provider"] = new_name
                fallback = spec.get("fallback")
                if isinstance(fallback, dict) and fallback.get("provider") == name:
                    fallback["provider"] = new_name
    write_yaml(providers_path, data)
    return data


def delete_provider(providers_path, name):
    """Удаление провайдера с защитой (решение №41).

    Провайдер, назначенный в любую роль (основная или fallback), не удаляется:
    ValueError с перечислением ролей. Иначе — удаление + атомарная запись.
    """
    data = read_yaml(providers_path)
    providers = data.setdefault("providers", {})
    if name not in providers:
        raise KeyError(name)
    refs = _referenced_roles(data, name)
    if refs:
        raise ValueError("Провайдер используется ролями: " + ", ".join(sorted(refs)))
    del providers[name]
    write_yaml(providers_path, data)
    return data


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
            merged = _merge_models(provider.get("models"), models)  # решение №53
            if merged != (provider.get("models") or {}):
                provider["models"] = merged
                changed = True
            result[name] = {"ok": True, "models": merged}
        except Exception as exc:
            result[name] = {"ok": False, "error": str(exc)}
    if changed:
        write_yaml(providers_path, data)
    return result
