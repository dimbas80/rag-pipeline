"""Read-only helpers for the local Qdrant store.

A client is deliberately opened and closed inside every operation.  Local
Qdrant uses an exclusive file lock, so retaining a client across requests
would prevent indexing jobs from opening the store.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient


def _client(qdrant_path: str | Path) -> QdrantClient:
    return QdrantClient(path=str(qdrant_path))


def list_collections(qdrant_path: str | Path) -> list[str]:
    client = None
    try:
        client = _client(qdrant_path)
        return [item.name for item in client.get_collections().collections]
    finally:
        if client is not None:
            client.close()


def distinct_documents(
    qdrant_path: str | Path,
    collection: str,
) -> list[dict[str, Any]]:
    """Return unique document metadata and chunk counts from *collection*."""
    client = None
    try:
        client = _client(qdrant_path)
        offset = None
        documents: dict[str, dict[str, Any]] = {}
        while True:
            records, next_offset = client.scroll(
                collection_name=collection,
                offset=offset,
                limit=256,
                with_payload=["document_id", "title", "domain", "document_type", "status"],
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                document_id = payload.get("document_id")
                if document_id is None:
                    continue
                key = str(document_id)
                entry = documents.setdefault(
                    key,
                    {
                        "document_id": key,
                        "title": str(payload.get("title") or key),
                        "domain": payload.get("domain"),
                        "document_type": payload.get("document_type"),
                        "status": payload.get("status"),
                        "count": 0,
                    },
                )
                entry["count"] += 1
            if next_offset is None:
                return list(documents.values())
            offset = next_offset
    finally:
        if client is not None:
            client.close()
