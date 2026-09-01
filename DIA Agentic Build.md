# DIA Agentic — Complete Build Specification

## What You Are Building

A standalone web application for agentic P2P invoice analytics. The user uploads an invoice CSV, the system maps the columns automatically, then lets the user run analytics tests — either from 4 pre-built tests OR by describing any pattern in plain English and letting an LLM generate the test logic on the fly.

**This is a brand new project. Do not reference any existing code unless explicitly told to copy a specific file.**

---

## Core Concept

Every test in this system — including the 4 pre-built ones — is defined the same way:

```python
{
  "id": "round_dollar",
  "test_name": "Round Dollar Invoices",
  "type": "preset",          # or "generated" for user-created tests
  "prompt": "...",           # the natural language description
  "code": "def run_test(rows):\n    ...",   # Python that runs on rows
  "explanation": "...",      # shown to reviewer
  "severity": "warning",
  "weightage": 8,
  "red_flag": False
}
```

Pre-built tests ship with their code already written — no LLM needed to run them. User-created tests go through Groq to generate the code, then run the same way. The UI treats both identically in results.

---

## Project Structure

```
DIA Agentic/
├── .env
├── requirements.txt
│
└── dia_agentic/
    ├── __init__.py
    ├── app.py                  ← FastAPI (port 8001)
    ├── service.py              ← pipeline orchestration
    │
    ├── core/
    │   ├── __init__.py
    │   ├── udm.py              ← COPY from DIA POC (41-field UDM + alias map)
    │   ├── ingestor.py         ← COPY from DIA POC (CSV parser)
    │   ├── schema_mapper.py    ← COPY from DIA POC (5-tier mapping)
    │   ├── erp_profiles.py     ← COPY from DIA POC
    │   ├── mapping_registry.py ← COPY from DIA POC
    │   ├── preset_tests.py     ← 4 pre-built tests (prompt + code + explanation)
    │   ├── test_builder.py     ← prompt → Groq → test definition
    │   ├── test_executor.py    ← safely run test code on rows
    │   ├── risk_scorer.py      ← 0-100 risk score per row
    │   └── run_store.py        ← SQLite run history
    │
    ├── data/
    │   ├── agentic_runs.db     ← auto-created
    │   └── erp_profiles/       ← COPY from DIA POC
    │       ├── sap.json
    │       ├── oracle.json
    │       ├── tally.json
    │       └── dynamics.json
    │
    └── static/
        ├── index.html
        ├── style.css
        └── app.js
```

### Files to copy from DIA POC

From `DIA POC/dia_validation/core/` → copy into `DIA Agentic/dia_agentic/core/`:
- `udm.py`
- `ingestor.py`
- `schema_mapper.py`
- `erp_profiles.py`
- `mapping_registry.py`

