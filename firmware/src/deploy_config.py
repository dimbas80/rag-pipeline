from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

ROOT = Path(__file__).resolve().parents[2]

@dataclass(frozen=True)
class DeployConfig:
    host: str
    port: int
    upload_base_dir: Path
    upload_max_mb: int
    create_markdown_dir: Path
    build_search_index_dir: Path
    qdrant_path: Path
    collection: str
    write_enabled: bool
    env_file: Path
    base_markdown: Path
    prompts: dict[str, Any]
    model_tags: dict[str, list[str]]
    environment: str

    @property
    def pipeline_env(self) -> dict[str, str]:
        return {"INTERFACE_RAG_ENV": self.environment}

def load(path: str | Path | None = None) -> DeployConfig:
    config_path = Path(path) if path else ROOT / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    env = os.environ.get("INTERFACE_RAG_ENV", raw.get("active", "dev"))
    section = raw.get(env)
    if not isinstance(section, dict):
        raise ValueError(f"Неизвестная среда деплоя: {env}")
    required = ("upload", "pipelines", "qdrant", "env_file", "base_markdown")
    missing = [key for key in required if key not in section]
    if missing:
        raise ValueError(f"В конфигурации {env} отсутствуют поля: {', '.join(missing)}")
    for group, keys in (("upload", ("base_dir", "max_mb")), ("pipelines", ("create_markdown_dir", "build_search_index_dir")), ("qdrant", ("path", "collection", "write_enabled"))):
        missing = [key for key in keys if key not in section[group]]
        if missing:
            raise ValueError(f"В конфигурации {env}.{group} отсутствуют поля: {', '.join(missing)}")
    return DeployConfig(
        host=str(section.get("host", "127.0.0.1")), port=int(section.get("port", 8081)),
        upload_base_dir=Path(section["upload"]["base_dir"]), upload_max_mb=int(section["upload"]["max_mb"]),
        create_markdown_dir=Path(section["pipelines"]["create_markdown_dir"]),
        build_search_index_dir=Path(section["pipelines"]["build_search_index_dir"]),
        qdrant_path=Path(section["qdrant"]["path"]), collection=str(section["qdrant"]["collection"]),
        write_enabled=bool(section["qdrant"]["write_enabled"]), env_file=Path(section["env_file"]),
        base_markdown=Path(section["base_markdown"]), prompts=raw.get("prompts", {}),
        model_tags=raw.get("model_tags", {}), environment=env,
    )
