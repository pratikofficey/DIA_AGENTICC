# DIA Agentic Test Builder — Requirements & Build Specification

## What This Is

A **new, separate project** — a pure LLM-powered agentic analytics system for P2P invoice data.

The user uploads an invoice CSV. Instead of running hardcoded tests, the system:
1. Maps the columns to a canonical schema (UDM) — same pipeline as the existing system
2. Shows 3–4 base tests as examples
3. Lets the user **write a natural language prompt** describing any pattern they want to detect
4. An LLM (Groq) reads the prompt + the actual column names + 5 sample rows and **writes Python code** to detect that pattern
5. The system executes the generated code on the full dataset
6. Returns hit rows, hit count, amount, and the LLM's explanation of what it found

This is fundamentally different from the existing system which has hardcoded SP-parity logic. Here the test logic itself is generated on the fly.

---

## Project Setup

### Folder structure (new, standalone project)

```
DIA Agentic/                          ← new folder in Downloads
├── .env                              ← GROQ_API_KEY
├── requirements.txt
├── README.md
│
└── dia_agentic/
    ├── __init__.py
    ├── app.py                        ← FastAPI app (port 8001)
    ├── service.py                    ← orchestration pipeline
    │
    ├── core/
    │   ├── __init__.py
    │   ├── udm.py                    ← copy from existing (41-field UDM + alias map)
    │   ├── ingestor.py               ← copy from existing (CSV parser + null normalisation)
    │   ├── schema_mapper.py          ← copy from existing (5-tier mapping pipeline)
    │   ├── erp_profiles.py           ← copy from existing
    │   ├── base_tests.py             ← 3–4 hardcoded example tests (as reference)
    │   ├── test_builder.py           ← NEW: prompt → LLM → Python code
    │   ├── code_executor.py          ← NEW: safely run LLM-generated test code on rows
    │   └── run_store.py              ← lightweight SQLite for run history
    │
    ├── data/
    │   └── erp_profiles/             ← copy from existing (sap.json, oracle.json, etc.)
    │
    └── static/
        ├── index.html
        ├── style.css
        └── app.js
```

### Files to copy from existing DIA POC project

Copy these files exactly as-is into `dia_agentic/core/`:
- `dia_validation/core/udm.py`
- `dia_validation/core/ingestor.py`
- `dia_validation/core/schema_mapper.py`
- `dia_validation/core/erp_profiles.py`
- `dia_validation/core/mapping_registry.py`

Copy this folder:
- `dia_validation/data/erp_profiles/` → `dia_agentic/data/erp_profiles/`

### requirements.txt

```
fastapi
uvicorn[standard]
python-multipart
groq
python-dotenv
```

### .env

```
GROQ_API_KEY=<paste your key here>
```

---

## Core Concept: Agentic Test Builder

### The flow

```
User writes: "Find all vendors who have submitted invoices where
              the person who captured the invoice is the same as
              the person who authorized it, and the amount is
              over 50,000"

→ System sends to Groq LLM:
   - User's prompt
   - UDM field names + descriptions
   - 5 sample rows from the actual uploaded CSV (after mapping)

→ LLM returns:
   {
     "test_name": "SoD Violation on High Value Invoices",
     "description": "Detects invoices where captured_by == authorized_by with amount > 50000",
     "required_fields": ["captured_by", "authorized_by", "amount_excl_lc"],
     "code": "
def run_test(rows):
    hits = []
    for i, row in enumerate(rows):
        captured = (row.get('captured_by') or '').strip().lower()
        authorized = (row.get('authorized_by') or '').strip().lower()
        amount = float(row.get('amount_excl_lc') or 0)
        if captured and authorized and captured == authorized and amount > 50000:
            hits.append(i)
    return hits
     ",
     "explanation": "I'm looking for cases where the same employee both entered and approved an invoice over $50K. This is a segregation of duties violation."
   }

→ code_executor.py runs the function on the full dataset
→ Returns hit_indices, hit_count, hit_pct, hit_amount, sample_rows
```

### Why code generation (not structured JSON plans)

- Natural language test descriptions can express ANY logic — Benford's, sequential numbers, ratio checks, date patterns, keyword matching, etc.
- A JSON plan spec would need to anticipate every possible operator — impossible
- The generated code is shown to the user in the UI — transparent and auditable
- Groq `llama-3.1-8b-instant` is fast enough for this (< 3 seconds per test generation)
- Risk is managed: the executor runs in a restricted namespace (no file I/O, no network, no imports)

---

## Module: test_builder.py

### Purpose

Takes a user's natural language prompt and the dataset context, calls Groq, and returns a structured test definition including executable Python code.

### LLM prompt design

