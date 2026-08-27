"""Per-WebSocket chat adapter around Build_Search_index.qa_graph."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .deploy_config import DeployConfig
except ImportError:  # direct `uvicorn app:app` from firmware/src
    from deploy_config import DeployConfig

# Паттерн импорта read_env_raw — дословно как в jobs.py:13-15 (оба варианта запуска).
try:
    from firmware.src.config_ui import read_env_raw
except ImportError:  # pragma: no cover - direct `uvicorn app:app` from firmware/src
    from config_ui import read_env_raw


def _pipeline_paths(cfg: DeployConfig) -> None:
    root = cfg.build_search_index_dir
    for path in (root, root / "telegram_bot"):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def _inject_env_file(env_file) -> None:
    """Инъекция ключей <config_dir>/.env в os.environ (override, как jobs.build_env).

    Единый источник API-ключей in-process чата: пайплайновый get_api_key()
    (llm_providers.py) резолвит ключи голым load_dotenv(setdefault) →
    os.environ.get. Положив ключи config/.env заранее с override, гарантируем,
    что чат видит те же ключи, что subprocess (build_env). Повторный вызов
    идемпотентен; ключи, которых нет в .env, из os.environ НЕ удаляются
    (systemd Environment= на проде сохраняется).
    """
    os.environ.update(read_env_raw(env_file))


@dataclass
class ChatSession:
    """Own exactly one QAGraph and its interrupt/checkpointer state."""

    cfg: DeployConfig

    def __post_init__(self) -> None:
        # ДО импорта пайплайна (qa_graph→llm_providers) и ДО первого graph.run() —
        # гарантированно раньше load_dotenv() внутри get_api_key().
        _inject_env_file(self.cfg.env_file)
        _pipeline_paths(self.cfg)
        from qa_graph import QAGraph, QAGraphConfig

        self._graph = QAGraph(
            QAGraphConfig(
                qdrant_path=str(self.cfg.qdrant_path),
                collection=self.cfg.collection,
                providers_path=str(self.cfg.providers_path),
                search_config_path=str(self.cfg.search_config_path),
            )
        )

    @staticmethod
    def _interrupt_text(result: dict[str, Any]) -> str | None:
        interrupts = result.get("__interrupt__")
        if not interrupts:
            return None
        item = interrupts[0]
        value = getattr(item, "value", item)
        return str(value)

    def _format(self, result: dict[str, Any]) -> dict[str, Any]:
        if result.get("error"):
            return {"type": "error", "text": str(result["error"])}
        clarification = self._interrupt_text(result)
        if clarification:
            return {"type": "clarification", "text": clarification}
        answer = result.get("final_answer")
        if answer is None:
            return {"type": "error", "text": "Граф не вернул ответ"}
        cited = result.get("cited_chunk_ids") or []
        images = select_images(result.get("search_results") or [], "", str(answer), cited, self.cfg.base_markdown)
        return {
            "type": "answer",
            "answer": answer,
            "cited_chunk_ids": cited,
            "sources": _dedup_sources(result.get("search_results") or []),
            "images": images,
        }

    def answer(self, query: str) -> dict[str, Any]:
        return self._format(self._graph.run(query))

    def stream(self, query: str):
        for update in self._graph.stream(query):
            yield update

    def resume(self, text: str) -> dict[str, Any]:
        return self._format(self._graph.resume(text))

    def list_documents(self) -> list[dict[str, Any]]:
        return self._graph.list_documents()


def _dedup_sources(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Дедуп источников для поля `sources` (решение 31).

    Один документ может дать несколько чанков — в «Источники:» он не должен
    повторяться. Ключ дедупа: `document_id` (приоритет), fallback — `title`
    (trim + case-insensitive). Порядок первого вхождения сохраняется. Записи
    без обоих ключей не дедуплицируются.

    Трогается ТОЛЬКО поле `sources`: `search_results`, который идёт в
    select_images/цитаты, остаётся полным.
    """
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in results:
        key = str(item.get("document_id") or "").strip()
        if not key:
            key = str(item.get("title") or "").strip().lower()
        if not key:
            deduped.append(item)
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def select_images(
    search_results: list[dict[str, Any]],
    query: str,
    answer: str,
    cited_chunk_ids: list[str],
    base_markdown: str | Path | None = None,
) -> list[dict[str, str]]:
    """Select assets using the bot's explicit-reference/citation priority."""
    _pipeline_paths_for_helpers()
    from telegram_bot.asset_helpers import resolve_images_to_send

    cited = set(cited_chunk_ids)
    cited_results = [r for r in search_results if r.get("chunk_id") in cited]
    # Explicit query references have priority; then answer references; finally
    # assets from cited chunks, but only when the text expresses image intent.
    paths, captions = resolve_images_to_send(search_results, answer, query, prefer="query")
    if not paths and cited_results and any(word in f"{query} {answer}".lower() for word in ("табл", "рис", "изображ")):
        paths, captions = resolve_images_to_send(cited_results, answer, query, prefer="answer")
    base = Path(base_markdown).resolve() if base_markdown else None
    result = []
    for index, path in enumerate(paths):
        absolute = Path(path).resolve()
        caption = captions[index] if index < len(captions) else ""
        if base and base in absolute.parents:
            relative = absolute.relative_to(base)
            result.append({"url": "/api/images/" + "/".join(relative.parts), "caption": caption or "", "asset_type": "image"})
        else:
            result.append({"url": str(path), "caption": caption or "", "asset_type": "image"})
    return result


def _pipeline_paths_for_helpers() -> None:
    # Helpers are pure stdlib but importing through the configured path keeps
    # this function usable in tests without importing the heavy graph module.
    return None
