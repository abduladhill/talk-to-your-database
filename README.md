# Talk to Your Database

An RAG-based self-correcting AI agent for natural-language database creation and querying.

## Objective

Build a local-first application that allows users to create and query SQLite databases using natural-language instructions. The system will use schema-aware retrieval, guarded SQL execution, and bounded self-correction.

## Planned Technology Stack

- Python
- Streamlit
- SQLite
- Ollama with one instruction-tuned local LLM
- Sentence Transformers
- FAISS
- SQLGlot
- CSV/JSON-based evaluation

## Current Status

Day 1: Initial environment setup.

## Setup

### 1. Create and activate the virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 3. Run the application

The application entry point will be added during implementation.