From `DIA POC/dia_validation/data/erp_profiles/` → copy into `DIA Agentic/dia_agentic/data/erp_profiles/`

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
GROQ_API_KEY=<your key>
```

---

## The 4 Pre-Built Tests (preset_tests.py)

These are the 4 most universally important fraud indicators. Each has a prompt (what a user would type), pre-written Python code (runs without LLM), and a plain-English explanation.

### TEST 1 — Round Dollar Invoices

**Why important:** Fraudulent invoices are often filed in suspiciously round amounts (10,000 / 50,000 / 100,000) to stay under approval thresholds or avoid scrutiny.

```python
PRESET_TESTS = [
  {
    "id": "round_dollar",
    "test_name": "Round Dollar Invoices",
    "type": "preset",
    "prompt": "Find all invoices where the amount is a suspiciously round number — divisible by 1000, 5000, 10000, 50000, or 100000. These are common in fraudulent billing.",
    "severity": "warning",
    "weightage": 8,
    "red_flag": False,
    "required_fields": ["amount_excl_lc"],
    "explanation": "Flags invoices with round amounts (divisible by 1,000+). Fraudulent invoices are often filed in exact round numbers to avoid scrutiny. Legitimate invoices typically have cents and specific amounts.",
    "code": """
def run_test(rows):
    THRESHOLDS = [1000000, 500000, 100000, 50000, 10000, 5000, 1000]
    hits = []
    for i, row in enumerate(rows):
        try:
            amt = abs(float(str(row.get('amount_excl_lc') or 0).replace(',', '')))
            if amt == 0:
                continue
            for t in THRESHOLDS:
                if amt % t == 0:
                    hits.append(i)
                    break
        except (ValueError, TypeError):
            pass
    return hits
"""
  },
```

### TEST 2 — Duplicate Invoice Detection

**Why important:** The most common P2P fraud — same invoice submitted twice (accidentally or deliberately) to get paid twice.

```python
  {
    "id": "duplicate_invoice",
    "test_name": "Duplicate Invoice Detection",
    "type": "preset",
    "prompt": "Find invoices that appear to be duplicates — same vendor, same amount, and same invoice number submitted more than once. These could be accidental double-submissions or deliberate duplicate billing.",
    "severity": "error",
    "weightage": 10,
    "red_flag": True,
    "required_fields": ["vendor_number", "amount_excl_lc"],
    "explanation": "Groups invoices by vendor + amount + invoice number. Any combination appearing more than once is a potential duplicate payment. This is one of the most common P2P frauds — the same invoice submitted twice to receive double payment.",
    "code": """
def run_test(rows):
    from collections import defaultdict
    seen = defaultdict(list)
    for i, row in enumerate(rows):
        vendor = str(row.get('vendor_number') or '').strip().lower()
        inv_no = str(row.get('physical_invoice_no') or row.get('system_invoice_no') or '').strip().lower()
        try:
            amt = round(abs(float(str(row.get('amount_excl_lc') or 0).replace(',', ''))), 2)
        except (ValueError, TypeError):
            amt = 0
        if vendor and amt > 0:
            key = (vendor, amt, inv_no)
            seen[key].append(i)
    hits = []
    for indices in seen.values():
        if len(indices) > 1:
            hits.extend(indices)
    return hits
"""
  },
```

### TEST 3 — Suspicious Keyword in Description

**Why important:** Invoice descriptions containing words like "bonus", "gift", "personal", "cash advance", "entertainment" are red flags for personal expense abuse or fictitious billing.

```python
  {
    "id": "keyword_suspicious",
    "test_name": "Suspicious Description Keywords",
    "type": "preset",
    "prompt": "Find invoices whose description contains suspicious words commonly associated with fraud or policy violations — such as bonus, gift, personal, cash, loan, advance, entertainment, alcohol, or travel.",
    "severity": "warning",
    "weightage": 9,
    "red_flag": True,
    "required_fields": ["invoice_description"],
    "explanation": "Scans invoice descriptions for keywords commonly associated with fraudulent or non-compliant spend: personal expenses, gifts, cash advances, entertainment. Even one match warrants a closer look at the vendor relationship and approval chain.",
    "code": """
def run_test(rows):
    KEYWORDS = [
        'bonus', 'gift', 'personal', 'cash', 'loan', 'advance',
        'entertainment', 'alcohol', 'drinks', 'travel', 'holiday',
        'vacation', 'reward', 'incentive', 'tip', 'gratuity',
        'donation', 'contribution', 'sponsorship', 'refund',
        'reimbursement', 'settlement', 'legal', 'penalty',
        'fine', 'commission', 'kickback', 'rebate', 'courtesy'
    ]
    hits = []
    for i, row in enumerate(rows):
        desc = str(row.get('invoice_description') or '').lower()
        if not desc or desc in ('none', 'null', 'nan', 'n/a', ''):
            continue
        for kw in KEYWORDS:
            if kw in desc:
                hits.append(i)
                break
    return hits
"""
  },
```

### TEST 4 — Same-Day Capture and Payment

**Why important:** When an invoice is captured and paid on the same day, it bypasses normal approval and aging controls — a key fraud indicator and internal control failure.

```python
  {
    "id": "same_day_payment",
    "test_name": "Same Day Capture and Payment",
    "type": "preset",
    "prompt": "Find invoices where the date the invoice was captured in the system is the same as the scheduled payment date. This means the invoice bypassed normal payment aging and approval cycles — a major internal control failure.",
    "severity": "error",
    "weightage": 9,
    "red_flag": True,
    "required_fields": ["date_captured", "scheduled_pay_date"],
    "explanation": "Normal invoice processing takes days to weeks — invoice received, entered, approved, aged, then paid. When capture date equals payment date, that entire control cycle was skipped. This can indicate rushed fraudulent payments or system bypasses.",
    "code": """
def run_test(rows):
    DATE_FORMATS = ['%d-%m-%Y', '%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y%m%d']
    def parse(val):
        if not val or str(val).strip().lower() in ('none','null','nan','n/a','','01-01-1900','1900-01-01'):
            return None
        from datetime import datetime
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(str(val).strip(), fmt).date()
            except ValueError:
                continue
        return None
    hits = []
    for i, row in enumerate(rows):
        captured = parse(row.get('date_captured'))
        scheduled = parse(row.get('scheduled_pay_date'))
        if captured and scheduled and captured == scheduled:
            try:
                amt = float(str(row.get('amount_excl_lc') or 0).replace(',',''))
                if amt != 0:
                    hits.append(i)
            except (ValueError, TypeError):
                hits.append(i)
    return hits
"""
  }
]
```

---

## Module: test_builder.py

Takes a user's natural language prompt, calls Groq, and returns a complete test definition including executable Python code.

```python
import json
import os
from groq import Groq

