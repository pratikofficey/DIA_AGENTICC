"""Agentic review layer: one LLM call produces an executive triage summary plus a
one-line plain-English summary for each cluster. Results are cached by signature.
"""
import json
import os
import re
import time

from groq import Groq

MODELS = ["qwen/qwen3.6-27b"]
MAX_RETRIES_ON_429 = 2
RETRY_WAIT_SECONDS = 15
MAX_CLUSTERS_SENT = 20  # keep payload tight to avoid empty responses

DISCLAIMER = "AI-generated. May be inaccurate \u2014 verify findings before acting."

SYSTEM_PROMPT = """You are a P2P fraud analytics lead helping an auditor triage findings.
You will receive a list of clusters. Each cluster is a group of flagged invoices that
share a characteristic (same vendor, same test-overlap, weekend dates, value band, etc.).

Your job:
1. Write a short executive summary (2-3 sentences) telling the auditor where to focus first.
2. Pick the top priority clusters (by risk / amount / anomaly) and give a one-line reason each.
3. Write a single plain-English sentence for EVERY cluster: what the pattern means and why it matters to an auditor.

Return ONLY valid JSON, no markdown:
{
  "executive_summary": "...",
  "priorities": [{"cluster_id": "...", "reason": "..."}],
  "cluster_summaries": {"<cluster_id>": "one sentence", ...}
}"""


def _get_keys():
    keys = []
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_2"):
        v = os.getenv(k, "").strip()
        if v:
            keys.append(v)
    return keys


def _compact(rule_clusters, emergent_clusters):
    out = []
    for c in rule_clusters[:MAX_CLUSTERS_SENT]:
        out.append({
            "cluster_id": c["cluster_id"],
            "source": "rule",
            "tests_fired": c.get("test_names", []),
            "group": c["title"],
            "size": c["size"],
            "amount_at_risk": c["amount_display"],
            "risk_band": c["dominant_band"],
            "red_flag": c.get("red_flag", False),
            "signals": c.get("evidence_chips", []),
        })
    for c in (emergent_clusters or [])[:MAX_CLUSTERS_SENT]:
        out.append({
            "cluster_id": c["cluster_id"],
            "source": "emergent",
            "anomaly": c["title"],
            "what_it_means": c.get("description", ""),
            "size": c["size"],
            "amount_at_risk": c["amount_display"],
            "risk_band": c.get("severity", "medium"),
            "signals": c.get("evidence_chips", []),
        })
    return out


def analyse_clusters(rule_clusters: list[dict], emergent_clusters: list[dict] | None = None) -> dict:
    keys = _get_keys()
    if not keys:
        return {"error": "GROQ_API_KEY not set in .env", "disclaimer": DISCLAIMER}
    if not rule_clusters and not emergent_clusters:
        return {"executive_summary": "No clusters to analyse.",
                "priorities": [], "cluster_summaries": {}, "disclaimer": DISCLAIMER}

    payload = json.dumps({"clusters": _compact(rule_clusters, emergent_clusters)}, default=str)
    last_error = ""

    for attempt in range(MAX_RETRIES_ON_429 + 1):
      for key in keys:
        for model in MODELS:
            client = Groq(api_key=key)
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"Analyse these clusters and return the JSON:\n{payload}"},
                    ],
                    temperature=0.2,
                    max_tokens=3000,
                )
                raw = (resp.choices[0].message.content or "").strip()

                if not raw:
                    last_error = f"Model {model} returned empty response"
                    continue

                # strip <think>...</think> blocks (Qwen reasoning models)
                raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

                if not raw:
                    last_error = f"Model {model} returned only a think block"
                    continue

                # strip markdown fences if present
                if "```" in raw:
                    parts = raw.split("```")
                    for p in parts:
                        p = p.strip()
                        if p.startswith("json"):
                            p = p[4:]
                        if p.strip().startswith("{"):
                            raw = p.strip()
                            break

                # extract first JSON object from response even with surrounding text
                m = re.search(r'\{[\s\S]*\}', raw)
                if not m:
                    last_error = f"No JSON object found in response from {model}"
                    continue
                raw = m.group(0)

                data = json.loads(raw)
                data.setdefault("executive_summary", "")
                data.setdefault("priorities", [])
                data.setdefault("cluster_summaries", {})
                data["disclaimer"] = DISCLAIMER
                return data

            except json.JSONDecodeError as e:
                last_error = f"Invalid JSON from {model}: {e}"
                continue
            except Exception as e:
                err = str(e)
                if "401" in err or "invalid_api_key" in err:
                    return {"error": "API key is invalid or expired. Update GROQ_API_KEY in .env.",
                            "disclaimer": DISCLAIMER}
                if any(x in err for x in ("model_not_found", "model_decommissioned", "404", "not supported")):
                    last_error = f"Model {model} unavailable"
                    continue
                if "429" in err or "rate_limit" in err:
                    last_error = f"Rate limit on {model}, attempt {attempt+1}"
                    break  # break model loop, sleep, then retry outer attempt
                last_error = err
                break
        else:
          continue  # key loop: go to next key
        break  # key loop: rate limit hit, break to retry outer attempt

      # if we got a rate limit on all keys/models, wait and retry
      if "Rate limit" in last_error and attempt < MAX_RETRIES_ON_429:
          time.sleep(RETRY_WAIT_SECONDS)
          continue
      break  # success or non-retryable error

    return {"error": f"AI triage failed: {last_error}", "disclaimer": DISCLAIMER}


