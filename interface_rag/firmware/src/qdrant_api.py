"""Helpers for the local Qdrant store (listing is read-only; deletion is the
one sanctioned write, used by the "Documents in base" tab).

A client is deliberately opened and closed inside every operation.  Local
Qdrant uses an exclusive file lock, so retaining a client across requests
would prevent indexing jobs from opening the store.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models


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


def delete_document(
    qdrant_path: str | Path,
    collection: str,
    document_id: str,
) -> int:
    """Delete all chunks of *document_id* from *collection*; return deleted count.

    Same filter pattern as pre-reindex cleanup in create_index.py: points are
    selected by the payload field ``document_id``.  Embedded Qdrant's delete()
    reports no counters, so the affected points are counted first.
    """
    client = None
    try:
        client = _client(qdrant_path)
        doc_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                )
            ]
        )
        deleted = int(client.count(collection_name=collection, count_filter=doc_filter).count)
        client.delete(collection_name=collection, points_selector=doc_filter)
        return deleted
    finally:
        if client is not None:
            client.close()