UDM_FIELD_DESCRIPTIONS = """
vendor_number: Vendor identifier code (string)
vendor_name: Vendor full name (string)
company_code: Company or entity code (string)
physical_invoice_no: Physical invoice number from the vendor (string)
system_invoice_no: System-assigned invoice number (string)
original_invoice_no: Original invoice reference number (string)
date_documented: Date invoice was created or entered (DD-MM-YYYY string)
date_captured: Date invoice was captured in the system (DD-MM-YYYY string)
date_authorized: Date invoice was approved/authorized (DD-MM-YYYY string)
scheduled_pay_date: Date payment is scheduled (DD-MM-YYYY string)
payment_date: Actual date payment was made (DD-MM-YYYY string)
amount_excl_lc: Invoice amount excluding VAT in local currency (numeric string)
amount_incl_lc: Invoice amount including VAT in local currency (numeric string)
amount_vat_lc: VAT amount in local currency (numeric string)
reference_currency: Currency code e.g. USD EUR ZAR (string)
invoice_description: Description or narration written on the invoice (string)
invoice_type: Type of invoice — standard, credit, debit memo (string)
captured_by: Person who entered the invoice (string)
authorized_by: Person who approved the invoice (string)
payment_type: Method of payment (string)
fiscal_year: Financial year (string)
block_group: Grouping or blocking classification (string)
vendor_country: Vendor country code (string)
po_non_po: Whether invoice is against a PO or not (string)
"""

