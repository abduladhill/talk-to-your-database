"""Data Entry Engine: Dynamic schema inspection, form validation, and transactional record insertion."""

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _quote_identifier(identifier: str) -> str:
    """Safely quote a SQLite table or column identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def get_available_tables(db_path: Path) -> List[str]:
    """Retrieve list of all ordinary user tables in the SQLite database."""
    target_path = Path(db_path)
    if not target_path.exists():
        return []

    conn = sqlite3.connect(target_path, timeout=5)
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def get_table_schema_details(db_path: Path, table_name: str) -> Dict[str, Any]:
    """
    Extract comprehensive schema metadata for a table, including data types,
    constraints (PK, NOT NULL, DEFAULT, UNIQUE), and foreign key references.
    """
    target_path = Path(db_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Database not found: {target_path}")

    conn = sqlite3.connect(target_path, timeout=5)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        quoted_table = _quote_identifier(table_name)

        # 1. Fetch column info: cid, name, type, notnull, dflt_value, pk
        columns_raw = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()

        # 2. Fetch foreign keys: id, seq, table, from, to, on_update, on_delete, match
        fk_raw = conn.execute(f"PRAGMA foreign_key_list({quoted_table})").fetchall()
        fk_map = {}
        for fk in fk_raw:
            from_col = fk[3]
            ref_table = fk[2]
            ref_col = fk[4]
            fk_map[from_col] = {
                "referenced_table": ref_table,
                "referenced_column": ref_col,
            }

        # 3. Fetch unique single-column indexes: PRAGMA index_list & index_info
        unique_cols = set()
        index_list = conn.execute(f"PRAGMA index_list({quoted_table})").fetchall()
        for idx in index_list:
            is_unique = bool(idx[2])
            idx_name = idx[1]
            if is_unique:
                idx_info = conn.execute(f"PRAGMA index_info({_quote_identifier(idx_name)})").fetchall()
                if len(idx_info) == 1:
                    unique_cols.add(idx_info[0][2])

        # 4. Check row count
        count_row = conn.execute(f"SELECT COUNT(*) FROM {quoted_table}").fetchone()
        row_count = count_row[0] if count_row else 0

        # 5. Determine primary key characteristics
        pk_cols = [c[1] for c in columns_raw if c[5] > 0]
        has_auto_pk = False
        pk_col_name = None
        if len(pk_cols) == 1:
            pk_col_name = pk_cols[0]
            col_def = next(c for c in columns_raw if c[1] == pk_col_name)
            col_type = (col_def[2] or "").strip().upper()
            if col_type == "INTEGER":
                has_auto_pk = True

        # 6. Build column metadata list
        columns = []
        for col in columns_raw:
            col_name = col[1]
            raw_type = (col[2] or "").strip().upper()
            not_null = bool(col[3])
            default_val = col[4]
            is_pk = bool(col[5] > 0)
            is_unique = (col_name in unique_cols) or (is_pk and len(pk_cols) == 1)

            # Normalize type category
            if raw_type in ("INT", "INTEGER", "TINYINT", "SMALLINT", "MEDIUMINT", "BIGINT"):
                type_category = "INTEGER"
            elif raw_type in ("REAL", "DOUBLE", "FLOAT", "NUMERIC", "DECIMAL"):
                type_category = "REAL"
            elif raw_type in ("BOOLEAN", "BOOL"):
                type_category = "BOOLEAN"
            elif raw_type in ("BLOB",):
                type_category = "BLOB"
            else:
                type_category = "TEXT"

            # Fetch FK options if available
            fk_info = fk_map.get(col_name)
            fk_options = []
            if fk_info:
                ref_tbl = fk_info["referenced_table"]
                ref_c = fk_info["referenced_column"]
                try:
                    ref_rows = conn.execute(
                        f"SELECT DISTINCT {_quote_identifier(ref_c)} FROM {_quote_identifier(ref_tbl)} "
                        f"WHERE {_quote_identifier(ref_c)} IS NOT NULL "
                        f"ORDER BY {_quote_identifier(ref_c)} LIMIT 100"
                    ).fetchall()
                    fk_options = [r[0] for r in ref_rows]
                except Exception:
                    fk_options = []

            columns.append({
                "name": col_name,
                "type": type_category,
                "raw_type": raw_type or "TEXT",
                "not_null": not_null,
                "default": default_val,
                "is_pk": is_pk,
                "is_unique": is_unique,
                "is_auto_pk": has_auto_pk and (col_name == pk_col_name),
                "foreign_key": {
                    "table": fk_info["referenced_table"],
                    "column": fk_info["referenced_column"],
                    "options": fk_options,
                } if fk_info else None,
            })

        return {
            "table_name": table_name,
            "row_count": row_count,
            "has_auto_pk": has_auto_pk,
            "pk_column": pk_col_name,
            "columns": columns,
        }
    finally:
        conn.close()


def validate_record_for_insertion(
    db_path: Path,
    table_name: str,
    form_data: Dict[str, Any],
    schema_details: Dict[str, Any],
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """
    Validate user-submitted form data against table constraints.
    Returns (is_valid, cleaned_record_dict, error_messages).
    """
    errors: List[str] = []
    cleaned_record: Dict[str, Any] = {}
    target_path = Path(db_path)

    conn = sqlite3.connect(target_path, timeout=5)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        quoted_table = _quote_identifier(table_name)

        for col in schema_details["columns"]:
            col_name = col["name"]
            type_cat = col["type"]
            not_null = col["not_null"]
            default_val = col["default"]
            is_auto_pk = col["is_auto_pk"]
            is_pk = col["is_pk"]
            is_unique = col["is_unique"]
            fk = col["foreign_key"]

            raw_val = form_data.get(col_name)

            # Auto-increment primary key handling
            if is_auto_pk:
                if raw_val is None or str(raw_val).strip() in ("", "0") or raw_val == 0:
                    # Omit from insert dict to let SQLite auto-generate rowid
                    continue
                else:
                    try:
                        int_val = int(raw_val)
                        if int_val <= 0:
                            errors.append(f"Primary key '{col_name}' must be a positive integer.")
                            continue
                        cleaned_record[col_name] = int_val
                    except (ValueError, TypeError):
                        errors.append(f"Primary key '{col_name}' must be a valid integer.")
                        continue
                continue

            # Manual Primary Key
            if is_pk and not is_auto_pk:
                if raw_val is None or str(raw_val).strip() == "":
                    errors.append(f"Primary key '{col_name}' is required.")
                    continue

            # Check if value is empty/omitted
            is_empty = (
                raw_val is None
                or (isinstance(raw_val, str) and raw_val.strip() == "")
            )

            if is_empty:
                if not_null and default_val is None:
                    errors.append(f"Field '{col_name}' is required and cannot be empty.")
                    continue
                elif default_val is not None:
                    # Let SQLite default apply or assign default
                    continue
                else:
                    cleaned_record[col_name] = None
                    continue

            # Type conversion and validation
            val = raw_val
            if type_cat == "INTEGER":
                try:
                    val = int(raw_val)
                except (ValueError, TypeError):
                    errors.append(f"Field '{col_name}' must be an integer (received: '{raw_val}').")
                    continue
            elif type_cat == "REAL":
                try:
                    val = float(raw_val)
                except (ValueError, TypeError):
                    errors.append(f"Field '{col_name}' must be a valid number (received: '{raw_val}').")
                    continue
            elif type_cat == "BOOLEAN":
                if isinstance(raw_val, bool):
                    val = 1 if raw_val else 0
                elif str(raw_val).strip().lower() in ("1", "true", "yes", "on"):
                    val = 1
                elif str(raw_val).strip().lower() in ("0", "false", "no", "off"):
                    val = 0
                else:
                    val = 1 if raw_val else 0
            else:
                val = str(raw_val).strip()

            # Unique constraint check
            if is_unique and val is not None:
                quoted_col = _quote_identifier(col_name)
                exists = conn.execute(
                    f"SELECT 1 FROM {quoted_table} WHERE {quoted_col} = ?",
                    (val,),
                ).fetchone()
                if exists:
                    errors.append(
                        f"Duplicate value detected: A record with {col_name} = '{val}' already exists."
                    )
                    continue

            # Foreign key constraint check
            if fk and val is not None:
                ref_tbl = fk["table"]
                ref_col = fk["column"]
                fk_valid = conn.execute(
                    f"SELECT 1 FROM {_quote_identifier(ref_tbl)} WHERE {_quote_identifier(ref_col)} = ?",
                    (val,),
                ).fetchone()
                if not fk_valid:
                    errors.append(
                        f"Foreign key violation: Value '{val}' for '{col_name}' does not exist in '{ref_tbl}.{ref_col}'."
                    )
                    continue

            cleaned_record[col_name] = val
    finally:
        conn.close()

    is_valid = len(errors) == 0
    return is_valid, cleaned_record, errors


def insert_record(db_path: Path, table_name: str, record_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Safely insert a record into the target table using parameterized SQL inside an immediate transaction.
    Returns the newly inserted record as a dictionary.
    """
    target_path = Path(db_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Database not found: {target_path}")

    if not record_data:
        raise ValueError("Cannot insert an empty record.")

    quoted_table = _quote_identifier(table_name)
    quoted_cols = [_quote_identifier(col) for col in record_data.keys()]
    placeholders = ["?"] * len(record_data)

    sql = (
        f"INSERT INTO {quoted_table} ({', '.join(quoted_cols)}) "
        f"VALUES ({', '.join(placeholders)})"
    )

    conn = sqlite3.connect(target_path, timeout=5)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(record_data.values()))
            inserted_rowid = cursor.lastrowid
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            msg = str(exc)
            if "UNIQUE" in msg.upper():
                raise ValueError("Insertion rejected: duplicate value violates a UNIQUE constraint.") from exc
            if "FOREIGN KEY" in msg.upper():
                raise ValueError("Insertion rejected: value violates a FOREIGN KEY relationship.") from exc
            if "NOT NULL" in msg.upper():
                raise ValueError("Insertion rejected: required NOT NULL field was missing.") from exc
            raise ValueError(f"Database constraint violation: {exc}") from exc
        except Exception as exc:
            conn.rollback()
            raise ValueError(f"Failed to insert record: {exc}") from exc

        # Retrieve the inserted row back
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        inserted_row = cur.execute(
            f"SELECT * FROM {quoted_table} WHERE rowid = ?",
            (inserted_rowid,),
        ).fetchone()

        if inserted_row:
            return dict(inserted_row)
        return dict(record_data)
    finally:
        conn.close()


