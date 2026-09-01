import csv
import io
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dia_agentic.core.ingestor import ingest_csv
from dia_agentic.core.preset_tests import PRESET_TESTS
from dia_agentic.core.test_builder import build_test
from dia_agentic.core.test_executor import execute_test
from dia_agentic.core.risk_scorer import compute_risk_scores
from dia_agentic.core.data_quality import analyse, compare_runs
from dia_agentic.core.run_store import (
    init_db, new_run_id, save_run, get_rows, get_run,
    list_runs, save_test_session, get_last_test_session,
    save_review_decision, get_all_decisions, save_bulk_decisions,
    get_ai_cache, set_ai_cache,
    list_saved_tests, save_test_to_library, delete_saved_test,
)
from dia_agentic.core.cluster_engine import build_clusters
from dia_agentic.core.anomaly_detect import detect_anomalies
from dia_agentic.core.ai_review import analyse_clusters, suggest_tests
from dia_agentic.core.evidence import evidence_for
from dia_agentic.core.auth import (
    init_auth_tables, seed_default_user, login as auth_login,
    logout as auth_logout, require_auth, create_user, list_users, change_password,
)

# Load .env
from pathlib import Path as _P
import os as _os
_env = _P(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _os.environ.setdefault(_k.strip(), _v.strip())

app = FastAPI(title="DIA Agentic")
init_db()
init_auth_tables()
seed_default_user()

_SERVER_SESSION = str(uuid.uuid4())

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _row_evidence(flagged_by: list, row: dict) -> list:
    """Combine evidence from every test that flagged this row, deduplicated by field.
    For AI-generated tests with no hardcoded evidence logic, fall back to
    highlighting required_fields with a generic note."""
    seen_fields = set()
    result = []
    for fb in flagged_by:
        specific = evidence_for(fb.get("test_id", ""), row)
        if specific:
            for ev in specific:
                if ev["field"] not in seen_fields:
                    seen_fields.add(ev["field"])
                    result.append(ev)
        else:
            # fallback: highlight required_fields declared by the test
            for field in fb.get("required_fields", []):
                if field not in seen_fields and row.get(field):
                    seen_fields.add(field)
                    result.append({"field": field, "value": row.get(field),
                                   "note": f"flagged by {fb.get('test_name','test')}"})
    return result


@app.get("/")
def home():
    return FileResponse(static_dir / "index.html")

@app.get("/login")
def login_page():
    return FileResponse(static_dir / "login.html")

# ── Auth endpoints ───────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    result = auth_login(body.get("username",""), body.get("password",""))
    if not result:
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    return result

@app.post("/api/auth/logout")
async def api_logout(request: Request, sess=Depends(require_auth)):
    token = request.headers.get("x-auth-token","")
    auth_logout(token)
    return {"ok": True}

@app.get("/api/auth/me")
def api_me(sess=Depends(require_auth)):
    return sess

@app.post("/api/auth/users")
async def api_create_user(request: Request, sess=Depends(require_auth)):
    body = await request.json()
    ok = create_user(body.get("username",""), body.get("password",""), body.get("display_name",""))
    if not ok:
        return JSONResponse({"error": "Username already exists"}, status_code=400)
    return {"ok": True}

@app.get("/api/auth/users")
def api_list_users(sess=Depends(require_auth)):
    return list_users()

@app.post("/api/auth/change-password")
async def api_change_password(request: Request, sess=Depends(require_auth)):
    body = await request.json()
    change_password(sess["user_id"], body.get("new_password",""))
    return {"ok": True}

# ── Session (kept for compatibility) ─────────────────────────────────────────

@app.get("/api/session")
def session():
    return {"session": _SERVER_SESSION}

@app.get("/api/runs")
def runs_list(limit: int = 20, sess=Depends(require_auth)):
    return list_runs(limit, user_id=sess["user_id"])

@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, sess=Depends(require_auth)):
    r = get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    return r


@app.post("/api/validate")
async def validate(
    file: UploadFile = File(...),
    use_llm: bool = Form(False),
    erp_type: str = Form(""),
    sess=Depends(require_auth),
):
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        temp_path = tmp.name

    ingest = ingest_csv(temp_path, use_llm_fallback=use_llm, erp_type=erp_type or None)

    run_id = new_run_id()

    mapped_fields = {k for row in ingest.rows for k, v in row.items() if v is not None}
    applicable_presets = []
    for t in PRESET_TESTS:
        missing = [f for f in t.get("required_fields", []) if f not in mapped_fields]
        if missing:
            applicable_presets.append({**t, "applicable": False, "missing_fields": missing})
        else:
            applicable_presets.append({**t, "applicable": True, "missing_fields": []})

    dq = analyse(ingest.rows)

    report = {
        "run_id": run_id,
        "source_file": file.filename,
        "total_rows": ingest.total_rows,
        "schema_mapping": ingest.mapping_result,
        "preset_tests": applicable_presets,
        "sample_rows": ingest.rows[:5],
        "data_quality": dq,
    }

    save_run(run_id, report, ingest.rows, file.filename or "upload.csv", user_id=sess["user_id"])
    return report


