"""
Mapping registry with pluggable storage backends.

Production path:
    PostgreSQL + pgvector
    (set DATABASE_URL and/or MAPPING_DB_BACKEND=pgvector)

Fallback path:
    SQLite local registry.

Vercel:
    SQLite uses /tmp because the deployed filesystem is read-only.

Local development:
    SQLite uses dia_agentic/data/mapping_registry.db
"""

import json
import math
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None


# ============================================================
# CONFIGURATION
# ============================================================

VECTOR_DIM = 256


def _get_sqlite_db_path() -> Path:
    """
    Return the correct SQLite database location.

    Local development:
        dia_agentic/data/mapping_registry.db

    Vercel:
        /tmp/mapping_registry.db

    Vercel's /var/task filesystem is read-only, while /tmp
    is writable for the lifetime of the serverless instance.
    """

    if os.environ.get("VERCEL"):
        return Path("/tmp/mapping_registry.db")

    return (
        Path(__file__).resolve().parent.parent
        / "data"
        / "mapping_registry.db"
    )


DB_PATH = _get_sqlite_db_path()


# ============================================================
# TEXT / VECTOR HELPERS
# ============================================================

def _normalize(name: str) -> str:
    return name.strip().lower()


def _tokenize(text: str) -> list[str]:
    t = (
        _normalize(text)
        .replace("_", " ")
        .replace("-", " ")
        .replace("/", " ")
    )

    words = [w for w in t.split() if w]

    ngrams = []

    for w in words:
        for n in (2, 3):
            ngrams += [
                w[i:i + n]
                for i in range(len(w) - n + 1)
            ]

    return words + ngrams


def _embed_dense(
    text: str,
    dim: int = VECTOR_DIM,
) -> list[float]:

    tokens = _tokenize(text)

    vec = [0.0] * dim

    if not tokens:
        return vec

    for tok in tokens:
        idx = hash(tok) % dim
        vec[idx] += 1.0

    norm = math.sqrt(
        sum(v * v for v in vec)
    ) or 1.0

    return [
        v / norm
        for v in vec
    ]


def _cosine_dense(
    a: list[float],
    b: list[float],
) -> float:

    if not a or not b:
        return 0.0

    dot = sum(
        x * y
        for x, y in zip(a, b)
    )

    mag_a = math.sqrt(
        sum(x * x for x in a)
    ) or 1.0

    mag_b = math.sqrt(
        sum(y * y for y in b)
    ) or 1.0

    return dot / (mag_a * mag_b)


def _to_pgvector_literal(
    vec: list[float],
) -> str:

    return "[" + ",".join(
        f"{x:.8f}"
        for x in vec
    ) + "]"


# ============================================================
# PROTOCOL
# ============================================================

