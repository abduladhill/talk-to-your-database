"""Shared read-only query engine, ambiguity detection, explanation, and result interpretation."""

import csv
import io
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import sqlglot
from sqlglot import exp

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:4b"
DEFAULT_TIMEOUT = 120


# ==================================================
# SCHEMA AND LLM CLIENT HELPERS
# ==================================================

def get_schema(db_path: Path) -> str:
    """Return the schema of all user-created tables."""
    target_path = Path(db_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Database not found: {target_path}")

    conn = sqlite3.connect(target_path)
    try:
        rows = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    finally:
        conn.close()
    return "\n".join(f"{name}: {sql}" for name, sql in rows if sql)


def call_ollama(prompt: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Send a prompt to the configured local Ollama model."""
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    message = response.json().get("message", {})
    content = message.get("content", "").strip()
    return content


def check_ollama_status(model_name: str = MODEL, timeout: float = 2.0) -> Dict[str, Any]:
    """
    Check if the local Ollama server is running and if the target model is installed.
    Does not assume connectivity without a live health probe.
    """
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            models = [m.get("name", "") for m in data.get("models", [])]
            target = model_name.lower()
            model_found = any(
                target == m.lower() or target in m.lower() or m.lower().startswith(target.split(":")[0])
                for m in models
            )
            return {
                "connected": True,
                "model_available": model_found,
                "available_models": models,
                "target_model": model_name,
                "error": None,
            }
        return {
            "connected": False,
            "model_available": False,
            "available_models": [],
            "target_model": model_name,
            "error": f"Ollama returned HTTP status {resp.status_code}",
        }
    except Exception as err:
        return {
            "connected": False,
            "model_available": False,
            "available_models": [],
            "target_model": model_name,
            "error": str(err),
        }


# ==================================================
# FEATURE 2: AMBIGUITY DETECTION AND CLARIFICATION
# ==================================================

AMBIGUITY_PATTERNS = [
    (
        r"\b(top|best|leading|star)\s+(employees?|workers?|staff|people)\b",
        "The request for 'top employees' is ambiguous because multiple ranking metrics are possible.",
        ["Highest salary", "Lowest ID / earliest hire", "Alphabetical by name"],
    ),
    (
        r"\b(recent|latest|newest)\s+(orders?|transactions?|sales?|records?)\b",
        "The timeframe or ordering for 'recent records' is ambiguous.",
        ["Highest primary key ID (most recent insertion)", "Alphabetical order", "All records"],
    ),
    (
        r"\b(best|top|leading)\s+(departments?|divisions?|teams?)\b",
        "The criteria for 'best department' is ambiguous under the current data.",
        ["Department with highest average salary", "Department with most employees", "Department with highest total payroll"],
    ),
    (
        r"\b(worst|lowest)\s+(departments?|divisions?|teams?)\b",
        "The criteria for 'lowest department' is ambiguous.",
        ["Department with lowest average salary", "Department with fewest employees", "Department with lowest total payroll"],
    ),
    (
        r"\b(most|highest)\s+experienced\b",
        "Employee experience or tenure information is not explicitly tracked in the database.",
        ["Lowest ID / earliest record as a proxy", "Highest salary as a proxy"],
    ),
]


def detect_ambiguity(question: str, schema: str = "", retrieved_context: str = "") -> Dict[str, Any]:
    """
    Detect whether a natural language question has multiple reasonable interpretations.
    Returns a dict with 'is_ambiguous', 'clarification_question', and 'options'.
    """
    clean_q = question.strip()
    # If the user has already provided a clarification in parentheses or prefix, it is not ambiguous.
    if "(clarification:" in clean_q.lower() or "clarification:" in clean_q.lower():
        return {"is_ambiguous": False, "clarification_question": "", "options": []}

    # Check known heuristic ambiguity patterns
    for pattern, explanation, options in AMBIGUITY_PATTERNS:
        if re.search(pattern, clean_q, re.IGNORECASE):
            # Check if metric was already specified in the question (e.g. "top employees by salary")
            if re.search(r"\bby\s+(salary|pay|id|name|department|count)\b", clean_q, re.IGNORECASE):
                continue

            return {
                "is_ambiguous": True,
                "clarification_question": f"{explanation} How would you like to interpret it?",
                "options": options,
            }

    return {"is_ambiguous": False, "clarification_question": "", "options": []}


def combine_clarification(original_question: str, clarification: str) -> str:
    """Combine the original ambiguous question with the user's chosen clarification."""
    base = original_question.strip().rstrip(".?!")
    clean_clarification = clarification.strip()
    return f"{base} (Clarification: {clean_clarification})"


# ==================================================
# FEATURE 3: UNSUPPORTED QUERY DETECTION
# ==================================================

UNSUPPORTED_PATTERNS = [
    # Predictions / Forecasting
    (
        r"\b(predict|forecast|future|next\s+month|next\s+year|trend\s+projection)\b",
        "The database contains historical and current operational records; it does not contain a machine learning model or forecasting data to project future values.",
        "Predictive models and future trend projections.",
        "View current records instead, e.g. 'What is the average employee salary?' or 'Show each employee and their salary.'",
    ),
    # Image / Multimedia / File blobs
    (
        r"\b(photo|photograph|picture|image|avatar|headshot|audio|video|document\s+file)\b",
        "The database schema does not store photo or binary multimedia files.",
        "Image/photo columns in the database tables.",
        "View available employee textual details: 'Show all employees' or 'Show each employee and their salary.'",
    ),
    # Out of scope company contact info
    (
        r"\b(company\s+name|office\s+address|headquarters|company\s+address)\b",
        "The database contains tables for employees, products, and customers, but contains no company profile, office location, or headquarters address table.",
        "Company profile or headquarters address table.",
        "Query existing tables: 'What departments exist in the employee data?' or 'Show all employees.'",
    ),
    # Personal sensitive fields not in table (email, phone)
    (
        r"\b(email|phone|mobile|telephone|social\s+security|ssn|home\s+address)\b",
        "The requested contact or identity field is not stored in the database.",
        "Contact information columns (email, phone, home address).",
        "Query supported employee fields: 'Show employee name, department, and salary.'",
    ),
    # Destructive write operations in Query Mode
    (
        r"^\s*(delete|drop|insert|update|alter|truncate|create)\b",
        "Query Mode is strictly read-only and prohibits INSERT, UPDATE, DELETE, DROP, and ALTER statements.",
        "Write authorization (Query Mode is read-only). Use Build Mode to create tables.",
        "Inspect matching records with a SELECT query: 'Find employees with a salary below 40000.'",
    ),
]


def detect_unsupported(question: str, schema: str = "", retrieved_context: str = "") -> Dict[str, Any]:
    """
    Detect whether a question cannot be answered by the SQLite database or is prohibited in Query Mode.
    Returns a dict with 'is_unsupported', 'reason', 'missing_info', and 'suggestion'.
    """
    clean_q = question.strip()

    # Destructive write operations in Query Mode
    if re.search(r"^\s*(delete|drop|insert|update|alter|truncate|create)\b", clean_q, re.IGNORECASE):
        del_match = re.match(r"^\s*delete\s+(?:from\s+)?(?:the\s+)?(.+)", clean_q, re.IGNORECASE)
        if del_match:
            target = del_match.group(1).strip().rstrip(".?!")
            suggestion = f"Show {target}." if target.lower().startswith(("all ", "the ", "each ")) else f"Show the {target}."
        elif re.match(r"^\s*drop\s+table\s+(\w+)", clean_q, re.IGNORECASE):
            tbl = re.match(r"^\s*drop\s+table\s+(\w+)", clean_q, re.IGNORECASE).group(1)
            suggestion = f"Show all records from {tbl}."
        elif re.match(r"^\s*update\s+(\w+)", clean_q, re.IGNORECASE):
            tbl = re.match(r"^\s*update\s+(\w+)", clean_q, re.IGNORECASE).group(1)
            suggestion = f"Show all records from {tbl}."
        else:
            suggestion = "Show all employees."

        return {
            "is_unsupported": True,
            "reason": "Query Mode is strictly read-only and prohibits INSERT, UPDATE, DELETE, DROP, and ALTER statements.",
            "missing_info": "Write authorization (Query Mode is read-only). Use Build Mode to create tables or Data Entry Mode to insert records.",
            "suggestion": suggestion,
        }

    for pattern, reason, missing_info, suggestion in UNSUPPORTED_PATTERNS:
        if re.search(pattern, clean_q, re.IGNORECASE):
            return {
                "is_unsupported": True,
                "reason": reason,
                "missing_info": missing_info,
                "suggestion": suggestion,
            }

    # Out of scope queries asking for external knowledge (weather, stocks, sports)
    external_domains = [r"\bweather\b", r"\bstock\s+price\b", r"\bpresident\b", r"\bcapital\s+city\b", r"\bmovie\b"]
    for ext in external_domains:
        if re.search(ext, clean_q, re.IGNORECASE):
            return {
                "is_unsupported": True,
                "reason": "The question asks for external world knowledge that is not stored in this SQLite database.",
                "missing_info": "External dataset.",
                "suggestion": "Ask questions about existing database tables such as employees, products, or customers.",
            }

    return {"is_unsupported": False, "reason": "", "missing_info": "", "suggestion": ""}


# ==================================================
# FEATURE 1: IMPROVED SQL GENERATION WITH TERMINOLOGY MATCHING
# ==================================================

def generate_sql(question: str, schema: str, retrieved_context: str) -> str:
    """Generate a single guarded SQLite SELECT query from natural language."""
    # Check unsupported queries first
    unsupported_check = detect_unsupported(question, schema, retrieved_context)
    if unsupported_check["is_unsupported"]:
        return (
            f"UNSUPPORTED: {unsupported_check['reason']} "
            f"Missing information: {unsupported_check['missing_info']} "
            f"Suggested query: {unsupported_check['suggestion']}"
        )

    # Check ambiguity
    ambiguity_check = detect_ambiguity(question, schema, retrieved_context)
    if ambiguity_check["is_ambiguous"]:
        options_str = " | ".join(ambiguity_check["options"])
        return f"AMBIGUOUS: {ambiguity_check['clarification_question']} | OPTIONS: {options_str}"

    prompt = f"""
You convert natural-language questions into SQLite SELECT queries.

Database Schema:
{schema}

Retrieved Schema & Categorical Values:
{retrieved_context}

Rules:
1. Return only one SQL SELECT statement for supported questions.
2. Use ONLY tables and columns in the database schema.
3. Database Terminology & Value Matching:
   - When the user uses domain terms, synonyms, or colloquial expressions (e.g., 'technical department', 'tech team', 'staff', 'pay', 'earnings'), check the schema and retrieved categorical values to find the exact database match (e.g. department='Engineering', salary, employees).
   - Use exact categorical values stored in the database (e.g., 'Engineering', 'HR', 'Sales').
   - Do NOT invent column names or categorical values that are not present in the database.
4. Security & Safety:
   - Never generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, DETACH, PRAGMA, VACUUM, or multiple statements.
5. Formatting & Cleanliness:
   - Do NOT use Markdown fences (```sql or ```). Output raw SQL only.
   - For grouped counts, include the grouping column in SELECT.
   - Give aggregate columns clear aliases (e.g. employee_count, average_salary).
6. Unsupported Questions:
   - If the question cannot be answered from the schema, return:
     UNSUPPORTED: followed by a brief explanation.

Question:
{question}
"""
    raw_response = call_ollama(prompt)
    # Strip markdown fences if the model included them despite instructions
    cleaned = re.sub(r"^```(?:sql)?\s*", "", raw_response, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


# ==================================================
# FEATURE 4: BOUNDED SELF-CORRECTION
# ==================================================

def correct_sql(
    question: str,
    schema: str,
    retrieved_context: str,
    failed_sql: str,
    error: str,
) -> str:
    """Correct a failed SQLite query using database error feedback, bounded and guarded."""
    # If the user question itself is unsupported, do not attempt to patch SQL
    unsupported_check = detect_unsupported(question, schema, retrieved_context)
    if unsupported_check["is_unsupported"]:
        return f"UNSUPPORTED: {unsupported_check['reason']} Suggested query: {unsupported_check['suggestion']}"

    prompt = f"""
Correct a failed SQLite query for the user's question.

Database Schema:
{schema}

Retrieved Schema and Categorical Values:
{retrieved_context}

Original Question:
{question}

Failed SQL:
{failed_sql}

Execution or Validation Error:
{error}

Rules:
1. Return ONLY one corrected SQLite SELECT query.
2. Fix the error by strictly using tables and columns from the schema.
3. Match categorical values exactly from the database (e.g. department names, status values).
4. Do NOT change the user's intent.
5. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ATTACH, DETACH, PRAGMA, VACUUM, or multiple statements.
6. Do NOT use Markdown fences or prose explanations.
7. If the request cannot be answered from the schema, return:
   UNSUPPORTED: followed by a brief explanation.
"""
    raw_response = call_ollama(prompt)
    cleaned = re.sub(r"^```(?:sql)?\s*", "", raw_response, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


# ==================================================
# SECURITY AND SQL VALIDATION
# ==================================================

def validate_sql(sql: str) -> str:
    """
    Allow only one valid SQLite SELECT statement.
    Rejects multi-statements, non-query operations (DML/DDL), and malformed syntax.
    """
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("The generated SQL is empty.")

    # Remove any stray markdown code fences if present
    cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        statements = sqlglot.parse(cleaned, read="sqlite")
    except sqlglot.errors.ParseError as exc:
        raise ValueError(f"Invalid SQL syntax: {exc}") from exc

    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Exactly one SQL statement is permitted.")

    expression = statements[0]
    # Permit only SELECT roots and their standard compound forms. SELECT ...
    # INTO is also a Select AST, so it must be rejected separately rather
    # than relying on the root type.
    read_only_select_roots = (exp.Select, exp.Union, exp.Intersect, exp.Except)
    if not isinstance(expression, read_only_select_roots):
        raise ValueError("Only SELECT queries are permitted.")

    prohibited = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Alter,
        exp.Create,
        exp.Command,
        exp.Into,
    )
    if any(isinstance(node, prohibited) for node in expression.walk()):
        raise ValueError("The query contains a prohibited SQL operation.")

    return expression.sql(dialect="sqlite")


def execute_query(db_path: Path, sql: str) -> Tuple[List[str], List[Tuple[Any, ...]]]:
    """Execute a query through a read-only SQLite connection with a strict timeout and row limit."""
    sql = validate_sql(sql)
    target_path = Path(db_path).resolve()
    if not target_path.exists():
        raise FileNotFoundError(f"Database not found: {target_path}")

    uri = target_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.set_progress_handler(lambda: 1, 100_000)
        cursor = conn.execute(sql)
        if cursor.description is None:
            raise ValueError("The query did not return a result set.")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchmany(200)
    finally:
        conn.close()
    return columns, rows


# ==================================================
# FEATURE 5: SQL EXPLANATION
# ==================================================

def explain_sql(sql: str, question: str = "") -> str:
    """
    Produce a concise, beginner-friendly explanation of the generated SQL query.
    Uses AST analysis for 100% factual accuracy without hallucinating operations.
    """
    try:
        parsed = sqlglot.parse_one(sql, read="sqlite")
    except Exception:
        return "Executes the generated SQL query against the database."

    # Extract tables
    tables = [t.name for t in parsed.find_all(exp.Table)]
    table_str = ", ".join(f"'{t}'" for t in set(tables)) if tables else "the database"

    # Extract selected items
    select_expressions = parsed.expressions
    is_select_all = any(isinstance(e, exp.Star) for e in select_expressions)

    # Extract aggregations
    has_count = any(isinstance(node, exp.Count) for node in parsed.walk())
    has_sum = any(isinstance(node, exp.Sum) for node in parsed.walk())
    has_avg = any(isinstance(node, exp.Avg) for node in parsed.walk())
    has_min = any(isinstance(node, exp.Min) for node in parsed.walk())
    has_max = any(isinstance(node, exp.Max) for node in parsed.walk())

    # Clauses
    has_where = parsed.find(exp.Where) is not None
    has_group = parsed.find(exp.Group) is not None
    has_order = parsed.find(exp.Order) is not None
    limit_node = parsed.find(exp.Limit)
    limit_val = limit_node.expression.this if limit_node and hasattr(limit_node, "expression") else None

    parts = []
    if is_select_all:
        parts.append(f"Selects all columns from {table_str}")
    elif has_count and not (has_sum or has_avg):
        parts.append(f"Counts records in {table_str}")
    elif has_sum or has_avg or has_min or has_max:
        agg_types = []
        if has_sum: agg_types.append("total")
        if has_avg: agg_types.append("average")
        if has_min: agg_types.append("minimum")
        if has_max: agg_types.append("maximum")
        parts.append(f"Calculates the {', '.join(agg_types)} values from {table_str}")
    else:
        col_names = []
        for e in select_expressions:
            if hasattr(e, "alias_or_name") and e.alias_or_name:
                col_names.append(e.alias_or_name)
            elif hasattr(e, "this") and hasattr(e.this, "name"):
                col_names.append(e.this.name)
            else:
                col_names.append(str(e))
        col_str = ", ".join(col_names[:4])
        if len(col_names) > 4:
            col_str += f", and {len(col_names) - 4} more"
        parts.append(f"Retrieves {col_str} from {table_str}")

    if has_where:
        parts.append("filters the rows based on specified conditions")

    if has_group:
        group_cols = [str(g) for g in parsed.find(exp.Group).expressions]
        parts.append(f"groups the data by {', '.join(group_cols)}")

    if has_order:
        order_clause = parsed.find(exp.Order)
        desc = any(o.args.get("desc") for o in order_clause.expressions)
        direction = "descending (highest first)" if desc else "ascending (lowest first)"
        parts.append(f"sorts the results in {direction} order")

    if limit_val:
        parts.append(f"limits output to {limit_val} record(s)")

    if not parts:
        return "Executes the query to retrieve data from the database."

    explanation = parts[0]
    if len(parts) > 1:
        explanation += ", " + ", ".join(parts[1:])
    return explanation + "."


# ==================================================
# FEATURE 6: NATURAL-LANGUAGE RESULT SUMMARY
# ==================================================

def summarize_results(
    question: str,
    sql: str,
    columns: List[str],
    rows: List[Tuple[Any, ...]],
) -> str:
    """
    Produce a concise natural-language explanation strictly based on returned rows.
    Handles empty results, single rows, aggregates, and large result sets.
    Never executes SQL.
    """
    row_count = len(rows)

    if row_count == 0:
        return "The query executed successfully, but no matching records were found in the database."

    # Single aggregate count or single metric value
    if row_count == 1 and len(columns) == 1:
        col_name = columns[0]
        val = rows[0][0]
        if isinstance(val, (int, float)):
            if "count" in col_name.lower():
                return f"There are {val:,} record(s) matching your question."
            if "total" in col_name.lower() or "sum" in col_name.lower() or "salary" in col_name.lower():
                return f"The total calculated value is {val:,.2f}."
            if "avg" in col_name.lower() or "average" in col_name.lower():
                return f"The average value is {val:,.2f}."
            return f"The result for {col_name} is {val:,}."
        return f"The result is {val}."

    # Single row with multiple columns (e.g. highest paid employee: ('Meera', 52000.0))
    if row_count == 1:
        details = [f"{col}: {val}" for col, val in zip(columns, rows[0])]
        return f"Found 1 matching record: {', '.join(details)}."

    # Department / categorical grouping (e.g. [('Engineering', 2), ('HR', 2), ('Sales', 1)])
    if 2 <= row_count <= 6 and len(columns) == 2:
        group_col, metric_col = columns[0], columns[1]
        group_summaries = []
        for row in rows:
            group_summaries.append(f"{row[0]} ({row[1]})")
        return f"Results by {group_col}: {', '.join(group_summaries)}."

    # General multi-row summary
    if row_count <= 5:
        sample_rows = [str(dict(zip(columns, row))) for row in rows]
        return f"Returned {row_count} records: {'; '.join(sample_rows)}."

    # Larger result set: summarize count and representative sample
    first_few = [str(dict(zip(columns, row))) for row in rows[:2]]
    return (
        f"Returned {row_count} records in total. "
        f"First records include: {'; '.join(first_few)}."
    )


# ==================================================
# FEATURE 8: RESULT HANDLING AND CSV EXPORT
# ==================================================

def results_to_csv(columns: List[str], rows: List[Tuple[Any, ...]]) -> str:
    """
    Format query results as a clean CSV string in memory.
    Strictly read-only and never modifies the database.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)
    return output.getvalue()