@app.post("/api/runs/{run_id}/tests/generate")
async def generate_test(run_id: str, request: Request, sess=Depends(require_auth)):
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
async def run_tests(run_id: str, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    tests = body.get("tests", [])

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    results = []
    for test in tests:
        result = execute_test(test, rows)
        results.append(result)

    scored = compute_risk_scores(rows, results)
    risk_summary = {
        "total_hit_rows": len(scored),
        "critical": sum(1 for r in scored if r.band_css == "critical"),
        "high": sum(1 for r in scored if r.band_css == "high"),
        "medium": sum(1 for r in scored if r.band_css == "medium"),
        "low": sum(1 for r in scored if r.band_css == "low"),
        "top_risks": [
            {
                "row_idx": r.row_idx,
                "risk_score": r.risk_score,
                "band_label": r.band_label,
                "band_css": r.band_css,
                "flagged_by": r.flagged_by,
                "overlap_count": r.overlap_count,
                "amount": r.amount,
            }
            for r in scored[:100]
        ],
    }

    save_test_session(run_id, tests, results, user_id=sess["user_id"])
    return {"results": results, "risk_summary": risk_summary}


@app.get("/api/runs/{run_id}/review")
def review_data(run_id: str, page: int = 1, per_page: int = 50, sess=Depends(require_auth),
                band: str = "", test_id: str = "", vendor: str = "", status: str = "",
                min_amount: float = 0, max_amount: float = 0,
                overlap_only: bool = False, red_flag_only: bool = False):
    sess = get_last_test_session(run_id)
    if not sess:
        return JSONResponse({"error": "No test session found"}, status_code=404)

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, sess["results"])
    decisions = get_all_decisions(run_id)

    # Build full hit row list
    results_by_id = {r["test_id"]: r for r in sess["results"]}
    hit_rows = []
    for rs in scored:
        row = rows[rs.row_idx]
        dec = decisions.get(rs.row_idx, {"status": "pending", "note": ""})
        hit_rows.append({
            "row_idx": rs.row_idx,
            "row_key": str(rs.row_idx),
            "risk_score": rs.risk_score,
            "band_label": rs.band_label,
            "band_css": rs.band_css,
            "overlap_count": rs.overlap_count,
            "amount": rs.amount,
            "flagged_by": rs.flagged_by,
            "review_status": dec["status"],
            "review_note": dec["note"],
            # invoice fields
            "vendor_number": row.get("vendor_number"),
            "vendor_name": row.get("vendor_name"),
            "physical_invoice_no": row.get("physical_invoice_no"),
            "system_invoice_no": row.get("system_invoice_no"),
            "date_authorized": row.get("date_authorized"),
            "date_documented": row.get("date_documented"),
            "date_captured": row.get("date_captured"),
            "scheduled_pay_date": row.get("scheduled_pay_date"),
            "amount_excl_lc": row.get("amount_excl_lc"),
            "amount_incl_lc": row.get("amount_incl_lc"),
            "reference_currency": row.get("reference_currency"),
            "invoice_description": row.get("invoice_description"),
            "company_code": row.get("company_code"),
            "payment_type": row.get("payment_type"),
            "fiscal_year": row.get("fiscal_year"),
            "captured_by": row.get("captured_by"),
            "authorized_by": row.get("authorized_by"),
            "evidence": _row_evidence(rs.flagged_by, row),
        })

    # Filters
    if band:
        hit_rows = [r for r in hit_rows if r["band_css"] == band]
    if test_id:
        hit_rows = [r for r in hit_rows if any(f["test_id"] == test_id for f in r["flagged_by"])]
    if vendor:
        v = vendor.lower()
        hit_rows = [r for r in hit_rows if v in (r["vendor_name"] or "").lower() or v in (r["vendor_number"] or "").lower()]
    if min_amount:
        hit_rows = [r for r in hit_rows if r["amount"] >= min_amount]
    if max_amount:
        hit_rows = [r for r in hit_rows if r["amount"] <= max_amount]
    if overlap_only:
        hit_rows = [r for r in hit_rows if r["overlap_count"] >= 2]
    if red_flag_only:
        hit_rows = [r for r in hit_rows if any(f.get("red_flag") for f in r["flagged_by"])]
    if status:
        hit_rows = [r for r in hit_rows if r["review_status"] == status]

    total_filtered = len(hit_rows)
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    page_rows = hit_rows[(page - 1) * per_page: page * per_page]

    # Dashboard stats
    all_scored = compute_risk_scores(rows, sess["results"])
    confirmed = sum(1 for r in all_scored if decisions.get(r.row_idx, {}).get("status") == "confirmed")
    fp = sum(1 for r in all_scored if decisions.get(r.row_idx, {}).get("status") == "false_positive")
    needs_info = sum(1 for r in all_scored if decisions.get(r.row_idx, {}).get("status") == "needs_info")
    reviewed = confirmed + fp + needs_info
    total_hit = len(all_scored)
    unreviewed_amt = sum(r.amount for r in all_scored if decisions.get(r.row_idx, {}).get("status", "pending") == "pending")

    # Unique test ids for filter panel
    test_ids = list({f["test_id"] for r in all_scored for f in r.flagged_by})

    return {
        "rows": page_rows,
        "page": page,
        "total_pages": total_pages,
        "total_filtered": total_filtered,
        "dashboard": {
            "total_hit_rows": total_hit,
            "critical": sum(1 for r in all_scored if r.band_css == "critical"),
            "high": sum(1 for r in all_scored if r.band_css == "high"),
            "medium": sum(1 for r in all_scored if r.band_css == "medium"),
            "low": sum(1 for r in all_scored if r.band_css == "low"),
            "confirmed": confirmed,
            "false_positive": fp,
            "needs_info": needs_info,
            "reviewed_pct": round(reviewed / total_hit * 100) if total_hit else 0,
            "unreviewed_amount": unreviewed_amt,
        },
        "test_ids": sorted(test_ids),
    }


