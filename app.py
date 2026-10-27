
import sqlite3
import re
from pathlib import Path

import streamlit as st
import requests

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sample.db"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:4b"

st.set_page_config(page_title="Talk to Your Database", page_icon="🗄️")
st.title("Talk to Your Database")
st.caption("Ask questions about your SQLite database in plain English.")

def get_schema():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """).fetchall()
    return "\n".join(f"{name}: {sql}" for name, sql in rows)

def generate_sql(question, schema):
    prompt = f"""
You convert natural-language questions into SQLite SELECT queries.

Rules:
- Return only one SQL SELECT statement.
- Use only tables and columns in the supplied schema.
- Never generate INSERT, UPDATE, DELETE, DROP, ALTER,
  CREATE, ATTACH, PRAGMA, or multiple statements.
- If the question cannot be answered from the schema,
  explain that it is unsupported instead of generating SQL.

Schema:
{schema}

Question: {question}
"""
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["message"]["content"].strip()

def validate_sql(sql):
    # Allow only one SELECT statement, with optional trailing semicolon.
    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    if not re.match(r"(?is)^SELECT\b", cleaned):
        raise ValueError("Only SELECT queries are permitted.")

    if ";" in cleaned:
        raise ValueError("Multiple SQL statements are not permitted.")

    forbidden = (
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|"
        r"ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|TRIGGER)\b"
    )
    if re.search(forbidden, cleaned, flags=re.IGNORECASE):
        raise ValueError("The query contains a prohibited SQL operation.")

    return cleaned

def execute_query(sql):
    # SQLite URI read-only mode prevents database modifications.
    uri = DB_PATH.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.set_progress_handler(
            lambda: 1, 100_000
        )
        cursor = conn.execute(sql)
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchmany(200)
    return columns, rows

question = st.text_input(
    "Your question",
    placeholder="e.g. What is the average salary by department?"
)

if st.button("Generate and run SQL", type="primary", disabled=not question.strip()):
    try:
        schema = get_schema()
        with st.spinner("Generating SQL with the local model..."):
            generated = generate_sql(question, schema)

        st.subheader("Generated SQL")
        st.code(generated, language="sql")

        safe_sql = validate_sql(generated)
        columns, rows = execute_query(safe_sql)

        st.subheader("Query results")
        st.dataframe(
            [dict(zip(columns, row)) for row in rows],
            use_container_width=True,
        )
        st.caption(f"Returned {len(rows)} row(s), up to 200 displayed.")

    except requests.RequestException as exc:
        st.error(f"Could not reach Ollama. Check that Ollama is running. Details: {exc}")
    except (ValueError, sqlite3.Error) as exc:
        st.error(f"Query could not be executed safely: {exc}")
    except Exception as exc:
        st.error(f"Unexpected error: {exc}")
