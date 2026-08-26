"""Provider registry loading, role resolution, endpoints, keys, and fallback."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
DEFAULT_PROVIDERS_PATH = str(Path(__file__).resolve().parent / "providers.yaml")
CAPABILITY_ENDPOINT = {"chat": "/chat/completions", "embedding": "/embeddings", "rerank": "/rerank"}


class ProviderConfigError(ValueError):
    """Invalid provider registry or role configuration."""


class ProviderUnavailableError(RuntimeError):
    """All configured attempts failed."""


_cache: dict[str, dict] = {}


def load_providers(path: str | None = None) -> dict:
    path = path or DEFAULT_PROVIDERS_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ProviderConfigError(f"Не удалось загрузить providers.yaml {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict) or not data["providers"]:
        raise ProviderConfigError(f"{path}: секция providers отсутствует или пуста")
    for name, provider in data["providers"].items():
        if not isinstance(provider, dict) or not isinstance(provider.get("base_url"), str) or not provider["base_url"].strip():
            raise ProviderConfigError(f"{path}: у провайдера {name!r} отсутствует base_url")
        if not isinstance(provider.get("api_key_env"), str) or not provider["api_key_env"].strip():
            raise ProviderConfigError(f"{path}: у провайдера {name!r} отсутствует api_key_env")
        if not isinstance(provider.get("models"), dict) or not provider["models"]:
            raise ProviderConfigError(f"{path}: у провайдера {name!r} models должен быть непустым словарём")
    return data


def _get_providers(path: str | None = None) -> dict:
    path = path or DEFAULT_PROVIDERS_PATH
    if path not in _cache:
        _cache[path] = load_providers(path)
    return _cache[path]


def _validate_target(cfg: dict, target: dict, label: str) -> dict:
    if not isinstance(target, dict) or not isinstance(target.get("provider"), str) or not isinstance(target.get("model"), str):
        raise ProviderConfigError(f"{label}: требуются provider и model")
    provider = cfg["providers"].get(target["provider"])
    if provider is None:
        raise ProviderConfigError(f"Неизвестный provider: {target['provider']}")
    if target["model"] not in provider["models"]:
        raise ProviderConfigError(f"Модель {target['model']!r} не зарегистрирована у {target['provider']}")
    return {"provider": target["provider"], "model": target["model"]}


def resolve_subrole(cfg: dict, pipeline: str, subrole: str) -> dict:
    try:
        raw = cfg["roles"][pipeline][subrole]
    except (KeyError, TypeError) as exc:
        raise ProviderConfigError(f"Роль {pipeline}.{subrole} не найдена") from exc
    primary = _validate_target(cfg, raw, f"Роль {pipeline}.{subrole}")
    fallback = raw.get("fallback") if isinstance(raw, dict) else None
    primary["fallback"] = None if fallback in (None, {}) else _validate_target(cfg, fallback, "fallback")
    return primary


def get_endpoint(cfg: dict, provider: str, capability: str) -> str:
    if capability not in CAPABILITY_ENDPOINT:
        raise ProviderConfigError(f"Неизвестная capability: {capability}")
    try:
        base = cfg["providers"][provider]["base_url"]
    except (KeyError, TypeError) as exc:
        raise ProviderConfigError(f"Неизвестный provider: {provider}") from exc
    return base.rstrip("/") + CAPABILITY_ENDPOINT[capability]


def get_api_key(cfg: dict, provider: str, override: str | None = None) -> str:
    if override:
        return override
    try:
        env_name = cfg["providers"][provider]["api_key_env"]
    except (KeyError, TypeError) as exc:
        raise ProviderConfigError(f"Неизвестный provider: {provider}") from exc
    load_dotenv()
    key = os.environ.get(env_name)
    if not key:
        raise ProviderConfigError(f"API-ключ {env_name} для провайдера {provider!r} не задан")
    return key


def run_with_fallback(spec: dict, attempt_fn: Callable[[dict], Any], capability: str):
    targets = [spec] + ([spec["fallback"]] if spec.get("fallback") else [])
    errors = []
    for target in targets:
        try:
            return attempt_fn(target)
        except ProviderUnavailableError as exc:
            errors.append(exc)
            logger.warning("%s: %s/%s unavailable: %s", capability, target["provider"], target["model"], exc)
    if len(targets) == 1:
        raise ProviderUnavailableError(f"{capability}: модель {spec['provider']}/{spec['model']} не ответила и fallback не задан — невозможно продолжить работу: {errors[0]}") from errors[0]
    raise ProviderUnavailableError(f"{capability}: ни основная модель, ни fallback не ответили: {errors}") from errors[-1]
