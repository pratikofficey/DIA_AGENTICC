import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

try:
    import psycopg
except Exception:
    psycopg = None


VECTOR_DIM = 256


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_erp(value: str | None) -> str:
    return (value or "").strip().upper()


def _tokenize(text: str) -> list[str]:
    text = (
        _normalize(text)
        .replace("_", " ")
        .replace("-", " ")
        .replace("/", " ")
    )

    words = [word for word in text.split() if word]

    ngrams = []

    for word in words:
        for size in (2, 3):
            if len(word) >= size:
                ngrams.extend(
                    word[i:i + size]
                    for i in range(len(word) - size + 1)
                )

    return words + ngrams


def _stable_bucket(
    token: str,
    dim: int,
) -> int:

    digest = hashlib.blake2b(
        token.encode("utf-8"),
        digest_size=8,
        person=b"DIA-MAP",
    ).digest()

    return int.from_bytes(
        digest,
        "big",
    ) % dim


def _embed_dense(
    text: str,
    dim: int = VECTOR_DIM,
) -> list[float]:

    tokens = _tokenize(text)

    vector = [0.0] * dim

    if not tokens:
        return vector

    for token in tokens:
        vector[
            _stable_bucket(
                token,
                dim,
            )
        ] += 1.0

    magnitude = math.sqrt(
        sum(
            value * value
            for value in vector
        )
    ) or 1.0

    return [
        value / magnitude
        for value in vector
    ]


def _cosine_dense(
    a: list[float],
    b: list[float],
) -> float:

    if not a or not b:
        return 0.0

    size = min(
        len(a),
        len(b),
    )

    if not size:
        return 0.0

    left = a[:size]
    right = b[:size]

    dot = sum(
        x * y
        for x, y in zip(
            left,
            right,
        )
    )

    mag_a = math.sqrt(
        sum(
            x * x
            for x in left
        )
    ) or 1.0

    mag_b = math.sqrt(
        sum(
            y * y
            for y in right
        )
    ) or 1.0

    return dot / (
        mag_a * mag_b
    )


def _to_pgvector_literal(
    vector: list[float],
) -> str:

    return (
        "["
        + ",".join(
            f"{value:.8f}"
            for value in vector
        )
        + "]"
    )


def _sqlite_db_path() -> Path:

    configured = (
        os.environ.get(
            "MAPPING_SQLITE_PATH"
        )
        or ""
    ).strip()

    if configured:
        return Path(
            configured
        ).expanduser()

    if os.environ.get("VERCEL"):
        return Path(
            "/tmp/mapping_registry.db"
        )

    return (
        Path(__file__)
        .resolve()
        .parent
        .parent
        / "data"
        / "mapping_registry.db"
    )


DB_PATH = _sqlite_db_path()


def _normalize_alternatives(
    alternatives: list | None,
    udm_field: str,
    confidence: float,
    method: str,
) -> list[dict]:

    result = []

    seen = set()

    primary = {
        "udm_field": udm_field,
        "confidence": confidence,
        "reason": method or "",
    }

    seen.add(
        str(udm_field)
    )

    result.append(
        primary
    )

    for item in alternatives or []:

        if not isinstance(
            item,
            dict,
        ):
            continue

        field = str(
            item.get(
                "udm_field"
            )
            or ""
        ).strip()

        if not field:
            continue

        if field in seen:
            continue

        seen.add(
            field
        )

        result.append(
            {
                "udm_field": field,
                "confidence": item.get(
                    "confidence",
                    0.0,
                ),
                "reason": item.get(
                    "reason",
                    "",
                ),
            }
        )

    return result


class RegistryProtocol(
    Protocol
):

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
        alternatives: list | None = None,
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

    def close(
        self,
    ) -> None:
        ...


