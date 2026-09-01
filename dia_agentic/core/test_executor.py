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
        '__import__': __import__,
        'None': None, 'True': True, 'False': False,
    }
}


from dia_agentic.core.evidence import evidence_for


def _parse_amount(val) -> float:
    try:
        return abs(float(str(val or 0).replace(',', '').strip()))
    except (ValueError, TypeError):
        return 0.0


def execute_test(test: dict, rows: list[dict]) -> dict:
    code = test.get("code", "")
    test_name = test.get("test_name", "Unknown Test")

    if not rows:
        return _empty_result(test, "No rows to test")

    try:
        namespace = dict(SAFE_BUILTINS)
        compile(code, "<test>", "exec")
        exec(code, namespace)

        if 'run_test' not in namespace:
            return _empty_result(test, "Generated code missing run_test function")

        hit_indices = namespace['run_test'](rows)

        if not isinstance(hit_indices, list):
            return _empty_result(test, "run_test must return a list")

        hit_indices = [i for i in hit_indices if isinstance(i, int) and 0 <= i < len(rows)]

    except Exception as e:
        return _empty_result(test, f"Execution error: {e}")

    total = len(rows)
    hits = len(hit_indices)
    hit_amount = sum(_parse_amount(rows[i].get('amount_excl_lc')) for i in hit_indices)

    test_id = test.get("id", test_name.lower().replace(" ", "_"))
    sample = []
    for i in sorted(set(hit_indices))[:200]:
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
            "evidence": evidence_for(test_id, r),
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
        "required_fields": test.get("required_fields", []),
    }


def _empty_result(test: dict, reason: str) -> dict:
    test_name = test.get("test_name", "Unknown Test")
    return {
        "test_id": test.get("id", test_name.lower().replace(" ", "_")),
        "test_name": test_name,
        "type": test.get("type", "preset"),
        "applicable": False,
        "not_applicable_reason": reason,
        "hit_count": 0, "hit_pct": 0, "hit_amount": 0,
        "sample_hits": [], "all_hit_indices": [],
        "severity": test.get("severity", "warning"),
        "red_flag": test.get("red_flag", False),
        "weightage": test.get("weightage", 7),
        "explanation": test.get("explanation", ""),
        "description": test.get("description", ""),
        "required_fields": test.get("required_fields", []),
    }
