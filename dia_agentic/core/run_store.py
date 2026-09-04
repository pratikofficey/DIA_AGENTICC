import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# DATABASE CONFIGURATION
# ============================================================
#
# LOCAL DEVELOPMENT:
#   SQLite will be stored in:
#       dia_agentic/data/agentic_runs.db
#
# VERCEL:
#   The deployed application filesystem (/var/task) is
#   read-only.
#
#   Therefore SQLite is stored in:
#       /tmp/agentic_runs.db
#
# IMPORTANT:
#   /tmp is writable but NOT persistent across all serverless
#   instances/redeployments. This is suitable for a POC.
#   For production persistence, move this database to
#   PostgreSQL/Neon/Supabase/etc.
#
# ============================================================

IS_VERCEL = bool(os.getenv("VERCEL"))

if IS_VERCEL:
    # Vercel serverless writable temporary directory
    DB_PATH = Path("/tmp/agentic_runs.db")
else:
    # Keep the existing local development behavior
    DB_PATH = Path(__file__).resolve().parent.parent / "data" / "agentic_runs.db"


# ============================================================
# DATABASE CONNECTION
# ============================================================

def _conn():
    """
    Create a SQLite database connection.

    Vercel:
        Uses /tmp/agentic_runs.db

    Local:
        Uses dia_agentic/data/agentic_runs.db
    """

    # Create the parent directory if it does not exist.
    #
    # On Vercel this will be /tmp, which is writable.
    # Locally this will be dia_agentic/data.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    c = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
        check_same_thread=False,
    )

    c.row_factory = sqlite3.Row

    # Give SQLite up to 30 seconds to wait for a locked database.
    c.execute("PRAGMA busy_timeout = 30000")

    # WAL improves SQLite behavior when multiple requests
    # access the database around the same time.
    try:
        c.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        # Do not allow a PRAGMA failure to crash the application.
        pass

    return c


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():
    """
    Initialize all application tables.

    Safe to call multiple times.
    """

    # Make sure the writable directory exists.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _conn() as c:

        # ----------------------------------------------------
        # RUNS
        # ----------------------------------------------------
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                user_id     INTEGER DEFAULT 0,
                created_at  TEXT,
                filename    TEXT,
                total_rows  INTEGER,
                report_json TEXT,
                rows_json   TEXT
            )
            """
        )

        # ----------------------------------------------------
        # TEST SESSIONS
        # ----------------------------------------------------
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS test_sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT,
                user_id      INTEGER DEFAULT 0,
                created_at   TEXT,
                tests_json   TEXT,
                results_json TEXT
            )
            """
        )

        # ----------------------------------------------------
        # SAVED TESTS
        # ----------------------------------------------------
        c.execute(
            """
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
            """
        )

        # ----------------------------------------------------
        # REVIEW DECISIONS
        # ----------------------------------------------------
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS review_decisions (
                run_id       TEXT,
                row_idx      INTEGER,
                user_id      INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'pending',
                note         TEXT DEFAULT '',
                updated_at   TEXT,
                PRIMARY KEY (run_id, row_idx)
            )
            """
        )

        # ----------------------------------------------------
        # AI CACHE
        # ----------------------------------------------------
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_cache (
                run_id       TEXT,
                signature    TEXT,
                payload_json TEXT,
                created_at   TEXT,
                PRIMARY KEY (run_id, signature)
            )
            """
        )

        c.commit()

        # ----------------------------------------------------
        # DATABASE MIGRATION
        # ----------------------------------------------------
        #
        # Existing databases created by older versions of the
        # application may not contain user_id.
        #
        # Attempt to add it if necessary.
        #
        # If it already exists, SQLite raises an exception and
        # we safely ignore it.
        #
        migration_tables = (
            "runs",
            "test_sessions",
            "saved_tests",
            "review_decisions",
        )

        for table in migration_tables:
            try:
                c.execute(
                    f"ALTER TABLE {table} "
                    "ADD COLUMN user_id INTEGER DEFAULT 0"
                )
                c.commit()
            except sqlite3.OperationalError:
                # Column already exists.
                pass
            except sqlite3.DatabaseError:
                # Do not break application startup because of a
                # non-critical migration attempt.
                pass


