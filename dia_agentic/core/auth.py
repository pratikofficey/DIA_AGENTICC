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
#
# /tmp is the writable temporary filesystem available to the
# serverless function.
#
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

    # This directory is writable in both environments:
    #
    # Local  -> dia_agentic/data
    # Vercel -> /tmp
    #
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    c = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
        check_same_thread=False,
    )

    c.row_factory = sqlite3.Row

    # Wait up to 30 seconds if another request is using SQLite.
    c.execute("PRAGMA busy_timeout = 30000")

    # WAL helps when multiple requests access SQLite.
    #
    # If the environment does not support it, don't let this
    # prevent the application from starting.
    try:
        c.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass

    return c


# ============================================================
# PASSWORD HASHING
# ============================================================

def _hash(password: str) -> str:
    """
    Hash a password using SHA-256.

    Kept compatible with the existing POC implementation.
    """

    return hashlib.sha256(password.encode()).hexdigest()


# ============================================================
# AUTH TABLE INITIALIZATION
# ============================================================

def init_auth_tables():
    """
    Create authentication tables if they do not already exist.

    Safe to call multiple times.
    """

    with _conn() as c:

        # ----------------------------------------------------
        # USERS
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # SESSIONS
        # ----------------------------------------------------

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
# DEFAULT USER
# ============================================================

def seed_default_user():
    """
    Create the default user if no user with the configured
    username exists.

    Environment variables:

        DIA_DEFAULT_USER
        DIA_DEFAULT_PASSWORD
        DIA_DEFAULT_DISPLAY

    Defaults:

        admin
        admin123
        Admin
    """

    username = os.environ.get(
        "DIA_DEFAULT_USER",
        "admin",
    ).strip()

    password = os.environ.get(
        "DIA_DEFAULT_PASSWORD",
        "admin123",
    ).strip()

    display = os.environ.get(
        "DIA_DEFAULT_DISPLAY",
        "Admin",
    ).strip()

    with _conn() as c:

        exists = c.execute(
            """
            SELECT id
            FROM users
            WHERE username=?
            """,
            (username,),
        ).fetchone()

        if not exists:

            c.execute(
                """
                INSERT INTO users
                    (
                        username,
                        password_hash,
                        display_name
                    )
                VALUES (?, ?, ?)
                """,
                (
                    username,
                    _hash(password),
                    display,
                ),
            )

            c.commit()

            print(
                f"[DIA Auth] Default user created: {username}"
            )


# ============================================================
# LOGIN
# ============================================================

def login(
    username: str,
    password: str,
) -> dict | None:
    """
    Authenticate a user and create a session token.

    Returns:
        Authentication dictionary on success.
        None on invalid credentials.
    """

    with _conn() as c:

        user = c.execute(
            """
            SELECT
                id,
                username,
                display_name
            FROM users
            WHERE username=?
              AND password_hash=?
            """,
            (
                username.strip(),
                _hash(password),
            ),
        ).fetchone()

        if not user:
            return None

        # Generate secure random session token.
        token = secrets.token_hex(32)

        now = datetime.now(timezone.utc)

        exp = now + timedelta(
            hours=TOKEN_TTL_HOURS
        )

        c.execute(
            """
            INSERT INTO sessions
                (
                    token,
                    user_id,
                    username,
                    created_at,
                    expires_at
                )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                token,
                user["id"],
                user["username"],
                now.isoformat(),
                exp.isoformat(),
            ),
        )

        c.commit()

        return {
            "token": token,
            "user_id": user["id"],
            "username": user["username"],
            "display_name": (
                user["display_name"]
                or user["username"]
            ),
            "expires_at": exp.isoformat(),
        }


# ============================================================
# LOGOUT
# ============================================================

def logout(token: str):
    """
    Delete a session token.
    """

    if not token:
        return

    with _conn() as c:

        c.execute(
            """
            DELETE FROM sessions
            WHERE token=?
            """,
            (token,),
        )

        c.commit()


# ============================================================
# GET SESSION
# ============================================================

def get_session(
    token: str,
) -> dict | None:
    """
    Validate a session token.

    Returns:
        {
            "user_id": ...,
            "username": ...
        }

    or None if the session is invalid/expired.
    """

    if not token:
        return None

    with _conn() as c:

        row = c.execute(
            """
            SELECT
                user_id,
                username
            FROM sessions
            WHERE token=?
              AND expires_at > datetime('now')
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

def require_auth(
    x_auth_token: str = Header(default=""),
):
    """
    FastAPI dependency.

    Add this dependency to routes that require authentication.

    Example:

        @app.get("/protected")
        def protected(user=Depends(require_auth)):
            ...
    """

    if not x_auth_token:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
        )

    sess = get_session(x_auth_token)

    if not sess:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session",
        )

    return sess


# ============================================================
# CREATE USER
# ============================================================

def create_user(
    username: str,
    password: str,
    display_name: str = "",
) -> bool:
    """
    Create a new user.

    Returns:
        True  -> user created
        False -> username already exists
    """

    username = username.strip()

    display_name = (
        display_name.strip()
        if display_name
        else username
    )

    try:

        with _conn() as c:

            c.execute(
                """
                INSERT INTO users
                    (
                        username,
                        password_hash,
                        display_name
                    )
                VALUES (?, ?, ?)
                """,
                (
                    username,
                    _hash(password),
                    display_name,
                ),
            )

            c.commit()

        return True

    except sqlite3.IntegrityError:
        return False


# ============================================================
# LIST USERS
# ============================================================

def list_users() -> list[dict]:
    """
    Return all registered users.
    """

    with _conn() as c:

        rows = c.execute(
            """
            SELECT
                id,
                username,
                display_name,
                created_at
            FROM users
            ORDER BY id
            """
        ).fetchall()

    return [
        dict(row)
        for row in rows
    ]


# ============================================================
# CHANGE PASSWORD
# ============================================================

def change_password(
    user_id: int,
    new_password: str,
):
    """
    Change a user's password.

    All existing sessions for that user are invalidated.
    """

    with _conn() as c:

        c.execute(
            """
            UPDATE users
            SET password_hash=?
            WHERE id=?
            """,
            (
                _hash(new_password),
                user_id,
            ),
        )

        # Force the user to log in again after changing
        # their password.
        c.execute(
            """
            DELETE FROM sessions
            WHERE user_id=?
            """,
            (user_id,),
        )

        c.commit()


# ============================================================
# DATABASE PATH / HEALTH CHECK
# ============================================================

def get_db_path() -> str:
    """
    Return the active database path.

    Useful for debugging.
    """

    return str(DB_PATH)


def auth_database_health_check() -> bool:
    """
    Verify that the authentication database can be opened.
    """

    try:

        with _conn() as c:
            c.execute("SELECT 1").fetchone()

        return True

    except Exception:
        return False