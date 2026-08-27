#!/usr/bin/env python3
"""Консолидация общих конфигов «чистого прода» в staging-каталог.

Используется скриптом scripts/deploy.sh (dry-run тоже строит staging, чтобы
показать корректный план). Ничего на проде не меняет — только читает источники
и пишет в --out.

Источники (приоритет значения — первый по списку):
  .env:   remote/config/.env (общий конфиг интерфейса на LXC, решение №29)
          > remote/interface_RAG/.env > remote/CMY/.env > remote/BSI/.env
          > dev config/.env (интерфейс dev) > dev CMY/.env > dev BSI/.env.
          Ключи объединяются, не удаляются.
  providers.yaml:  эталон — «полный» вариант (с ролями build_search_index,
          remote BSI > dev BSI); доливаются отсутствующие провайдеры/модели
          из варианта Create_Markdown_YA (dev CMY).
  create_markdown_config.yaml:  из Create_Markdown_YA (dev).
  search_config.yaml:           из Build_Search_index (dev).
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import yaml

# Каталог интерфейса (родитель scripts/) — его config/.env читается как
# первоочередной dev-источник .env (QDRANT_PATH и прочие UI-ключи).
DEV_ROOT = Path(__file__).resolve().parents[1]

# Канонический набор ключей (architecture.md §8.1). Реально берётся объединение
# всех ключей из всех источников (ничего не теряем); этот список нужен для
# упорядоченного вывода и как справочник.
CANONICAL_KEYS = [
    "YANDEX_API_KEY", "YANDEX_FOLDER_ID", "DEEPSEEK_API_KEY", "PROVOD_API_KEY",
    "ANYMODEL_API_KEY", "Z_AI_API_KEY", "SILICONFLOW_API_KEY", "TELEGRAM_BOT_TOKEN",
]


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def merge_providers(base: dict, extra: dict) -> dict:
    """Эталон base + отсутствующие провайдеры/модели из extra (base приоритетен)."""
    merged = dict(base)
    merged.setdefault("providers", {})
    merged.setdefault("roles", {})
    base_providers = base.get("providers", {})
    for name, spec in extra.get("providers", {}).items():
        if name in base_providers:
            # Долить отсутствующие модели в существующего провайдера.
            base_models = dict(base_providers[name].get("models", {}))
            for model, tag in spec.get("models", {}).items():
                base_models.setdefault(model, tag)
            base_providers[name]["models"] = base_models
        else:
            merged["providers"][name] = spec
    # Роли: объединить деревья пайплайнов (base приоритетен внутри роли).
    base_roles = merged["roles"]
    for pipeline, roles in extra.get("roles", {}).items():
        pipeline_roles = base_roles.setdefault(pipeline, {})
        for role, spec in roles.items():
            pipeline_roles.setdefault(role, spec)
    return merged


def merge_env(sources: list[tuple[str, Path]]) -> tuple[dict[str, str], list[str]]:
    merged: dict[str, str] = {}
    for label, path in sources:
        for key, value in parse_env(path).items():
            merged.setdefault(key, value)  # первый источник выигрывает
    keys = list(merged)
    ordered = [k for k in CANONICAL_KEYS if k in merged] + \
              [k for k in keys if k not in CANONICAL_KEYS]
    return merged, ordered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--remote", type=Path, default=None,
                        help="каталог с файлами, скачанными с LXC (read-only)")
    parser.add_argument("--dev-cmy", type=Path, default=Path("/root/projects/Create_Markdown_YA"))
    parser.add_argument("--dev-bsi", type=Path, default=Path("/root/projects/Build_Search_index"))
    args = parser.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    remote = args.remote
    dev_cmy, dev_bsi = args.dev_cmy, args.dev_bsi

    # --- providers.yaml: эталон «полный» (с ролями build_search_index) ---
    base_providers = None
    base_source: Path | None = None
    candidates = []
    if remote:
        candidates.append(remote / "BSI/providers.yaml")
    candidates.append(dev_bsi / "firmware/src/providers.yaml")
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            base_providers = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            base_source = candidate
            break
    if base_providers is None or base_source is None:
        print("ОШИБКА: не найден эталонный providers.yaml (remote BSI / dev BSI)", file=sys.stderr)
        return 1
    extra_providers = yaml.safe_load((dev_cmy / "firmware/src/providers.yaml").read_text(encoding="utf-8")) or {}
    merged_providers = merge_providers(base_providers, extra_providers)
    (out / "providers.yaml").write_text(
        yaml.safe_dump(merged_providers, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # --- create_markdown_config.yaml / search_config.yaml ---
    shutil.copy2(dev_cmy / "firmware/src/create_markdown_config.yaml", out / "create_markdown_config.yaml")
    shutil.copy2(dev_bsi / "firmware/src/search_config.yaml", out / "search_config.yaml")

    # --- .env: объединение, приоритет config/.env (LXC) > legacy > dev, ключи не удаляются ---
    env_sources: list[tuple[str, Path]] = []
    if remote:
        env_sources += [
            ("remote/config/.env", remote / "config/.env"),
            ("remote/interface_RAG/.env", remote / "interface_RAG/.env"),
            ("remote/CMY/.env", remote / "CMY/.env"),
            ("remote/BSI/.env", remote / "BSI/.env"),
        ]
    env_sources += [
        ("dev config/.env", DEV_ROOT / "config/.env"),
        ("dev CMY/.env", dev_cmy / "firmware/src/.env"),
        ("dev BSI/.env", dev_bsi / ".env"),
    ]
    env_sources = [(label, path) for label, path in env_sources if path.exists()]
    merged_env, ordered_keys = merge_env(env_sources)
    env_path = out / ".env"
    env_path.write_text("".join(f"{k}={merged_env[k]}\n" for k in ordered_keys), encoding="utf-8")
    os.chmod(env_path, 0o600)

    # --- сводка (только имена ключей, без значений) ---
    print(f"consolidated -> {out}")
    print(f"  providers: {len(merged_providers.get('providers', {}))} шт, "
          f"roles: {sorted(merged_providers.get('roles', {}))}, источник эталона: {base_source}")
    print(f"  env keys ({len(ordered_keys)}): {', '.join(ordered_keys)}")
    print(f"  env sources: {', '.join(label for label, _ in env_sources)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
