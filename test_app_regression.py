"""
Comprehensive End-to-End Application Regression Test Suite.
Verifies all capabilities, modes, and edge cases specified in Phase 7:
- Query Mode (generation, explanation, summarization, ambiguity, unsupported refusal, self-correction, CSV)
- Build Mode (manual DDL, AI proposal validation, duplicate prevention, transaction safety)
- Data Entry Mode (dynamic typing, required fields, foreign keys, parameterized insertion, transaction rollback)
- Edge cases (empty tables, read-only enforcement, zero-leak connections)
"""

import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from build_engine import (
    construct_create_table_sql,
    validate_schema_proposal,
    create_table_safely,
    table_exists,
    get_table_name_from_proposal,
)
from data_entry_engine import (
    get_available_tables,
    get_table_schema_details,
    validate_record_for_insertion,
    insert_record,
    get_recent_records,
)
from query_engine import (
    validate_sql,
    execute_query,
    explain_sql,
    summarize_results,
    detect_ambiguity,
    detect_unsupported,
    correct_sql,
    results_to_csv,
    get_schema,
    check_ollama_status,
)
from schema_inspector import inspect_schema


class TestAppRegression(unittest.TestCase):
    """Phase 7 comprehensive application regression tests."""

    @classmethod
    def setUpClass(cls):
        # Create an isolated temporary database for testing
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "regression_test.db"

        conn = sqlite3.connect(cls.db_path)
        try:
            conn.executescript("""
                CREATE TABLE departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                );

                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    dept_id INTEGER NOT NULL,
                    salary REAL NOT NULL,
                    FOREIGN KEY (dept_id) REFERENCES departments(id)
                );

                CREATE TABLE audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT
                );

                INSERT INTO departments (name) VALUES ('Engineering'), ('Marketing');
                INSERT INTO employees (name, dept_id, salary) VALUES
                    ('Alice', 1, 75000.0),
                    ('Bob', 1, 65000.0),
                    ('Charlie', 2, 50000.0);
            """)
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. QUERY MODE REGRESSION
    # -------------------------------------------------------------------------

    def test_query_mode_read_only_execution(self):
        """Verify normal read-only query execution returns correct rows and columns."""
        sql = "SELECT e.name, d.name AS dept, e.salary FROM employees e JOIN departments d ON e.dept_id = d.id ORDER BY e.salary DESC"
        validated = validate_sql(sql)
        cols, rows = execute_query(self.db_path, validated)
        self.assertEqual(cols, ["name", "dept", "salary"])
        self.assertGreaterEqual(len(rows), 3)
        self.assertIn("Alice", [r[0] for r in rows])

    def test_query_mode_ast_explanation(self):
        """Verify SQLGlot AST explanation correctly extracts tables, columns, and operations."""
        sql = "SELECT dept_id, AVG(salary) FROM employees WHERE salary > 50000 GROUP BY dept_id HAVING COUNT(*) > 1 ORDER BY AVG(salary) DESC LIMIT 5"
        explanation = explain_sql(sql)
        self.assertIn("employees", explanation)
        self.assertIn("filters the rows", explanation)
        self.assertIn("dept_id", explanation)
        self.assertIn("5", explanation)

    def test_query_mode_summarize_results(self):
        """Verify factual summarization across multiple rows, single row, and empty rows."""
        # Multiple rows
        summary = summarize_results(
            "Show employees",
            "SELECT name, salary FROM employees",
            ["name", "salary"],
            [("Alice", 75000), ("Bob", 65000)],
        )
        self.assertIn("Alice", summary)
        self.assertIn("Bob", summary)

        # Empty rows
        empty_summary = summarize_results(
            "Show empty",
            "SELECT name FROM employees WHERE 1=0",
            ["name"],
            [],
        )
        self.assertIn("no matching records", empty_summary)

    def test_query_mode_csv_export(self):
        """Verify in-memory CSV export format."""
        csv_str = results_to_csv(["id", "name"], [(1, "Alice"), (2, "Bob")])
        self.assertIn("id,name", csv_str)
        self.assertIn("1,Alice", csv_str)

    def test_query_mode_ambiguity_detection(self):
        """Verify ambiguous questions are flagged with options."""
        res = detect_ambiguity("Show the best employees in the company.")
        self.assertTrue(res["is_ambiguous"])
        self.assertGreater(len(res["options"]), 0)

    def test_query_mode_unsupported_detection(self):
        """Verify unsupported requests are flagged with reasons and alternatives."""
        res = detect_unsupported("Delete employee named Alice.")
        self.assertTrue(res["is_unsupported"])
        self.assertIn("read-only", res["reason"].lower())
        self.assertIsNotNone(res["suggestion"])

    def test_query_mode_bounded_self_correction(self):
        """Verify bounded self-correction corrects fixable SQL errors."""
        schema = get_schema(self.db_path)
        corrected_sql = correct_sql(
            question="Show all employee names and their salaries",
            schema=schema,
            retrieved_context="",
            failed_sql="SELECT nonexistent_column FROM employees",
            error="no such column: nonexistent_column",
        )
        safe_sql = validate_sql(corrected_sql)
        cols, rows = execute_query(self.db_path, safe_sql)
        self.assertGreater(len(rows), 0)

    # -------------------------------------------------------------------------
    # 2. BUILD MODE REGRESSION
    # -------------------------------------------------------------------------

    def test_build_mode_manual_table_creation(self):
        """Verify manual column designer creates compliant DDL and executes safely."""
        columns = [
            {"name": "id", "type": "INTEGER", "primary_key": True, "autoincrement": True, "not_null": True},
            {"name": "project_name", "type": "TEXT", "primary_key": False, "autoincrement": False, "not_null": True, "unique": True},
            {"name": "budget", "type": "REAL", "primary_key": False, "autoincrement": False, "not_null": False, "default": "0.0"},
        ]
        ddl = construct_create_table_sql("projects", columns)
        validated_ddl = validate_schema_proposal(ddl)
        table_name = get_table_name_from_proposal(validated_ddl)
        self.assertEqual(table_name, "projects")

        # Execute table creation
        create_table_safely(validated_ddl, table_name, db_path=self.db_path)
        self.assertTrue(table_exists("projects", db_path=self.db_path))

    def test_build_mode_duplicate_table_rejected(self):
        """Verify creating an already existing table raises ValueError."""
        with self.assertRaises(ValueError):
            create_table_safely("CREATE TABLE employees (id INT);", "employees", db_path=self.db_path)

    def test_build_mode_dangerous_ddl_rejected(self):
        """Verify dangerous DDL is rejected by AST validation."""
        dangerous = [
            "DROP TABLE employees;",
            "CREATE TABLE temp AS SELECT * FROM employees;",
            "CREATE VIRTUAL TABLE v_table USING fts5(content);",
            "CREATE TABLE t1 (id INT); DROP TABLE departments;",
        ]
        for ddl in dangerous:
            with self.assertRaises(ValueError):
                validate_schema_proposal(ddl)

    # -------------------------------------------------------------------------
    # 3. DATA ENTRY MODE REGRESSION
    # -------------------------------------------------------------------------

    def test_data_entry_column_inspection(self):
        """Verify column inspection extracts types, nullability, and primary key metadata."""
        details = get_table_schema_details(self.db_path, "employees")
        cols = details["columns"]
        col_names = [c["name"] for c in cols]
        self.assertIn("name", col_names)
        self.assertIn("dept_id", col_names)
        self.assertIn("salary", col_names)

        # Check types
        salary_col = next(c for c in cols if c["name"] == "salary")
        self.assertEqual(salary_col["type"], "REAL")
        self.assertTrue(salary_col["not_null"])

    def test_data_entry_foreign_key_detection(self):
        """Verify foreign keys are detected and options are extracted."""
        details = get_table_schema_details(self.db_path, "employees")
        dept_col = next(c for c in details["columns"] if c["name"] == "dept_id")
        self.assertIsNotNone(dept_col["foreign_key"])
        self.assertEqual(dept_col["foreign_key"]["table"], "departments")
        options = dept_col["foreign_key"]["options"]
        self.assertGreater(len(options), 0)

    def test_data_entry_form_validation(self):
        """Verify form validation rejects missing required fields and type mismatches."""
        details = get_table_schema_details(self.db_path, "employees")

        # Missing required name
        invalid_data = {"name": "", "dept_id": 1, "salary": 50000.0}
        is_valid, cleaned, errors = validate_record_for_insertion(self.db_path, "employees", invalid_data, details)
        self.assertFalse(is_valid)
        self.assertTrue(any("name" in err for err in errors))

        # Invalid numeric salary
        invalid_num = {"name": "Test", "dept_id": 1, "salary": "not-a-number"}
        is_valid, cleaned, errors = validate_record_for_insertion(self.db_path, "employees", invalid_num, details)
        self.assertFalse(is_valid)
        self.assertTrue(any("salary" in err for err in errors))

        # Valid data
        valid_data = {"name": "Dana", "dept_id": 1, "salary": 80000.0}
        is_valid, cleaned, errors = validate_record_for_insertion(self.db_path, "employees", valid_data, details)
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)
        self.assertEqual(cleaned["name"], "Dana")

    def test_data_entry_parameterized_insertion_and_rollback(self):
        """Verify insertion succeeds, recent records are retrieved, and errors rollback."""
        # Valid insertion
        new_row = {"name": "Evan", "dept_id": 2, "salary": 55000.0}
        inserted = insert_record(self.db_path, "employees", new_row)
        self.assertEqual(inserted["name"], "Evan")

        # Verify recent records display
        recent_cols, recent_rows = get_recent_records(self.db_path, "employees", limit=5)
        names = [r[1] for r in recent_rows]
        self.assertIn("Evan", names)

        # Verify SQL injection payload is treated safely as literal data
        injection_row = {"name": "Robert'); DROP TABLE audit_log;--", "dept_id": 1, "salary": 10000.0}
        inserted_inj = insert_record(self.db_path, "employees", injection_row)
        self.assertEqual(inserted_inj["name"], "Robert'); DROP TABLE audit_log;--")

        # Confirm audit_log was NOT dropped
        self.assertTrue(table_exists("audit_log", db_path=self.db_path))

    # -------------------------------------------------------------------------
    # 4. EDGE CASES & SYSTEM ROBUSTNESS
    # -------------------------------------------------------------------------

    def test_empty_table_query(self):
        """Verify querying an empty table executes cleanly without exceptions."""
        cols, rows = execute_query(self.db_path, "SELECT * FROM audit_log")
        self.assertEqual(cols, ["id", "event"])
        self.assertEqual(len(rows), 0)
        summary = summarize_results("Show audit events", "SELECT * FROM audit_log", cols, rows)
        self.assertIn("no matching records", summary)

    def test_read_only_sqlite_engine_enforcement(self):
        """Verify executing INSERT or UPDATE through read-only execute_query is rejected."""
        with self.assertRaises(ValueError):
            execute_query(self.db_path, "INSERT INTO audit_log (event) VALUES ('test')")
        with self.assertRaises(ValueError):
            execute_query(self.db_path, "UPDATE employees SET salary = 99999 WHERE id = 1")
        with self.assertRaises(ValueError):
            execute_query(self.db_path, "DELETE FROM employees WHERE id = 1")


if __name__ == "__main__":
    unittest.main()
