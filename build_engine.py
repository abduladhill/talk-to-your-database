"""Build Mode database operations, schema proposal validation, and table creation."""

import re
import sqlite3
from pathlib import Path

from schema_inspector import DB_PATH
from query_engine import call_ollama


def table_exists(table_name: str, db_path: Path = DB_PATH) -> bool:
    """Check whether a table already exists in the SQLite database."""
    target_path = Path(db_path) if db_path else DB_PATH
    if not target_path.exists():
        return False

    conn = sqlite3.connect(target_path, timeout=5)
    try:
        result = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND lower(name) = lower(?)
            """,
            (table_name,),
        ).fetchone()
        return result is not None
    finally:
        conn.close()


def generate_schema_proposal(build_request: str, existing_schema: str) -> str:
    """Generate a single SQLite CREATE TABLE statement proposal."""
    prompt = f"""
Design a SQLite table schema based on the user's requirement.

Return only one CREATE TABLE statement.

Rules:
- Create exactly one new ordinary table.
- Use valid SQLite column definitions and data types.
- Include an INTEGER PRIMARY KEY AUTOINCREMENT id unless
  the user explicitly requests a different primary key.
- Include fields relevant to the user's request.
- Do not use CREATE VIRTUAL TABLE.
- Do not use CREATE TABLE AS SELECT.
- Do not include triggers or additional SQL statements.
- Never modify or replace existing tables.
- Do not use Markdown fences.

Existing database schema:
{existing_schema}

User requirement:
{build_request}
"""
    return call_ollama(prompt)


def validate_schema_proposal(sql: str) -> str:
    """
    Perform strict validation on a proposed CREATE TABLE statement.
    Rejects multi-statements, non-CREATE operations, virtual tables, and CTAS.
    """
    cleaned = sql.strip()

    # Remove any stray markdown code fences
    cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    # Remove one optional trailing semicolon.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    if not re.match(r"(?is)^CREATE\s+TABLE\b", cleaned):
        raise ValueError("Only CREATE TABLE proposals are permitted.")

    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not permitted.")

    if re.match(r"(?is)^CREATE\s+VIRTUAL\s+TABLE\b", cleaned):
        raise ValueError("Virtual tables are not permitted.")

    if re.search(r"(?is)\bAS\s+SELECT\b", cleaned):
        raise ValueError("CREATE TABLE AS SELECT is not permitted.")

    forbidden = (
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|ATTACH|DETACH|"
        r"PRAGMA|VACUUM|REINDEX|TRIGGER)\b"
    )

    if re.search(forbidden, cleaned, flags=re.IGNORECASE):
        raise ValueError("The proposal contains a prohibited SQL operation.")

    if not re.search(r"(?is)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", cleaned):
        raise ValueError("Invalid CREATE TABLE statement.")

    return cleaned


def get_table_name_from_proposal(sql: str) -> str:
    """Extract a simple, validated table name from a CREATE TABLE statement."""
    match = re.match(
        r"(?is)^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r'(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))',
        sql.strip(),
    )
    if not match:
        raise ValueError("Could not identify the proposed table name.")

    table_name = next(group for group in match.groups() if group is not None)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError("Use a simple table name containing letters, numbers, and underscores.")

    return table_name


def construct_create_table_sql(table_name: str, columns: list) -> str:
    """
    Construct a validated SQLite CREATE TABLE statement from manual column definitions.
    """
    clean_table = table_name.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean_table):
        raise ValueError(
            "Table name must contain only letters, numbers, and underscores, "
            "and start with a letter or underscore."
        )
    if not columns:
        raise ValueError("At least one column definition is required.")

    col_defs = []
    seen_names = set()
    for col in columns:
        c_name = col.get("name", "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", c_name):
            raise ValueError(f"Invalid column name: '{c_name}'. Use letters, numbers, and underscores only.")
        if c_name.lower() in seen_names:
            raise ValueError(f"Duplicate column name: '{c_name}'.")
        seen_names.add(c_name.lower())

        c_type = col.get("type", "TEXT").strip().upper()
        if c_type not in ("INTEGER", "TEXT", "REAL", "BLOB", "NUMERIC"):
            c_type = "TEXT"

        parts = [f'"{c_name}"', c_type]
        if col.get("primary_key"):
            if col.get("autoincrement") and c_type == "INTEGER":
                parts.append("PRIMARY KEY AUTOINCREMENT")
            else:
                parts.append("PRIMARY KEY")
        if col.get("not_null"):
            parts.append("NOT NULL")
        if col.get("unique") and not col.get("primary_key"):
            parts.append("UNIQUE")

        dflt = col.get("default")
        if dflt is not None and str(dflt).strip() != "":
            dflt_str = str(dflt).strip()
            if c_type in ("INTEGER", "REAL") and dflt_str.replace(".", "", 1).replace("-", "", 1).isdigit():
                parts.append(f"DEFAULT {dflt_str}")
            elif dflt_str.upper() in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "NULL"):
                parts.append(f"DEFAULT {dflt_str.upper()}")
            else:
                escaped = dflt_str.replace("'", "''")
                parts.append(f"DEFAULT '{escaped}'")
        col_defs.append("  " + " ".join(parts))

    return f"CREATE TABLE \"{clean_table}\" (\n" + ",\n".join(col_defs) + "\n)"


def create_table_safely(sql: str, table_name: str, db_path: Path = DB_PATH) -> None:
    """Create one table after checking for duplicates inside an immediate transaction."""
    sql = validate_schema_proposal(sql)
    target_path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(target_path, timeout=5)
    try:
        conn.execute("BEGIN IMMEDIATE")

        exists = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND lower(name) = lower(?)
            """,
            (table_name,),
        ).fetchone()

        if exists:
            raise ValueError(f"The table '{table_name}' already exists.")

        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()