The system prompt should:
1. Tell the LLM it is a financial data analyst writing Python test functions
2. Give it the exact UDM field names and their meanings
3. Give it 5 sample rows (actual data from the upload) so it knows what values look like
4. Instruct it to return **only JSON** in the exact format specified
5. Specify that the `run_test(rows)` function receives `rows: list[dict]` where each dict has UDM field names as keys
6. Specify that the function must return `list[int]` — the 0-based indices of hit rows
7. Tell it to handle nulls gracefully (use `or ''`, `or 0`, `try/except float()`)
8. Tell it NOT to import anything — only use Python builtins

### Output format (JSON)

```json
{
  "test_name": "Short descriptive name",
  "description": "One sentence: what pattern this detects",
  "required_fields": ["field1", "field2"],
  "severity": "error|warning|info",
  "code": "def run_test(rows):\n    ...\n    return hits",
  "explanation": "Plain English explanation of the logic for the reviewer"
}
```

### Error handling

- If LLM returns invalid JSON → retry once with stricter prompt
- If LLM returns code that fails to parse → return error to UI ("Could not generate test — try rephrasing")
- If generated code raises exception during execution → catch it, return 0 hits + error message

---

## Module: code_executor.py

### Purpose

Safely executes the LLM-generated `run_test(rows)` function on the full dataset.

### Safety restrictions

The executor must run the code in a **restricted namespace**:

```python
SAFE_BUILTINS = {
    'len', 'range', 'enumerate', 'zip', 'list', 'dict', 'set', 'tuple',
    'str', 'int', 'float', 'bool', 'abs', 'round', 'min', 'max', 'sum',
    'sorted', 'reversed', 'isinstance', 'type', 'print',
    'any', 'all', 'filter', 'map',
    'ValueError', 'TypeError', 'KeyError', 'IndexError',
}
```

No `import`, no `open`, no `os`, no `subprocess`, no `eval`, no `exec` within the generated code.

### Execution

```python
def execute_test(code: str, rows: list[dict]) -> dict:
    # 1. Compile the code to check for syntax errors
    # 2. Create restricted namespace
    # 3. exec() the function definition into the namespace
    # 4. Call namespace['run_test'](rows)
    # 5. Validate return value is list[int]
    # 6. Return {hit_indices, hit_count, hit_pct, hit_amount, sample_hits}
```

### Timeout

Wrap execution in a thread with a 10-second timeout. If it exceeds, kill it and return an error.

---

## Module: base_tests.py

### Purpose

3–4 hardcoded example tests that are always available (no LLM needed). These:
1. Serve as examples for users to understand what kinds of tests are possible
2. Run instantly (no LLM call needed)
3. Show in the UI with a "built-in" badge vs "AI-generated" badge

### The 4 base tests to implement

**BASE1 — Duplicate Invoices Same Vendor**
```
GROUP BY (vendor_number, amount_excl_lc, date_authorized, physical_invoice_no)
HAVING COUNT > 1
```

**BASE2 — Round Dollar Amounts**
```
Check if amount_excl_lc is divisible by 100, 1000, 10000, etc.
9-level ladder: 100 → 500 → 1000 → 5000 → 10000 → 50000 → 100000 → 500000 → 1000000
```

**BASE3 — Payment Before Invoice Date**
```
date_authorized > scheduled_pay_date AND amount_excl_lc != 0
```

**BASE4 — High Value Keyword Match**
```
invoice_description contains any of: bonus, gift, personal, cash, loan, advance,
entertainment, travel, consulting, commission
AND amount_excl_lc > 10000
```

---

## API Endpoints

### POST /api/validate
Upload CSV, run schema mapping + DQ validation. Returns run_id + mapped row data.

Same as existing system.

### GET /api/runs/{run_id}/schema
Returns UDM field names present in this run + 5 sample rows. Used by test builder to give LLM context.

### POST /api/runs/{run_id}/tests/generate
Body: `{ "prompt": "user's natural language description" }`

Calls Groq → returns:
```json
{
  "test_name": "...",
  "description": "...",
  "code": "def run_test(rows):\n    ...",
  "explanation": "...",
  "required_fields": [...],
  "severity": "warning"
}
```

User reviews the generated test (sees the code + explanation) before running.

### POST /api/runs/{run_id}/tests/run
Body: `{ "tests": [ { "id": "BASE1", "type": "base" }, { "id": "custom_1", "type": "generated", "code": "...", "test_name": "..." } ] }`

Runs all selected tests (base + generated) and returns results.

### GET /api/runs/{run_id}/tests/results
Returns cached results from last run.

### GET /api/runs
List recent runs.

---

## UI Design

### Layout: same 3-section structure as existing system

