#!/usr/bin/env python3
"""
Гибридный поиск по Qdrant (dense+sparse) с реранком через SiliconFlow Rerank API.

Dense-эмбеддинг запроса — SiliconFlow API (Qwen/Qwen3-Embedding-8B),
sparse — локальный fastembed (Qdrant/bm25),
реранк — SiliconFlow Rerank API (Qwen/Qwen3-Reranker-8B).

Установка:
    pip install -r requirements.txt

Запуск:
    python search.py --query "какая высота молниеотвода для зоны Б" \
        --collection technical_standard

Режимы вывода:
    --json              JSON-массив результатов
    --sources           только уникальные имена документов в выдаче
    --list-collections  список коллекций Qdrant
"""
import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv
from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient, models

EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
SPARSE_MODEL = "Qdrant/bm25"
RERANK_MODEL = "Qwen/Qwen3-Reranker-8B"
DEFAULT_COLLECTION = "technical_standard"

# Базовый URL SiliconFlow. По умолчанию — публичный API; переопределяется
# через SILICONFLOW_BASE_URL (например, для прокси или тестового стенда).
SILICONFLOW_BASE_URL = os.environ.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.com")
EMBED_API_URL = f"{SILICONFLOW_BASE_URL}/v1/embeddings"
RERANK_API_URL = f"{SILICONFLOW_BASE_URL}/v1/rerank"

# Qwen3-Embedding рекомендует давать инструкцию только для запросов,
# документы эмбеддятся без неё (см. build_embed_text в create_index.py).
QUERY_INSTRUCTION = (
    "Instruct: Given a search query about Russian technical/regulatory "
    "documents, retrieve the most relevant passage.\nQuery: {query}"
)

# Distance-фильтр перед реранком: кандидаты с RRF-скором ниже порога
# отсеиваются; если отсеялись все — fallback на сырые данные.
RRF_SCORE_THRESHOLD = 0.15

# Метки качества (адаптация порогов distance из ChromaDB-скриптов:
# dist < 0.22 -> "точное", dist < 0.28 -> "хорошее", иначе "среднее";
# здесь score = 1 - distance, поэтому пороги инвертированы).
QUALITY_EXACT = 0.78
QUALITY_GOOD = 0.72

API_TIMEOUT = 120
API_RETRIES = 3
API_RETRY_BACKOFF = 2.0


def resolve_api_key(cli_key: str | None) -> str:
    """Порядок: --api-key > переменная окружения > .env (через python-dotenv)."""
    load_dotenv()
    key = cli_key or os.environ.get("SILICONFLOW_API_KEY")
    if not key:
        print(
            "[ERROR] SILICONFLOW_API_KEY не задан. Укажи --api-key или "
            "положи ключ в .env (SILICONFLOW_API_KEY=...).",
            file=sys.stderr,
        )
        sys.exit(2)
    return key


def embed_query_siliconflow(query: str, api_key: str, model: str = EMBED_MODEL) -> list[float]:
    """Dense-эмбеддинг запроса (с инструкцией Qwen3-Embedding) через SiliconFlow API."""
    text = QUERY_INSTRUCTION.format(query=query)
    payload = {"model": model, "input": [text], "encoding_format": "float"}
    headers = {"Authorization": f"Bearer {api_key}"}

    last_exc: Exception | None = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.post(EMBED_API_URL, json=payload, headers=headers, timeout=API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
        except (requests.RequestException, KeyError, ValueError) as exc:
            last_exc = exc
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_BACKOFF * attempt)
    raise RuntimeError(f"SiliconFlow embedding failed after {API_RETRIES} attempts: {last_exc}")