SYSTEM_PROMPT = """You are a financial fraud analyst and Python developer.
You write Python functions to detect anomalies and fraud patterns in invoice data.

The invoice data is a list of Python dicts. Each dict has these fields (all values are strings or None):
{field_descriptions}

Sample rows from the actual dataset:
{sample_rows}

Rules for the function you write:
1. Function signature must be exactly: def run_test(rows):
2. Input: rows is a list[dict] with the field names above as keys
3. Output: return a list[int] of 0-based indices of rows that match the pattern
4. Handle None and missing values gracefully — use row.get('field') or '', or 0
5. Handle numeric fields with: float(str(val).replace(',','')) inside try/except
6. Handle date fields by parsing common formats: %d-%m-%Y, %Y-%m-%d, %m/%d/%Y
7. Do NOT import anything at the top — put any imports inside the function
8. Keep it efficient — avoid O(n²) where possible

You must return ONLY valid JSON, no markdown, no explanation outside the JSON:
{{
  "test_name": "3-5 word name",
  "description": "One sentence: what pattern this detects and why it matters",
  "required_fields": ["field_name_1", "field_name_2"],
  "severity": "error or warning or info",
  "code": "def run_test(rows):\\n    ...\\n    return hits",
  "explanation": "2-3 sentences in plain English explaining the logic to a non-technical auditor"
}}"""


def build_test(prompt: str, sample_rows: list[dict]) -> dict:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    
    sample_str = json.dumps(sample_rows[:5], indent=2, default=str)
    
    user_message = f'Detect this pattern: "{prompt}"'
    
    system = SYSTEM_PROMPT.format(
        field_descriptions=UDM_FIELD_DESCRIPTIONS,
        sample_rows=sample_str
    )
    
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message}
                ],
                temperature=0.2,
                max_tokens=1500,
            )
            raw = resp.choices[0].message.content.strip()
            
            # strip markdown if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            
            result = json.loads(raw)
            
            # validate required keys
            for key in ("test_name", "description", "code", "explanation", "severity"):
                if key not in result:
                    raise ValueError(f"Missing key: {key}")
            
            # validate code compiles
            compile(result["code"], "<llm>", "exec")
            
            result["type"] = "generated"
            result.setdefault("weightage", 7)
            result.setdefault("red_flag", False)
            result.setdefault("required_fields", [])
            return result
            
        except json.JSONDecodeError:
            if attempt == 0:
                user_message += "\n\nIMPORTANT: Return ONLY the JSON object, absolutely nothing else."
                continue
            return {"error": "Could not parse LLM response as JSON. Try rephrasing your prompt."}
        except SyntaxError as e:
            return {"error": f"Generated code has a syntax error: {e}. Try rephrasing your prompt."}
        except Exception as e:
            return {"error": str(e)}
    
    return {"error": "Test generation failed after 2 attempts. Try rephrasing your prompt."}
```

---

## Module: test_executor.py

Safely executes a `run_test(rows)` function — whether pre-built or LLM-generated.

```python
import math
from datetime import datetime

SAFE_BUILTINS = {
    '__builtins__': {
        'len': len, 'range': range, 'enumerate': enumerate,
        'zip': zip, 'list': list, 'dict': dict, 'set': set,
        'tuple': tuple, 'str': str, 'int': int, 'float': float,
        'bool': bool, 'abs': abs, 'round': round,
        'min': min, 'max': max, 'sum': sum,
        'sorted': sorted, 'reversed': reversed,
        'isinstance': isinstance, 'type': type,
        'any': any, 'all': all, 'filter': filter, 'map': map,
        'print': print, 'repr': repr,
        'ValueError': ValueError, 'TypeError': TypeError,
        'KeyError': KeyError, 'IndexError': IndexError,
        'AttributeError': AttributeError, 'StopIteration': StopIteration,
        '__import__': __import__,   # allow imports INSIDE function body only
        'None': None, 'True': True, 'False': False,
    }
}


def _parse_amount(val) -> float:
    try:
        return abs(float(str(val or 0).replace(',', '').strip()))
    except (ValueError, TypeError):
        return 0.0


