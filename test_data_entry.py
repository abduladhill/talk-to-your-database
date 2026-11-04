"""
Unit and integration tests for Data Entry Mode and database safety.
Operates strictly on temporary databases without modifying sample.db.
"""

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data_entry_engine import (
    get_available_tables,
    get_database_overview,
    get_recent_records,
    get_table_schema_details,
    insert_record,
    validate_record_for_insertion,
)
from build_engine import construct_create_table_sql, create_table_safely, validate_schema_proposal


class TestDataEntryEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_data_entry.db"

        # Create relational schema with PK, AUTOINCREMENT, UNIQUE, NOT NULL, DEFAULT, and FOREIGN KEY
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                CREATE TABLE departments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    budget REAL DEFAULT 10000.0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    department_id INTEGER NOT NULL,
                    email TEXT UNIQUE,
                    salary REAL,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (department_id) REFERENCES departments(id)
                )
                """
            )
            # Seed parent table
            conn.execute("INSERT INTO departments (name, budget) VALUES ('Engineering', 50000.0)")
            conn.execute("INSERT INTO departments (name, budget) VALUES ('HR', 20000.0)")
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    def test_get_available_tables(self):
        tables = get_available_tables(self.db_path)
        self.assertIn("departments", tables)
        self.assertIn("employees", tables)
        self.assertNotIn("sqlite_sequence", tables)

    def test_get_table_schema_details(self):
        emp_details = get_table_schema_details(self.db_path, "employees")
        self.assertEqual(emp_details["table_name"], "employees")
        self.assertEqual(emp_details["row_count"], 0)
        self.assertTrue(emp_details["has_auto_pk"])
        self.assertEqual(emp_details["pk_column"], "id")

        cols = {c["name"]: c for c in emp_details["columns"]}
        self.assertTrue(cols["id"]["is_auto_pk"])
        self.assertTrue(cols["name"]["not_null"])
        self.assertTrue(cols["email"]["is_unique"])
        self.assertIsNotNone(cols["department_id"]["foreign_key"])
        self.assertEqual(cols["department_id"]["foreign_key"]["table"], "departments")
        # Check that FK options contain the seeded department IDs
        self.assertIn(1, cols["department_id"]["foreign_key"]["options"])
        self.assertIn(2, cols["department_id"]["foreign_key"]["options"])

    def test_validate_record_success(self):
        details = get_table_schema_details(self.db_path, "employees")
        form_data = {
            "id": 0,  # auto-increment
            "name": "Alice Smith",
            "department_id": 1,
            "email": "alice@example.com",
            "salary": "85000.50",
            "is_active": True,
        }
        is_valid, cleaned, errors = validate_record_for_insertion(
            self.db_path, "employees", form_data, details
        )
        self.assertTrue(is_valid)
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["name"], "Alice Smith")
        self.assertEqual(cleaned["department_id"], 1)
        self.assertEqual(cleaned["email"], "alice@example.com")
        self.assertEqual(cleaned["salary"], 85000.5)
        self.assertEqual(cleaned["is_active"], 1)
        self.assertNotIn("id", cleaned)  # auto-increment omitted for sqlite auto-id

    def test_validate_required_field_missing(self):
        details = get_table_schema_details(self.db_path, "employees")
        form_data = {
            "name": "",  # Missing required field
            "department_id": 1,
        }
        is_valid, cleaned, errors = validate_record_for_insertion(
            self.db_path, "employees", form_data, details
        )
        self.assertFalse(is_valid)
        self.assertTrue(any("required" in err.lower() and "name" in err.lower() for err in errors))

    def test_validate_invalid_data_types(self):
        details = get_table_schema_details(self.db_path, "employees")
        form_data = {
            "name": "Bob",
            "department_id": "not_an_int",
            "salary": "not_a_number",
        }
        is_valid, cleaned, errors = validate_record_for_insertion(
            self.db_path, "employees", form_data, details
        )
        self.assertFalse(is_valid)
        self.assertTrue(any("integer" in err.lower() for err in errors))
        self.assertTrue(any("valid number" in err.lower() for err in errors))

    def test_validate_foreign_key_violation(self):
        details = get_table_schema_details(self.db_path, "employees")
        form_data = {
            "name": "Charlie",
            "department_id": 9999,  # Department does not exist
        }
        is_valid, cleaned, errors = validate_record_for_insertion(
            self.db_path, "employees", form_data, details
        )
        self.assertFalse(is_valid)
        self.assertTrue(any("foreign key" in err.lower() for err in errors))

    def test_validate_unique_constraint(self):
        # Insert a record first
        details = get_table_schema_details(self.db_path, "departments")
        insert_record(self.db_path, "departments", {"name": "Sales", "budget": 15000.0})

        # Try to validate another department with duplicate name
        form_data = {"name": "Sales", "budget": 20000.0}
        is_valid, cleaned, errors = validate_record_for_insertion(
            self.db_path, "departments", form_data, details
        )
        self.assertFalse(is_valid)
        self.assertTrue(any("duplicate" in err.lower() or "already exists" in err.lower() for err in errors))

    def test_insert_record_transaction_and_parameterization(self):
        record = {
            "name": "Robert'); DROP TABLE employees;--",  # SQL injection test
            "department_id": 1,
            "email": "bobby.tables@example.com",
            "salary": 72000.0,
        }
        inserted = insert_record(self.db_path, "employees", record)
        self.assertIsNotNone(inserted)
        self.assertEqual(inserted["name"], "Robert'); DROP TABLE employees;--")
        self.assertEqual(inserted["id"], 1)

        # Confirm table still exists and record is present
        tables = get_available_tables(self.db_path)
        self.assertIn("employees", tables)

        cols, rows = get_recent_records(self.db_path, "employees", limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "Robert'); DROP TABLE employees;--")

    def test_insert_rollback_on_failure(self):
        # Direct attempt to insert duplicate unique value via insert_record
        insert_record(self.db_path, "departments", {"name": "Marketing", "budget": 10000.0})
        with self.assertRaises(ValueError):
            insert_record(self.db_path, "departments", {"name": "Marketing", "budget": 12000.0})

        # Confirm marketing appears only once
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM departments WHERE name = 'Marketing'").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            conn.close()

    def test_database_overview(self):
        overview = get_database_overview(self.db_path)
        self.assertEqual(overview["total_tables"], 2)
        self.assertEqual(overview["tables"]["departments"]["row_count"], 2)
        self.assertEqual(overview["tables"]["employees"]["row_count"], 0)


class TestBuildModeEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_build.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE existing_table (id INTEGER PRIMARY KEY)")
        finally:
            conn.close()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    def test_construct_create_table_sql(self):
        cols = [
            {"name": "id", "type": "INTEGER", "primary_key": True, "autoincrement": True, "not_null": True},
            {"name": "title", "type": "TEXT", "primary_key": False, "not_null": True, "unique": True},
            {"name": "cost", "type": "REAL", "primary_key": False, "not_null": False, "default": "0.0"},
        ]
        sql = construct_create_table_sql("products", cols)
        validated = validate_schema_proposal(sql)
        self.assertIn('CREATE TABLE "products"', validated)
        self.assertIn('"id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL', validated)
        self.assertIn('"title" TEXT NOT NULL UNIQUE', validated)
        self.assertIn('"cost" REAL DEFAULT 0.0', validated)

    def test_create_table_safely_success_and_duplicate_rejection(self):
        cols = [{"name": "id", "type": "INTEGER", "primary_key": True}]
        sql = construct_create_table_sql("new_projects", cols)
        create_table_safely(sql, "new_projects", self.db_path)

        # Verify table exists
        tables = get_available_tables(self.db_path)
        self.assertIn("new_projects", tables)

        # Duplicate table creation should be rejected
        with self.assertRaises(ValueError):
            create_table_safely(sql, "new_projects", self.db_path)

    def test_create_table_safely_revalidates_ddl(self):
        with self.assertRaises(ValueError):
            create_table_safely(
                "CREATE TABLE attempted_write AS SELECT 1",
                "attempted_write",
                self.db_path,
            )


if __name__ == "__main__":
    unittest.main()