def _chat_json(system_prompt: str, user_content: str) -> dict | None:
    """Single robust LLM call returning a parsed JSON object, or None on failure."""
    keys = _get_keys()
    if not keys:
        return None
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        rate_limited = False
        for key in keys:
            for model in MODELS:
                try:
                    resp = Groq(api_key=key).chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        temperature=0.2,
                        max_tokens=3000,
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
                    if not raw:
                        continue
                    m = re.search(r'\{[\s\S]*\}', raw)
                    if not m:
                        continue
                    return json.loads(m.group(0))
                except Exception as e:
                    if "429" in str(e) or "rate_limit" in str(e):
                        rate_limited = True
                    continue
        if rate_limited and attempt < MAX_RETRIES_ON_429:
            time.sleep(RETRY_WAIT_SECONDS)
            continue
        break
    return None


SUGGEST_PROMPT = """You are a P2P fraud analytics lead. You are given emergent data-driven
anomalies discovered by statistical profiling (not by any test the auditor wrote).
For each anomaly, propose a reusable test the auditor could generate.

Return ONLY valid JSON, no markdown:
{
  "suggestions": [
    {"detector_id": "...", "name": "short test name",
     "description": "one sentence on what it catches and why it matters",
     "prompt": "a clear plain-English instruction that a code generator can turn into a Python test using canonical UDM fields"}
  ]
}"""


def suggest_tests(emergent_clusters: list[dict]) -> dict:
    """Propose generatable tests from emergent anomalies. LLM-polished, with a
    deterministic fallback built from each detector's own seed prompt."""
    def _fallback():
        out = []
        for c in emergent_clusters:
            st = c.get("suggested_test")
            if st:
                out.append({
                    "detector_id": c.get("detector_id"),
                    "name": st.get("name", c["title"]),
                    "description": c.get("description", ""),
                    "prompt": st.get("prompt", ""),
                    "anomaly_title": c["title"],
                    "size": c["size"],
                })
        return {"suggestions": out, "disclaimer": DISCLAIMER, "source": "rules"}

    profile = [{
        "detector_id": c.get("detector_id"),
        "anomaly": c["title"],
        "means": c.get("description", ""),
        "size": c["size"],
        "amount_at_risk": c.get("amount_display"),
        "signals": c.get("evidence_chips", []),
        "seed_prompt": (c.get("suggested_test") or {}).get("prompt", ""),
    } for c in emergent_clusters]

    data = _chat_json(SUGGEST_PROMPT,
                      "Anomalies:\n" + json.dumps({"anomalies": profile}, default=str))
    if not data or "suggestions" not in data or not isinstance(data["suggestions"], list):
        return _fallback()

    size_by_id = {c.get("detector_id"): c["size"] for c in emergent_clusters}
    title_by_id = {c.get("detector_id"): c["title"] for c in emergent_clusters}
    for s in data["suggestions"]:
        did = s.get("detector_id")
        s["size"] = size_by_id.get(did, 0)
        s["anomaly_title"] = title_by_id.get(did, s.get("name", ""))
    data["disclaimer"] = DISCLAIMER
    data["source"] = "ai"
    return data
