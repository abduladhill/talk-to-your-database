import sqlite3

conn = sqlite3.connect("sample.db")
cursor = conn.cursor()

tables = cursor.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type = 'table'
    AND name NOT LIKE 'sqlite_%'
""").fetchall()

for (table,) in tables:
    print(f"\nTABLE: {table}")

    count = cursor.execute(
        f'SELECT COUNT(*) FROM "{table}"'
    ).fetchone()[0]

    print("ROWS:", count)

    data = cursor.execute(
        f'SELECT * FROM "{table}" LIMIT 10'
    ).fetchall()

    print("DATA:", data)

conn.close()