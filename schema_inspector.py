
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "sample.db"


def inspect_schema():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found: {DB_PATH}. "
            "Run create_sample_db.py first."
        )

    schema = {}

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        tables = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """).fetchall()

        for (table_name,) in tables:
            columns = conn.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()

            foreign_keys = conn.execute(
                f'PRAGMA foreign_key_list("{table_name}")'
            ).fetchall()

            schema[table_name] = {
                "columns": [
                    {
                        "name": column[1],
                        "type": column[2],
                        "not_null": bool(column[3]),
                        "default": column[4],
                        "primary_key": bool(column[5]),
                    }
                    for column in columns
                ],
                "foreign_keys": [
                    {
                        "column": fk[3],
                        "referenced_table": fk[2],
                        "referenced_column": fk[4],
                    }
                    for fk in foreign_keys
                ],
            }

    return schema


if __name__ == "__main__":
    import json

    print(json.dumps(inspect_schema(), indent=2))
