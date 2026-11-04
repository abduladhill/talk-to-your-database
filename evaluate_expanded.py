"""Run the expanded Talk to Your Database evaluation benchmark."""

import argparse
import ast
import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

from query_engine import (
    correct_sql,
    detect_ambiguity,
    detect_unsupported,
    execute_query,
    explain_sql,
    generate_sql,
    get_schema,
    summarize_results,
    validate_sql,
)
from schema_retriever import build_retriever, search_schema


BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sample.db"
EXPANDED_DATASET_PATH = BASE_DIR / "evaluation_dataset_expanded.csv"
MAX_CORRECTION_ATTEMPTS = 2

CONFIGURATIONS = {
    "baseline": {"rag": False, "self_correction": False, "intent_check": False},
    "rag": {"rag": True, "self_correction": False, "intent_check": True},
    "full_system": {"rag": True, "self_correction": True, "intent_check": True},
}


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def retrieve(question: str, retriever: Any) -> str:
    if retriever is None:
        return ""
    embedding_model, index, documents = retriever
    items = search_schema(question, embedding_model, index, documents, top_k=3)
    return "\n\n".join(item["document"]["content"] for item in items)


def run_question(row: Dict[str, str], config_name: str, schema: str, retriever: Any) -> Dict[str, Any]:
    configuration = CONFIGURATIONS[config_name]
    started = time.perf_counter()
    question = row["question"]
    category = row["category"]
    expected_sql = row["expected_sql"].strip()
    expected_unsupported = expected_sql == "UNSUPPORTED"
    expected_ambiguous = expected_sql == "AMBIGUOUS"

    attempts = []
    error = ""
    result = None
    final_sql = ""
    sql_valid = False
    ambiguity_detected = False
    unsupported_detected = False
    clarification_info = None

    retrieved_context = retrieve(question, retriever) if configuration["rag"] else ""

    try:
        # Step 1: Intent check if enabled
        if configuration["intent_check"]:
            unsupported_info = detect_unsupported(question, schema, retrieved_context)
            if unsupported_info["is_unsupported"]:
                unsupported_detected = True
                final_sql = f"UNSUPPORTED: {unsupported_info['reason']} Missing: {unsupported_info['missing_info']}"

            if not unsupported_detected:
                amb_info = detect_ambiguity(question, schema, retrieved_context)
                if amb_info["is_ambiguous"]:
                    ambiguity_detected = True
                    clarification_info = amb_info
                    final_sql = f"AMBIGUOUS: {amb_info['clarification_question']} | OPTIONS: {' | '.join(amb_info['options'])}"

        # Step 2: SQL generation if not already marked ambiguous or unsupported
        if not (unsupported_detected or ambiguity_detected):
            current_sql = generate_sql(question, schema, retrieved_context)
            initial_sql = current_sql

            for attempt_number in range(MAX_CORRECTION_ATTEMPTS + 1):
                if current_sql.upper().startswith("UNSUPPORTED:"):
                    unsupported_detected = True
                    final_sql = current_sql
                    break
                if current_sql.upper().startswith("AMBIGUOUS:"):
                    ambiguity_detected = True
                    final_sql = current_sql
                    break

                try:
                    safe_sql = validate_sql(current_sql)
                    sql_valid = True
                    columns, rows = execute_query(DB_PATH, safe_sql)
                    final_sql = safe_sql
                    result = {"columns": columns, "rows": rows}
                    break
                except Exception as exc:
                    error = str(exc)
                    attempts.append({"sql": current_sql, "error": error})
                    if not configuration["self_correction"] or attempt_number == MAX_CORRECTION_ATTEMPTS:
                        final_sql = current_sql
                        break
                    current_sql = correct_sql(
                        question, schema, retrieved_context, current_sql, error
                    )
        else:
            initial_sql = final_sql

    except requests.RequestException as exc:
        initial_sql = ""
        error = f"Ollama request failed: {exc}"
    except Exception as exc:
        initial_sql = ""
        error = f"Unexpected evaluation error: {exc}"

    # Determine correctness based on category
    if expected_unsupported:
        correct = unsupported_detected or final_sql.upper().startswith("UNSUPPORTED:")
        execution_success = correct
    elif expected_ambiguous:
        correct = ambiguity_detected or final_sql.upper().startswith("AMBIGUOUS:")
        execution_success = correct
    else:
        try:
            expected_rows = ast.literal_eval(row["expected_result"])
            exact_match = (result is not None and result["rows"] == expected_rows)
        except Exception:
            exact_match = False
        execution_success = (result is not None and error == "")
        correct = exact_match

    latency = round(time.perf_counter() - started, 3)

    return {
        "question": question,
        "category": category,
        "notes": row["notes"],
        "expected_sql": expected_sql,
        "expected_result": row["expected_result"],
        "initial_sql": initial_sql,
        "generated_sql": final_sql,
        "execution_result": result,
        "sql_valid": sql_valid,
        "execution_success": execution_success,
        "correct": correct,
        "error": error,
        "ambiguity_detected": ambiguity_detected,
        "unsupported_detected": unsupported_detected,
        "correction_attempts": len(attempts),
        "attempt_details": attempts,
        "latency_seconds": latency,
    }


