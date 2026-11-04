# Talk to Your Database — Live Demonstration Script

This document provides a step-by-step walk-through to demonstrate all capabilities, safety mechanisms, conversational features, and operational modes of **Talk to Your Database**.

---

## Prerequisites & Launch

1. Ensure Ollama is running with `qwen3:4b`:
   ```powershell
   ollama list
   ```
2. Activate the virtual environment and start Streamlit:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   streamlit run app.py
   ```
3. Open `http://localhost:8501` in your browser.

---

## Scenario 1: Interface Overview & Live Health Check

**Goal:** Demonstrate the professional dashboard layout, live Ollama connection health check, and live database overview.

1. **Observe the Sidebar:**
   - **Live LLM Connection Status:** Displays `● Ollama Online | qwen3:4b Ready` in a green health badge. Note that the application actively verifies this via `http://localhost:11434/api/tags` rather than assuming connectivity.
   - **Database Metadata:** Confirms active database `sample.db`.
   - **Database Overview Panel:** Expand the **📊 Database Overview** expander:
     - Shows total table count.
     - Lists all tables (`customers`, `employees`, `invoices`, `products`, etc.) with live row counts and full column definitions.
   - **Navigation:** Shows three distinct operational modes:
     - `🔍 Query Mode`
     - `🛠️ Build Mode`
     - `📝 Data Entry Mode`

---

## Scenario 2: Standard Natural Language Query & AST Explanation

**Goal:** Demonstrate accurate NL-to-SQL generation, guarded read-only execution, AST SQL explanation, result summarization, and CSV export.

1. Mode: Ensure **🔍 Query Mode** is selected.
2. Enter Question:
   ```text
   What is the average salary by department?
   ```
3. Click **Generate & Execute SQL**.
4. **Observe:**
   - **Retrieved Schema Context:** Displays similarity scores for the `employees` table and `department`/`salary` columns.
   - **Generated SQL:**
     ```sql
     SELECT department, AVG(salary) AS average_salary FROM employees GROUP BY department;
     ```
   - **SQL Explanation:** Beginner-friendly explanation generated via SQLGlot AST inspection:
     *"Retrieves department, average_salary from 'employees', groups the data by department."*
   - **Query Results:** Clean table showing Engineering (48,500), HR (36,500), and Sales (40,000).
   - **Performance Metrics:** Model latency (~1-3s) and SQLite execution latency (~2-5ms).
   - **Natural Language Summary:** *"Results by department: Engineering (48500.0), HR (36500.0), Sales (40000.0)."*
   - **CSV Export:** Click **📥 Export Results as CSV** to verify the in-memory download.

---

## Scenario 3: Categorical Value Matching (Feature 1)

**Goal:** Demonstrate matching colloquial domain terminology ("technical department") to actual stored SQLite categorical values (`Engineering`).

1. Enter Question:
   ```text
   Show employees working in the technical department.
   ```
2. Click **Generate & Execute SQL**.
3. **Observe:**
   - The value profiler retrieves: `Known categorical values: Engineering; HR; Sales`.
   - The system recognizes that the database department is `'Engineering'`.
   - Generated SQL:
     ```sql
     SELECT * FROM employees WHERE department = 'Engineering';
     ```
   - Returns Anu ($45,000) and Meera ($52,000).
   - Result summary confirms two employees found in Engineering.

---

## Scenario 4: Ambiguity Detection & Clarification Flow (Feature 2)

**Goal:** Demonstrate that the system refuses to guess arbitrary metrics on genuinely ambiguous questions and asks for clarification.

1. Enter Question:
   ```text
   Show me the top employees.
   ```
2. Click **Generate & Execute SQL**.
3. **Observe:**
   - The system detects ambiguity:
     > **Clarification Needed**: The request for 'top employees' is ambiguous because multiple ranking metrics are possible. How would you like to interpret it?
   - Interactive options appear:
     - `👉 Highest salary`
     - `👉 Lowest ID / earliest hire`
     - `👉 Alphabetical by name`
     - `❌ Dismiss`
   - Notice that **no SQL was generated prematurely**.
4. Click **👉 Highest salary**.
5. **Observe:**
   - The question updates to: `Show me the top employees (Clarification: Highest salary)`.
   - The system generates:
     ```sql
     SELECT name, salary FROM employees ORDER BY salary DESC LIMIT 1;
     ```
   - Displays Meera ($52,000) as the top employee.

---

## Scenario 5: Unsupported Query Detection & Guidance (Feature 3)

**Goal:** Demonstrate refusing out-of-scope requests (predictions, multimedia, missing tables) and suggesting actionable alternatives.

1. Enter Question:
   ```text
   Predict next month's salary for all employees.
   ```
2. Click **Generate & Execute SQL**.
3. **Observe:**
   - Refusal card:
     > 🚫 **Unsupported Request**: The database contains historical and current operational records; it does not contain a machine learning model or forecasting data to project future values.
     >
     > **Missing Information**: Predictive models and future trend projections.
     >
     > 💡 **Suggested Alternative**: View current records instead, e.g. 'What is the average employee salary?' or 'Show each employee and their salary.'
   - Click the button **Run suggested query instead** to seamlessly execute the valid alternative!
4. Try another unsupported query:
   ```text
   Show the employee photo and avatar.
   ```
   - Clearly explains that binary multimedia/photo blobs are not stored in the schema.

---

## Scenario 6: Security & Read-Only Enforcement

