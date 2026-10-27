
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "sample.db"


def create_database():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                department TEXT NOT NULL,
                salary REAL NOT NULL
            )
        """)

        cursor.execute("SELECT COUNT(*) FROM employees")
        if cursor.fetchone()[0] == 0:
            employees = [
                ("Anu", "Engineering", 45000),
                ("Rahul", "HR", 35000),
                ("Meera", "Engineering", 52000),
                ("Arun", "Sales", 40000),
                ("Priya", "HR", 38000),
            ]

            cursor.executemany(
                """
                INSERT INTO employees (name, department, salary)
                VALUES (?, ?, ?)
                """,
                employees,
            )

    print(f"Database created: {DB_PATH}")
    print("Table: employees")
    print("Sample records: 5")


if __name__ == "__main__":
    create_database()
