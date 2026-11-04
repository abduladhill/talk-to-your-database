"""
Comprehensive security regression audit test suite for Talk to Your Database.
Verifies Query Mode read-only controls, SQLGlot AST validation, Build Mode DDL checks,
and Data Entry Mode parameterized transactions.
"""

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path

from query_engine import validate_sql, execute_query
from build_engine import validate_schema_proposal, create_table_safely
from data_entry_engine import insert_record, get_table_schema_details, validate_record_for_insertion


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "sec_test.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    salary REAL DEFAULT 30000.0
                )
                """
            )
            for i in range(250):
                conn.execute(
                    "INSERT INTO employees (name, salary) VALUES (?, ?)",
                    (f"emp_{i}", 30000.0 + i * 100),
                )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    # ----------------------------------------------------
    # QUERY MODE SECURITY
    # ----------------------------------------------------

    def test_select_into_rejected(self):
        with self.assertRaises(ValueError):
            validate_sql("SELECT * INTO new_table FROM employees;")

    def test_cte_with_dml_rejected(self):
        with self.assertRaises(ValueError):
            validate_sql("WITH cte AS (DELETE FROM employees RETURNING *) SELECT * FROM cte;")

    def test_multi_statement_rejected(self):
        with self.assertRaises(ValueError):
            validate_sql("SELECT * FROM employees; DROP TABLE employees;")

    def test_prohibited_dml_ddl_operations(self):
        prohibited = [
            "INSERT INTO employees (name) VALUES ('Hacker');",
            "UPDATE employees SET salary = 999999;",
            "DELETE FROM employees WHERE id = 1;",
            "DROP TABLE employees;",
            "ALTER TABLE employees ADD COLUMN ssn TEXT;",
            "ATTACH DATABASE 'leak.db' AS leak;",
            "DETACH DATABASE leak;",
            "PRAGMA foreign_keys = OFF;",
            "VACUUM;",
        ]
        for query in prohibited:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    validate_sql(query)

    def test_execute_query_validates_sql_internally(self):
        with self.assertRaises(ValueError):
            execute_query(self.db_path, "DROP TABLE employees;")

    def test_execute_query_row_limit_200(self):
        cols, rows = execute_query(self.db_path, "SELECT * FROM employees;")
        self.assertEqual(len(rows), 200)

    def test_readonly_connection_blocks_modifications_at_sqlite_level(self):
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO employees (name) VALUES ('Fail');")
        finally:
            conn.close()

    # ----------------------------------------------------
    # BUILD MODE SECURITY
    # ----------------------------------------------------

    def test_build_mode_rejects_ctas_and_virtual_tables(self):
        with self.assertRaises(ValueError):
            validate_schema_proposal("CREATE TABLE t AS SELECT * FROM employees;")
        with self.assertRaises(ValueError):
            validate_schema_proposal("CREATE VIRTUAL TABLE t USING fts5(col);")

    def test_build_mode_rejects_multi_statement_and_drop(self):
        with self.assertRaises(ValueError):
            validate_schema_proposal("CREATE TABLE t (id INT); DROP TABLE employees;")
        with self.assertRaises(ValueError):
            validate_schema_proposal("DROP TABLE employees;")

    def test_create_table_safely_validates_internally_and_rejects_duplicates(self):
        # Valid creation
        create_table_safely("CREATE TABLE projects (id INTEGER PRIMARY KEY)", "projects", self.db_path)
        # Duplicate creation rejected
        with self.assertRaises(ValueError):
            create_table_safely("CREATE TABLE projects (id INTEGER PRIMARY KEY)", "projects", self.db_path)

    # ----------------------------------------------------
    # DATA ENTRY MODE SECURITY
    # ----------------------------------------------------

    def test_data_entry_parameterization_defends_against_sql_injection(self):
        # SQL injection payload: Robert'); DROP TABLE employees;--
        payload = "Robert'); DROP TABLE employees;--"
        inserted = insert_record(self.db_path, "employees", {"name": payload, "salary": 50000.0})
        self.assertEqual(inserted["name"], payload)

        # Confirm table still exists and record count is 251
        cols, rows = execute_query(self.db_path, "SELECT COUNT(*) FROM employees;")
        self.assertEqual(rows[0][0], 251)


if __name__ == "__main__":
    unittest.main()

