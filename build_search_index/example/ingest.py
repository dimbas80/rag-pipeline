"""
Индексация JSONL-чанков (+assets.json) в Qdrant с гибридным поиском.

Установка:
    pip install qdrant-client sentence-transformers fastembed

Перед запуском:
    - поднять Qdrant: docker run -p 6333:6333 qdrant/qdrant
    - убедиться, что есть доступ к HuggingFace для загрузки Qwen3-Embedding
      (если веса уже скачаны локально — укажи путь вместо repo id)

Запуск:
    python ingest.py --chunks path/to/chunks.jsonl --assets path/to/assets.json \
        --collection so153_molniezashita
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from fastembed import SparseTextEmbedding

EMBED_MODEL = "Qwen/Qwen3-Embedding-4B"   # или -0.6B / -8B в зависимости от твоего железа
SPARSE_MODEL = "Qdrant/bm25"


def load_assets(assets_path: Path) -> tuple[dict, dict]:
    """
    Возвращает (assets_by_id, doc_meta).

    assets_by_id: asset_id -> asset dict (для резолвинга таблиц/картинок в чанках).
    doc_meta: document-level метаданные (document_id, document_type, domain и т.п.),
              которые кладутся в payload каждого чанка этого документа —
              чтобы можно было фильтровать поиск по домену/типу документа
              без отдельного join'а.
    """
    data = json.loads(assets_path.read_text(encoding="utf-8"))

    by_id = {}
    for group in ("tables", "images"):
        for asset in data.get("assets", {}).get(group, []):
            by_id[asset["asset_id"]] = asset

    doc_meta = {
        "document_id": data.get("document_id"),
        "doc_slug": data.get("doc_slug"),
        "document_type": data.get("document_type"),
        "domain": data.get("domain"),
    }
    return by_id, doc_meta


def validate_data(chunks: list[dict], assets_by_id: dict, doc_meta: dict) -> list[str]:
    """
    Прогоняет sanity-check перед индексацией и возвращает список ошибок
    (пустой список = данные чистые). Ничего не бросает сама — решение,
    падать или только предупреждать, принимает main() через --strict.
    """
    errors = []

    # 0. document_id в чанках должен совпадать с document_id в assets.json —
    #    иначе, скорее всего, перепутаны файлы chunks.jsonl / assets.json
    #    от разных документов.
    chunk_doc_ids = {c.get("document_id") for c in chunks}
    if doc_meta.get("document_id") and chunk_doc_ids - {doc_meta["document_id"]}:
        errors.append(
            f"document_id в чанках {chunk_doc_ids} не совпадает с "
            f"document_id в assets.json ({doc_meta['document_id']!r}) — "
            f"проверь, не перепутаны ли файлы"
        )

    # 1. Уникальность chunk_id.
    seen = {}
    for i, c in enumerate(chunks):
        seen.setdefault(c["chunk_id"], []).append(i)
    for chunk_id, rows in seen.items():
        if len(rows) > 1:
            errors.append(f"дубликат chunk_id={chunk_id!r} на строках {rows}")

    chunk_ids_present = set(seen.keys())

    # 2. Пустой/отсутствующий текст — такой чанк бесполезен и в индексе,
    #    и как источник для LLM.
    for i, c in enumerate(chunks):
        if not (c.get("text") or "").strip():
            errors.append(f"пустой text у chunk_id={c.get('chunk_id')!r} (строка {i})")

    # 3. chunk.assets -> ссылка на asset_id, которого нет в assets.json.
    for c in chunks:
        for asset_id in c.get("assets", []):
            if asset_id not in assets_by_id:
                errors.append(
                    f"chunk_id={c['chunk_id']!r} ссылается на несуществующий "
                    f"asset_id={asset_id!r}"
                )

    # 4. assets.json -> chunk_ids, которых нет в jsonl (осиротевший asset:
    #    таблица/картинка, которую при поиске никогда не подтянут, потому
    #    что чанк, к которому она привязана, отсутствует или переименован).
    for asset_id, asset in assets_by_id.items():
        for chunk_id in asset.get("chunk_ids", []):
            if chunk_id not in chunk_ids_present:
                errors.append(
                    f"asset_id={asset_id!r} ссылается на несуществующий "
                    f"chunk_id={chunk_id!r} (возможно, чанк переименован "
                    f"из-за коллизии, а assets.json не обновили)"
                )

    return errors


def build_embed_text(chunk: dict, assets_by_id: dict, doc_meta: dict) -> str:
    """
    Собирает текст, который реально пойдёт в эмбеддер.
    Короткие/контекстно-зависимые чанки (например section без текста)
    без заголовков практически неотличимы друг от друга — поэтому
    всегда добавляем путь по иерархии документа.
    """
    heading = chunk.get("heading_texts", {}) or {}
    path_parts = [
        heading.get("chapter"),
        heading.get("section"),
        heading.get("clause"),
    ]
    heading_line = " → ".join(p for p in path_parts if p)

    doc_line = f"{chunk['document_id']}. {chunk.get('title', '')}"
    if doc_meta.get("domain"):
        doc_line += f" ({doc_meta['domain']})"

    # Подписи связанных таблиц/картинок — помогают найти чанк
    # по запросу вида "таблица с параметрами катанки" даже если
    # само слово "таблица" не встречается в тексте пункта.
    asset_captions = []
    for asset_id in chunk.get("assets", []):
        asset = assets_by_id.get(asset_id)
        if asset:
            label = "Таблица" if asset["asset_type"] == "table" else "Рисунок"
            asset_captions.append(f"{label}: {asset.get('caption', asset_id)}")

    parts = [doc_line, heading_line, chunk["text"]]
    if asset_captions:
        parts.append("Содержит: " + "; ".join(asset_captions))

    return "\n\n".join(p for p in parts if p)


def stable_point_id(chunk: dict, row_index: int) -> str:
    """
    chunk_id в исходных данных не гарантированно уникален (см. проблему
    с дублирующимися ID приложений). Поэтому реальный ID точки в Qdrant
    строим из chunk_id + порядкового номера строки в файле — а сам
    chunk_id сохраняем в payload для человекочитаемых ссылок/дебага.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['chunk_id']}::{row_index}"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True, type=Path)
    ap.add_argument("--assets", required=True, type=Path)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--qdrant-url", default="http://localhost:6333")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Прервать индексацию, если валидация нашла ошибки (по умолчанию только предупреждает).",
    )
    args = ap.parse_args()

    assets_by_id, doc_meta = load_assets(args.assets)

    chunks = [json.loads(l) for l in args.chunks.read_text(encoding="utf-8").splitlines() if l.strip()]

    errors = validate_data(chunks, assets_by_id, doc_meta)
    if errors:
        print(f"[VALIDATION] Найдено проблем: {len(errors)}")
        for e in errors:
            print(f"  - {e}")
        if args.strict:
            print("Прерываю индексацию (--strict). Почини пайплайн генерации или запусти без --strict.")
            sys.exit(1)
        print("Продолжаю без --strict, но результат индексации может быть неполным/некорректным.\n")
    else:
        print("[VALIDATION] OK, проблем не найдено.")

    print(f"Загружаю эмбеддер {EMBED_MODEL} ...")
    dense_model = SentenceTransformer(EMBED_MODEL)
    sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)

    client = QdrantClient(url=args.qdrant_url)

    dense_dim = dense_model.get_sentence_embedding_dimension()
    if not client.collection_exists(args.collection):
        client.create_collection(
            collection_name=args.collection,
            vectors_config={
                "dense": models.VectorParams(size=dense_dim, distance=models.Distance.COSINE),
            },
            sparse_vectors_config={
                "sparse": models.SparseVectorParams(),
            },
        )
        print(f"Коллекция {args.collection} создана (dim={dense_dim}).")

    embed_texts = [build_embed_text(c, assets_by_id, doc_meta) for c in chunks]

    for start in range(0, len(chunks), args.batch_size):
        batch_chunks = chunks[start:start + args.batch_size]
        batch_texts = embed_texts[start:start + args.batch_size]

        dense_vecs = dense_model.encode(batch_texts, normalize_embeddings=True)
        sparse_vecs = list(sparse_model.embed(batch_texts))

        points = []
        for j, (chunk, dense_vec, sparse_vec) in enumerate(zip(batch_chunks, dense_vecs, sparse_vecs)):
            row_index = start + j
            resolved_assets = [
                assets_by_id[a] for a in chunk.get("assets", []) if a in assets_by_id
            ]
            payload = {
                "chunk_id": chunk["chunk_id"],
                "document_id": chunk["document_id"],
                "document_type": doc_meta.get("document_type"),
                "domain": doc_meta.get("domain"),
                "title": chunk.get("title"),
                "status": chunk.get("status"),
                "chapter": chunk.get("chapter"),
                "section": chunk.get("section"),
                "clause": chunk.get("clause"),
                "section_path": chunk.get("section_path"),
                "heading_texts": chunk.get("heading_texts"),
                "text": chunk["text"],
                "references": chunk.get("references", []),
                "assets": resolved_assets,
                "chunk_tokens": chunk.get("chunk_tokens"),
                "row_index": row_index,
            }
            points.append(
                models.PointStruct(
                    id=stable_point_id(chunk, row_index),
                    vector={
                        "dense": dense_vec.tolist(),
                        "sparse": models.SparseVector(
                            indices=sparse_vec.indices.tolist(),
                            values=sparse_vec.values.tolist(),
                        ),
                    },
                    payload=payload,
                )
            )

        client.upsert(collection_name=args.collection, points=points)
        print(f"  proindexed {start + len(batch_chunks)}/{len(chunks)}")

    print("Готово.")


if __name__ == "__main__":
    main()
