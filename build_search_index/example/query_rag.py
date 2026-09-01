"""
Умный поиск по документам: гибридный retrieve (dense+sparse) из Qdrant,
затем rerank через Qwen3-Reranker.

Установка:
    pip install qdrant-client sentence-transformers fastembed transformers torch

Запуск:
    python query_rag.py --collection so153_molniezashita --query "какая высота молниеотвода для зоны Б"
"""
import argparse

import torch
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from fastembed import SparseTextEmbedding
from transformers import AutoModelForCausalLM, AutoTokenizer

EMBED_MODEL = "Qwen/Qwen3-Embedding-4B"
SPARSE_MODEL = "Qdrant/bm25"
RERANK_MODEL = "Qwen/Qwen3-Reranker-4B"

# Qwen3-Embedding рекомендует давать инструкцию только для запросов,
# документы эмбеддятся без неё (см. build_embed_text в ingest.py).
QUERY_INSTRUCTION = (
    "Instruct: Given a search query about Russian technical/regulatory "
    "documents, retrieve the most relevant passage.\nQuery: {query}"
)

RERANK_SYSTEM = (
    'Judge whether the Document meets the requirements based on the Query '
    'and the Instruct provided. Note that the answer can only be "yes" or "no".'
)
RERANK_INSTRUCTION = "Given a query, determine if the document answers it."


class Reranker:
    def __init__(self, model_name: str = RERANK_MODEL):
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, padding_side="left")
        self.model = AutoModelForCausalLM.from_pretrained(model_name).eval()
        self.token_true = self.tokenizer.convert_tokens_to_ids("yes")
        self.token_false = self.tokenizer.convert_tokens_to_ids("no")
        self.prefix = self.tokenizer.encode(
            "<|im_start|>system\n" + RERANK_SYSTEM + "<|im_end|>\n<|im_start|>user\n",
            add_special_tokens=False,
        )
        self.suffix = self.tokenizer.encode(
            "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
            add_special_tokens=False,
        )

    def _format(self, query: str, doc: str) -> str:
        return f"<Instruct>: {RERANK_INSTRUCTION}\n<Query>: {query}\n<Document>: {doc}"

    @torch.no_grad()
    def score(self, query: str, docs: list[str], max_length: int = 2048) -> list[float]:
        pairs = [self._format(query, d) for d in docs]
        inputs = self.tokenizer(
            pairs,
            padding=False,
            truncation="longest_first",
            max_length=max_length - len(self.prefix) - len(self.suffix),
            add_special_tokens=False,
        )
        for i in range(len(inputs["input_ids"])):
            inputs["input_ids"][i] = self.prefix + inputs["input_ids"][i] + self.suffix
        inputs = self.tokenizer.pad(inputs, padding=True, return_tensors="pt")

        logits = self.model(**inputs).logits[:, -1, :]
        true_scores = logits[:, self.token_true]
        false_scores = logits[:, self.token_false]
        stacked = torch.stack([false_scores, true_scores], dim=1)
        probs = torch.nn.functional.log_softmax(stacked, dim=1).exp()
        return probs[:, 1].tolist()  # вероятность "yes"


def hybrid_search(client, collection, dense_model, sparse_model, query, top_k=30):
    dense_vec = dense_model.encode(
        QUERY_INSTRUCTION.format(query=query), normalize_embeddings=True
    )
    sparse_vec = next(sparse_model.embed([query]))

    result = client.query_points(
        collection_name=collection,
        prefetch=[
            models.Prefetch(query=dense_vec.tolist(), using="dense", limit=top_k),
            models.Prefetch(
                query=models.SparseVector(
                    indices=sparse_vec.indices.tolist(),
                    values=sparse_vec.values.tolist(),
                ),
                using="sparse",
                limit=top_k,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        limit=top_k,
    )
    return result.points


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--qdrant-url", default="http://localhost:6333")
    ap.add_argument("--retrieve-k", type=int, default=30)
    ap.add_argument("--final-k", type=int, default=6)
    args = ap.parse_args()

    client = QdrantClient(url=args.qdrant_url)
    dense_model = SentenceTransformer(EMBED_MODEL)
    sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)
    reranker = Reranker()

    candidates = hybrid_search(
        client, args.collection, dense_model, sparse_model, args.query, args.retrieve_k
    )
    if not candidates:
        print("Ничего не найдено.")
        return

    docs = [c.payload["text"] for c in candidates]
    scores = reranker.score(args.query, docs)

    ranked = sorted(zip(candidates, scores), key=lambda x: x[1], reverse=True)[: args.final_k]

    for point, score in ranked:
        p = point.payload
        print(f"\n[{score:.3f}] {p['chunk_id']}  ({p['document_id']}, {p['section_path']})")
        print(f"  {p['heading_texts']}")
        print(f"  {p['text'][:200]}...")
        if p["assets"]:
            for a in p["assets"]:
                print(f"  -> asset: {a['asset_type']} {a['caption']} ({a['image_path']})")


if __name__ == "__main__":
    main()