class RegistryProtocol(Protocol):

    backend: str

    def exact_lookup(
        self,
        source_column: str,
        erp_type: str | None = None,
        module: str = "invoices",
    ) -> dict | None:
        ...

    def vector_search(
        self,
        source_column: str,
        module: str = "invoices",
        threshold: float = 0.55,
    ) -> dict | None:
        ...

    def list_pending(
        self,
        module: str = "invoices",
    ) -> list[dict]:
        ...

    def list_all(
        self,
        module: str = "invoices",
    ) -> list[dict]:
        ...

    def upsert(
        self,
        source_column: str,
        udm_field: str,
        status: str = "inferred",
        confidence: float = 0.0,
        method: str = "",
        erp_type: str | None = None,
        module: str = "invoices",
        approved_by: str | None = None,
    ) -> int:
        ...

    def approve(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:
        ...

    def reject(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:
        ...

    def stats(
        self,
        module: str = "invoices",
    ) -> dict:
        ...


# ============================================================
# SQLITE REGISTRY
# ============================================================

class SQLiteMappingRegistry:

    backend = "sqlite"

    def __init__(
        self,
        db_path: Path | None = None,
    ):

        self.db_path = (
            Path(db_path)
            if db_path is not None
            else DB_PATH
        )

        # IMPORTANT:
        #
        # On Vercel this will be:
        #     /tmp
        #
        # and therefore writable.
        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30,
        )

        # Return rows as sqlite3.Row objects.
        self.conn.row_factory = sqlite3.Row

        # Helps when multiple requests access SQLite.
        try:
            self.conn.execute(
                "PRAGMA busy_timeout = 30000"
            )
        except Exception:
            pass

        # WAL is useful for concurrent reads/writes.
        try:
            self.conn.execute(
                "PRAGMA journal_mode = WAL"
            )
        except Exception:
            pass

        self._init_db()

    # --------------------------------------------------------
    # DATABASE INITIALIZATION
    # --------------------------------------------------------

    def _init_db(self):

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS column_mappings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source_column TEXT NOT NULL,
                source_norm   TEXT NOT NULL,
                erp_type      TEXT,
                module        TEXT DEFAULT 'invoices',
                udm_field     TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'inferred',
                confidence    REAL DEFAULT 0.0,
                method        TEXT,
                vector        TEXT,
                alternatives  TEXT DEFAULT '[]',
                approved_by   TEXT,
                approved_at   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(source_norm, erp_type, module)
            )
            """
        )

        # ----------------------------------------------------
        # Backward compatibility:
        # Add alternatives to databases created by an older
        # version of the application.
        # ----------------------------------------------------

        try:

            self.conn.execute(
                """
                ALTER TABLE column_mappings
                ADD COLUMN alternatives TEXT DEFAULT '[]'
                """
            )

            self.conn.commit()

        except Exception:
            pass

        self.conn.commit()

    # --------------------------------------------------------
    # EXACT LOOKUP
    # --------------------------------------------------------

    def exact_lookup(
        self,
        source_column: str,
        erp_type: str | None = None,
        module: str = "invoices",
    ) -> dict | None:

        norm = _normalize(source_column)

        erp = (
            (erp_type or "")
            .strip()
            .upper()
        )

        row = self.conn.execute(
            """
            SELECT
                source_column,
                udm_field,
                status,
                confidence,
                method
            FROM column_mappings
            WHERE source_norm = ?
              AND (
                    erp_type = ?
                    OR erp_type IS NULL
                    OR erp_type = ''
                  )
              AND module = ?
              AND status = 'confirmed'
            ORDER BY confidence DESC
            LIMIT 1
            """,
            (
                norm,
                erp,
                module,
            ),
        ).fetchone()

        if not row:
            return None

        return {
            "source": row["source_column"],
            "udm_field": row["udm_field"],
            "status": row["status"],
            "confidence": row["confidence"],
            "method": row["method"],
        }

    # --------------------------------------------------------
    # VECTOR SEARCH
    # --------------------------------------------------------

    def vector_search(
        self,
        source_column: str,
        module: str = "invoices",
        threshold: float = 0.55,
    ) -> dict | None:

        query = _embed_dense(source_column)

        rows = self.conn.execute(
            """
            SELECT
                source_column,
                udm_field,
                vector
            FROM column_mappings
            WHERE status = 'confirmed'
              AND module = ?
              AND vector IS NOT NULL
            """,
            (module,),
        ).fetchall()

        best = None
        best_score = 0.0

        for row in rows:

            try:

                score = _cosine_dense(
                    query,
                    json.loads(
                        row["vector"]
                    ),
                )

                if score > best_score:

                    best_score = score

                    best = {
                        "source": row["source_column"],
                        "udm_field": row["udm_field"],
                        "status": "confirmed",
                        "confidence": round(
                            score,
                            3,
                        ),
                        "method": (
                            f"vector({score:.0%})"
                        ),
                    }

            except Exception:
                continue

        return (
            best
            if best_score >= threshold
            else None
        )

    # --------------------------------------------------------
    # PENDING MAPPINGS
    # --------------------------------------------------------

    def list_pending(
        self,
        module: str = "invoices",
    ) -> list[dict]:

        rows = self.conn.execute(
            """
            SELECT
                id,
                source_column,
                erp_type,
                udm_field,
                confidence,
                method,
                created_at,
                alternatives
            FROM column_mappings
            WHERE status = 'inferred'
              AND module = ?
            ORDER BY created_at DESC
            """,
            (module,),
        ).fetchall()

        result = []

        for r in rows:

            try:

                alts = json.loads(
                    r["alternatives"] or "[]"
                )

            except Exception:

                alts = []

            # Ensure primary suggestion is always first.
            primary = {
                "udm_field": r["udm_field"],
                "confidence": r["confidence"],
                "reason": r["method"] or "",
            }

            if (
                not alts
                or alts[0].get("udm_field")
                != r["udm_field"]
            ):

                alts = [
                    primary
                ] + [
                    a
                    for a in alts
                    if a.get("udm_field")
                    != r["udm_field"]
                ]

            result.append(
                {
                    "id": r["id"],
                    "source_column": r[
                        "source_column"
                    ],
                    "erp_type": r["erp_type"],
                    "udm_field": r["udm_field"],
                    "confidence": r["confidence"],
                    "method": r["method"],
                    "created_at": r["created_at"],
                    "alternatives": alts,
                }
            )

        return result

    # --------------------------------------------------------
    # ALL MAPPINGS
    # --------------------------------------------------------

    def list_all(
        self,
        module: str = "invoices",
    ) -> list[dict]:

        rows = self.conn.execute(
            """
            SELECT
                id,
                source_column,
                erp_type,
                udm_field,
                status,
                confidence,
                method,
                approved_by,
                created_at
            FROM column_mappings
            WHERE module = ?
            ORDER BY
                CASE status
                    WHEN 'confirmed' THEN 0
                    WHEN 'inferred' THEN 1
                    ELSE 2
                END,
                created_at DESC
            """,
            (module,),
        ).fetchall()

        return [
            {
                "id": r["id"],
                "source_column": r[
                    "source_column"
                ],
                "erp_type": r["erp_type"],
                "udm_field": r["udm_field"],
                "status": r["status"],
                "confidence": r["confidence"],
                "method": r["method"],
                "approved_by": r["approved_by"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # --------------------------------------------------------
    # UPSERT
    # --------------------------------------------------------

    def upsert(
        self,
        source_column: str,
        udm_field: str,
        status: str = "inferred",
        confidence: float = 0.0,
        method: str = "",
        erp_type: str | None = None,
        module: str = "invoices",
        approved_by: str | None = None,
        alternatives: list | None = None,
    ) -> int:

        norm = _normalize(source_column)

        erp = (
            (erp_type or "")
            .strip()
            .upper()
        )

        vector_json = json.dumps(
            _embed_dense(source_column)
        )

        alternatives_json = json.dumps(
            alternatives or []
        )

        approved_at = (
            datetime.utcnow().isoformat()
            if approved_by
            else None
        )

        self.conn.execute(
            """
            INSERT INTO column_mappings
            (
                source_column,
                source_norm,
                erp_type,
                module,
                udm_field,
                status,
                confidence,
                method,
                vector,
                alternatives,
                approved_by,
                approved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(
                source_norm,
                erp_type,
                module
            )
            DO UPDATE SET
                udm_field = excluded.udm_field,
                status = excluded.status,
                confidence = excluded.confidence,
                method = excluded.method,
                vector = excluded.vector,
                alternatives = excluded.alternatives,
                approved_by = excluded.approved_by,
                approved_at = excluded.approved_at
            """,
            (
                source_column,
                norm,
                erp,
                module,
                udm_field,
                status,
                confidence,
                method,
                vector_json,
                alternatives_json,
                approved_by,
                approved_at,
            ),
        )

        self.conn.commit()

        row = self.conn.execute(
            """
            SELECT id
            FROM column_mappings
            WHERE source_norm = ?
              AND erp_type = ?
              AND module = ?
            """,
            (
                norm,
                erp,
                module,
            ),
        ).fetchone()

        return (
            row["id"]
            if row
            else -1
        )

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    def approve(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        cur = self.conn.execute(
            """
            UPDATE column_mappings
            SET
                status = 'confirmed',
                approved_by = ?,
                approved_at = datetime('now')
            WHERE id = ?
            """,
            (
                approved_by,
                mapping_id,
            ),
        )

        self.conn.commit()

        return cur.rowcount > 0

    # --------------------------------------------------------
    # REJECT
    # --------------------------------------------------------

    def reject(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        cur = self.conn.execute(
            """
            UPDATE column_mappings
            SET
                status = 'rejected',
                approved_by = ?,
                approved_at = datetime('now')
            WHERE id = ?
            """,
            (
                approved_by,
                mapping_id,
            ),
        )

        self.conn.commit()

        return cur.rowcount > 0

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    def stats(
        self,
        module: str = "invoices",
    ) -> dict:

        rows = self.conn.execute(
            """
            SELECT
                status,
                COUNT(*)
            FROM column_mappings
            WHERE module = ?
            GROUP BY status
            """,
            (module,),
        ).fetchall()

        return {
            r[0]: r[1]
            for r in rows
        }

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    def close(self):

        try:
            self.conn.close()
        except Exception:
            pass


# ============================================================
# POSTGRESQL + PGVECTOR REGISTRY
# ============================================================

class PgVectorMappingRegistry:

    backend = "pgvector"

    def __init__(
        self,
        database_url: str,
    ):

        if psycopg is None:
            raise RuntimeError(
                "psycopg is not installed"
            )

        self.conn = psycopg.connect(
            database_url,
            autocommit=True,
        )

        self._init_db()

    # --------------------------------------------------------
    # DATABASE INITIALIZATION
    # --------------------------------------------------------

    def _init_db(self):

        with self.conn.cursor() as cur:

            cur.execute(
                """
                CREATE EXTENSION IF NOT EXISTS vector
                """
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS column_mappings (
                    id BIGSERIAL PRIMARY KEY,
                    source_column TEXT NOT NULL,
                    source_norm TEXT NOT NULL,
                    erp_type TEXT,
                    module TEXT DEFAULT 'invoices',
                    udm_field TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'inferred',
                    confidence DOUBLE PRECISION DEFAULT 0.0,
                    method TEXT,
                    vector VECTOR({VECTOR_DIM}),
                    approved_by TEXT,
                    approved_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(
                        source_norm,
                        erp_type,
                        module
                    )
                )
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_cm_status_module
                ON column_mappings(status, module)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_cm_norm_module
                ON column_mappings(source_norm, module)
                """
            )

            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_cm_vector
                ON column_mappings
                USING ivfflat (
                    vector vector_cosine_ops
                )
                """
            )

    # --------------------------------------------------------
    # EXACT LOOKUP
    # --------------------------------------------------------

    def exact_lookup(
        self,
        source_column: str,
        erp_type: str | None = None,
        module: str = "invoices",
    ) -> dict | None:

        norm = _normalize(source_column)

        erp = (
            (erp_type or "")
            .strip()
            .upper()
        )

        with self.conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    source_column,
                    udm_field,
                    status,
                    confidence,
                    method
                FROM column_mappings
                WHERE source_norm = %s
                  AND (
                        erp_type = %s
                        OR erp_type IS NULL
                        OR erp_type = ''
                      )
                  AND module = %s
                  AND status = 'confirmed'
                ORDER BY confidence DESC
                LIMIT 1
                """,
                (
                    norm,
                    erp,
                    module,
                ),
            )

            row = cur.fetchone()

        if not row:
            return None

        return {
            "source": row[0],
            "udm_field": row[1],
            "status": row[2],
            "confidence": row[3],
            "method": row[4],
        }

    # --------------------------------------------------------
    # VECTOR SEARCH
    # --------------------------------------------------------

    def vector_search(
        self,
        source_column: str,
        module: str = "invoices",
        threshold: float = 0.55,
    ) -> dict | None:

        vec_literal = _to_pgvector_literal(
            _embed_dense(source_column)
        )

        with self.conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    source_column,
                    udm_field,
                    (
                        1 - (
                            vector <=> %s::vector
                        )
                    ) AS similarity
                FROM column_mappings
                WHERE status = 'confirmed'
                  AND module = %s
                  AND vector IS NOT NULL
                ORDER BY
                    vector <=> %s::vector
                LIMIT 1
                """,
                (
                    vec_literal,
                    module,
                    vec_literal,
                ),
            )

            row = cur.fetchone()

        if not row:
            return None

        similarity = float(
            row[2] or 0.0
        )

        if similarity < threshold:
            return None

        return {
            "source": row[0],
            "udm_field": row[1],
            "status": "confirmed",
            "confidence": round(
                similarity,
                3,
            ),
            "method": (
                f"vector({similarity:.0%})"
            ),
        }

    # --------------------------------------------------------
    # PENDING
    # --------------------------------------------------------

    def list_pending(
        self,
        module: str = "invoices",
    ) -> list[dict]:

        with self.conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    source_column,
                    erp_type,
                    udm_field,
                    confidence,
                    method,
                    created_at
                FROM column_mappings
                WHERE status = 'inferred'
                  AND module = %s
                ORDER BY created_at DESC
                """,
                (module,),
            )

            rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "source_column": r[1],
                "erp_type": r[2],
                "udm_field": r[3],
                "confidence": r[4],
                "method": r[5],
                "created_at": str(r[6]),
            }
            for r in rows
        ]

    # --------------------------------------------------------
    # ALL
    # --------------------------------------------------------

    def list_all(
        self,
        module: str = "invoices",
    ) -> list[dict]:

        with self.conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    source_column,
                    erp_type,
                    udm_field,
                    status,
                    confidence,
                    method,
                    approved_by,
                    created_at
                FROM column_mappings
                WHERE module = %s
                ORDER BY
                    CASE status
                        WHEN 'confirmed' THEN 0
                        WHEN 'inferred' THEN 1
                        ELSE 2
                    END,
                    created_at DESC
                """,
                (module,),
            )

            rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "source_column": r[1],
                "erp_type": r[2],
                "udm_field": r[3],
                "status": r[4],
                "confidence": r[5],
                "method": r[6],
                "approved_by": r[7],
                "created_at": str(r[8]),
            }
            for r in rows
        ]

    # --------------------------------------------------------
    # UPSERT
    # --------------------------------------------------------

    def upsert(
        self,
        source_column: str,
        udm_field: str,
        status: str = "inferred",
        confidence: float = 0.0,
        method: str = "",
        erp_type: str | None = None,
        module: str = "invoices",
        approved_by: str | None = None,
    ) -> int:

        norm = _normalize(source_column)

        erp = (
            (erp_type or "")
            .strip()
            .upper()
        )

        vec_literal = _to_pgvector_literal(
            _embed_dense(source_column)
        )

        with self.conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO column_mappings
                (
                    source_column,
                    source_norm,
                    erp_type,
                    module,
                    udm_field,
                    status,
                    confidence,
                    method,
                    vector,
                    approved_by,
                    approved_at
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::vector,
                    %s,
                    %s
                )

                ON CONFLICT(
                    source_norm,
                    erp_type,
                    module
                )
                DO UPDATE SET
                    udm_field = excluded.udm_field,
                    status = excluded.status,
                    confidence = excluded.confidence,
                    method = excluded.method,
                    vector = excluded.vector,
                    approved_by = excluded.approved_by,
                    approved_at = excluded.approved_at

                RETURNING id
                """,
                (
                    source_column,
                    norm,
                    erp,
                    module,
                    udm_field,
                    status,
                    confidence,
                    method,
                    vec_literal,
                    approved_by,
                    (
                        datetime.utcnow()
                        if approved_by
                        else None
                    ),
                ),
            )

            row = cur.fetchone()

        return (
            int(row[0])
            if row
            else -1
        )

    # --------------------------------------------------------
    # APPROVE
    # --------------------------------------------------------

    def approve(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        with self.conn.cursor() as cur:

            cur.execute(
                """
                UPDATE column_mappings
                SET
                    status = 'confirmed',
                    approved_by = %s,
                    approved_at = NOW()
                WHERE id = %s
                """,
                (
                    approved_by,
                    mapping_id,
                ),
            )

            return cur.rowcount > 0

    # --------------------------------------------------------
    # REJECT
    # --------------------------------------------------------

    def reject(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        with self.conn.cursor() as cur:

            cur.execute(
                """
                UPDATE column_mappings
                SET
                    status = 'rejected',
                    approved_by = %s,
                    approved_at = NOW()
                WHERE id = %s
                """,
                (
                    approved_by,
                    mapping_id,
                ),
            )

            return cur.rowcount > 0

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    def stats(
        self,
        module: str = "invoices",
    ) -> dict:

        with self.conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    status,
                    COUNT(*)
                FROM column_mappings
                WHERE module = %s
                GROUP BY status
                """,
                (module,),
            )

            rows = cur.fetchall()

        return {
            r[0]: r[1]
            for r in rows
        }

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    def close(self):

        try:
            self.conn.close()
        except Exception:
            pass


# ============================================================
# REGISTRY SINGLETON
# ============================================================

_registry: RegistryProtocol | None = None


def get_registry() -> RegistryProtocol:

    global _registry

    if _registry is not None:
        return _registry

    backend = (
        os.environ.get(
            "MAPPING_DB_BACKEND"
        )
        or ""
    ).strip().lower()

    database_url = (
        os.environ.get(
            "DATABASE_URL"
        )
        or ""
    ).strip()

    prefer_pg = (
        backend == "pgvector"
        or bool(database_url)
    )

    # --------------------------------------------------------
    # PostgreSQL / pgvector
    # --------------------------------------------------------

    if prefer_pg and database_url:

        try:

            _registry = PgVectorMappingRegistry(
                database_url
            )

            return _registry

        except Exception as exc:

            # Do not crash the entire POC if PostgreSQL/
            # pgvector is unavailable.
            print(
                "[MappingRegistry] "
                "PostgreSQL backend unavailable; "
                "falling back to SQLite: "
                f"{exc}"
            )

    # --------------------------------------------------------
    # SQLite fallback
    # --------------------------------------------------------

    _registry = SQLiteMappingRegistry()

    return _registry