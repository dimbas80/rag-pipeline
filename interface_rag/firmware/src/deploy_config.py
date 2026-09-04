from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

try:  # supports both documented package and legacy module invocation
    from firmware.src.config_ui import read_env_raw
except ImportError:  # pragma: no cover - direct `uvicorn app:app` from firmware/src
    from config_ui import read_env_raw

ROOT = Path(__file__).resolve().parents[2]

@dataclass(frozen=True)
class DeployConfig:
    host: str
    port: int
    config_dir: Path
    upload_base_dir: Path
    upload_max_mb: int
    create_markdown_dir: Path
    build_search_index_dir: Path
    qdrant_path_default: Path
    collection: str
    write_enabled: bool
    env_file: Path
    base_markdown_default: Path
    prompts: dict[str, Any]
    model_tags: dict[str, list[str]]
    environment: str

    @property
    def base_dir_override(self) -> str | None:
        """BASE_DIR — корневая папка документов: только из .env интерфейса
        (<config_dir>/.env, пишет UI через config_ui.write_env). process-env НЕ
        читается — тот же аргумент, что у qdrant_path_override. None — override
        отсутствует. Чтение ленивое, без кэша."""
        return read_env_raw(self.env_file).get("BASE_DIR") or None

    @property
    def base_dir(self) -> Path:
        """Эффективный корень документов: BASE_DIR из .env или upload.base_dir
        из config.yaml (в проде это уже корневая папка с документами)."""
        return Path(self.base_dir_override) if self.base_dir_override else self.upload_base_dir

    @property
    def base_markdown(self) -> Path:
        """Папка итоговых markdown: <base_dir>/Markdown при override,
        иначе дефолт из config.yaml."""
        if self.base_dir_override:
            return self.base_dir / "Markdown"
        return self.base_markdown_default

    @property
    def qdrant_path_override(self) -> str | None:
        """QDRANT_PATH: только из .env интерфейса (<config_dir>/.env, пишет UI через
        config_ui.write_env). process-env НЕ читается: пайплайн голым load_dotenv()
        засоряет os.environ значением QDRANT_PATH из СВОЕГО .env — это не источник
        истины для интерфейса. None — override отсутствует.

        Чтение .env ленивое, без кэша (файл крошечный, доступ 1–2 раза на запрос).
        """
        return read_env_raw(self.env_file).get("QDRANT_PATH") or None

    @property
    def qdrant_path(self) -> Path:
        """Эффективный путь Qdrant: override (env/.env) или дефолт из config.yaml."""
        return Path(self.qdrant_path_override) if self.qdrant_path_override else self.qdrant_path_default

    @property
    def providers_path(self) -> Path:
        """Единый реестр провайдеров в общем каталоге конфигов."""
        return self.config_dir / "providers.yaml"

    @property
    def document_types_path(self) -> Path:
        """Пользовательские типы документов для datalist регистрации (решение №50)."""
        return self.config_dir / "document_types.yaml"

    @property
    def create_markdown_config_path(self) -> Path:
        """Единый конфиг Create_Markdown_YA (промпты + RAG-секции)."""
        return self.config_dir / "create_markdown_config.yaml"

    @property
    def search_config_path(self) -> Path:
        """Единый конфиг узлов поискового графа."""
        return self.config_dir / "search_config.yaml"

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
    required = ("config_dir", "upload", "pipelines", "qdrant", "base_markdown")
    missing = [key for key in required if key not in section]
    if missing:
        raise ValueError(f"В конфигурации {env} отсутствуют поля: {', '.join(missing)}")
    for group, keys in (("upload", ("base_dir", "max_mb")), ("pipelines", ("create_markdown_dir", "build_search_index_dir")), ("qdrant", ("path", "collection", "write_enabled"))):
        missing = [key for key in keys if key not in section[group]]
        if missing:
            raise ValueError(f"В конфигурации {env}.{group} отсутствуют поля: {', '.join(missing)}")
    config_dir = Path(section["config_dir"])
    # Инвариант: env_file всегда указывает на <config_dir>/.env (единый каталог конфигов).
    env_file = Path(section.get("env_file") or config_dir / ".env")
    return DeployConfig(
        host=str(section.get("host", "127.0.0.1")), port=int(section.get("port", 8081)),
        config_dir=config_dir,
        upload_base_dir=Path(section["upload"]["base_dir"]), upload_max_mb=int(section["upload"]["max_mb"]),
        create_markdown_dir=Path(section["pipelines"]["create_markdown_dir"]),
        build_search_index_dir=Path(section["pipelines"]["build_search_index_dir"]),
        qdrant_path_default=Path(section["qdrant"]["path"]), collection=str(section["qdrant"]["collection"]),
        write_enabled=bool(section["qdrant"]["write_enabled"]), env_file=env_file,
        base_markdown_default=Path(section["base_markdown"]), prompts=raw.get("prompts", {}),
        model_tags=raw.get("model_tags", {}), environment=env,
    )