@app.patch("/api/runs/{run_id}/review/{row_idx}")
async def save_review(run_id: str, row_idx: int, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    save_review_decision(run_id, row_idx, body.get("status", "pending"), body.get("note", ""))
    return {"ok": True}


@app.post("/api/runs/{run_id}/review/bulk")
async def save_review_bulk(run_id: str, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    row_indices = body.get("row_indices", [])
    status = body.get("status", "pending")
    note = body.get("note", "")
    if not isinstance(row_indices, list) or not row_indices:
        return JSONResponse({"error": "row_indices required"}, status_code=400)
    save_bulk_decisions(run_id, row_indices, status, note)
    return {"ok": True, "updated": len(row_indices)}


@app.get("/api/runs/{run_id}/clusters")
def clusters_data(run_id: str, group_by: str = "signature", sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test session found"}, status_code=404)
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    result = build_clusters(rows, session["results"], scored, decisions, group_by=group_by)
    result["emergent_clusters"] = detect_anomalies(rows, scored, decisions)

    # attach any cached AI summaries for the current signature
    cached = get_ai_cache(run_id, result["signature"])
    if cached:
        summaries = cached.get("cluster_summaries", {})
        for c in result["rule_clusters"] + result["emergent_clusters"]:
            c["ai_summary"] = summaries.get(c["cluster_id"])
        result["ai"] = {k: cached.get(k) for k in ("executive_summary", "priorities", "disclaimer")}
        result["ai_cached"] = True
    else:
        result["ai_cached"] = False
    return result


@app.post("/api/runs/{run_id}/ai/triage")
def ai_triage(run_id: str, group_by: str = "signature", sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test session found"}, status_code=404)
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    result = build_clusters(rows, session["results"], scored, decisions, group_by=group_by)
    emergent = detect_anomalies(rows, scored, decisions)
    signature = result["signature"]

    cached = get_ai_cache(run_id, signature)
    if cached:
        cached["cached"] = True
        return cached

    ai = analyse_clusters(result["rule_clusters"], emergent)
    if "error" not in ai:
        set_ai_cache(run_id, signature, ai)
    ai["cached"] = False
    return ai


@app.post("/api/runs/{run_id}/anomaly/suggest")
def anomaly_suggest(run_id: str, sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test session found"}, status_code=404)
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    emergent = detect_anomalies(rows, scored, decisions)
    if not emergent:
        return {"suggestions": [], "disclaimer": ""}
    return suggest_tests(emergent)


@app.post("/api/compare")
async def compare(
    file_new: UploadFile = File(...),
    run_id_old: str = Form(...),
    sess=Depends(require_auth),
):
    old_rows = get_rows(run_id_old)
    if not old_rows:
        return JSONResponse({"error": "Previous run not found"}, status_code=404)

    suffix = Path(file_new.filename or "upload.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file_new.file, tmp)
        temp_path = tmp.name

    ingest = ingest_csv(temp_path)
    result = compare_runs(old_rows, ingest.rows)
    result["new_file"] = file_new.filename
    result["old_run_id"] = run_id_old
    return result


# ── Saved test library ───────────────────────────────────────────────────────

@app.get("/api/tests/library")
def get_library(sess=Depends(require_auth)):
    return list_saved_tests(user_id=sess["user_id"])


@app.post("/api/tests/library")
async def add_to_library(request: Request, sess=Depends(require_auth)):
    body = await request.json()
    test_id = save_test_to_library(body, user_id=sess["user_id"])
    return {"id": test_id, "ok": True}


@app.delete("/api/tests/library/{test_id}")
def remove_from_library(test_id: int, sess=Depends(require_auth)):
    delete_saved_test(test_id, user_id=sess["user_id"])
    return {"ok": True}


# ── Vendor drill-down ────────────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/vendor/{vendor_number}")
def vendor_detail(run_id: str, vendor_number: str, sess=Depends(require_auth)):
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    vendor_rows = [
        r for r in rows
        if str(r.get("vendor_number") or "").strip().lower() == vendor_number.strip().lower()
        or str(r.get("vendor_name") or "").strip().lower() == vendor_number.strip().lower()
    ]

    sess = get_last_test_session(run_id)
    flagged_indices = set()
    if sess:
        for tr in sess["results"]:
            for idx in tr.get("all_hit_indices", []):
                flagged_indices.add(idx)

    total_amount = sum(
        float(str(r.get("amount_excl_lc") or 0).replace(",", ""))
        for r in vendor_rows
        if r.get("amount_excl_lc")
    )

    enriched = []
    for r in vendor_rows:
        idx = rows.index(r)
        enriched.append({**r, "row_idx": idx, "flagged": idx in flagged_indices})

    return {
        "vendor_number": vendor_number,
        "vendor_name": vendor_rows[0].get("vendor_name") if vendor_rows else vendor_number,
        "invoice_count": len(vendor_rows),
        "total_amount": round(total_amount, 2),
        "flagged_count": sum(1 for r in enriched if r["flagged"]),
        "invoices": enriched[:200],
    }


# ── Export confirmed findings ────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/export")
def export_findings(run_id: str, status: str = "confirmed", sess=Depends(require_auth)):
    sess = get_last_test_session(run_id)
    if not sess:
        return JSONResponse({"error": "No test session"}, status_code=404)

    rows = get_rows(run_id)
    scored = compute_risk_scores(rows, sess["results"])
    decisions = get_all_decisions(run_id)

    export_rows = []
    for rs in scored:
        dec = decisions.get(rs.row_idx, {"status": "pending", "note": ""})
        if status != "all" and dec["status"] != status:
            continue
        row = rows[rs.row_idx]
        export_rows.append({
            "Risk Score":        rs.risk_score,
            "Risk Band":         rs.band_label,
            "Review Status":     dec["status"],
            "Reviewer Note":     dec["note"],
            "Flagged By":        " | ".join(f["test_name"] for f in rs.flagged_by),
            "Tests Count":       rs.overlap_count,
            "Vendor Number":     row.get("vendor_number",""),
            "Vendor Name":       row.get("vendor_name",""),
            "Invoice No":        row.get("physical_invoice_no") or row.get("system_invoice_no",""),
            "Date Authorized":   row.get("date_authorized",""),
            "Date Captured":     row.get("date_captured",""),
            "Scheduled Pay":     row.get("scheduled_pay_date",""),
            "Amount (Excl.)":    row.get("amount_excl_lc",""),
            "Amount (Incl.)":    row.get("amount_incl_lc",""),
            "Currency":          row.get("reference_currency",""),
            "Description":       row.get("invoice_description",""),
            "Company Code":      row.get("company_code",""),
            "Captured By":       row.get("captured_by",""),
            "Authorized By":     row.get("authorized_by",""),
            "Payment Type":      row.get("payment_type",""),
            "Fiscal Year":       row.get("fiscal_year",""),
        })

    output = io.StringIO()
    if export_rows:
        writer = csv.DictWriter(output, fieldnames=list(export_rows[0].keys()))
        writer.writeheader()
        writer.writerows(export_rows)
    else:
        output.write("No findings match the selected status\n")

    output.seek(0)
    fname = f"dia_findings_{run_id}_{status}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/results")
def results_page():
    return FileResponse(static_dir / "results.html")


@app.get("/api/runs/{run_id}/tests/last")
def last_test_session(run_id: str, sess=Depends(require_auth)):
    sess = get_last_test_session(run_id)
    if not sess:
        return JSONResponse({"error": "No test sessions found"}, status_code=404)
    rows = get_rows(run_id)
    scored = compute_risk_scores(rows, sess["results"])
    sess["risk_summary"] = {
        "total_hit_rows": len(scored),
        "critical": sum(1 for r in scored if r.band_css == "critical"),
        "high": sum(1 for r in scored if r.band_css == "high"),
        "medium": sum(1 for r in scored if r.band_css == "medium"),
        "low": sum(1 for r in scored if r.band_css == "low"),
    }
    return sess
