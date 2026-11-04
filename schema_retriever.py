import os
from pathlib import Path
import numpy as np
import faiss

# Optimize local model loading without slow remote metadata checks
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from sentence_transformers import SentenceTransformer
from schema_documents import build_schema_documents
from schema_inspector import DB_PATH

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_EMBEDDING_MODEL = None


def get_embedding_model():
    """Lazily load and cache the SentenceTransformer model in memory."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        print(f"Loading embedding model: {MODEL_NAME}")
        _EMBEDDING_MODEL = SentenceTransformer(MODEL_NAME)
    return _EMBEDDING_MODEL


def build_retriever(db_path: Path = DB_PATH):
    target_path = Path(db_path) if db_path else DB_PATH
    documents = build_schema_documents(db_path=target_path)

    if not documents:
        raise ValueError("No schema documents were generated.")

    model = get_embedding_model()

    texts = [doc["content"] for doc in documents]
    embeddings = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32")

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    return model, index, documents


def refresh_retriever(db_path: Path = DB_PATH):
    """Rebuild schema documents and FAISS index from the live database."""
    return build_retriever(db_path=db_path)


def search_schema(question, model, index, documents, top_k=3):
    query_embedding = model.encode(
        [question],
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32")

    k = min(top_k, len(documents))
    scores, indices = index.search(query_embedding, k)

    results = []
    for score, idx in zip(scores[0], indices[0]):
        results.append({
            "score": float(score),
            "document": documents[int(idx)],
        })

    return results


if __name__ == "__main__":
    model, index, documents = build_retriever()

    questions = [
        "Which table stores employee salary information?",
        "How many employees are in each department?",
        "Find the employee name and salary columns.",
    ]

    for question in questions:
        print(f"\nQuestion: {question}")
        for result in search_schema(question, model, index, documents):
            print(f"Score: {result['score']:.3f}")
            print(result["document"]["content"])
            print("-" * 40)