# ============================================================
# REVIEW DECISIONS
# ============================================================

def get_review_decision(run_id: str, row_idx: int) -> dict:
    """
    Get the review decision for a specific row.
    """

    with _conn() as c:
        row = c.execute(
            """
            SELECT status, note
            FROM review_decisions
            WHERE run_id=? AND row_idx=?
            """,
            (run_id, row_idx),
        ).fetchone()

    if row:
        return {
            "status": row["status"],
            "note": row["note"],
        }

    return {
        "status": "pending",
        "note": "",
    }


def save_review_decision(
    run_id: str,
    row_idx: int,
    status: str,
    note: str,
):
    """
    Save or update a review decision.
    """

    with _conn() as c:
        c.execute(
            """
            INSERT INTO review_decisions
                (run_id, row_idx, status, note, updated_at)
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(run_id, row_idx)
            DO UPDATE SET
                status=excluded.status,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                row_idx,
                status,
                note,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        c.commit()


# ============================================================
# SAVED TEST LIBRARY
# ============================================================

def list_saved_tests(user_id: int = 0) -> list[dict]:
    """
    Return all saved tests for a user.
    """

    with _conn() as c:
        rows = c.execute(
            """
            SELECT *
            FROM saved_tests
            WHERE user_id=?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()

    return [dict(r) for r in rows]


def save_test_to_library(
    test: dict,
    user_id: int = 0,
) -> int:
    """
    Save a test into the test library.

    Returns:
        Newly created test ID.
    """

    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO saved_tests
                (
                    user_id,
                    name,
                    prompt,
                    code,
                    description,
                    explanation,
                    severity,
                    weightage,
                    red_flag
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                test.get("test_name", ""),
                test.get("prompt", ""),
                test.get("code", ""),
                test.get("description", ""),
                test.get("explanation", ""),
                test.get("severity", "warning"),
                test.get("weightage", 7),
                1 if test.get("red_flag") else 0,
            ),
        )

        c.commit()

        return cur.lastrowid


def delete_saved_test(
    test_id: int,
    user_id: int = 0,
):
    """
    Delete a saved test belonging to a specific user.
    """

    with _conn() as c:
        c.execute(
            """
            DELETE FROM saved_tests
            WHERE id=? AND user_id=?
            """,
            (test_id, user_id),
        )

        c.commit()


# ============================================================
# BULK REVIEW DECISIONS
# ============================================================

def save_bulk_decisions(
    run_id: str,
    row_indices: list[int],
    status: str,
    note: str = "",
):
    """
    Save the same review decision for multiple rows.
    """

    ts = datetime.now(timezone.utc).isoformat()

    with _conn() as c:
        c.executemany(
            """
            INSERT INTO review_decisions
                (
                    run_id,
                    row_idx,
                    status,
                    note,
                    updated_at
                )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(run_id, row_idx)
            DO UPDATE SET
                status=excluded.status,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            [
                (
                    run_id,
                    int(i),
                    status,
                    note,
                    ts,
                )
                for i in row_indices
            ],
        )

        c.commit()


# ============================================================
# AI CACHE
# ============================================================

def get_ai_cache(
    run_id: str,
    signature: str,
) -> dict | None:
    """
    Retrieve cached AI response for a run/signature pair.
    """

    with _conn() as c:
        row = c.execute(
            """
            SELECT payload_json
            FROM ai_cache
            WHERE run_id=? AND signature=?
            """,
            (run_id, signature),
        ).fetchone()

    if not row:
        return None

    try:
        return json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def set_ai_cache(
    run_id: str,
    signature: str,
    payload: dict,
):
    """
    Save or update an AI cache entry.
    """

    with _conn() as c:
        c.execute(
            """
            INSERT INTO ai_cache
                (
                    run_id,
                    signature,
                    payload_json,
                    created_at
                )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(run_id, signature)
            DO UPDATE SET
                payload_json=excluded.payload_json,
                created_at=excluded.created_at
            """,
            (
                run_id,
                signature,
                json.dumps(payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        c.commit()


# ============================================================
# REVIEW DECISION RETRIEVAL
# ============================================================

def get_all_decisions(
    run_id: str,
) -> dict[int, dict]:
    """
    Get all review decisions for a run.
    """

    with _conn() as c:
        rows = c.execute(
            """
            SELECT row_idx, status, note
            FROM review_decisions
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchall()

    return {
        r["row_idx"]: {
            "status": r["status"],
            "note": r["note"],
        }
        for r in rows
    }


# ============================================================
# RUN MANAGEMENT
# ============================================================

def new_run_id() -> str:
    """
    Generate a unique run ID.
    """

    return "run_" + uuid.uuid4().hex[:12]


def save_run(
    run_id: str,
    report: dict,
    rows: list[dict],
    filename: str,
    user_id: int = 0,
):
    """
    Save a complete analysis run.
    """

    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO runs
                (
                    run_id,
                    user_id,
                    created_at,
                    filename,
                    total_rows,
                    report_json,
                    rows_json
                )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                user_id,
                datetime.now(timezone.utc).isoformat(),
                filename,
                len(rows),
                json.dumps(report),
                json.dumps(rows),
            ),
        )

        c.commit()


