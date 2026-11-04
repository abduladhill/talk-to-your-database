"""
Talk to Your Database — Streamlit Application.
Provides Query Mode (strictly read-only), Build Mode (controlled table creation),
and Data Entry Mode (validated transactional record insertion).
"""

import io
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

from schema_retriever import build_retriever, search_schema
from build_engine import (
    construct_create_table_sql,
    create_table_safely as _create_table_safely,
    generate_schema_proposal,
    get_table_name_from_proposal,
    table_exists as _table_exists,
    validate_schema_proposal,
)
from data_entry_engine import (
    get_available_tables,
    get_database_overview,
    get_recent_records,
    get_table_schema_details,
    insert_record,
    validate_record_for_insertion,
)
from query_engine import (
    MODEL,
    call_ollama,
    check_ollama_status,
    combine_clarification,
    correct_sql,
    detect_ambiguity,
    detect_unsupported,
    execute_query as _execute_query,
    explain_sql,
    generate_sql,
    get_schema as _get_schema,
    results_to_csv,
    summarize_results,
    validate_sql,
)


# ==================================================
# CONFIGURATION & PAGE SETUP
# ==================================================

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "sample.db"
MAX_CORRECTION_ATTEMPTS = 2

st.set_page_config(
    page_title="Talk to Your Database",
    page_icon="🗄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom styling for a clean, professional dashboard appearance
st.markdown(
    """
    <style>
    .main-header {
        font-size: 1.85rem;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 0.95rem;
        color: #64748b;
        margin-bottom: 1.25rem;
    }
    .status-badge-connected {
        background-color: #f0fdf4;
        border: 1px solid #bbf7d0;
        color: #166534;
        padding: 6px 12px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 600;
        margin-bottom: 10px;
    }
    .status-badge-warning {
        background-color: #fefce8;
        border: 1px solid #fef08a;
        color: #854d0e;
        padding: 6px 12px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 600;
        margin-bottom: 10px;
    }
    .status-badge-offline {
        background-color: #fef2f2;
        border: 1px solid #fecaca;
        color: #991b1b;
        padding: 6px 12px;
        border-radius: 6px;
        font-size: 0.85rem;
        font-weight: 600;
        margin-bottom: 10px;
    }
    .metric-card {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 12px;
        margin-bottom: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==================================================
# SESSION STATE INITIALIZATION
# ==================================================

if "active_question" not in st.session_state:
    st.session_state.active_question = ""

if "natural_language_question" not in st.session_state:
    st.session_state.natural_language_question = ""

if "pending_query" not in st.session_state:
    st.session_state.pending_query = None

if "execute_suggested" not in st.session_state:
    st.session_state.execute_suggested = False

if "current_suggestion" not in st.session_state:
    st.session_state.current_suggestion = None

if "query_history" not in st.session_state:
    st.session_state.query_history = []

if "pending_ambiguity" not in st.session_state:
    st.session_state.pending_ambiguity = None

if "schema_proposal" not in st.session_state:
    st.session_state.schema_proposal = None

if "schema_proposal_request" not in st.session_state:
    st.session_state.schema_proposal_request = None

if "build_status" not in st.session_state:
    st.session_state.build_status = None

if "pending_insert_record" not in st.session_state:
    st.session_state.pending_insert_record = None

if "data_entry_table" not in st.session_state:
    st.session_state.data_entry_table = None

if "data_entry_status" not in st.session_state:
    st.session_state.data_entry_status = None


# ==================================================
# CACHED RETRIEVER & HELPERS
# ==================================================

@st.cache_resource
def load_retriever():
    """Load the embedding model and schema search index once."""
    return build_retriever(DB_PATH)


def get_schema() -> str:
    return _get_schema(DB_PATH)


def table_exists(table_name: str) -> bool:
    return _table_exists(table_name, DB_PATH)


def create_table_safely(sql: str, table_name: str) -> None:
    return _create_table_safely(sql, table_name, DB_PATH)


def execute_query(sql: str):
    return _execute_query(DB_PATH, sql)


# ==================================================
# SIDEBAR CONTROLS & STATUS
# ==================================================

with st.sidebar:
    st.title("🗄️ Talk to Your DB")
    st.caption("AI-Powered Local Database Assistant")

    st.markdown("---")

    # Live Ollama & Qwen3:4B Status Check
    st.markdown("**LLM Connection Status**")
    ollama_info = check_ollama_status(model_name=MODEL, timeout=1.5)

    if ollama_info["connected"] and ollama_info["model_available"]:
        st.markdown(
            f'<div class="status-badge-connected">● Ollama Online &nbsp;|&nbsp; {MODEL} Ready</div>',
            unsafe_allow_html=True,
        )
    elif ollama_info["connected"] and not ollama_info["model_available"]:
        models_str = ", ".join(ollama_info["available_models"][:3]) or "None"
        st.markdown(
            f'<div class="status-badge-warning">▲ Ollama Online &nbsp;|&nbsp; {MODEL} Missing<br><small>Installed: {models_str}</small></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="status-badge-offline">■ Ollama Offline<br><small>Start Ollama on localhost:11434</small></div>',
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # Mode Navigation
    mode = st.radio(
        "Application Mode",
        ["🔍 Query Mode", "🛠️ Build Mode", "📝 Data Entry Mode"],
        index=0,
        help="Query Mode is strictly read-only. Build Mode creates new tables. Data Entry Mode inserts validated records.",
    )

    st.markdown("---")

    # Active Database & Live Overview
    st.markdown("**Database Metadata**")
    st.caption(f"Active file: `{DB_PATH.name}`")

    with st.expander("📊 Database Overview", expanded=False):
        overview = get_database_overview(DB_PATH)
        st.write(f"**Total Tables**: {overview['total_tables']}")
        for tname, tinfo in overview["tables"].items():
            st.markdown(f"**`{tname}`** ({tinfo['row_count']} rows)")
            col_list = [f"{c['name']} ({c['type']})" for c in tinfo["columns"]]
            st.caption(", ".join(col_list))

    st.markdown("---")

    # Schema & Index Refresh Action
    st.markdown("**Schema & Index Management**")
    if st.button("🔄 Refresh Schema & Index", use_container_width=True):
        load_retriever.clear()
        st.success("Retriever index & categorical profiles refreshed from database.")
        st.rerun()

    # Query History
    if st.session_state.query_history:
        st.markdown("---")
        st.markdown("**Recent Queries**")
        for idx, hist_item in enumerate(reversed(st.session_state.query_history[-5:])):
            label = hist_item if len(hist_item) <= 28 else hist_item[:25] + "..."
            if st.button(label, key=f"hist_{idx}", use_container_width=True):
                st.session_state.pending_query = hist_item
                st.session_state.pending_ambiguity = None
                st.rerun()


# ==================================================
# MODE 1: QUERY MODE (READ-ONLY)
# ==================================================

if mode == "🔍 Query Mode":
    def handle_suggested_query(suggestion: Optional[str] = None):
        target = suggestion or st.session_state.get("current_suggestion")
        if target:
            st.session_state.pending_query = target
            st.session_state.execute_suggested = True

    st.markdown('<div class="main-header">Query Mode</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Ask questions in natural language. Powered by local Qwen3:4B, schema RAG, '
        'and strict read-only query execution.</div>',
        unsafe_allow_html=True,
    )

    # Sample Question Chips
    st.markdown("<small>Quick test queries:</small>", unsafe_allow_html=True)
    example_cols = st.columns(4)
    examples = [
        "What is the average salary by department?",
        "Show employees working in the technical department.",
        "Show me the top employees.",
        "Predict next month's salary for all employees.",
    ]
    for i, ex in enumerate(examples):
        with example_cols[i]:
            if st.button(ex, key=f"ex_{i}", use_container_width=True):
                st.session_state.pending_query = ex
                st.session_state.pending_ambiguity = None
                st.rerun()

    # Consume pending query before widget instantiation
    if st.session_state.pending_query is not None:
        st.session_state.active_question = st.session_state.pending_query
        st.session_state.natural_language_question = st.session_state.pending_query
        st.session_state.pending_query = None

    question = st.text_input(
        "Natural-language question",
        key="natural_language_question",
        placeholder="e.g. Which department has the highest total salary expenditure?",
    )
    st.session_state.active_question = question

    # Ambiguity Clarification Card
    if st.session_state.pending_ambiguity:
        amb_info = st.session_state.pending_ambiguity
        st.warning(f"🤔 **Clarification Needed**: {amb_info['clarification_question']}")
        st.write("Please select your intended interpretation:")

        opt_cols = st.columns(len(amb_info["options"]) + 1)
        for idx, option in enumerate(amb_info["options"]):
            with opt_cols[idx]:
                if st.button(f"👉 {option}", key=f"amb_opt_{idx}", use_container_width=True):
                    combined = combine_clarification(amb_info["original_question"], option)
                    st.session_state.pending_query = combined
                    st.session_state.execute_suggested = True
                    st.session_state.pending_ambiguity = None
                    st.rerun()

        with opt_cols[-1]:
            if st.button("❌ Dismiss", key="amb_cancel", use_container_width=True):
                st.session_state.pending_ambiguity = None
                st.rerun()

    execute_clicked = st.button(
        "Generate & Execute SQL",
        type="primary",
        disabled=not question.strip(),
    )

    should_execute = execute_clicked or st.session_state.execute_suggested
    st.session_state.execute_suggested = False

    if should_execute:
        try:
            schema = get_schema()

            # Step 1: Schema & Categorical Value Retrieval
            with st.spinner("Retrieving relevant schema documents and categorical values..."):
                embedding_model, index, documents = load_retriever()
                retrieved = search_schema(question, embedding_model, index, documents, top_k=3)

            retrieved_context = "\n\n".join(item["document"]["content"] for item in retrieved)

            # Step 2: Unsupported Query Detection
            unsupported_check = detect_unsupported(question, schema, retrieved_context)
            if unsupported_check["is_unsupported"]:
                st.warning(f"🚫 **Unsupported Request**: {unsupported_check['reason']}")
                if unsupported_check.get("missing_info"):
                    st.markdown(f"**Missing Information**: {unsupported_check['missing_info']}")
                if unsupported_check.get("suggestion"):
                    st.session_state.current_suggestion = unsupported_check["suggestion"]
                    st.info(f"💡 **Suggested Alternative**: {unsupported_check['suggestion']}")
                    st.button(
                        "Run suggested query instead",
                        key="run_suggested",
                        on_click=handle_suggested_query,
                        args=(unsupported_check["suggestion"],),
                    )

            else:
                # Step 3: Ambiguity Detection Pre-Check
                ambiguity_check = detect_ambiguity(question, schema, retrieved_context)
                if ambiguity_check["is_ambiguous"]:
                    st.session_state.pending_ambiguity = {
                        "original_question": question,
                        "clarification_question": ambiguity_check["clarification_question"],
                        "options": ambiguity_check["options"],
                    }
                    st.rerun()

                # Add to history
                if question not in st.session_state.query_history:
                    st.session_state.query_history.append(question)

                # Show retrieved context in collapsed expander
                with st.expander("Retrieved Schema and Categorical Context", expanded=False):
                    for item in retrieved:
                        st.caption(f"Relevance Score: {item['score']:.3f}")
                        st.code(item["document"]["content"])

                # Step 4: SQL Generation with Latency Tracking
                gen_t0 = time.perf_counter()
                with st.spinner("Generating SQL query with local Qwen3:4B..."):
                    generated = generate_sql(question, schema, retrieved_context)
                gen_latency = time.perf_counter() - gen_t0

                if generated.upper().startswith("UNSUPPORTED:"):
                    st.warning(generated[len("UNSUPPORTED:"):].strip())
                elif generated.upper().startswith("AMBIGUOUS:"):
                    st.info(generated[len("AMBIGUOUS:"):].strip())
                else:
                    current_sql = generated
                    columns = None
                    rows = None
                    unsupported = False
                    correction_count = 0
                    exec_latency = 0.0

                    # Step 5: Validation and Bounded Self-Correction Loop
                    for attempt in range(MAX_CORRECTION_ATTEMPTS + 1):
                        if current_sql.upper().startswith("UNSUPPORTED:"):
                            st.warning(current_sql[len("UNSUPPORTED:"):].strip())
                            unsupported = True
                            break

                        try:
                            safe_sql = validate_sql(current_sql)
                            exec_t0 = time.perf_counter()
                            columns, rows = execute_query(safe_sql)
                            exec_latency = (time.perf_counter() - exec_t0) * 1000.0  # ms
                            break

                        except (ValueError, sqlite3.Error) as exc:
                            correction_count += 1
                            if attempt == MAX_CORRECTION_ATTEMPTS:
                                raise

                            st.warning(
                                f"SQL execution attempt {attempt + 1} failed: {exc}. "
                                f"Initiating bounded self-correction ({attempt + 1}/{MAX_CORRECTION_ATTEMPTS})..."
                            )

                            with st.spinner("Correcting SQL query..."):
                                current_sql = correct_sql(
                                    question=question,
                                    schema=schema,
                                    retrieved_context=retrieved_context,
                                    failed_sql=current_sql,
                                    error=str(exc),
                                )

                    if not unsupported and columns is not None and rows is not None:
                        # Display Generated SQL
                        with st.expander("Generated SQL Query", expanded=True):
                            st.code(safe_sql, language="sql")

                        # AST-Based Explanation
                        explanation = explain_sql(safe_sql, question)
                        st.info(f"💡 **SQL Explanation**: {explanation}")

                        # Query Execution Results
                        st.subheader("Query Results")
                        if len(rows) == 0:
                            st.info("The query executed successfully and returned 0 matching records.")
                        else:
                            st.dataframe(
                                [dict(zip(columns, row)) for row in rows],
                                use_container_width=True,
                            )

                        st.caption(
                            f"Returned {len(rows)} row(s) &nbsp;|&nbsp; "
                            f"Model Latency: {gen_latency:.2f}s &nbsp;|&nbsp; "
                            f"Execution Time: {exec_latency:.2f}ms"
                            + (f" &nbsp;|&nbsp; Self-corrected ({correction_count} attempt)" if correction_count > 0 else "")
                        )

                        # Natural Language Summary
                        summary_text = summarize_results(question, safe_sql, columns, rows)
                        st.markdown(f"**Natural Language Summary**: {summary_text}")

                        # CSV Export
                        csv_data = results_to_csv(columns, rows)
                        st.download_button(
                            label="📥 Export Results as CSV",
                            data=csv_data,
                            file_name="query_results.csv",
                            mime="text/csv",
                            use_container_width=False,
                        )

        except requests.RequestException as exc:
            st.error(f"Could not reach Ollama: {exc}. Ensure Ollama is running locally.")
        except (ValueError, sqlite3.Error) as exc:
            st.error(f"Query execution rejected: {exc}")
        except Exception as exc:
            st.error(f"Unexpected error: {exc}")


# ==================================================
# MODE 2: BUILD MODE (CONTROLLED TABLE CREATION)
# ==================================================

elif mode == "🛠️ Build Mode":
    st.markdown('<div class="main-header">Build Mode</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Design and create new database tables safely. '
        'Support for manual column configuration or AI natural-language generation.</div>',
        unsafe_allow_html=True,
    )

    if st.session_state.build_status:
        stype, smsg = st.session_state.build_status
        if stype == "success":
            st.success(smsg)
        else:
            st.info(smsg)

    creation_method = st.radio(
        "Table Creation Method",
        ["Manual Column Designer", "Natural-Language AI Proposal"],
        horizontal=True,
    )

    # METHOD A: MANUAL COLUMN DESIGNER
    if creation_method == "Manual Column Designer":
        st.subheader("Manual Column Designer")
        designer_table = st.text_input("New Table Name", placeholder="e.g. suppliers")

        num_cols = st.number_input("Number of Columns", min_value=1, max_value=10, value=3, step=1)
        manual_columns: List[Dict[str, Any]] = []

        st.markdown("**Define Columns:**")
        for i in range(int(num_cols)):
            col_container = st.container()
            with col_container:
                c1, c2, c3, c4, c5, c6 = st.columns([3, 2, 1.5, 1.5, 1.5, 2.5])
                with c1:
                    c_name = st.text_input(f"Name #{i+1}", key=f"m_name_{i}", placeholder="column_name")
                with c2:
                    c_type = st.selectbox(f"Type #{i+1}", ["TEXT", "INTEGER", "REAL", "BLOB", "NUMERIC"], key=f"m_type_{i}")
                with c3:
                    c_pk = st.checkbox("PK", key=f"m_pk_{i}", value=(i == 0))
                with c4:
                    c_auto = st.checkbox("Auto-Inc", key=f"m_auto_{i}", value=(i == 0 and c_type == "INTEGER"))
                with c5:
                    c_nn = st.checkbox("Not Null", key=f"m_nn_{i}", value=(i == 0))
                with c6:
                    c_dflt = st.text_input("Default", key=f"m_dflt_{i}", placeholder="Optional")

                if c_name.strip():
                    manual_columns.append({
                        "name": c_name.strip(),
                        "type": c_type,
                        "primary_key": c_pk,
                        "autoincrement": c_auto,
                        "not_null": c_nn,
                        "unique": False,
                        "default": c_dflt.strip() if c_dflt.strip() else None,
                    })

        if st.button("Generate SQL from Column Design", type="primary"):
            if not designer_table.strip():
                st.error("Please provide a valid table name.")
            elif not manual_columns:
                st.error("Please configure at least one column with a valid name.")
            elif table_exists(designer_table.strip()):
                st.error(f"The table '{designer_table.strip()}' already exists. Please choose a different name.")
            else:
                try:
                    constructed_sql = construct_create_table_sql(designer_table.strip(), manual_columns)
                    validated_sql = validate_schema_proposal(constructed_sql)
                    st.session_state.schema_proposal = validated_sql
                    st.session_state.schema_proposal_request = f"Manual design for '{designer_table.strip()}'"
                    st.session_state.build_status = None
                    st.rerun()
                except ValueError as err:
                    st.error(f"Invalid column design: {err}")

    # METHOD B: NATURAL LANGUAGE AI PROPOSAL
    else:
        st.subheader("Natural-Language AI Proposal")
        nl_request = st.text_area(
            "Describe the table you want to create",
            placeholder="e.g. Create a suppliers table with supplier name, contact email, city, and rating.",
        )

        if st.button("Generate AI Proposal", type="primary", disabled=not nl_request.strip()):
            try:
                existing_schema = get_schema()
                with st.spinner("Generating CREATE TABLE proposal with local Qwen3:4B..."):
                    raw_proposal = generate_schema_proposal(nl_request, existing_schema)

                validated_proposal = validate_schema_proposal(raw_proposal)
                tname = get_table_name_from_proposal(validated_proposal)

                if table_exists(tname):
                    st.session_state.schema_proposal = None
                    st.session_state.build_status = ("info", f"The table '{tname}' already exists. Choose a different table name.")
                else:
                    st.session_state.schema_proposal = validated_proposal
                    st.session_state.schema_proposal_request = nl_request
                    st.session_state.build_status = None
                st.rerun()

            except requests.RequestException as exc:
                st.session_state.build_status = ("info", f"Could not reach Ollama: {exc}")
            except (ValueError, sqlite3.Error) as exc:
                st.session_state.build_status = ("info", f"Schema proposal rejected: {exc}")

    # PROPOSAL PREVIEW & TRANSACTIONAL EXECUTION
    saved_proposal = st.session_state.schema_proposal
    if saved_proposal:
        st.markdown("---")
        st.subheader("Proposed Schema for Review")
        try:
            safe_proposal = validate_schema_proposal(saved_proposal)
            table_name = get_table_name_from_proposal(safe_proposal)

            st.code(safe_proposal, language="sql")
            st.caption(f"Source request: {st.session_state.schema_proposal_request}")

            if table_exists(table_name):
                st.warning(f"The table '{table_name}' already exists in the database.")
            else:
                st.info("Review the SQL definition above carefully. Execution requires explicit confirmation.")
                confirmed = st.checkbox(
                    f"I confirm that I want to create the table '{table_name}' in SQLite.",
                    value=False,
                )

                col_act1, col_act2 = st.columns([2, 8])
                with col_act1:
                    if st.button("Create Table", type="primary", disabled=not confirmed):
                        try:
                            create_table_safely(safe_proposal, table_name)
                            load_retriever.clear()
                            st.session_state.build_status = (
                                "success",
                                f"Table '{table_name}' created successfully! Schema and search index were automatically refreshed.",
                            )
                            st.session_state.schema_proposal = None
                            st.session_state.schema_proposal_request = None
                            st.rerun()
                        except (ValueError, sqlite3.Error) as exc:
                            st.error(f"Table creation failed: {exc}")
                with col_act2:
                    if st.button("Discard Proposal"):
                        st.session_state.schema_proposal = None
                        st.session_state.schema_proposal_request = None
                        st.rerun()

        except ValueError as exc:
            st.error(f"Saved schema proposal is invalid: {exc}")
            st.session_state.schema_proposal = None


# ==================================================
# MODE 3: DATA ENTRY MODE (TRANSACTIONAL INSERTION)
# ==================================================

elif mode == "📝 Data Entry Mode":
    st.markdown('<div class="main-header">Data Entry Mode</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="sub-header">Insert records into existing tables via dynamically generated forms. '
        'Enforces SQLite constraints, foreign key references, and transactional commit/rollback.</div>',
        unsafe_allow_html=True,
    )

    available_tables = get_available_tables(DB_PATH)
    if not available_tables:
        st.warning("No tables found in the active SQLite database.")
    else:
        selected_table = st.selectbox("Select Target Table", available_tables)

        if selected_table:
            schema_details = get_table_schema_details(DB_PATH, selected_table)

            # Metadata overview
            with st.expander(f"Table Schema: {selected_table} ({schema_details['row_count']} existing rows)", expanded=False):
                meta_rows = []
                for col in schema_details["columns"]:
                    constraints = []
                    if col["is_pk"]:
                        constraints.append("PRIMARY KEY" + (" AUTOINCREMENT" if col["is_auto_pk"] else ""))
                    if col["not_null"]:
                        constraints.append("NOT NULL")
                    if col["is_unique"] and not col["is_pk"]:
                        constraints.append("UNIQUE")
                    if col["default"] is not None:
                        constraints.append(f"DEFAULT {col['default']}")
                    if col["foreign_key"]:
                        constraints.append(f"FK -> {col['foreign_key']['table']}.{col['foreign_key']['column']}")

                    meta_rows.append({
                        "Column": col["name"],
                        "Type": col["type"],
                        "Constraints": ", ".join(constraints) if constraints else "None",
                    })
                st.dataframe(meta_rows, use_container_width=True)

            # Dynamic Input Form
            st.subheader(f"Insert Record into '{selected_table}'")

            form_inputs: Dict[str, Any] = {}
            with st.form(key=f"insert_form_{selected_table}"):
                for col in schema_details["columns"]:
                    col_name = col["name"]
                    type_cat = col["type"]
                    not_null = col["not_null"]
                    is_auto_pk = col["is_auto_pk"]
                    is_pk = col["is_pk"]
                    dflt = col["default"]
                    fk = col["foreign_key"]

                    label_suffix = " *(Required)*" if (not_null and not is_auto_pk and dflt is None) else " *(Optional)*"
                    if is_auto_pk:
                        st.caption(f"**{col_name}**: INTEGER PRIMARY KEY (leave blank / 0 to auto-generate)")
                        form_inputs[col_name] = st.number_input(
                            f"{col_name} (Auto ID)",
                            min_value=0,
                            value=0,
                            step=1,
                            key=f"field_{col_name}",
                            help="Leave 0 for automatic sequential ID generation.",
                        )

                    elif fk and fk.get("options"):
                        options = ["(Select referenced record)"] + [str(opt) for opt in fk["options"]]
                        chosen = st.selectbox(
                            f"{col_name} -> References {fk['table']}.{fk['column']}{label_suffix}",
                            options=options,
                            key=f"field_{col_name}",
                        )
                        form_inputs[col_name] = None if chosen == "(Select referenced record)" else chosen

                    elif type_cat == "INTEGER":
                        if not_null and dflt is None and not is_pk:
                            form_inputs[col_name] = st.number_input(
                                f"{col_name} (Integer){label_suffix}",
                                value=0,
                                step=1,
                                key=f"field_{col_name}",
                            )
                        else:
                            raw_txt = st.text_input(
                                f"{col_name} (Integer){label_suffix}",
                                key=f"field_{col_name}",
                                placeholder=f"Integer value (default: {dflt})" if dflt else "Optional integer",
                            )
                            form_inputs[col_name] = raw_txt.strip() if raw_txt.strip() else None

                    elif type_cat == "REAL":
                        if not_null and dflt is None:
                            form_inputs[col_name] = st.number_input(
                                f"{col_name} (Numeric/Decimal){label_suffix}",
                                value=0.0,
                                step=0.01,
                                key=f"field_{col_name}",
                            )
                        else:
                            raw_txt = st.text_input(
                                f"{col_name} (Numeric/Decimal){label_suffix}",
                                key=f"field_{col_name}",
                                placeholder=f"Numeric value (default: {dflt})" if dflt else "Optional numeric value",
                            )
                            form_inputs[col_name] = raw_txt.strip() if raw_txt.strip() else None

                    elif type_cat == "BOOLEAN":
                        form_inputs[col_name] = st.checkbox(
                            f"{col_name} (Boolean){label_suffix}",
                            value=False,
                            key=f"field_{col_name}",
                        )

                    else:
                        form_inputs[col_name] = st.text_input(
                            f"{col_name} (Text){label_suffix}",
                            key=f"field_{col_name}",
                            placeholder=f"Default: {dflt}" if dflt else "Text value",
                        )

                preview_submitted = st.form_submit_button("Preview Record for Insertion")

            # Validate on Form Submit
            if preview_submitted:
                is_valid, cleaned, errors = validate_record_for_insertion(
                    DB_PATH,
                    selected_table,
                    form_inputs,
                    schema_details,
                )
                if not is_valid:
                    st.error("Validation Failed:\n- " + "\n- ".join(errors))
                    st.session_state.pending_insert_record = None
                else:
                    st.session_state.pending_insert_record = {
                        "table": selected_table,
                        "data": cleaned,
                    }
                    st.rerun()

            # Record Preview & Confirmation Card
            if (
                st.session_state.pending_insert_record
                and st.session_state.pending_insert_record.get("table") == selected_table
            ):
                st.markdown("---")
                st.subheader("Record Preview & Confirmation")
                pending_data = st.session_state.pending_insert_record["data"]

                st.write("Review the validated values to be inserted:")
                preview_display = [{"Field": k, "Value": str(v) if v is not None else "(NULL / DEFAULT)"} for k, v in pending_data.items()]
                st.table(preview_display)

                confirm_insert = st.checkbox(
                    f"I confirm that I want to insert this record into table '{selected_table}'.",
                    value=False,
                )

                col_ins1, col_ins2 = st.columns([3, 7])
                with col_ins1:
                    if st.button("Confirm & Insert Record", type="primary", disabled=not confirm_insert):
                        try:
                            inserted_row = insert_record(DB_PATH, selected_table, pending_data)
                            load_retriever.clear()  # Refresh schema & categorical values in RAG
                            st.success(f"Record successfully inserted into '{selected_table}'!")
                            st.write("**Inserted Record:**", inserted_row)
                            st.session_state.pending_insert_record = None
                            st.rerun()
                        except ValueError as exc:
                            st.error(f"Insertion failed: {exc}")
                with col_ins2:
                    if st.button("Cancel Insertion"):
                        st.session_state.pending_insert_record = None
                        st.rerun()

            # Display Recent Records in the Table
            st.markdown("---")
            st.subheader(f"Recent Records in '{selected_table}'")
            cols, rows = get_recent_records(DB_PATH, selected_table, limit=5)
            if rows:
                st.dataframe([dict(zip(cols, r)) for r in rows], use_container_width=True)
            else:
                st.info(f"No records currently exist in '{selected_table}'.")