**Left sidebar** (dark, same style):
- DIA Agentic logo
- Upload
- Tests
- Results
- Run history

**Main content** (light cards, same CSS variables):
- Section 1: Upload + schema mapping (same as existing)
- Section 2: Test Builder (new)
- Section 3: Results (similar to existing)

### Section 2: Test Builder

```
┌─────────────────────────────────────────────────────────────────┐
│  ANALYTICS TESTS                                                 │
│                                                                  │
│  BASE TESTS (always available)                                   │
│  ┌──────────────────┐ ┌──────────────────┐                      │
│  │ [✓] Duplicate    │ │ [✓] Round Dollar  │                      │
│  │     Invoices     │ │     Amounts       │                      │
│  │     BUILT-IN     │ │     BUILT-IN      │                      │
│  └──────────────────┘ └──────────────────┘                      │
│  ┌──────────────────┐ ┌──────────────────┐                      │
│  │ [✓] Payment      │ │ [✓] Keyword Match │                      │
│  │     Before Inv   │ │     High Value    │                      │
│  │     BUILT-IN     │ │     BUILT-IN      │                      │
│  └──────────────────┘ └──────────────────┘                      │
│                                                                  │
│  ──────────────────────────────────────────────────────────     │
│                                                                  │
│  ✦ ADD AI-GENERATED TEST                                        │
│                                                                  │
│  Describe what pattern you want to detect:                       │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Find vendors who submitted invoices where the amount ends  │ │
│  │ in .99 or .95 — possible price manipulation               │ │
│  └────────────────────────────────────────────────────────────┘ │
│  [Generate Test ✦]   ← calls Groq                               │
│                                                                  │
│  ┌────────────────────────────────────────────────────────── ┐  │
│  │ ✓ TEST GENERATED: "Near-Round Amount Detection"           │  │
│  │                                                           │  │
│  │ What it detects:                                          │  │
│  │ Flags invoices where amount ends in .99 or .95,          │  │
│  │ which may indicate deliberate price manipulation.        │  │
│  │                                                           │  │
│  │ Generated code:                                           │  │
│  │ ┌─────────────────────────────────────────────────────┐  │  │
│  │ │ def run_test(rows):                                  │  │  │
│  │ │     hits = []                                        │  │  │
│  │ │     for i, row in enumerate(rows):                   │  │  │
│  │ │         try:                                         │  │  │
│  │ │             amt = float(row.get('amount_excl_lc',0)) │  │  │
│  │ │             cents = round(amt % 1, 2)                │  │  │
│  │ │             if cents in (0.99, 0.95):                │  │  │
│  │ │                 hits.append(i)                       │  │  │
│  │ │         except: pass                                 │  │  │
│  │ │     return hits                                      │  │  │
│  │ └─────────────────────────────────────────────────────┘  │  │
│  │                                                           │  │
│  │ [✓ Add to Tests]   [✕ Discard]   [↻ Regenerate]         │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  MY TESTS FOR THIS RUN (6 total)                                 │
│  BASE1 BASE2 BASE3 BASE4 [Near-Round ✦] [SoD Violation ✦]      │
│                                                                  │
│  [▶ Run All Tests]                                               │
└─────────────────────────────────────────────────────────────────┘
```

### Section 3: Results

Same card layout as existing system:
- One card per test
- Hit count, hit %, hit amount
- BUILT-IN or AI GENERATED badge
- Click to expand sample rows
- Each result card shows the LLM explanation (for AI-generated tests)

### Code display

Show generated code in a styled `<pre>` block with syntax highlighting (use a lightweight highlighter like highlight.js or Prism.js, or just simple CSS).

---

## LLM Prompt (exact system prompt for test_builder.py)

```
You are a financial data analyst writing Python functions to detect anomalies in invoice data.

The data has already been mapped to a canonical schema. Each row is a Python dict with these fields:
{FIELD_DESCRIPTIONS}

Here are 5 sample rows from the actual dataset:
{SAMPLE_ROWS}

The user wants to detect this pattern:
"{USER_PROMPT}"

Write a Python function called run_test that:
1. Takes rows: list[dict] as input
2. Returns list[int] — the 0-based indices of rows that match the pattern
3. Handles missing/null values gracefully (use row.get('field') or '', or 0)
4. Does NOT import anything — only use Python builtins
5. Is efficient — avoid nested O(n²) loops if possible

Return ONLY valid JSON in this exact format, nothing else:
{
  "test_name": "Short name (max 5 words)",
  "description": "One sentence: exactly what pattern this detects",
  "required_fields": ["field1", "field2"],
  "severity": "error or warning or info",
  "code": "def run_test(rows):\n    ...\n    return hits",
  "explanation": "2-3 sentences explaining the logic in plain English for a non-technical reviewer"
}
```