def execute_test(test: dict, rows: list[dict]) -> dict:
    code = test.get("code", "")
    test_name = test.get("test_name", "Unknown Test")
    
    if not rows:
        return _empty_result(test_name, "No rows to test")
    
    try:
        namespace = dict(SAFE_BUILTINS)
        compile(code, "<test>", "exec")
        exec(code, namespace)
        
        if 'run_test' not in namespace:
            return _empty_result(test_name, "Generated code missing run_test function")
        
        hit_indices = namespace['run_test'](rows)
        
        if not isinstance(hit_indices, list):
            return _empty_result(test_name, "run_test must return a list")
        
        hit_indices = [i for i in hit_indices if isinstance(i, int) and 0 <= i < len(rows)]
        
    except Exception as e:
        return _empty_result(test_name, f"Execution error: {e}")
    
    total = len(rows)
    hits = len(hit_indices)
    
    # compute hit amount
    hit_amount = sum(_parse_amount(rows[i].get('amount_excl_lc')) for i in hit_indices)
    
    # build sample rows (up to 50)
    sample = []
    for i in sorted(set(hit_indices))[:50]:
        r = rows[i]
        inv_no = r.get('physical_invoice_no') or r.get('system_invoice_no') or '—'
        vendor = r.get('vendor_number') or ''
        inv_id = f"{vendor} / {inv_no}" if vendor and inv_no != '—' else (inv_no if inv_no != '—' else f"Row {i+2}")
        sample.append({
            "row": i + 2,
            "inv_id": inv_id,
            "vendor_number": r.get('vendor_number'),
            "vendor_name": r.get('vendor_name'),
            "physical_invoice_no": inv_no,
            "date_authorized": r.get('date_authorized') or r.get('date_documented'),
            "amount_excl_lc": r.get('amount_excl_lc'),
            "invoice_description": str(r.get('invoice_description') or '')[:120],
            "reference_currency": r.get('reference_currency'),
        })
    
    return {
        "test_id": test.get("id", test_name.lower().replace(" ", "_")),
        "test_name": test_name,
        "type": test.get("type", "generated"),
        "applicable": True,
        "hit_count": hits,
        "hit_pct": round(hits / total * 100, 1) if total else 0.0,
        "hit_amount": round(hit_amount, 2),
        "total_rows": total,
        "severity": test.get("severity", "warning"),
        "red_flag": test.get("red_flag", False),
        "weightage": test.get("weightage", 7),
        "all_hit_indices": sorted(set(hit_indices)),
        "sample_hits": sample,
        "explanation": test.get("explanation", ""),
        "description": test.get("description", ""),
        "code": code,
        "prompt": test.get("prompt", ""),
    }


def _empty_result(test_name: str, reason: str) -> dict:
    return {
        "test_id": test_name.lower().replace(" ", "_"),
        "test_name": test_name,
        "applicable": False,
        "not_applicable_reason": reason,
        "hit_count": 0, "hit_pct": 0, "hit_amount": 0,
        "sample_hits": [], "all_hit_indices": [],
    }
```

---

## Module: risk_scorer.py

```python
import math
from dataclasses import dataclass


OVERLAP_BONUS = {1: 1.0, 2: 1.2, 3: 1.35}
OVERLAP_MAX = 1.5

BANDS = [(75, "Critical", "critical"), (50, "High", "high"),
         (25, "Medium", "medium"), (0, "Low", "low")]


@dataclass
class RowRisk:
    row_idx: int
    risk_score: int
    band_label: str
    band_css: str
    flagged_by: list
    overlap_count: int
    amount: float


