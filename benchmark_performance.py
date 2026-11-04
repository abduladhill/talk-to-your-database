"""
Performance analysis and latency benchmark for Talk to Your Database.
Measures cold-start vs. warm-run latencies across 5 query archetypes:
- Simple SELECT
- Filtering
- Aggregation
- Multi-table JOIN
- Complex GROUP BY / HAVING
"""

import gc
import json
import os
import sqlite3
import time
import tracemalloc
from pathlib import Path

from schema_retriever import build_retriever, search_schema
from query_engine import generate_sql, validate_sql, execute_query, get_schema


DB_PATH = Path(__file__).parent / "sample.db"


def run_benchmark():
    tracemalloc.start()
    schema = get_schema(DB_PATH)

    # 1. Cold start measurement
    cold_t0 = time.perf_counter()
    model, index, docs = build_retriever(DB_PATH)
    cold_retriever_time = time.perf_counter() - cold_t0

    # Cold generation
    cold_q = "What is the average salary by department?"
    retrieved = search_schema(cold_q, model, index, docs, top_k=3)
    context = "\n\n".join(r["document"]["content"] for r in retrieved)

    gen_t0 = time.perf_counter()
    cold_sql = generate_sql(cold_q, schema, context)
    cold_gen_time = time.perf_counter() - gen_t0

    exec_t0 = time.perf_counter()
    safe_sql = validate_sql(cold_sql)
    cols, rows = execute_query(DB_PATH, safe_sql)
    cold_exec_time = (time.perf_counter() - exec_t0) * 1000.0

    cold_total = cold_retriever_time + cold_gen_time + (cold_exec_time / 1000.0)

    cold_metrics = {
        "retriever_build_ms": round(cold_retriever_time * 1000.0, 2),
        "sql_generation_sec": round(cold_gen_time, 3),
        "execution_ms": round(cold_exec_time, 2),
        "total_response_sec": round(cold_total, 3),
    }

    # 2. Warm run measurements across 5 query archetypes
    test_queries = [
        ("Simple SELECT", "Show all employee names and their salaries."),
        ("Filtering", "Find employees in Engineering with salary above 45000."),
        ("Aggregation", "What is the total salary expenditure across all departments?"),
        ("JOIN", "Show pairs of distinct employees who work in the same department."),
        ("Complex HAVING", "Which departments have more than one employee and an average salary over 35000?"),
    ]

    warm_results = []
    for category, query in test_queries:
        # Measure schema retrieval
        r_t0 = time.perf_counter()
        retrieved_items = search_schema(query, model, index, docs, top_k=3)
        retrieval_ms = (time.perf_counter() - r_t0) * 1000.0
        retrieved_text = "\n\n".join(r["document"]["content"] for r in retrieved_items)

        # Measure SQL generation with Qwen3:4b
        g_t0 = time.perf_counter()
        raw_sql = generate_sql(query, schema, retrieved_text)
        gen_sec = time.perf_counter() - g_t0

        # Measure validation & execution
        e_t0 = time.perf_counter()
        clean_sql = validate_sql(raw_sql)
        q_cols, q_rows = execute_query(DB_PATH, clean_sql)
        exec_ms = (time.perf_counter() - e_t0) * 1000.0

        total_sec = (retrieval_ms / 1000.0) + gen_sec + (exec_ms / 1000.0)

        warm_results.append({
            "archetype": category,
            "query": query,
            "generated_sql": clean_sql,
            "row_count": len(q_rows),
            "retrieval_latency_ms": round(retrieval_ms, 2),
            "generation_latency_sec": round(gen_sec, 3),
            "execution_latency_ms": round(exec_ms, 2),
            "total_latency_sec": round(total_sec, 3),
        })

    current_mem, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    summary = {
        "cold_start": cold_metrics,
        "warm_runs": warm_results,
        "average_warm_generation_sec": round(sum(r["generation_latency_sec"] for r in warm_results) / len(warm_results), 3),
        "average_warm_retrieval_ms": round(sum(r["retrieval_latency_ms"] for r in warm_results) / len(warm_results), 2),
        "average_warm_execution_ms": round(sum(r["execution_latency_ms"] for r in warm_results) / len(warm_results), 2),
        "average_warm_total_sec": round(sum(r["total_latency_sec"] for r in warm_results) / len(warm_results), 3),
        "memory_traced_peak_mb": round(peak_mem / (1024 * 1024), 2),
    }

    output_path = Path(__file__).parent / "performance_benchmark_results.json"
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    run_benchmark()