**Goal:** Demonstrate strict rejection of prohibited SQL operations (INSERT, UPDATE, DELETE, DROP, ALTER, and multi-statements) in Query Mode.

1. Enter Question:
   ```text
   Delete all employees with salary below 40000.
   ```
2. Click **Generate & Execute SQL**.
3. **Observe:**
   - Query Mode immediately flags the destructive request:
     > 🚫 **Unsupported Request**: Query Mode is strictly read-only and prohibits INSERT, UPDATE, DELETE, DROP, and ALTER statements.
4. Try submitting raw malicious multi-statement SQL via input:
   ```text
   SELECT * FROM employees; DROP TABLE employees;
   ```
   - SQLGlot validation catches the violation and safely rejects execution before reaching SQLite.
   - The read-only SQLite URI connection (`?mode=ro`) provides engine-level defense against write attempts.

---

## Scenario 7: Build Mode — Table Creation (Feature 7)

**Goal:** Demonstrate controlled, guarded table creation via Manual Column Designer and Natural-Language AI Proposal, with explicit confirmation and automatic index refresh.

1. Select **🛠️ Build Mode** in the sidebar.
2. Select **Manual Column Designer**:
   - Table Name: `suppliers`
   - Number of columns: `3`
   - Column 1: `id` | `INTEGER` | PK: Checked | Auto-Inc: Checked | Not Null: Checked
   - Column 2: `company_name` | `TEXT` | Not Null: Checked
   - Column 3: `city` | `TEXT` | Default: `New York`
3. Click **Generate SQL from Column Design**.
4. **Observe:**
   - Validated DDL proposal:
     ```sql
     CREATE TABLE "suppliers" (
       "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
       "company_name" TEXT NOT NULL,
       "city" TEXT DEFAULT 'New York'
     )
     ```
   - Warning and explicit checkbox:
     `[x] I confirm that I want to create the table 'suppliers' in SQLite.`
5. Click **Create Table**.
6. **Observe:**
   - Success alert: *"Table 'suppliers' created successfully! Schema and search index were automatically refreshed."*
   - In sidebar, **Database Overview** now immediately reflects `suppliers (0 rows)`.

---

## Scenario 8: Data Entry Mode — Transactional Record Insertion

**Goal:** Demonstrate dynamic schema-driven form generation, multi-level validation, record preview, and parameterized insertion with rollback.

1. Select **📝 Data Entry Mode** in the sidebar.
2. Target Table: Select `products` (or `suppliers`).
3. **Observe Dynamic Form:**
   - Form fields reflect the exact SQLite schema:
     - `product_name` (Text, Required)
     - `price` (Numeric/Decimal, Required)
     - `stock_quantity` (Integer, Required)
4. **Demonstrate Validation (Invalid Input):**
   - Leave `product_name` empty.
   - Enter `not_a_number` for `price`.
   - Click **Preview Record for Insertion**.
   - **Observe:** Validation errors are displayed:
     - `Field 'product_name' is required and cannot be empty.`
     - `Field 'price' must be a valid number (received: 'not_a_number').`
5. **Demonstrate Successful Insertion:**
   - Product Name: `Wireless Noise-Canceling Headphones`
   - Price: `199.99`
   - Stock Quantity: `45`
   - Click **Preview Record for Insertion**.
   - **Observe Record Preview Card:**
     - Clean key-value preview of the validated values to be inserted.
     - Explicit confirmation checkbox:
       `[x] I confirm that I want to insert this record into table 'products'.`
6. Click **Confirm & Insert Record**.
7. **Observe:**
   - Success message: *"Record successfully inserted into 'products'!"*
   - Displays the returned record with its newly generated `id`.
   - The **Recent Records in 'products'** table immediately displays the new record at the top!
8. **Verify in Query Mode:**
   - Switch back to **🔍 Query Mode** and ask:
     ```text
     What is the stock quantity of Wireless Noise-Canceling Headphones?
     ```
   - Returns `45` immediately without restarting or manual re-indexing!

---

## Summary Checklist

| Feature | Demonstration Scenario | Result |
|---|---|:---:|
| Health Status & DB Overview | Scenario 1 | ✅ Live Ollama check & table statistics |
| Schema RAG | Scenario 2 | ✅ Context similarity scores visible |
| Terminology Matching | Scenario 3 | ✅ 'technical department' -> 'Engineering' |
| Ambiguity Detection | Scenario 4 | ✅ Refuses to guess, offers interactive options |
| Unsupported Detection | Scenario 5 | ✅ Explains missing info & suggests alternative |
| SQL AST Explanation | Scenario 2 | ✅ Plain-English AST summary |
| Result Summarization | Scenario 2 & 3 | ✅ Factual natural language summary |
| Safe CSV Export | Scenario 2 | ✅ Safe in-memory download |
| Read-Only Safety | Scenario 6 | ✅ Rejects DDL/DML and multi-statement |
| Build Mode Table Creation | Scenario 7 | ✅ Column designer + proposal + auto-refresh |
| Data Entry Mode | Scenario 8 | ✅ Form generation, validation, preview & INSERT |

---

## Reset & Teardown (Post-Demo Cleanup)

If you created demonstration tables (like `suppliers`) or inserted test records during the demo and wish to restore the sample database back to its clean initial state:

```powershell
.\.venv\Scripts\python.exe create_sample_db.py
```

This recreates `sample.db` with clean initial records without touching benchmarks or code.