def compute_risk_scores(rows: list[dict], test_results: list[dict]) -> list[RowRisk]:
    row_to_tests: dict[int, list] = {}
    for tr in test_results:
        if not tr.get("applicable") or tr.get("hit_count", 0) == 0:
            continue
        entry = {
            "test_id": tr["test_id"],
            "test_name": tr["test_name"],
            "weightage": tr.get("weightage", 7),
            "red_flag": tr.get("red_flag", False),
            "severity": tr.get("severity", "warning"),
            "type": tr.get("type", "generated"),
        }
        for idx in tr.get("all_hit_indices", []):
            row_to_tests.setdefault(idx, []).append(entry)

    if not row_to_tests:
        return []

    amounts = []
    for idx in row_to_tests:
        try:
            a = abs(float(str(rows[idx].get("amount_excl_lc") or 0).replace(",", "")))
        except (ValueError, TypeError):
            a = 0.0
        amounts.append(a)
    max_amount = max(amounts) if amounts else 1.0
    log_max = math.log10(max_amount + 1) if max_amount > 0 else 1.0

    results = []
    for idx, tests in row_to_tests.items():
        contributions = [t["weightage"] * (1.5 if t["red_flag"] else 1.0) for t in tests]
        test_score = (sum(contributions) / len(contributions) / 15.0) * 10.0
        n = len(tests)
        overlap = OVERLAP_BONUS.get(n, OVERLAP_MAX)
        try:
            amt = abs(float(str(rows[idx].get("amount_excl_lc") or 0).replace(",", "")))
        except (ValueError, TypeError):
            amt = 0.0
        amount_score = min(math.log10(amt + 1) / log_max, 1.0) * 10.0
        raw = test_score * overlap * amount_score
        score = min(100, round(raw / 150.0 * 100))

        label, css = next(((l, c) for t, l, c in BANDS if score >= t), ("Low", "low"))
        results.append(RowRisk(
            row_idx=idx, risk_score=score,
            band_label=label, band_css=css,
            flagged_by=tests, overlap_count=n, amount=amt
        ))

    results.sort(key=lambda r: r.risk_score, reverse=True)
    return results