def run_expanded_configuration(name: str, rows: List[Dict[str, str]], output_dir: Path) -> Dict[str, Any]:
    configuration = CONFIGURATIONS[name]
    schema = get_schema(DB_PATH)
    retriever = build_retriever(DB_PATH) if configuration["rag"] else None

    results = [run_question(row, name, schema, retriever) for row in rows]

    total_q = len(results)
    correct_count = sum(item["correct"] for item in results)
    exec_success_count = sum(item["execution_success"] for item in results)
    sql_valid_count = sum(item["sql_valid"] for item in results)

    # Targeted sub-metric counts
    amb_questions = [r for r in results if r["expected_sql"] == "AMBIGUOUS"]
    amb_detected = sum(1 for r in amb_questions if r["ambiguity_detected"])

    unsupp_questions = [r for r in results if r["expected_sql"] == "UNSUPPORTED"]
    unsupp_detected = sum(1 for r in unsupp_questions if r["unsupported_detected"])

    corrections_triggered = [r for r in results if r["correction_attempts"] > 0]
    corrections_succeeded = sum(1 for r in corrections_triggered if r["correct"])

    avg_latency = round(sum(r["latency_seconds"] for r in results) / total_q, 2) if total_q else 0

    report = {
        "configuration": name,
        "settings": configuration,
        "dataset": EXPANDED_DATASET_PATH.name,
        "question_count": total_q,
        "correct_count": correct_count,
        "exact_accuracy": round(correct_count / total_q, 4) if total_q else 0,
        "execution_accuracy": round(exec_success_count / total_q, 4) if total_q else 0,
        "sql_validity_rate": round(sql_valid_count / total_q, 4) if total_q else 0,
        "ambiguity_detection_rate": round(amb_detected / len(amb_questions), 4) if amb_questions else 1.0,
        "unsupported_detection_rate": round(unsupp_detected / len(unsupp_questions), 4) if unsupp_questions else 1.0,
        "self_correction_success_rate": round(corrections_succeeded / len(corrections_triggered), 4) if corrections_triggered else 1.0,
        "average_latency_seconds": avg_latency,
        "results": results,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"expanded_{name}.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    csv_path = output_dir / f"expanded_{name}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fields = list(results[0]) if results else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for res in results:
            writer.writerow({
                k: json_value(v) if isinstance(v, (dict, list)) else v
                for k, v in res.items()
            })

    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", choices=[*CONFIGURATIONS, "all"], default="all")
    parser.add_argument("--output-dir", type=Path, default=BASE_DIR / "evaluation_results")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with EXPANDED_DATASET_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    configs_to_run = CONFIGURATIONS if args.config == "all" else [args.config]
    reports = [run_expanded_configuration(c, rows, args.output_dir) for c in configs_to_run]

    summary = [
        {
            "configuration": r["configuration"],
            "question_count": r["question_count"],
            "correct_count": r["correct_count"],
            "exact_accuracy": r["exact_accuracy"],
            "execution_accuracy": r["execution_accuracy"],
            "sql_validity_rate": r["sql_validity_rate"],
            "ambiguity_detection_rate": r["ambiguity_detection_rate"],
            "unsupported_detection_rate": r["unsupported_detection_rate"],
            "average_latency_seconds": r["average_latency_seconds"],
        }
        for r in reports
    ]

    summary_file = args.output_dir / "expanded_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

