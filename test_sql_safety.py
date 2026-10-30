import sqlglot
from sqlglot import exp

tests = [
    "SELECT * FROM employees",
    "DELETE FROM employees",
    "SELECT * FROM employees; DROP TABLE employees",
]

for sql in tests:
    try:
        parsed = sqlglot.parse(sql, read="sqlite")

        safe = (
            len(parsed) == 1
            and parsed[0] is not None
            and isinstance(parsed[0], exp.Query)
            and not any(
                isinstance(
                    node,
                    (
                        exp.Insert,
                        exp.Update,
                        exp.Delete,
                        exp.Drop,
                        exp.Alter,
                        exp.Create,
                        exp.Command,
                    ),
                )
                for node in parsed[0].walk()
            )
        )

        print(("PASS" if safe else "REJECTED") + ": " + sql)

    except Exception as error:
        print("REJECTED: " + sql)