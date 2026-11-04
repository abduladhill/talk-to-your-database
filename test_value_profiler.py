import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path

from value_profiler import build_value_documents


def _load_search_schema():
    try:
        from schema_retriever import search_schema
        return search_schema
    except ModuleNotFoundError:
        sys.modules.setdefault("faiss", types.ModuleType("faiss"))
        sys.modules.setdefault("numpy", types.ModuleType("numpy"))
        transformers = types.ModuleType("sentence_transformers")
        transformers.SentenceTransformer = object
        sys.modules.setdefault("sentence_transformers", transformers)
        from schema_retriever import search_schema
        return search_schema


search_schema = _load_search_schema()


class FixedModel:
    def encode(self, texts, **_kwargs):
        return FixedEmbedding()


class FixedEmbedding:
    def astype(self, _dtype):
        return self


class FixedIndex:
    def __init__(self, index):
        self.index = index

    def search(self, _embedding, _top_k):
        return [[1.0]], [[self.index]]


class ValueProfilerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "profile.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE records (
                    id INTEGER PRIMARY KEY,
                    department TEXT,
                    status TEXT,
                    salary REAL,
                    employee_name TEXT,
                    customer_name TEXT,
                    email TEXT,
                    description TEXT,
                    summary TEXT,
                    tags TEXT
                )
                """
            )
            for index in range(21):
                conn.execute(
                    """
                    INSERT INTO records
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        index,
                        "Engineering" if index % 2 else "HR",
                        "Active" if index % 2 else "Inactive",
                        50000 + index,
                        f"Person {index}",
                        "Alice" if index % 2 else "Bob",
                        f"person{index}@example.com",
                        "Long free-text description that must not be indexed.",
                        "This otherwise unclassified text is deliberately longer than eighty characters and must not be indexed.",
                        f"tag-{index}",
                    ),
                )
            conn.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_only_safe_categorical_text_values_are_profiled(self):
        documents = build_value_documents(self.db_path)
        by_column = {document["column"]: document for document in documents}

        self.assertEqual(set(by_column), {"department", "status"})
        self.assertIn("Engineering", by_column["department"]["content"])
        self.assertIn("HR", by_column["department"]["content"])
        self.assertNotIn("salary", by_column)
        self.assertNotIn("employee_name", by_column)
        self.assertNotIn("customer_name", by_column)
        self.assertNotIn("email", by_column)
        self.assertNotIn("description", by_column)
        self.assertNotIn("summary", by_column)
        self.assertNotIn("tags", by_column)

    def test_known_value_document_is_returned_by_retriever(self):
        documents = build_value_documents(self.db_path)
        department_index = next(
            index
            for index, document in enumerate(documents)
            if document["column"] == "department"
        )

        results = search_schema(
            "technical team",
            FixedModel(),
            FixedIndex(department_index),
            documents,
            top_k=1,
        )

        self.assertEqual(results[0]["document"]["column"], "department")
        self.assertIn("Engineering", results[0]["document"]["content"])


if __name__ == "__main__":
    unittest.main()
