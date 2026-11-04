"""Run the Talk to Your Database evaluation configurations."""

import argparse
import ast
import csv
import json
import time
from pathlib import Path

import requests

from query_engine import (
    correct_sql, execute_query, generate_sql, get_schema, validate_sql,
)
from schema_retriever import build_retriever, search_schema


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sample.db"
DATASET_PATH = BASE_DIR / "evaluation_dataset.csv"
MAX_CORRECTION_ATTEMPTS = 2
CONFIGURATIONS = {
    "baseline": {"rag": False, "self_correction": False},
    "rag": {"rag": True, "self_correction": False},
    "full_system": {"rag": True, "self_correction": True},
}


def json_value(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def retrieve(question, retriever):
    if retriever is None:
        return ""
    embedding_model, index, documents = retriever
    items = search_schema(question, embedding_model, index, documents, top_k=3)
    return "\n\n".join(item["document"]["content"] for item in items)


def run_question(row, configuration, schema, retriever):
    started = time.perf_counter()
    expected_unsupported = row["expected_sql"].strip().upper() == "N/A"
    attempts = []
    error = ""
    result = None
    final_sql = ""
    retrieved_context = retrieve(row["question"], retriever)

    try:
        current_sql = generate_sql(row["question"], schema, retrieved_context)
        initial_sql = current_sql
        for attempt_number in range(MAX_CORRECTION_ATTEMPTS + 1):
            if current_sql.upper().startswith("UNSUPPORTED:"):
                final_sql = current_sql
                break
            try:
                safe_sql = validate_sql(current_sql)
                columns, rows = execute_query(DB_PATH, safe_sql)
                final_sql = safe_sql
                result = {"columns": columns, "rows": rows}
                break
            except Exception as exc:  # Record model/SQL failures per attempt.
                error = str(exc)
                attempts.append({"sql": current_sql, "error": error})
                if not configuration["self_correction"] or attempt_number == MAX_CORRECTION_ATTEMPTS:
                    final_sql = current_sql
                    break
                current_sql = correct_sql(
                    row["question"], schema, retrieved_context, current_sql, error
                )
        else:
            initial_sql = ""
    except requests.RequestException as exc:
        initial_sql = ""
        error = f"Ollama request failed: {exc}"
    except Exception as exc:
        initial_sql = ""
        error = f"Unexpected evaluation error: {exc}"

    if expected_unsupported:
        correct = final_sql.upper().startswith("UNSUPPORTED:")
    else:
        expected_rows = ast.literal_eval(row["expected_result"])
        correct = result is not None and result["rows"] == expected_rows

    return {
        "question": row["question"],
        "category": row["category"],
        "notes": row["notes"],
        "expected_sql": row["expected_sql"],
        "expected_result": row["expected_result"],
        "expected_unsupported": expected_unsupported,
        "initial_sql": initial_sql,
        "generated_sql": final_sql,
        "execution_result": result,
        "correct": correct,
        "error": error,
        "correction_attempts": len(attempts),
        "attempt_details": attempts,
        "latency_seconds": round(time.perf_counter() - started, 3),
    }


def run_configuration(name, rows, output_dir):
    configuration = CONFIGURATIONS[name]
    schema = get_schema(DB_PATH)
    retriever = build_retriever() if configuration["rag"] else None
    results = [run_question(row, configuration, schema, retriever) for row in rows]
    correct_count = sum(item["correct"] for item in results)
    report = {
        "configuration": name,
        "settings": configuration,
        "dataset": DATASET_PATH.name,
        "question_count": len(results),
        "correct_count": correct_count,
        "accuracy": correct_count / len(results) if results else 0,
        "results": results,
    }
    (output_dir / f"{name}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    with (output_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(results[0]) if results else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow({
                key: json_value(value) if isinstance(value, (dict, list)) else value
                for key, value in result.items()
            })
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", choices=[*CONFIGURATIONS, "all"], default="all")
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR / "evaluation_results")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with DATASET_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    names = CONFIGURATIONS if args.config == "all" else [args.config]
    reports = [run_configuration(name, rows, args.output_dir) for name in names]
    summary = [{
        "configuration": report["configuration"],
        "question_count": report["question_count"],
        "correct_count": report["correct_count"],
        "accuracy": report["accuracy"],
    } for report in reports]
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