def get_recent_records(db_path: Path, table_name: str, limit: int = 5) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """Retrieve the most recent records from a table for verification and UI display."""
    target_path = Path(db_path)
    if not target_path.exists():
        return [], []
    conn = sqlite3.connect(target_path, timeout=5)
    try:
        quoted = _quote_identifier(table_name)
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {quoted} ORDER BY rowid DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return cols, rows
    finally:
        conn.close()


def get_database_overview(db_path: Path) -> Dict[str, Any]:
    """
    Retrieve live database overview statistics: tables, row counts, and column definitions.
    """
    target_path = Path(db_path)
    if not target_path.exists():
        return {"filename": "N/A", "total_tables": 0, "tables": {}}

    tables_summary = {}
    conn = sqlite3.connect(target_path, timeout=5)
    try:
        tables = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        for (table_name,) in tables:
            quoted = _quote_identifier(table_name)
            count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            cols = conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            columns_info = [
                {
                    "name": col[1],
                    "type": col[2] or "TEXT",
                    "not_null": bool(col[3]),
                    "default": col[4],
                    "is_pk": bool(col[5] > 0),
                }
                for col in cols
            ]
            tables_summary[table_name] = {
                "row_count": count,
                "columns": columns_info,
            }

        return {
            "filename": target_path.name,
            "total_tables": len(tables_summary),
            "tables": tables_summary,
        }
    finally:
        conn.close()
