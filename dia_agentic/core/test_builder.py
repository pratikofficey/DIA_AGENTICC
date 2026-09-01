import json
import os
import re
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
7. You may put imports (e.g. from collections import defaultdict) inside the function body
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


def _get_api_keys() -> list[str]:
    keys = []
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_2"):
        v = os.getenv(k, "").strip()
        if v:
            keys.append(v)
    return keys


def build_test(prompt: str, sample_rows: list[dict]) -> dict:
    api_keys = _get_api_keys()
    if not api_keys:
        return {"error": "GROQ_API_KEY not set in .env"}

    sample_str = json.dumps(sample_rows[:5], indent=2, default=str)
    system = SYSTEM_PROMPT.format(
        field_descriptions=UDM_FIELD_DESCRIPTIONS,
        sample_rows=sample_str,
    )

    for api_key in api_keys:
        client = Groq(api_key=api_key)
        user_message = f'Detect this pattern: "{prompt}"'

        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model="qwen/qwen3.6-27b",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.2,
                    max_tokens=3000,
                )
                raw = (resp.choices[0].message.content or "").strip()
                # strip Qwen think blocks
                raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
                # strip markdown fences
                if "```" in raw:
                    parts = raw.split("```")
                    for p in parts:
                        p = p.strip()
                        if p.startswith("json"):
                            p = p[4:]
                        if p.strip().startswith("{"):
                            raw = p.strip()
                            break
                # extract first JSON object even if surrounding text
                m = re.search(r"\{[\s\S]*\}", raw)
                raw = m.group(0) if m else raw

                result = json.loads(raw)

                for key in ("test_name", "description", "code", "explanation", "severity"):
                    if key not in result:
                        raise ValueError(f"Missing key: {key}")

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
                break  # try next key
            except SyntaxError as e:
                return {"error": f"Generated code has a syntax error: {e}. Try rephrasing your prompt."}
            except Exception as e:
                last_error = str(e)
                break

    if "401" in str(locals().get("last_error", "")) or "invalid_api_key" in str(locals().get("last_error", "")):
        return {"error": "API key is invalid or expired. Please update GROQ_API_KEY in your .env file at console.groq.com/keys"}
    return {"error": f"Test generation failed: {locals().get('last_error', 'unknown error')}"}