def get_rows(
    run_id: str,
) -> list[dict]:
    """
    Get stored rows for a run.
    """

    with _conn() as c:
        row = c.execute(
            """
            SELECT rows_json
            FROM runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()

    if not row:
        return []

    try:
        return json.loads(row["rows_json"])
    except (TypeError, json.JSONDecodeError):
        return []


def get_run(
    run_id: str,
) -> dict | None:
    """
    Get the analysis report for a run.
    """

    with _conn() as c:
        row = c.execute(
            """
            SELECT report_json
            FROM runs
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()

    if not row:
        return None

    try:
        return json.loads(row["report_json"])
    except (TypeError, json.JSONDecodeError):
        return None


def list_runs(
    limit: int = 20,
    user_id: int = 0,
) -> list[dict]:
    """
    List recent runs for a user.
    """

    # Prevent invalid/negative LIMIT values.
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 20

    with _conn() as c:
        rows = c.execute(
            """
            SELECT
                run_id,
                created_at,
                filename,
                total_rows
            FROM runs
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    return [dict(r) for r in rows]


# ============================================================
# TEST SESSIONS
# ============================================================

def save_test_session(
    run_id: str,
    tests: list[dict],
    results: list[dict],
    user_id: int = 0,
):
    """
    Save a test session for a run.
    """

    with _conn() as c:
        c.execute(
            """
            INSERT INTO test_sessions
                (
                    run_id,
                    user_id,
                    created_at,
                    tests_json,
                    results_json
                )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                user_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(tests),
                json.dumps(results),
            ),
        )

        c.commit()


def get_last_test_session(
    run_id: str,
) -> dict | None:
    """
    Retrieve the latest test session for a run.
    """

    with _conn() as c:
        row = c.execute(
            """
            SELECT
                tests_json,
                results_json
            FROM test_sessions
            WHERE run_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    if not row:
        return None

    try:
        tests = json.loads(row["tests_json"])
    except (TypeError, json.JSONDecodeError):
        tests = []

    try:
        results = json.loads(row["results_json"])
    except (TypeError, json.JSONDecodeError):
        results = []

    return {
        "tests": tests,
        "results": results,
    }


# ============================================================
# OPTIONAL DEBUG / HEALTH FUNCTIONS
# ============================================================

def get_db_path() -> str:
    """
    Return the currently configured database path.

    Useful for debugging locally and on Vercel.
    """

    return str(DB_PATH)


def database_health_check() -> bool:
    """
    Check whether SQLite can be opened and queried.
    """

    try:
        with _conn() as c:
            c.execute("SELECT 1").fetchone()

        return True

    except Exception:
        return False