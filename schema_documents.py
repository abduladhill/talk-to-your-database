from pathlib import Path
from schema_inspector import DB_PATH, inspect_schema
from value_profiler import build_value_documents


def build_schema_documents(db_path: Path = DB_PATH):
    target_path = Path(db_path) if db_path else DB_PATH
    schema = inspect_schema(db_path=target_path)
    documents = []

    for table_name, details in schema.items():
        columns = details["columns"]

        column_descriptions = []
        for column in columns:
            description = (
                f"{column['name']} ({column['type']})"
            )
            if column["primary_key"]:
                description += " - primary key"
            if column["not_null"]:
                description += " - required"
            column_descriptions.append(description)

        table_document = (
            f"Table: {table_name}\n"
            f"Purpose: Stores records for {table_name}.\n"
            f"Columns: {'; '.join(column_descriptions)}"
        )
        documents.append({
            "table": table_name,
            "content": table_document,
        })

        for column in columns:
            column_document = (
                f"Table: {table_name}\n"
                f"Column: {column['name']}\n"
                f"Data type: {column['type']}\n"
                f"Primary key: {column['primary_key']}\n"
                f"Required: {column['not_null']}\n"
                f"Context: {table_name} contains "
                f"{column['name']} information."
            )
            documents.append({
                "table": table_name,
                "column": column["name"],
                "content": column_document,
            })

    documents.extend(build_value_documents(db_path=target_path))
    return documents


if __name__ == "__main__":
    for document in build_schema_documents():
        print("-" * 50)
        print(document["content"])