def rerank_siliconflow(
    query: str,
    documents: list[str],
    api_key: str,
    top_n: int,
    model: str = RERANK_MODEL,
) -> list[float]:
    """
    Реранк документов через SiliconFlow Rerank API.

    POST https://api.siliconflow.cn/v1/rerank
    {"model": "Qwen/Qwen3-Reranker-8B", "query": "...", "documents": [...], "top_n": N}

    Возвращает relevance_score для каждого документа в исходном порядке.
    """
    payload = {"model": model, "query": query, "documents": documents, "top_n": top_n}
    headers = {"Authorization": f"Bearer {api_key}"}

    last_exc: Exception | None = None
    for attempt in range(1, API_RETRIES + 1):
        try:
            resp = requests.post(RERANK_API_URL, json=payload, headers=headers, timeout=API_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            by_index = {item["index"]: item["relevance_score"] for item in data["results"]}
            return [by_index.get(i, 0.0) for i in range(len(documents))]
        except (requests.RequestException, KeyError, ValueError) as exc:
            last_exc = exc
            if attempt < API_RETRIES:
                time.sleep(API_RETRY_BACKOFF * attempt)
    raise RuntimeError(f"SiliconFlow rerank failed after {API_RETRIES} attempts: {last_exc}")


def build_payload_filter(domain: str | None, document_type: str | None, document_id: str | None) -> models.Filter | None:
    """Фильтр по payload-полям Qdrant; None = без фильтра."""
    conditions = []
    if domain:
        conditions.append(models.FieldCondition(key="domain", match=models.MatchValue(value=domain)))
    if document_type:
        conditions.append(models.FieldCondition(key="document_type", match=models.MatchValue(value=document_type)))
    if document_id:
        conditions.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)))
    if not conditions:
        return None
    return models.Filter(must=conditions)