```

---

## Module: run_store.py

```python
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "agentic_runs.db"


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                created_at TEXT,
                filename TEXT,
                total_rows INTEGER,
                report_json TEXT,
                rows_json TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS test_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                created_at TEXT,
                tests_json TEXT,
                results_json TEXT
            )
        """)
        c.commit()


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def save_run(run_id: str, report: dict, rows: list[dict], filename: str):
    with _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO runs (run_id, created_at, filename, total_rows, report_json, rows_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (run_id, datetime.now(timezone.utc).isoformat(),
              filename, len(rows),
              json.dumps(report), json.dumps(rows)))
        c.commit()


def get_rows(run_id: str) -> list[dict]:
    with _conn() as c:
        row = c.execute("SELECT rows_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["rows_json"]) if row else []


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT report_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["report_json"]) if row else None


def list_runs(limit: int = 20) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT run_id, created_at, filename, total_rows FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def save_test_session(run_id: str, tests: list[dict], results: list[dict]):
    with _conn() as c:
        c.execute("""
            INSERT INTO test_sessions (run_id, created_at, tests_json, results_json)
            VALUES (?, ?, ?, ?)
        """, (run_id, datetime.now(timezone.utc).isoformat(),
              json.dumps(tests), json.dumps(results)))
        c.commit()


def get_last_test_session(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("""
            SELECT tests_json, results_json FROM test_sessions
            WHERE run_id=? ORDER BY created_at DESC LIMIT 1
        """, (run_id,)).fetchone()
    if not row:
        return None
    return {"tests": json.loads(row["tests_json"]), "results": json.loads(row["results_json"])}
```

---

## app.py (FastAPI)

```python
import math
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dia_agentic.core.ingestor import ingest_csv
from dia_agentic.core.preset_tests import PRESET_TESTS
from dia_agentic.core.test_builder import build_test
from dia_agentic.core.test_executor import execute_test
from dia_agentic.core.risk_scorer import compute_risk_scores
from dia_agentic.core.run_store import (
    init_db, new_run_id, save_run, get_rows, get_run,
    list_runs, save_test_session, get_last_test_session
)

app = FastAPI(title="DIA Agentic")
init_db()

_SERVER_SESSION = str(uuid.uuid4())

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def home():
    return FileResponse(static_dir / "index.html")


@app.get("/api/session")
def session():
    return {"session": _SERVER_SESSION}


@app.get("/api/runs")
def runs_list(limit: int = 20):
    return list_runs(limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    return r


@app.post("/api/validate")
async def validate(
    file: UploadFile = File(...),
    use_llm: bool = Form(False),
    erp_type: str = Form(""),
):
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        temp_path = tmp.name

    ingest = ingest_csv(temp_path, use_llm_fallback=use_llm, erp_type=erp_type or None)
    run_id = new_run_id()

    # determine which preset tests are applicable
    mapped_fields = {k for row in ingest.rows for k, v in row.items() if v is not None}
    applicable_presets = []
    for t in PRESET_TESTS:
        missing = [f for f in t.get("required_fields", []) if f not in mapped_fields]
        if missing:
            applicable_presets.append({**t, "applicable": False, "missing_fields": missing})
        else:
            applicable_presets.append({**t, "applicable": True})

    report = {
        "run_id": run_id,
        "source_file": file.filename,
        "total_rows": ingest.total_rows,
        "schema_mapping": ingest.mapping_result,
        "preset_tests": applicable_presets,
        "sample_rows": ingest.rows[:5],
    }

    save_run(run_id, report, ingest.rows, file.filename or "upload.csv")
    return report


@app.post("/api/runs/{run_id}/tests/generate")
async def generate_test(run_id: str, request: Request):
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    result = build_test(prompt, rows[:5])
    if "error" in result:
        return JSONResponse(result, status_code=422)
    return result


@app.post("/api/runs/{run_id}/tests/run")
async def run_tests(run_id: str, request: Request):
    body = await request.json()
    tests = body.get("tests", [])

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    results = []
    for test in tests:
        result = execute_test(test, rows)
        results.append(result)

    # compute risk scores
    scored = compute_risk_scores(rows, results)
    risk_summary = {
        "total_hit_rows": len(scored),
        "critical": sum(1 for r in scored if r.band_css == "critical"),
        "high": sum(1 for r in scored if r.band_css == "high"),
        "medium": sum(1 for r in scored if r.band_css == "medium"),
        "low": sum(1 for r in scored if r.band_css == "low"),
    }

    save_test_session(run_id, tests, results)
    return {"results": results, "risk_summary": risk_summary}


@app.get("/api/runs/{run_id}/tests/last")
def last_test_session(run_id: str):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test sessions found"}, status_code=404)
    rows = get_rows(run_id)
    scored = compute_risk_scores(rows, session["results"])
    session["risk_summary"] = {
        "total_hit_rows": len(scored),
        "critical": sum(1 for r in scored if r.band_css == "critical"),
        "high": sum(1 for r in scored if r.band_css == "high"),
        "medium": sum(1 for r in scored if r.band_css == "medium"),
        "low": sum(1 for r in scored if r.band_css == "low"),
    }
    return session
```

---

## UI Requirements (style.css, index.html, app.js)

### Visual Style

Same color system as DIA POC:

```css
:root {
  --sb-bg: #0d1117;
  --sb-surface: #161b22;
  --sb-border: #21262d;
  --sb-text: #8b949e;
  --sb-text-active: #e6edf3;
  --sb-accent: #388bfd;
  --main-bg: #f1f5f9;
  --card-bg: #ffffff;
  --border: #e2e8f0;
  --text-primary: #0f172a;
  --text-muted: #94a3b8;
  --blue: #2563eb;
  --green: #059669;
  --red: #dc2626;
  --amber: #d97706;
  --purple: #7c3aed;
}
body { font-family: 'Inter', sans-serif; background: var(--sb-bg); }
```

Load Inter from Google Fonts.

### Layout

Dark sidebar (240px) + light main content area. Same shell as DIA POC.

### Page sections

**Section 1 — Upload**
- Drag and drop zone
- ERP type selector (auto-detect / SAP / Oracle / Tally / Dynamics)
- AI mapping toggle
- Validate button
- After upload: show schema mapping table (source col → UDM field → method → status)

**Section 2 — Tests**

Two parts:

*Part A — Pre-built Tests (4 cards)*

Show 4 cards in a 2×2 grid. Each card:
- Test name
- Short description
- `BUILT-IN` green badge
- Checkbox to include/exclude
- If N/A (missing columns): show `N/A` grey badge + which column is missing
- Red flag icon (⚑) if red_flag=True

*Part B — AI Test Builder*

```
[ + Add AI Test ]  ← button that expands the builder

Textarea: "Describe what you want to detect..."

[ ✦ Generate Test ]  ← calls /api/runs/{id}/tests/generate

After generation, show a preview card:
  ┌─────────────────────────────────────────────────┐
  │ ✦ AI GENERATED          [ ✓ Add ] [ ✕ Discard ] │
  │                                                  │
  │ Test Name: Near-Round Amount Detection           │
  │                                                  │
  │ What it detects:                                 │
  │ Flags invoices with amounts ending in .99/.95    │
  │                                                  │
  │ Generated Code:                                  │
  │ ┌──────────────────────────────────────────┐    │
  │ │ def run_test(rows):                       │    │
  │ │     hits = []                             │    │
  │ │     for i, row in enumerate(rows):        │    │
  │ │         ...                               │    │
  │ │     return hits                           │    │
  │ └──────────────────────────────────────────┘    │
  └─────────────────────────────────────────────────┘
```

Code block: dark background (#0d1117), monospace font, green text (#34d399).
Show/hide toggle for code (collapsed by default, "Show Code ▾" link).

Once added, the test appears in a "My Tests" chip list below the builder.

*Run Button*

`[ ▶ Run All Tests (N selected) ]` — big blue button

**Section 3 — Results**

Show after tests run. Top KPI strip:
- Total hits | Critical | High | Medium | Low | Amount at risk

Then result cards (one per test, sorted: red flags first, then by hit count):
- Test name + badge (BUILT-IN green / AI GENERATED purple)
- Hit count, hit %, hit amount
- Severity badge
- LLM explanation (for AI tests)
- "Inspect N rows →" drill panel

Drill panel (inline expand):
- Table: Invoice ID | Vendor | Invoice No | Date | Amount | Description

---

## Running the Application

```powershell
cd "C:\Users\[username]\Downloads\DIA Agentic"
python -m uvicorn dia_agentic.app:app --host 127.0.0.1 --port 8001 --reload
```

Open http://127.0.0.1:8001

---

## Test Data

Use these files from the DIA POC folder:
- `C:\Users\[username]\Downloads\DIA POC\test_invoice_2000.csv` — full 40 columns, best for testing
- `C:\Users\[username]\Downloads\DIA POC\test_invoice_2000_demo.csv` — 35 columns (some tests N/A)

---

## Example AI Test Prompts (for testing)

These prompts should all generate working tests:

1. `"Find invoices where the same person both captured and authorized the invoice (segregation of duties violation)"`
2. `"Flag vendors who appear to submit invoices with amounts just below round thresholds like 9999 or 49999 — threshold manipulation"`
3. `"Detect invoices where the invoice number contains only sequential digits like 00001, 00002 — possible fabrication"`
4. `"Find all invoices with no invoice description or description shorter than 5 characters"`
5. `"Flag invoices where the amount in local currency is the same for more than 3 different invoices from the same vendor"`

---

## Definition of Done

- [ ] Upload CSV → schema mapping shows correctly
- [ ] 4 pre-built tests appear, checkboxes work, N/A shows correctly for missing columns
- [ ] "Generate Test" calls Groq and returns test with code + explanation visible
- [ ] Generated test can be added and runs on full dataset
- [ ] Results show for all tests (built-in + AI) with hit count, amount, sample rows
- [ ] Risk score summary (Critical/High/Medium/Low) shows after run
- [ ] Drill panel shows Invoice ID (not raw row number)
- [ ] State restored after browser refresh (same session), cleared after server restart
