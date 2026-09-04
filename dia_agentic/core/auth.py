"""
Simple token-based authentication.

POC-compatible authentication implementation.

Local development:
    dia_agentic/data/agentic_runs.db

Vercel:
    /tmp/agentic_runs.db

No external dependencies are required.
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import Header, HTTPException


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

# Vercel's deployed filesystem is read-only.
# /tmp is the writable temporary filesystem available to the
# serverless function.
# Locally we continue using the original database location.

if os.getenv("VERCEL"):
    DB_PATH = Path("/tmp/agentic_runs.db")
else:
    DB_PATH = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "agentic_runs.db"
    )


# Session lifetime
TOKEN_TTL_HOURS = 72


# ============================================================
# DATABASE CONNECTION
# ============================================================

def _conn():
    """
    Create a SQLite connection.

    Local:
        dia_agentic/data/agentic_runs.db

    Vercel:
        /tmp/agentic_runs.db
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    c = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
        check_same_thread=False,
    )

    c.row_factory = sqlite3.Row

    c.execute("PRAGMA busy_timeout = 30000")

    try:
        c.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass

    return c


# ============================================================
# PASSWORD HASHING
# ============================================================

def _hash(password: str) -> str:
    """Hash a password using SHA-256."""
    return hashlib.sha256(password.encode()).hexdigest()


# ============================================================
# AUTH TABLE INITIALIZATION
# ============================================================

def init_auth_tables():
    """Create authentication tables if they do not already exist."""
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                display_name    TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                created_at  TEXT,
                expires_at  TEXT
            )
            """
        )

        c.commit()


# ============================================================
# DEFAULT USER  (UPDATED – always forces password from env)
# ============================================================

def seed_default_user():
    """
    Always ensure the default user exists and has the password
    that is currently set in the environment variables.

    Environment variables:
        DIA_DEFAULT_USER
        DIA_DEFAULT_PASSWORD
        DIA_DEFAULT_DISPLAY

    Defaults (only if env vars are missing):
        admin
        Adi@1029
        Admin
    """
    username = os.environ.get("DIA_DEFAULT_USER", "admin").strip()
    password = os.environ.get("DIA_DEFAULT_PASSWORD", "Adi@1029").strip()
    display  = os.environ.get("DIA_DEFAULT_DISPLAY", "Admin").strip()

    password_hash = _hash(password)

    with _conn() as c:
        exists = c.execute(
            "SELECT id FROM users WHERE username=?",
            (username,),
        ).fetchone()

        if exists:
            # Force-update the password and display name
            c.execute(
                """
                UPDATE users
                SET password_hash = ?, display_name = ?
                WHERE username = ?
                """,
                (password_hash, display, username),
            )
            # Invalidate all existing sessions for this user
            c.execute(
                "DELETE FROM sessions WHERE user_id = ?",
                (exists["id"],),
            )
            print(f"[DIA Auth] Default user password updated: {username}")
        else:
            c.execute(
                """
                INSERT INTO users (username, password_hash, display_name)
                VALUES (?, ?, ?)
                """,
                (username, password_hash, display),
            )
            print(f"[DIA Auth] Default user created: {username}")

        c.commit()


# ============================================================
# LOGIN
# ============================================================

def login(username: str, password: str) -> dict | None:
    """Authenticate a user and create a session token."""
    with _conn() as c:
        user = c.execute(
            """
            SELECT id, username, display_name
            FROM users
            WHERE username=? AND password_hash=?
            """,
            (username.strip(), _hash(password)),
        ).fetchone()

        if not user:
            return None

        token = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=TOKEN_TTL_HOURS)

        c.execute(
            """
            INSERT INTO sessions (token, user_id, username, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token, user["id"], user["username"], now.isoformat(), exp.isoformat()),
        )
        c.commit()

        return {
            "token": token,
            "user_id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"] or user["username"],
            "expires_at": exp.isoformat(),
        }


# ============================================================
# LOGOUT
# ============================================================

def logout(token: str):
    """Delete a session token."""
    if not token:
        return
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
        c.commit()


# ============================================================
# GET SESSION
# ============================================================

def get_session(token: str) -> dict | None:
    """Validate a session token."""
    if not token:
        return None

    with _conn() as c:
        row = c.execute(
            """
            SELECT user_id, username
            FROM sessions
            WHERE token=? AND expires_at > datetime('now')
            """,
            (token,),
        ).fetchone()

    if not row:
        return None

    return {
        "user_id": row["user_id"],
        "username": row["username"],
    }


# ============================================================
# FASTAPI AUTH DEPENDENCY
# ============================================================

def require_auth(x_auth_token: str = Header(default="")):
    """FastAPI dependency that requires a valid session."""
    if not x_auth_token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    sess = get_session(x_auth_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return sess


# ============================================================
# CREATE USER
# ============================================================

def create_user(username: str, password: str, display_name: str = "") -> bool:
    """Create a new user. Returns True on success, False if username exists."""
    username = username.strip()
    display_name = display_name.strip() if display_name else username

    try:
        with _conn() as c:
            c.execute(
                """
                INSERT INTO users (username, password_hash, display_name)
                VALUES (?, ?, ?)
                """,
                (username, _hash(password), display_name),
            )
            c.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# ============================================================
# LIST USERS
# ============================================================

def list_users() -> list[dict]:
    """Return all registered users."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT id, username, display_name, created_at
            FROM users
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


# ============================================================
# CHANGE PASSWORD
# ============================================================

def change_password(user_id: int, new_password: str):
    """Change a user's password and invalidate all their sessions."""
    with _conn() as c:
        c.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (_hash(new_password), user_id),
        )
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        c.commit()


# ============================================================
# DATABASE PATH / HEALTH CHECK
# ============================================================

def get_db_path() -> str:
    return str(DB_PATH)


def auth_database_health_check() -> bool:
    try:
        with _conn() as c:
            c.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False