def hybrid_search(client, collection, sparse_model, query, api_key, top_k=30, payload_filter=None):
    """Гибридный retrieve (dense+sparse, RRF-фьюжн) из Qdrant."""
    dense_vec = embed_query_siliconflow(query, api_key)
    sparse_vec = next(sparse_model.embed([query]))

    result = client.query_points(
        collection_name=collection,
        prefetch=[
            models.Prefetch(
                query=dense_vec,
                using="dense",
                limit=top_k,
                filter=payload_filter,
            ),
            models.Prefetch(
                query=models.SparseVector(
                    indices=sparse_vec.indices.tolist(),
                    values=sparse_vec.values.tolist(),
                ),
                using="sparse",
                limit=top_k,
                filter=payload_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=top_k,
    )
    return result.points


def filter_by_rrf_score(candidates, threshold: float = RRF_SCORE_THRESHOLD):
    """
    Distance-фильтр перед реранком: отсев кандидатов с RRF score < threshold.
    Если отсеялись все — fallback на сырые данные.
    """
    kept = [c for c in candidates if c.score >= threshold]
    if not kept:
        return candidates  # fallback на сырые данные
    return kept


def quality_label(score: float) -> str:
    """Метка качества по скору (точное/хорошее/среднее)."""
    if score >= QUALITY_EXACT:
        return "точное"
    if score >= QUALITY_GOOD:
        return "хорошее"
    return "среднее"


def source_name(payload: dict) -> str:
    """Человекочитаемое имя документа (title, иначе document_id)."""
    return payload.get("title") or payload.get("document_id") or "unknown"


def result_to_dict(point, score: float, rank: int) -> dict:
    p = point.payload
    return {
        "rank": rank,
        "score": round(score, 4),
        "rrf_score": round(point.score, 4),
        "quality": quality_label(score),
        "chunk_id": p.get("chunk_id"),
        "document_id": p.get("document_id"),
        "document_type": p.get("document_type"),
        "domain": p.get("domain"),
        "title": p.get("title"),
        "status": p.get("status"),
        "section_path": p.get("section_path"),
        "heading_texts": p.get("heading_texts"),
        "text": p.get("text"),
        "references": p.get("references", []),
        "assets": [
            {"asset_type": a.get("asset_type"), "caption": a.get("caption"), "image_path": a.get("image_path")}
            for a in (p.get("assets") or [])
        ],
    }


def list_collections(client):
    """Список коллекций Qdrant с числом точек."""
    for info in client.get_collections().collections:
        name = info.name
        try:
            count = client.get_collection(name).points_count
        except Exception:
            count = client.count(collection_name=name).count
        print(f"  {name}: {count} чанков")


def main():
    ap = argparse.ArgumentParser(
        description="Гибридный поиск по Qdrant с реранком через SiliconFlow."
    )
    ap.add_argument("--collection", default=DEFAULT_COLLECTION,
                    help=f"Имя коллекции Qdrant (по умолчанию {DEFAULT_COLLECTION})")
    ap.add_argument("--query", help="Поисковый запрос")
    ap.add_argument("--retrieve-k", type=int, default=30, help="Сколько кандидатов взять из Qdrant (default: 30)")
    ap.add_argument("--final-k", type=int, default=6, help="Сколько вернуть после реранка (default: 6)")
    ap.add_argument("--qdrant-path", default="./qdrant_data",
                    help="Путь к локальному хранилищу Qdrant (по умолчанию ./qdrant_data)")
    ap.add_argument("--api-key", help="API-ключ SiliconFlow (или SILICONFLOW_API_KEY в .env)")
    ap.add_argument("--domain", help="Фильтр по payload.domain")
    ap.add_argument("--document_type", help="Фильтр по payload.document_type")
    ap.add_argument("--document_id", help="Фильтр по payload.document_id")
    ap.add_argument("--json", action="store_true", help="Вывод в JSON")
    ap.add_argument("--sources", action="store_true", help="Только уникальные имена документов в выдаче")
    ap.add_argument("--list-collections", action="store_true", help="Список коллекций Qdrant")
    args = ap.parse_args()

    client = QdrantClient(path=args.qdrant_path)

    if args.list_collections:
        list_collections(client)
        return

    if not args.query:
        ap.print_help()
        return

    api_key = resolve_api_key(args.api_key)

    print(f"Загружаю sparse-эмбеддер {SPARSE_MODEL} ...", file=sys.stderr)
    sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)

    payload_filter = build_payload_filter(args.domain, args.document_type, args.document_id)

    candidates = hybrid_search(
        client, args.collection, sparse_model, args.query, api_key,
        top_k=args.retrieve_k, payload_filter=payload_filter,
    )
    if not candidates:
        print("Ничего не найдено.")
        return

    if args.sources:
        # Только имена документов: реранк не нужен.
        for s in sorted(set(source_name(c.payload) for c in candidates)):
            print(s)
        return

    # Distance-фильтр перед реранком (RRF score < 0.15 отсеиваются,
    # fallback на сырые данные, если отсеялись все).
    filtered = filter_by_rrf_score(candidates)
    if len(filtered) != len(candidates):
        print(
            f"[FILTER] отсеяно {len(candidates) - len(filtered)} "
            f"кандидатов (RRF score < {RRF_SCORE_THRESHOLD}), "
            f"осталось {len(filtered)}.",
            file=sys.stderr,
        )

    docs = [c.payload["text"] for c in filtered]
    print(f"[RERANK] {len(docs)} документов через {RERANK_MODEL} ...", file=sys.stderr)
    scores = rerank_siliconflow(args.query, docs, api_key, top_n=min(args.final_k, len(docs)))

    ranked = sorted(zip(filtered, scores), key=lambda x: x[1], reverse=True)[: args.final_k]

    if args.json:
        out = [
            result_to_dict(point, score, i)
            for i, (point, score) in enumerate(ranked, 1)
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f"\n{'=' * 60}")
    print(f"Запрос: {args.query}")
    print(f"Коллекция: {args.collection} | Найдено: {len(ranked)} результатов\n")
    for i, (point, score) in enumerate(ranked, 1):
        p = point.payload
        bar = "█" * int(score * 20)
        tag = quality_label(score)
        print(f"  [{i}] score={score:.3f} ({tag}) {bar}")
        print(f"      {p.get('chunk_id')}  ({source_name(p)})")
        if p.get("section_path"):
            print(f"      {p['section_path']}")
        if p.get("heading_texts"):
            print(f"      {p['heading_texts']}")
        print(f"      {str(p.get('text', ''))[:200]}...")
        for a in (p.get("assets") or []):
            print(f"      -> asset: {a.get('asset_type')} {a.get('caption')} ({a.get('image_path')})")
        print()


if __name__ == "__main__":
    main()