### Field descriptions to inject

```
vendor_number: Vendor identifier code
vendor_name: Vendor full name
company_code: Company/entity code
physical_invoice_no: Physical invoice number from vendor
system_invoice_no: System-assigned invoice number
date_documented: Date invoice was created/entered
date_captured: Date invoice was captured in system
date_authorized: Date invoice was approved
scheduled_pay_date: Date payment is scheduled
payment_date: Actual date payment was made
amount_excl_lc: Invoice amount excluding VAT (local currency)
amount_incl_lc: Invoice amount including VAT (local currency)
amount_vat_lc: VAT amount (local currency)
reference_currency: Currency code (e.g. USD, EUR, ZAR)
invoice_description: Description/narration on the invoice
invoice_type: Type of invoice (standard, credit, debit memo)
captured_by: Person who entered the invoice
authorized_by: Person who approved the invoice
payment_type: Method of payment
fiscal_year: Financial year
```

---

## Risk Scoring (same formula as existing system)

For each row hit by at least one test, compute:

```python
test_score    = mean(weightage × red_flag_mult) / 15 × 10
overlap_bonus = {1: 1.0, 2: 1.2, 3: 1.35, 4+: 1.5}
amount_score  = min(log10(abs_amount+1) / log10(max_amount+1), 1) × 10
final_score   = round((test_score × overlap_bonus × amount_score) / 150 × 100)
```

Default weightage for base tests: 5. For AI-generated tests: 7 (unknown pattern = higher suspicion).

Score bands: Critical (75–100), High (50–74), Medium (25–49), Low (0–24).

---

## UI Style Guide

Use the same visual language as the existing DIA system:

```css
/* CSS variables — same as existing */
--sb-bg: #0d1117;          /* sidebar background */
--sb-surface: #161b22;     /* sidebar surface */
--sb-border: #21262d;      /* sidebar border */
--sb-text: #8b949e;        /* sidebar text */
--sb-text-active: #e6edf3; /* sidebar active text */
--sb-accent: #388bfd;      /* accent blue */
--main-bg: #f1f5f9;        /* main content background */
--card-bg: #ffffff;        /* card background */
--border: #e2e8f0;         /* content border */
--text-primary: #0f172a;
--text-secondary: #475569;
--text-muted: #94a3b8;
--blue: #2563eb;
--green: #059669;
--red: #dc2626;
--amber: #d97706;
--purple: #7c3aed;
--radius: 12px;
--shadow: 0 1px 3px rgba(0,0,0,.06);

/* Font */
font-family: 'Inter', sans-serif;  /* Google Fonts */
```

**AI-generated test badge**: purple background `#7c3aed` with sparkle icon ✦
**Built-in test badge**: teal/green background `#059669`

---

## What Files to Create in the New Folder

### Step 1: Create folder `DIA Agentic` in Downloads

### Step 2: Paste these files from `DIA POC/dia_validation/core/` into `DIA Agentic/dia_agentic/core/`:
- `udm.py`
- `ingestor.py`
- `schema_mapper.py`
- `erp_profiles.py`
- `mapping_registry.py`

### Step 3: Paste this folder:
- `DIA POC/dia_validation/data/erp_profiles/` → `DIA Agentic/dia_agentic/data/erp_profiles/`

### Step 4: Create `.env` with your Groq API key

### Step 5: Open new Droid chat, paste this file, and say "Build this"

---

## Test Data

Use the same test CSVs from the DIA POC folder:
- `test_invoice_2000.csv` — full featured, all columns
- `test_invoice_2000_demo.csv` — fewer columns, good for testing N/A behavior

---

## Key Differences from Existing DIA POC

| Existing DIA POC | New DIA Agentic |
|---|---|
| 16 hardcoded SP-parity tests | 4 base tests + unlimited AI-generated tests |
| Fixed logic per test | LLM writes the logic from user's description |
| Deterministic, always same result | Dynamic — new test = new logic generated |
| SP logic validated against DB | User validates by inspecting generated code |
| Python functions in test_engine.py | Python functions generated by LLM at runtime |
| Fast (no LLM per test run) | ~2-3s per test generation (one-time, then cached) |
| Test results only | Test results + LLM explanation of what was found |

---

## Definition of Done

The system is complete when:
1. User can upload a CSV and see schema mapping results
2. 4 base tests show and can be run without any LLM call
3. User can type a natural language prompt and receive a generated test (code + explanation shown)
4. Generated test can be added to the run and executed on the full dataset
5. Results show hit count, hit %, amount, sample rows, and LLM explanation
6. Multiple AI-generated tests can be added in one run
7. Risk score computed per row across all tests (base + generated)
8. UI matches the style guide above
