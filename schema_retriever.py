
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from schema_documents import build_schema_documents

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def build_retriever():
    documents = build_schema_documents()

    if not documents:
        raise ValueError("No schema documents were generated.")

    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    texts = [doc["content"] for doc in documents]
    embeddings = model.encode(
        texts,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype("float32")

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    return model, index, documents


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
