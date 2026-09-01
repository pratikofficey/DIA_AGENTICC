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
                run_id      TEXT PRIMARY KEY,
                user_id     INTEGER DEFAULT 0,
                created_at  TEXT,
                filename    TEXT,
                total_rows  INTEGER,
                report_json TEXT,
                rows_json   TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS test_sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT,
                user_id      INTEGER DEFAULT 0,
                created_at   TEXT,
                tests_json   TEXT,
                results_json TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_tests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER DEFAULT 0,
                name        TEXT NOT NULL,
                prompt      TEXT,
                code        TEXT,
                description TEXT,
                explanation TEXT,
                severity    TEXT DEFAULT 'warning',
                weightage   INTEGER DEFAULT 7,
                red_flag    INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS review_decisions (
                run_id       TEXT,
                row_idx      INTEGER,
                user_id      INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'pending',
                note         TEXT DEFAULT '',
                updated_at   TEXT,
                PRIMARY KEY (run_id, row_idx)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                run_id       TEXT,
                signature    TEXT,
                payload_json TEXT,
                created_at   TEXT,
                PRIMARY KEY (run_id, signature)
            )
        """)
        c.commit()
        # migrate existing tables — add user_id if missing
        for tbl in ('runs', 'test_sessions', 'saved_tests', 'review_decisions'):
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER DEFAULT 0")
                c.commit()
            except Exception:
                pass


def get_review_decision(run_id: str, row_idx: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT status, note FROM review_decisions WHERE run_id=? AND row_idx=?",
            (run_id, row_idx)
        ).fetchone()
    return {"status": row["status"], "note": row["note"]} if row else {"status": "pending", "note": ""}


def save_review_decision(run_id: str, row_idx: int, status: str, note: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO review_decisions (run_id, row_idx, status, note, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(run_id, row_idx) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
        """, (run_id, row_idx, status, note, datetime.now(timezone.utc).isoformat()))
        c.commit()


def list_saved_tests(user_id: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM saved_tests WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def save_test_to_library(test: dict, user_id: int = 0) -> int:
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO saved_tests (user_id, name, prompt, code, description, explanation, severity, weightage, red_flag)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (user_id, test.get("test_name",""), test.get("prompt",""), test.get("code",""),
              test.get("description",""), test.get("explanation",""),
              test.get("severity","warning"), test.get("weightage",7),
              1 if test.get("red_flag") else 0))
        c.commit()
        return cur.lastrowid


def delete_saved_test(test_id: int, user_id: int = 0):
    with _conn() as c:
        c.execute("DELETE FROM saved_tests WHERE id=? AND user_id=?", (test_id, user_id))
        c.commit()


def save_bulk_decisions(run_id: str, row_indices: list[int], status: str, note: str = ""):
    ts = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany("""
            INSERT INTO review_decisions (run_id, row_idx, status, note, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(run_id, row_idx) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
        """, [(run_id, int(i), status, note, ts) for i in row_indices])
        c.commit()


def get_ai_cache(run_id: str, signature: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT payload_json FROM ai_cache WHERE run_id=? AND signature=?",
            (run_id, signature)
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def set_ai_cache(run_id: str, signature: str, payload: dict):
    with _conn() as c:
        c.execute("""
            INSERT INTO ai_cache (run_id, signature, payload_json, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(run_id, signature) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at
        """, (run_id, signature, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
        c.commit()


def get_all_decisions(run_id: str) -> dict[int, dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT row_idx, status, note FROM review_decisions WHERE run_id=?", (run_id,)
        ).fetchall()
    return {r["row_idx"]: {"status": r["status"], "note": r["note"]} for r in rows}


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def save_run(run_id: str, report: dict, rows: list[dict], filename: str, user_id: int = 0):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs (run_id, user_id, created_at, filename, total_rows, report_json, rows_json) VALUES (?,?,?,?,?,?,?)",
            (run_id, user_id, datetime.now(timezone.utc).isoformat(), filename, len(rows),
             json.dumps(report), json.dumps(rows)),
        )
        c.commit()


def get_rows(run_id: str) -> list[dict]:
    with _conn() as c:
        row = c.execute("SELECT rows_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["rows_json"]) if row else []


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT report_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["report_json"]) if row else None


def list_runs(limit: int = 20, user_id: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT run_id, created_at, filename, total_rows FROM runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def save_test_session(run_id: str, tests: list[dict], results: list[dict], user_id: int = 0):
    with _conn() as c:
        c.execute(
            "INSERT INTO test_sessions (run_id, user_id, created_at, tests_json, results_json) VALUES (?,?,?,?,?)",
            (run_id, user_id, datetime.now(timezone.utc).isoformat(), json.dumps(tests), json.dumps(results)),
        )
        c.commit()


def get_last_test_session(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT tests_json, results_json FROM test_sessions WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    return {"tests": json.loads(row["tests_json"]), "results": json.loads(row["results_json"])}
