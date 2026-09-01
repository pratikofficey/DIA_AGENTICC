"""Per-hit evidence extraction for built-in tests.

Given a test_id and a row, returns a list of evidence items that tell the
reviewer exactly *why* the row was flagged, so they don't have to work it out
themselves (e.g. which date is a weekend, which amount is round).

Each evidence item: {"field": <udm_field>, "value": <raw value>, "note": <label>}
"""
from datetime import datetime

DATE_FORMATS = ["%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d"]
SENTINELS = {"none", "null", "nan", "n/a", "", "01-01-1900", "1900-01-01"}

KEYWORDS = [
    "bonus", "gift", "personal", "cash", "loan", "advance",
    "entertainment", "alcohol", "drinks", "travel", "holiday",
    "vacation", "reward", "incentive", "tip", "gratuity",
    "donation", "contribution", "sponsorship", "refund",
    "reimbursement", "settlement", "legal", "penalty",
    "fine", "commission", "kickback", "rebate", "courtesy",
]

ROUND_THRESHOLDS = [1000000, 500000, 100000, 50000, 10000, 5000, 1000]


def parse_date(val):
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in SENTINELS:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def day_of_week(val):
    """Return short day name (Mon..Sun) or None."""
    d = parse_date(val)
    return d.strftime("%a") if d else None


def is_weekend(val):
    d = parse_date(val)
    return d.weekday() >= 5 if d else False


def parse_amount(val) -> float:
    try:
        return abs(float(str(val or 0).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0.0


def _date_note(val, prefix=""):
    dow = day_of_week(val)
    if not dow:
        return prefix.strip() or "date"
    weekend = is_weekend(val)
    tag = f"{dow}"
    if weekend:
        tag += " (weekend)"
    return f"{prefix}{tag}".strip()


def evidence_for(test_id: str, row: dict) -> list[dict]:
    ev: list[dict] = []

    if test_id == "round_dollar":
        raw = row.get("amount_excl_lc")
        amt = parse_amount(raw)
        div = next((t for t in ROUND_THRESHOLDS if amt and amt % t == 0), None)
        note = f"round \u00f7 {div:,}" if div else "round amount"
        ev.append({"field": "amount_excl_lc", "value": raw, "note": note})

    elif test_id == "duplicate_invoice":
        ev.append({"field": "vendor_number", "value": row.get("vendor_number"), "note": "same vendor"})
        ev.append({"field": "physical_invoice_no",
                   "value": row.get("physical_invoice_no") or row.get("system_invoice_no"),
                   "note": "same invoice no"})
        ev.append({"field": "amount_excl_lc", "value": row.get("amount_excl_lc"), "note": "same amount"})

    elif test_id == "keyword_suspicious":
        raw = row.get("invoice_description")
        desc = str(raw or "").lower()
        kw = next((k for k in KEYWORDS if k in desc), None)
        note = f"keyword: {kw}" if kw else "suspicious keyword"
        ev.append({"field": "invoice_description", "value": raw, "note": note})

    elif test_id == "same_day_payment":
        cap = row.get("date_captured")
        sch = row.get("scheduled_pay_date")
        ev.append({"field": "date_captured", "value": cap, "note": _date_note(cap, "captured ")})
        ev.append({"field": "scheduled_pay_date", "value": sch,
                   "note": _date_note(sch, "paid ") + " \u00b7 same day"})

    return [e for e in ev if e.get("value") not in (None, "", "None")]
