"""Comprehensive test suite for Talk to Your Database features and security controls."""

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path

from schema_inspector import inspect_schema
from schema_documents import build_schema_documents
from value_profiler import build_value_documents
from schema_retriever import build_retriever, refresh_retriever, search_schema
from query_engine import (
    combine_clarification,
    correct_sql,
    detect_ambiguity,
    detect_unsupported,
    execute_query,
    explain_sql,
    get_schema,
    results_to_csv,
    summarize_results,
    validate_sql,
)
from build_engine import (
    create_table_safely,
    get_table_name_from_proposal,
    table_exists,
    validate_schema_proposal,
)


class TalkToYourDBTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE employees (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    department TEXT NOT NULL,
                    salary REAL NOT NULL,
                    email TEXT,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    price REAL NOT NULL,
                    stock_quantity INTEGER NOT NULL
                )
            """)
            employees_data = [
                (1, "Anu", "Engineering", 45000.0, "anu@example.com", "Senior dev"),
                (2, "Rahul", "HR", 35000.0, "rahul@example.com", "Recruiter"),
                (3, "Meera", "Engineering", 52000.0, "meera@example.com", "Lead"),
                (4, "Arun", "Sales", 40000.0, "arun@example.com", "Account exec"),
                (5, "Priya", "HR", 38000.0, "priya@example.com", "HR specialist"),
            ]
            conn.executemany(
                "INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?)",
                employees_data,
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    # ----------------------------------------------------
    # 1. VALUE PROFILING & PRIVACY EXCLUSIONS
    # ----------------------------------------------------
    def test_value_profiling_and_privacy_exclusions(self):
        docs = build_value_documents(self.db_path)
        col_names = {d["column"] for d in docs}

        # Safe categorical column 'department' must be profiled
        self.assertIn("department", col_names)

        # Personal and free-text columns must be excluded
        self.assertNotIn("name", col_names)
        self.assertNotIn("email", col_names)
        self.assertNotIn("notes", col_names)
        self.assertNotIn("salary", col_names)

        dept_doc = next(d for d in docs if d["column"] == "department")
        self.assertIn("Engineering", dept_doc["content"])
        self.assertIn("HR", dept_doc["content"])
        self.assertIn("Sales", dept_doc["content"])

    # ----------------------------------------------------
    # 2. SCHEMA RETRIEVAL & INDEXING
    # ----------------------------------------------------
    def test_schema_retrieval(self):
        model, index, documents = build_retriever(self.db_path)
        self.assertTrue(len(documents) > 0)

        results = search_schema("employees department salary", model, index, documents, top_k=3)
        self.assertTrue(len(results) > 0)
        top_content = results[0]["document"]["content"]
        self.assertTrue("employees" in top_content or "department" in top_content)

    # ----------------------------------------------------
    # 3. AMBIGUITY DETECTION & CLARIFICATION FLOW
    # ----------------------------------------------------
    def test_ambiguity_detection(self):
        # Top employees without metric
        res = detect_ambiguity("Show me the top employees.")
        self.assertTrue(res["is_ambiguous"])
        self.assertTrue(len(res["options"]) >= 2)
        self.assertIn("Highest salary", res["options"])

        # Recent orders without date
        recent = detect_ambiguity("Show recent orders.")
        self.assertTrue(recent["is_ambiguous"])

        # Best department
        best_dept = detect_ambiguity("Which is the best department?")
        self.assertTrue(best_dept["is_ambiguous"])

        # Unambiguous question must NOT trigger ambiguity
        clear_q = detect_ambiguity("Show all employees in Engineering.")
        self.assertFalse(clear_q["is_ambiguous"])

    def test_clarification_combination(self):
        original = "Show me the top employees"
        clarification = "Highest salary"
        combined = combine_clarification(original, clarification)
        self.assertEqual(combined, "Show me the top employees (Clarification: Highest salary)")

        # Once clarified, detect_ambiguity must be False
        check = detect_ambiguity(combined)
        self.assertFalse(check["is_ambiguous"])

    # ----------------------------------------------------
    # 4. UNSUPPORTED QUERY DETECTION & REASONING
    # ----------------------------------------------------
    def test_unsupported_query_detection(self):
        # Forecasting / Prediction
        pred = detect_unsupported("Predict next month's salary for all employees.")
        self.assertTrue(pred["is_unsupported"])
        self.assertIn("forecasting", pred["reason"].lower())
        self.assertTrue(len(pred["missing_info"]) > 0)
        self.assertTrue(len(pred["suggestion"]) > 0)

        # Image / Photograph
        photo = detect_unsupported("Show employee photograph.")
        self.assertTrue(photo["is_unsupported"])
        self.assertIn("multimedia", photo["reason"].lower())

        # Non-existent company info
        company = detect_unsupported("What is the company name and office address?")
        self.assertTrue(company["is_unsupported"])

        # Prohibited DML in Query Mode
        delete_q = detect_unsupported("Delete all employees with salary below 40000.")
        self.assertTrue(delete_q["is_unsupported"])
        self.assertIn("read-only", delete_q["reason"].lower())

        # Supported query
        supported = detect_unsupported("How many employees are in HR?")
        self.assertFalse(supported["is_unsupported"])

    # ----------------------------------------------------
    # 5. SQL VALIDATION & SECURITY ENFORCEMENT
    # ----------------------------------------------------
    def test_sql_validation_safety(self):
        # Permitted single SELECT
        valid = validate_sql("SELECT name, salary FROM employees WHERE salary > 40000;")
        self.assertIn("SELECT", valid)

        # SELECT ... INTO has a Select root in SQLGlot but writes a table.
        for sql in (
            "SELECT 1 INTO new_table",
            "SELECT 1 INTO TEMP new_table",
            "WITH source AS (SELECT 1) SELECT * INTO new_table FROM source",
            "VALUES (1)",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                validate_sql(sql)

        # Multi-statement rejection
        with self.assertRaises(ValueError):
            validate_sql("SELECT * FROM employees; DROP TABLE employees;")

        with self.assertRaises(ValueError):
            validate_sql("SELECT 1; SELECT 2;")

        # Prohibited non-SELECT operations
        for bad_sql in [
            "DELETE FROM employees WHERE id = 1",
            "DROP TABLE employees",
            "INSERT INTO employees (name, department, salary) VALUES ('Test', 'HR', 10000)",
            "UPDATE employees SET salary = 99999",
            "ALTER TABLE employees ADD COLUMN bonus REAL",
            "CREATE TABLE hack (id INT)",
            "",
            "   ",
        ]:
            with self.assertRaises(ValueError):
                validate_sql(bad_sql)

    # ----------------------------------------------------
    # 6. READ-ONLY QUERY EXECUTION & TIMEOUT
    # ----------------------------------------------------
    def test_read_only_execution(self):
        columns, rows = execute_query(self.db_path, "SELECT name, salary FROM employees ORDER BY id LIMIT 2")
        self.assertEqual(columns, ["name", "salary"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("Anu", 45000.0))

        # Query on empty table
        p_cols, p_rows = execute_query(self.db_path, "SELECT * FROM products")
        self.assertEqual(len(p_rows), 0)
        self.assertIn("product_name", p_cols)

        # execute_query is a second validation boundary, not just a read-only
        # connection wrapper.
        with self.assertRaises(ValueError):
            execute_query(self.db_path, "SELECT 1 INTO attempted_write")

    # ----------------------------------------------------
    # 7. SQL EXPLANATION
    # ----------------------------------------------------
    def test_sql_explanation(self):
        sql = "SELECT department, COUNT(*) AS count FROM employees GROUP BY department HAVING count > 1 ORDER BY count DESC LIMIT 5"
        explanation = explain_sql(sql)
        self.assertIn("employees", explanation)
        self.assertIn("groups the data by department", explanation)
        self.assertIn("sorts the results in descending", explanation)
        self.assertIn("limits output to 5", explanation)

    # ----------------------------------------------------
    # 8. NATURAL LANGUAGE RESULT SUMMARY
    # ----------------------------------------------------
    def test_result_summaries(self):
        # Empty result
        s_empty = summarize_results("Find employees in Finance", "SELECT * FROM employees WHERE department='Finance'", ["name"], [])
        self.assertIn("no matching records", s_empty.lower())

        # Single aggregate count
        s_count = summarize_results("Total count", "SELECT COUNT(*) FROM employees", ["employee_count"], [(5,)])
        self.assertIn("5", s_count)

        # Single row (e.g. max salary)
        s_single = summarize_results("Highest paid", "SELECT name, salary FROM employees", ["name", "salary"], [("Meera", 52000.0)])
        self.assertIn("Meera", s_single)
        self.assertIn("52000", s_single)

        # Grouping
        s_group = summarize_results("By department", "SELECT department, cnt FROM employees", ["department", "cnt"], [("Engineering", 2), ("HR", 2)])
        self.assertIn("Engineering", s_group)
        self.assertIn("HR", s_group)

    # ----------------------------------------------------
    # 9. CSV EXPORT (IN-MEMORY, SAFE)
    # ----------------------------------------------------
    def test_csv_export(self):
        cols = ["id", "name", "salary"]
        data = [(1, "Anu", 45000.0), (2, "Rahul", 35000.0)]
        csv_str = results_to_csv(cols, data)
        self.assertEqual(csv_str.strip().splitlines()[0], "id,name,salary")
        self.assertEqual(csv_str.strip().splitlines()[1], "1,Anu,45000.0")

    # ----------------------------------------------------
    # 10. SCHEMA REFRESH (FEATURE 7)
    # ----------------------------------------------------
    def test_schema_refresh_after_table_creation(self):
        # Initial retriever
        model, index, docs1 = build_retriever(self.db_path)
        tables_initial = {d["table"] for d in docs1}
        self.assertNotIn("orders", tables_initial)

        # Create new table in test database
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER,
                    status TEXT NOT NULL,
                    total_amount REAL
                )
            """)
            conn.execute("INSERT INTO orders VALUES (1, 101, 'Completed', 150.0)")
            conn.commit()
        finally:
            conn.close()

        # Refresh retriever
        _, _, docs2 = refresh_retriever(self.db_path)
        tables_refreshed = {d["table"] for d in docs2}
        self.assertIn("orders", tables_refreshed)

        # Check categorical profiling included the new table's status
        status_docs = [d for d in docs2 if d.get("column") == "status"]
        self.assertTrue(len(status_docs) > 0)
        self.assertTrue(any("Completed" in d["content"] for d in status_docs))

    # ----------------------------------------------------
    # 11. BUILD MODE PROPOSAL VALIDATION & CONFIRMATION
    # ----------------------------------------------------
    def test_build_mode_safety_and_proposal(self):
        # Valid proposal
        good_proposal = "CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, due_date TEXT);"
        validated = validate_schema_proposal(good_proposal)
        tbl_name = get_table_name_from_proposal(validated)
        self.assertEqual(tbl_name, "invoices")

        # Prohibited proposals
        with self.assertRaises(ValueError):
            validate_schema_proposal("CREATE TABLE test (id INT); DROP TABLE employees;")

        with self.assertRaises(ValueError):
            validate_schema_proposal("CREATE VIRTUAL TABLE v_test USING fts5(content);")

        with self.assertRaises(ValueError):
            validate_schema_proposal("CREATE TABLE hacked AS SELECT * FROM employees;")

        with self.assertRaises(ValueError):
            validate_schema_proposal("DROP TABLE employees;")

        # Safe creation in database
        create_table_safely(validated, tbl_name, self.db_path)
        self.assertTrue(table_exists("invoices", self.db_path))

        # Duplicate table creation must fail
        with self.assertRaises(ValueError):
            create_table_safely(validated, tbl_name, self.db_path)


if __name__ == "__main__":
    unittest.main()
