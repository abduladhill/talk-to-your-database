"""Build retrieval documents for safe, low-cardinality database values."""

import sqlite3
import re
from contextlib import closing
from pathlib import Path

from schema_inspector import DB_PATH


MAX_DISTINCT_VALUES = 20
MAX_VALUE_LENGTH = 80
PERSONAL_COLUMN_TOKENS = {
    "name", "email", "phone", "mobile", "address",
}
FREE_TEXT_COLUMN_TOKENS = {
    "description", "comment", "comments", "note", "notes", "message",
    "content", "details", "bio", "biography",
}


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _excluded_column(column_name: str) -> bool:
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", column_name.lower())))
    return (
        bool(tokens & PERSONAL_COLUMN_TOKENS)
        or bool(tokens & FREE_TEXT_COLUMN_TOKENS)
    )


def build_value_documents(db_path: Path = DB_PATH) -> list[dict]:
    """Return documents for safe categorical TEXT columns in a SQLite database."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    documents = []

    with closing(sqlite3.connect(uri, uri=True)) as conn:
        tables = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        for (table_name,) in tables:
            table_identifier = _quote_identifier(table_name)
            columns = conn.execute(
                f"PRAGMA table_info({table_identifier})"
            ).fetchall()

            for column in columns:
                column_name = column[1]
                column_type = (column[2] or "").upper()
                if column_type != "TEXT" or _excluded_column(column_name):
                    continue

                column_identifier = _quote_identifier(column_name)
                distinct_count = conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT {column_identifier})
                    FROM {table_identifier}
                    WHERE {column_identifier} IS NOT NULL
                      AND TRIM({column_identifier}) <> ''
                    """
                ).fetchone()[0]

                if not 0 < distinct_count <= MAX_DISTINCT_VALUES:
                    continue

                values = [
                    row[0]
                    for row in conn.execute(
                        f"""
                        SELECT DISTINCT {column_identifier}
                        FROM {table_identifier}
                        WHERE {column_identifier} IS NOT NULL
                          AND TRIM({column_identifier}) <> ''
                        ORDER BY {column_identifier}
                        """
                    ).fetchall()
                ]

                if any(len(value) > MAX_VALUE_LENGTH for value in values):
                    continue

                documents.append({
                    "table": table_name,
                    "column": column_name,
                    "document_type": "categorical_values",
                    "content": (
                        f"Table: {table_name}\n"
                        f"Column: {column_name}\n"
                        f"Known categorical values: {'; '.join(values)}"
                    ),
                })

    return documents
