import unittest
from pathlib import Path
from query_engine import (
    detect_ambiguity,
    combine_clarification,
    detect_unsupported,
    validate_sql,
    explain_sql,
    summarize_results,
    results_to_csv,
)

class QueryEngineTests(unittest.TestCase):
    def test_ambiguity_detection(self):
        amb = detect_ambiguity("Show me the top employees.")
        self.assertTrue(amb["is_ambiguous"])
        self.assertIn("clarification_question", amb)
        self.assertTrue(len(amb["options"]) >= 2)

        # Once clarified, it should NOT be ambiguous
        combined = combine_clarification("Show me the top employees", "Highest salary")
        self.assertEqual(combined, "Show me the top employees (Clarification: Highest salary)")
        amb_after = detect_ambiguity(combined)
        self.assertFalse(amb_after["is_ambiguous"])

        # Unambiguous question
        self.assertFalse(detect_ambiguity("Show all employees in Engineering.")["is_ambiguous"])

    def test_unsupported_detection(self):
        pred = detect_unsupported("Predict next month's salary for all employees.")
        self.assertTrue(pred["is_unsupported"])
        self.assertIn("forecasting", pred["reason"].lower())
        self.assertTrue(len(pred["suggestion"]) > 0)

        photo = detect_unsupported("Show employee photograph.")
        self.assertTrue(photo["is_unsupported"])

        company = detect_unsupported("What is the company name and office address?")
        self.assertTrue(company["is_unsupported"])

        delete_q = detect_unsupported("Delete all employees with salary below 40000.")
        self.assertTrue(delete_q["is_unsupported"])

        valid = detect_unsupported("What is the average salary in each department?")
        self.assertFalse(valid["is_unsupported"])

    def test_sql_validation_safety(self):
        # Valid SELECT
        safe = validate_sql("SELECT name, salary FROM employees WHERE salary > 40000;")
        self.assertIn("SELECT", safe)

        # Prohibited operations
        with self.assertRaises(ValueError):
            validate_sql("DELETE FROM employees WHERE id = 1;")
        with self.assertRaises(ValueError):
            validate_sql("DROP TABLE employees;")
        with self.assertRaises(ValueError):
            validate_sql("SELECT * FROM employees; DROP TABLE employees;")
        with self.assertRaises(ValueError):
            validate_sql("")

    def test_sql_validation_rejects_select_output_clauses(self):
        # SQLGlot models SELECT ... INTO as a Select containing exp.Into,
        # even though SQLite normalizes it to CREATE TABLE AS SELECT.
        for sql in (
            "SELECT 1 INTO new_table",
            "SELECT 1 INTO TEMP new_table",
            "WITH source AS (SELECT 1) SELECT * INTO new_table FROM source",
            "VALUES (1)",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                validate_sql(sql)

    def test_sql_explanation(self):
        sql = "SELECT name, salary FROM employees ORDER BY salary DESC LIMIT 1;"
        explanation = explain_sql(sql)
        self.assertIn("employees", explanation)
        self.assertIn("salary", explanation)
        self.assertIn("descending", explanation)
        self.assertIn("limits output to 1", explanation)

    def test_result_summary(self):
        # Empty result
        empty_sum = summarize_results("Find employees in Marketing", "SELECT * FROM employees WHERE department='Marketing'", ["name"], [])
        self.assertIn("no matching records", empty_sum.lower())

        # Single aggregate
        count_sum = summarize_results("How many employees are there?", "SELECT COUNT(*) AS employee_count FROM employees", ["employee_count"], [(5,)])
        self.assertIn("5", count_sum)

        # Grouping
        group_sum = summarize_results("Count by department", "SELECT department, count FROM employees", ["department", "count"], [("Engineering", 2), ("HR", 2), ("Sales", 1)])
        self.assertIn("Engineering", group_sum)
        self.assertIn("HR", group_sum)

    def test_csv_export(self):
        columns = ["id", "name", "salary"]
        rows = [(1, "Anu", 45000.0), (2, "Rahul", 35000.0)]
        csv_text = results_to_csv(columns, rows)
        self.assertIn("id,name,salary", csv_text)
        self.assertIn("1,Anu,45000.0", csv_text)
        self.assertIn("2,Rahul,35000.0", csv_text)

if __name__ == "__main__":
    unittest.main()