class SQLiteMappingRegistry:

    backend = "sqlite"

    def __init__(
        self,
        db_path: Path | str | None = None,
    ):

        self.db_path = (
            Path(db_path)
            if db_path is not None
            else DB_PATH
        )

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.conn = sqlite3.connect(
            str(
                self.db_path
            ),
            timeout=30,
            check_same_thread=False,
        )

        self.conn.row_factory = (
            sqlite3.Row
        )

        self.conn.execute(
            "PRAGMA busy_timeout = 30000"
        )

        try:
            self.conn.execute(
                "PRAGMA journal_mode = WAL"
            )
        except sqlite3.DatabaseError:
            pass

        try:
            self.conn.execute(
                "PRAGMA synchronous = NORMAL"
            )
        except sqlite3.DatabaseError:
            pass

        self._init_db()

    def _init_db(
        self,
    ) -> None:

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS column_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_column TEXT NOT NULL,
                source_norm TEXT NOT NULL,
                erp_type TEXT NOT NULL DEFAULT '',
                module TEXT NOT NULL DEFAULT 'invoices',
                udm_field TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'inferred',
                confidence REAL NOT NULL DEFAULT 0.0,
                method TEXT NOT NULL DEFAULT '',
                vector TEXT,
                alternatives TEXT NOT NULL DEFAULT '[]',
                approved_by TEXT,
                approved_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(
                    source_norm,
                    erp_type,
                    module
                )
            )
            """
        )

        columns = {
            row["name"]
            for row
            in self.conn.execute(
                """
                PRAGMA table_info(
                    column_mappings
                )
                """
            ).fetchall()
        }

        migrations = {
            "alternatives":
                """
                ALTER TABLE column_mappings
                ADD COLUMN alternatives
                TEXT NOT NULL
                DEFAULT '[]'
                """,

            "approved_by":
                """
                ALTER TABLE column_mappings
                ADD COLUMN approved_by TEXT
                """,

            "approved_at":
                """
                ALTER TABLE column_mappings
                ADD COLUMN approved_at TEXT
                """,

            "vector":
                """
                ALTER TABLE column_mappings
                ADD COLUMN vector TEXT
                """,
        }

        for (
            column,
            statement,
        ) in migrations.items():

            if column not in columns:
                self.conn.execute(
                    statement
                )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_cm_status_module
            ON column_mappings(
                status,
                module
            )
            """
        )

        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS
            idx_cm_norm_module
            ON column_mappings(
                source_norm,
                module
            )
            """
        )

        self.conn.commit()

    def exact_lookup(
        self,
        source_column: str,
        erp_type: str | None = None,
        module: str = "invoices",
    ) -> dict | None:

        norm = _normalize(
            source_column
        )

        erp = _normalize_erp(
            erp_type
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
                    OR erp_type = ''
                    OR erp_type IS NULL
                  )
              AND module = ?
              AND status = 'confirmed'
            ORDER BY
                CASE
                    WHEN erp_type = ?
                    THEN 0
                    ELSE 1
                END,
                confidence DESC
            LIMIT 1
            """,
            (
                norm,
                erp,
                module,
                erp,
            ),
        ).fetchone()

        if not row:
            return None

        return {
            "source":
                row[
                    "source_column"
                ],

            "udm_field":
                row[
                    "udm_field"
                ],

            "status":
                row[
                    "status"
                ],

            "confidence":
                float(
                    row[
                        "confidence"
                    ]
                    or 0.0
                ),

            "method":
                row[
                    "method"
                ]
                or "",
        }

    def vector_search(
        self,
        source_column: str,
        module: str = "invoices",
        threshold: float = 0.55,
    ) -> dict | None:

        query = _embed_dense(
            source_column
        )

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
            (
                module,
            ),
        ).fetchall()

        best = None

        best_score = 0.0

        for row in rows:

            try:
                stored = json.loads(
                    row[
                        "vector"
                    ]
                )

                score = _cosine_dense(
                    query,
                    stored,
                )

            except Exception:
                continue

            if score > best_score:

                best_score = score

                best = {
                    "source":
                        row[
                            "source_column"
                        ],

                    "udm_field":
                        row[
                            "udm_field"
                        ],

                    "status":
                        "confirmed",

                    "confidence":
                        round(
                            score,
                            3,
                        ),

                    "method":
                        f"vector({score:.0%})",
                }

        if best is None:
            return None

        if (
            best_score
            < float(
                threshold
            )
        ):
            return None

        return best

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
            ORDER BY
                created_at DESC,
                id DESC
            """,
            (
                module,
            ),
        ).fetchall()

        result = []

        for row in rows:

            try:
                alternatives = json.loads(
                    row[
                        "alternatives"
                    ]
                    or "[]"
                )

            except Exception:
                alternatives = []

            alternatives = (
                _normalize_alternatives(
                    alternatives,
                    row[
                        "udm_field"
                    ],
                    float(
                        row[
                            "confidence"
                        ]
                        or 0.0
                    ),
                    row[
                        "method"
                    ]
                    or "",
                )
            )

            result.append(
                {
                    "id":
                        row[
                            "id"
                        ],

                    "source_column":
                        row[
                            "source_column"
                        ],

                    "erp_type":
                        row[
                            "erp_type"
                        ]
                        or "",

                    "udm_field":
                        row[
                            "udm_field"
                        ],

                    "confidence":
                        float(
                            row[
                                "confidence"
                            ]
                            or 0.0
                        ),

                    "method":
                        row[
                            "method"
                        ]
                        or "",

                    "created_at":
                        row[
                            "created_at"
                        ],

                    "alternatives":
                        alternatives,
                }
            )

        return result

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
                    WHEN 'rejected' THEN 2
                    ELSE 3
                END,
                created_at DESC,
                id DESC
            """,
            (
                module,
            ),
        ).fetchall()

        return [
            {
                "id":
                    row[
                        "id"
                    ],

                "source_column":
                    row[
                        "source_column"
                    ],

                "erp_type":
                    row[
                        "erp_type"
                    ]
                    or "",

                "udm_field":
                    row[
                        "udm_field"
                    ],

                "status":
                    row[
                        "status"
                    ],

                "confidence":
                    float(
                        row[
                            "confidence"
                        ]
                        or 0.0
                    ),

                "method":
                    row[
                        "method"
                    ]
                    or "",

                "approved_by":
                    row[
                        "approved_by"
                    ],

                "created_at":
                    row[
                        "created_at"
                    ],
            }

            for row in rows
        ]

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

        source_column = (
            source_column
            or ""
        ).strip()

        udm_field = (
            udm_field
            or ""
        ).strip()

        status = (
            status
            or "inferred"
        ).strip().lower()

        method = (
            method
            or ""
        ).strip()

        module = (
            module
            or "invoices"
        ).strip()

        if not source_column:
            raise ValueError(
                "source_column is required"
            )

        if not udm_field:
            raise ValueError(
                "udm_field is required"
            )

        norm = _normalize(
            source_column
        )

        erp = _normalize_erp(
            erp_type
        )

        confidence_value = float(
            confidence
            or 0.0
        )

        vector_json = json.dumps(
            _embed_dense(
                source_column
            ),
            separators=(
                ",",
                ":",
            ),
        )

        alternatives_json = json.dumps(
            _normalize_alternatives(
                alternatives,
                udm_field,
                confidence_value,
                method,
            ),
            separators=(
                ",",
                ":",
            ),
        )

        approved_at = (
            datetime
            .now(
                timezone.utc
            )
            .isoformat()
            if approved_by
            else None
        )

        self.conn.execute(
            """
            INSERT INTO column_mappings (
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
            VALUES (
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?,
                ?
            )
            ON CONFLICT(
                source_norm,
                erp_type,
                module
            )
            DO UPDATE SET
                source_column =
                    excluded.source_column,
                udm_field =
                    excluded.udm_field,
                status =
                    excluded.status,
                confidence =
                    excluded.confidence,
                method =
                    excluded.method,
                vector =
                    excluded.vector,
                alternatives =
                    excluded.alternatives,
                approved_by =
                    excluded.approved_by,
                approved_at =
                    excluded.approved_at
            """,
            (
                source_column,
                norm,
                erp,
                module,
                udm_field,
                status,
                confidence_value,
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

        if not row:
            return -1

        return int(
            row[
                "id"
            ]
        )

    def approve(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        cursor = self.conn.execute(
            """
            UPDATE column_mappings
            SET
                status = 'confirmed',
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
            """,
            (
                approved_by,
                datetime
                .now(
                    timezone.utc
                )
                .isoformat(),
                int(
                    mapping_id
                ),
            ),
        )

        self.conn.commit()

        return (
            cursor.rowcount
            > 0
        )

    def reject(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        cursor = self.conn.execute(
            """
            UPDATE column_mappings
            SET
                status = 'rejected',
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
            """,
            (
                approved_by,
                datetime
                .now(
                    timezone.utc
                )
                .isoformat(),
                int(
                    mapping_id
                ),
            ),
        )

        self.conn.commit()

        return (
            cursor.rowcount
            > 0
        )

    def stats(
        self,
        module: str = "invoices",
    ) -> dict:

        rows = self.conn.execute(
            """
            SELECT
                status,
                COUNT(*) AS count
            FROM column_mappings
            WHERE module = ?
            GROUP BY status
            """,
            (
                module,
            ),
        ).fetchall()

        return {
            row[
                "status"
            ]:
                int(
                    row[
                        "count"
                    ]
                )

            for row in rows
        }

    def close(
        self,
    ) -> None:

        try:
            self.conn.close()
        except Exception:
            pass


class PgVectorMappingRegistry:

    backend = "pgvector"

    def __init__(
        self,
        database_url: str,
    ):

        if psycopg is None:
            raise RuntimeError(
                "psycopg is required for the pgvector mapping backend"
            )

        self.database_url = (
            database_url
        )

        self.conn = psycopg.connect(
            database_url,
            autocommit=True,
            connect_timeout=int(
                os.environ.get(
                    "MAPPING_DB_CONNECT_TIMEOUT",
                    "5",
                )
            ),
        )

        self._init_db()

    def _init_db(
        self,
    ) -> None:

        with self.conn.cursor() as cursor:

            cursor.execute(
                """
                CREATE EXTENSION
                IF NOT EXISTS vector
                """
            )

            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS column_mappings (
                    id BIGSERIAL PRIMARY KEY,
                    source_column TEXT NOT NULL,
                    source_norm TEXT NOT NULL,
                    erp_type TEXT NOT NULL DEFAULT '',
                    module TEXT NOT NULL DEFAULT 'invoices',
                    udm_field TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'inferred',
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    method TEXT NOT NULL DEFAULT '',
                    vector VECTOR({VECTOR_DIM}),
                    alternatives JSONB NOT NULL DEFAULT '[]'::jsonb,
                    approved_by TEXT,
                    approved_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(
                        source_norm,
                        erp_type,
                        module
                    )
                )
                """
            )

            cursor.execute(
                """
                ALTER TABLE column_mappings
                ADD COLUMN IF NOT EXISTS
                alternatives JSONB
                NOT NULL
                DEFAULT '[]'::jsonb
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_cm_status_module
                ON column_mappings(
                    status,
                    module
                )
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_cm_norm_module
                ON column_mappings(
                    source_norm,
                    module
                )
                """
            )

    def exact_lookup(
        self,
        source_column: str,
        erp_type: str | None = None,
        module: str = "invoices",
    ) -> dict | None:

        norm = _normalize(
            source_column
        )

        erp = _normalize_erp(
            erp_type
        )

        with self.conn.cursor() as cursor:

            cursor.execute(
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
                        OR erp_type = ''
                      )
                  AND module = %s
                  AND status = 'confirmed'
                ORDER BY
                    CASE
                        WHEN erp_type = %s
                        THEN 0
                        ELSE 1
                    END,
                    confidence DESC
                LIMIT 1
                """,
                (
                    norm,
                    erp,
                    module,
                    erp,
                ),
            )

            row = cursor.fetchone()

        if not row:
            return None

        return {
            "source":
                row[0],

            "udm_field":
                row[1],

            "status":
                row[2],

            "confidence":
                float(
                    row[3]
                    or 0.0
                ),

            "method":
                row[4]
                or "",
        }

    def vector_search(
        self,
        source_column: str,
        module: str = "invoices",
        threshold: float = 0.55,
    ) -> dict | None:

        vector_literal = (
            _to_pgvector_literal(
                _embed_dense(
                    source_column
                )
            )
        )

        with self.conn.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    source_column,
                    udm_field,
                    1 - (
                        vector
                        <=> %s::vector
                    ) AS similarity
                FROM column_mappings
                WHERE status = 'confirmed'
                  AND module = %s
                  AND vector IS NOT NULL
                ORDER BY
                    vector
                    <=> %s::vector
                LIMIT 1
                """,
                (
                    vector_literal,
                    module,
                    vector_literal,
                ),
            )

            row = cursor.fetchone()

        if not row:
            return None

        similarity = float(
            row[2]
            or 0.0
        )

        if (
            similarity
            < float(
                threshold
            )
        ):
            return None

        return {
            "source":
                row[0],

            "udm_field":
                row[1],

            "status":
                "confirmed",

            "confidence":
                round(
                    similarity,
                    3,
                ),

            "method":
                f"vector({similarity:.0%})",
        }

    def list_pending(
        self,
        module: str = "invoices",
    ) -> list[dict]:

        with self.conn.cursor() as cursor:

            cursor.execute(
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
                  AND module = %s
                ORDER BY
                    created_at DESC,
                    id DESC
                """,
                (
                    module,
                ),
            )

            rows = cursor.fetchall()

        result = []

        for row in rows:

            raw_alternatives = row[7]

            if isinstance(
                raw_alternatives,
                str,
            ):

                try:
                    raw_alternatives = (
                        json.loads(
                            raw_alternatives
                        )
                    )
                except Exception:
                    raw_alternatives = []

            alternatives = (
                _normalize_alternatives(
                    (
                        raw_alternatives
                        if isinstance(
                            raw_alternatives,
                            list,
                        )
                        else []
                    ),
                    row[3],
                    float(
                        row[4]
                        or 0.0
                    ),
                    row[5]
                    or "",
                )
            )

            created_at = (
                row[6].isoformat()
                if hasattr(
                    row[6],
                    "isoformat",
                )
                else str(
                    row[6]
                )
            )

            result.append(
                {
                    "id":
                        int(
                            row[0]
                        ),

                    "source_column":
                        row[1],

                    "erp_type":
                        row[2]
                        or "",

                    "udm_field":
                        row[3],

                    "confidence":
                        float(
                            row[4]
                            or 0.0
                        ),

                    "method":
                        row[5]
                        or "",

                    "created_at":
                        created_at,

                    "alternatives":
                        alternatives,
                }
            )

        return result

    def list_all(
        self,
        module: str = "invoices",
    ) -> list[dict]:

        with self.conn.cursor() as cursor:

            cursor.execute(
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
                        WHEN 'rejected' THEN 2
                        ELSE 3
                    END,
                    created_at DESC,
                    id DESC
                """,
                (
                    module,
                ),
            )

            rows = cursor.fetchall()

        result = []

        for row in rows:

            created_at = (
                row[8].isoformat()
                if hasattr(
                    row[8],
                    "isoformat",
                )
                else str(
                    row[8]
                )
            )

            result.append(
                {
                    "id":
                        int(
                            row[0]
                        ),

                    "source_column":
                        row[1],

                    "erp_type":
                        row[2]
                        or "",

                    "udm_field":
                        row[3],

                    "status":
                        row[4],

                    "confidence":
                        float(
                            row[5]
                            or 0.0
                        ),

                    "method":
                        row[6]
                        or "",

                    "approved_by":
                        row[7],

                    "created_at":
                        created_at,
                }
            )

        return result

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

        source_column = (
            source_column
            or ""
        ).strip()

        udm_field = (
            udm_field
            or ""
        ).strip()

        status = (
            status
            or "inferred"
        ).strip().lower()

        method = (
            method
            or ""
        ).strip()

        module = (
            module
            or "invoices"
        ).strip()

        if not source_column:
            raise ValueError(
                "source_column is required"
            )

        if not udm_field:
            raise ValueError(
                "udm_field is required"
            )

        norm = _normalize(
            source_column
        )

        erp = _normalize_erp(
            erp_type
        )

        confidence_value = float(
            confidence
            or 0.0
        )

        vector_literal = (
            _to_pgvector_literal(
                _embed_dense(
                    source_column
                )
            )
        )

        alternatives_json = (
            json.dumps(
                _normalize_alternatives(
                    alternatives,
                    udm_field,
                    confidence_value,
                    method,
                ),
                separators=(
                    ",",
                    ":",
                ),
            )
        )

        approved_at = (
            datetime.now(
                timezone.utc
            )
            if approved_by
            else None
        )

        with self.conn.cursor() as cursor:

            cursor.execute(
                """
                INSERT INTO column_mappings (
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
                    %s::jsonb,
                    %s,
                    %s
                )
                ON CONFLICT(
                    source_norm,
                    erp_type,
                    module
                )
                DO UPDATE SET
                    source_column =
                        excluded.source_column,
                    udm_field =
                        excluded.udm_field,
                    status =
                        excluded.status,
                    confidence =
                        excluded.confidence,
                    method =
                        excluded.method,
                    vector =
                        excluded.vector,
                    alternatives =
                        excluded.alternatives,
                    approved_by =
                        excluded.approved_by,
                    approved_at =
                        excluded.approved_at
                RETURNING id
                """,
                (
                    source_column,
                    norm,
                    erp,
                    module,
                    udm_field,
                    status,
                    confidence_value,
                    method,
                    vector_literal,
                    alternatives_json,
                    approved_by,
                    approved_at,
                ),
            )

            row = cursor.fetchone()

        if not row:
            return -1

        return int(
            row[0]
        )

    def approve(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        with self.conn.cursor() as cursor:

            cursor.execute(
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
                    int(
                        mapping_id
                    ),
                ),
            )

            return (
                cursor.rowcount
                > 0
            )

    def reject(
        self,
        mapping_id: int,
        approved_by: str = "system",
    ) -> bool:

        with self.conn.cursor() as cursor:

            cursor.execute(
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
                    int(
                        mapping_id
                    ),
                ),
            )

            return (
                cursor.rowcount
                > 0
            )

    def stats(
        self,
        module: str = "invoices",
    ) -> dict:

        with self.conn.cursor() as cursor:

            cursor.execute(
                """
                SELECT
                    status,
                    COUNT(*)
                FROM column_mappings
                WHERE module = %s
                GROUP BY status
                """,
                (
                    module,
                ),
            )

            rows = cursor.fetchall()

        return {
            row[0]:
                int(
                    row[1]
                )

            for row in rows
        }

    def close(
        self,
    ) -> None:

        try:
            self.conn.close()
        except Exception:
            pass


_registry: RegistryProtocol | None = None


def get_registry() -> RegistryProtocol:

    global _registry

    if _registry is not None:
        return _registry

    backend = (
        os.environ.get(
            "MAPPING_DB_BACKEND"
        )
        or "auto"
    ).strip().lower()

    database_url = (
        os.environ.get(
            "DATABASE_URL"
        )
        or ""
    ).strip()

    supported_backends = {
        "auto",
        "sqlite",
        "pgvector",
        "postgres",
        "postgresql",
    }

    if (
        backend
        not in supported_backends
    ):
        raise RuntimeError(
            f"Unsupported MAPPING_DB_BACKEND: {backend}"
        )

    use_postgres = (
        backend
        in {
            "pgvector",
            "postgres",
            "postgresql",
        }
        or (
            backend == "auto"
            and bool(
                database_url
            )
        )
    )

    if use_postgres:

        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is required when MAPPING_DB_BACKEND uses PostgreSQL"
            )

        try:

            _registry = (
                PgVectorMappingRegistry(
                    database_url
                )
            )

            return _registry

        except Exception as exc:

            strict = (
                os.environ.get(
                    "MAPPING_DB_STRICT",
                    "0",
                )
                .strip()
                .lower()
                in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            )

            if strict:
                raise

            print(
                "[MappingRegistry] "
                "PostgreSQL unavailable; "
                "using SQLite fallback: "
                f"{exc}"
            )

    _registry = (
        SQLiteMappingRegistry()
    )

    return _registry


def reset_registry() -> None:

    global _registry

    if _registry is not None:

        try:
            _registry.close()
        except Exception:
            pass

    _registry = None


def registry_health() -> dict:

    registry = get_registry()

    try:

        return {
            "ok": True,
            "backend":
                registry.backend,
            "stats":
                registry.stats(),
        }

    except Exception as exc:

        return {
            "ok": False,
            "backend":
                getattr(
                    registry,
                    "backend",
                    "unknown",
                ),
            "error":
                str(
                    exc
                ),
        }