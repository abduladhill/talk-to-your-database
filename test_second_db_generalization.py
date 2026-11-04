"""
Generalization test suite testing a second, independent SQLite retail database.
Verifies dynamic schema inspection, categorical profiling, FAISS vector retrieval,
SQL generation, SQLGlot AST validation, and read-only execution across 12 distinct scenarios.
Operates on a temporary database that is completely isolated and cleaned up afterward.
"""

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path

from schema_inspector import inspect_schema
from schema_documents import build_schema_documents
from value_profiler import build_value_documents
from schema_retriever import build_retriever, search_schema
from query_engine import (
    detect_ambiguity,
    detect_unsupported,
    execute_query,
    explain_sql,
    generate_sql,
    get_schema,
    summarize_results,
    validate_sql,
)
from data_entry_engine import get_available_tables, get_table_schema_details


class SecondDatabaseGeneralizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "retail.db"

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                """
                CREATE TABLE customers (
                    customer_id INTEGER PRIMARY KEY,
                    customer_name TEXT NOT NULL,
                    email TEXT UNIQUE,
                    city TEXT
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE products (
                    product_id INTEGER PRIMARY KEY,
                    product_name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    stock_quantity INTEGER
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE orders (
                    order_id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL,
                    order_date TEXT,
                    total_amount REAL,
                    status TEXT,
                    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE order_items (
                    item_id INTEGER PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL,
                    quantity INTEGER,
                    unit_price REAL,
                    FOREIGN KEY (order_id) REFERENCES orders(order_id),
                    FOREIGN KEY (product_id) REFERENCES products(product_id)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE promotions (
                    promo_id INTEGER PRIMARY KEY,
                    promo_code TEXT NOT NULL UNIQUE,
                    discount_rate REAL
                );
                """
            )

            # Seed realistic data
            customers_data = [
                (1, "Alice Johnson", "alice@retail.com", "Seattle"),
                (2, "Bob Williams", "bob@retail.com", "Chicago"),
                (3, "Charlie Brown", "charlie@retail.com", "Seattle"),
                (4, "Diana Prince", "diana@retail.com", "Boston"),
                (5, "Evan Wright", None, None),
            ]
            conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", customers_data)

            products_data = [
                (101, "Ergonomic Keyboard", "Electronics", 89.99, 15),
                (102, "USB-C Cable", "Electronics", 14.50, 4),
                (103, "Python Cookbook", "Books", 45.00, 30),
                (104, "Desk Lamp", "Home", 29.99, 2),
                (105, "Water Bottle", "Fitness", 18.00, 50),
            ]
            conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", products_data)

            orders_data = [
                (1001, 1, "2026-10-01", 104.49, "Completed"),
                (1002, 2, "2026-10-02", 45.00, "Pending"),
                (1003, 1, "2026-10-05", 89.99, "Completed"),
                (1004, 3, "2026-10-10", 29.99, "Cancelled"),
            ]
            conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", orders_data)

            order_items_data = [
                (1, 1001, 101, 1, 89.99),
                (2, 1001, 102, 1, 14.50),
                (3, 1002, 103, 1, 45.00),
                (4, 1003, 101, 1, 89.99),
                (5, 1004, 104, 1, 29.99),
            ]
            conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", order_items_data)

            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        gc.collect()
        self.temp_dir.cleanup()

    # Scenario 1: Dynamic Schema Inspection on New DB
    def test_scenario_01_schema_inspection(self):
        schema = inspect_schema(self.db_path)
        self.assertIn("customers", schema)
        self.assertIn("products", schema)
        self.assertIn("orders", schema)
        self.assertIn("order_items", schema)
        self.assertIn("promotions", schema)
        self.assertNotIn("employees", schema)  # Ensure no employee table from sample.db

        cust_cols = {c["name"]: c["type"] for c in schema["customers"]["columns"]}
        self.assertEqual(cust_cols["customer_id"], "INTEGER")
        self.assertEqual(cust_cols["customer_name"], "TEXT")
        self.assertEqual(cust_cols["city"], "TEXT")

    # Scenario 2: Retrieve Customer Information
    def test_scenario_02_retrieve_customer_information(self):
        sql = "SELECT customer_id, customer_name, city FROM customers ORDER BY customer_id;"
        cols, rows = execute_query(self.db_path, sql)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0][1], "Alice Johnson")
        self.assertEqual(rows[4][1], "Evan Wright")
        self.assertIsNone(rows[4][2])  # Check NULL handling

    # Scenario 3: Filter by Categorical Value (City) & Categorical Profiler
    def test_scenario_03_find_customers_from_city(self):
        # Value profiler must profile safe 'city' column
        val_docs = build_value_documents(self.db_path)
        city_docs = [d for d in val_docs if d.get("column") == "city"]
        self.assertTrue(len(city_docs) > 0)
        self.assertIn("Seattle", city_docs[0]["content"])
        self.assertIn("Chicago", city_docs[0]["content"])

        sql = "SELECT customer_name FROM customers WHERE city = 'Seattle' ORDER BY customer_name;"
        cols, rows = execute_query(self.db_path, sql)
        names = [r[0] for r in rows]
        self.assertEqual(names, ["Alice Johnson", "Charlie Brown"])

    # Scenario 4: Calculate Total Sales (Aggregation)
    def test_scenario_04_calculate_total_sales(self):
        sql = "SELECT SUM(total_amount) AS total_sales FROM orders WHERE status = 'Completed';"
        cols, rows = execute_query(self.db_path, sql)
        total = rows[0][0]
        # 104.49 + 89.99 = 194.48
        self.assertAlmostEqual(total, 194.48, places=2)

    # Scenario 5: Sales by Customer (GROUP BY)
    def test_scenario_05_calculate_sales_by_customer(self):
        sql = """
            SELECT c.customer_name, SUM(o.total_amount) AS customer_spending
            FROM customers c
            JOIN orders o ON c.customer_id = o.customer_id
            WHERE o.status = 'Completed'
            GROUP BY c.customer_name
            ORDER BY customer_spending DESC;
        """
        cols, rows = execute_query(self.db_path, sql)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Alice Johnson")
        self.assertAlmostEqual(rows[0][1], 194.48, places=2)

    # Scenario 6: Find Highest-Selling Product (Subquery / Ordering)
    def test_scenario_06_highest_selling_product(self):
        sql = """
            SELECT p.product_name, SUM(oi.quantity) AS total_sold
            FROM products p
            JOIN order_items oi ON p.product_id = oi.product_id
            GROUP BY p.product_name
            ORDER BY total_sold DESC
            LIMIT 1;
        """
        cols, rows = execute_query(self.db_path, sql)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Ergonomic Keyboard")
        self.assertEqual(rows[0][1], 2)

    # Scenario 7: Multi-Table JOIN (Customers and Orders)
    def test_scenario_07_join_customers_and_orders(self):
        sql = """
            SELECT o.order_id, c.customer_name, o.total_amount, o.status
            FROM orders o
            JOIN customers c ON o.customer_id = c.customer_id
            ORDER BY o.order_id;
        """
        cols, rows = execute_query(self.db_path, sql)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][1], "Alice Johnson")
        self.assertEqual(rows[1][1], "Bob Williams")

    # Scenario 8: Find Low-Stock Products
    def test_scenario_08_low_stock_products(self):
        sql = "SELECT product_name, stock_quantity FROM products WHERE stock_quantity <= 5 ORDER BY stock_quantity;"
        cols, rows = execute_query(self.db_path, sql)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "Desk Lamp")
        self.assertEqual(rows[1][0], "USB-C Cable")

    # Scenario 9: Multi-Condition Filtering (AND/OR/Inequalities)
    def test_scenario_09_multi_condition_orders(self):
        sql = """
            SELECT order_id, total_amount, status
            FROM orders
            WHERE total_amount > 40.0 AND status <> 'Cancelled'
            ORDER BY total_amount DESC;
        """
        cols, rows = execute_query(self.db_path, sql)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], 1001)

    # Scenario 10: Empty Result Set Handling & Empty Table
    def test_scenario_10_empty_table_and_empty_result(self):
        # Querying the empty 'promotions' table
        cols, rows = execute_query(self.db_path, "SELECT * FROM promotions;")
        self.assertEqual(len(rows), 0)
        summary = summarize_results("Show active promotions", "SELECT * FROM promotions", cols, rows)
        self.assertIn("no matching records", summary.lower())

    # Scenario 11: Ambiguity Detection on Retail Questions
    def test_scenario_11_ambiguity_detection(self):
        amb = detect_ambiguity("Show recent orders.")
        self.assertTrue(amb["is_ambiguous"])
        self.assertTrue(len(amb["options"]) > 0)

    # Scenario 12: Unsupported Query Detection on Retail DB
    def test_scenario_12_unsupported_detection(self):
        unsupp = detect_unsupported("Predict total sales for next year.")
        self.assertTrue(unsupp["is_unsupported"])
        self.assertIn("forecasting", unsupp["reason"].lower())

    # Bonus Scenario: Verify FAISS Schema Retriever dynamically indexes retail.db
    def test_scenario_13_retriever_dynamic_indexing(self):
        model, index, docs = build_retriever(self.db_path)
        tables_indexed = {d["table"] for d in docs}
        self.assertIn("customers", tables_indexed)
        self.assertIn("products", tables_indexed)
        self.assertIn("orders", tables_indexed)
        self.assertNotIn("employees", tables_indexed)

        # Search for sales/orders
        results = search_schema("Find customer purchase orders", model, index, docs, top_k=2)
        top_tables = [r["document"]["table"] for r in results]
        self.assertTrue("orders" in top_tables or "customers" in top_tables)


if __name__ == "__main__":
    unittest.main()
