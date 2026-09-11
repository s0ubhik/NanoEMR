"""SQLite access layer for NanoEMR.

Stdlib only. A thread-local connection keeps the ThreadingHTTPServer happy
without a pool, which is plenty for a single-clinic deployment.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "schema.sql")
DB_PATH = os.environ.get(
    "NANOEMR_DB", os.path.join(os.path.dirname(HERE), "data", "emr.db")
)

# India Standard Time — every timestamp written by the app carries +05:30 so the
# FHIR instants exported downstream are unambiguous.
IST = timezone(timedelta(hours=5, minutes=30))

_local = threading.local()
_write_lock = threading.Lock()


# --------------------------------------------------------------------- time
def now() -> datetime:
    """Current time in IST — every timestamp the app writes is timezone-aware."""
    return datetime.now(IST)


def now_iso() -> str:
    """FHIR `instant` — second precision with an explicit offset."""
    return now().replace(microsecond=0).isoformat()


def today_iso() -> str:
    """Today's date as ``YYYY-MM-DD``."""
    return now().date().isoformat()


def to_instant(value: str | None) -> str | None:
    """Coerce a stored value (date, datetime-local, instant) to a FHIR instant."""
    if not value:
        return None
    value = value.strip()
    if len(value) == 10:  # YYYY-MM-DD
        value += "T00:00:00"
    if len(value) == 16:  # YYYY-MM-DDTHH:MM (html datetime-local)
        value += ":00"
    if value.endswith("Z") or "+" in value[10:] or value[10:].count("-") > 0:
        return value
    return value + "+05:30"


# ---------------------------------------------------------------- connection
def connect() -> sqlite3.Connection:
    """The calling thread's SQLite connection, opened on first use.

    One connection per thread keeps ThreadingHTTPServer safe without a pool.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


# Columns added after the first release; applied to databases created earlier.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("claim_payment", "notice_id", "TEXT"),
    ("claim_preauth", "enhancement_no", "INTEGER"),
    ("organization", "participant_code", "TEXT"),
    ("observation", "dialysis_session_id", "INTEGER"),
    ("observation", "phase", "TEXT"),
    ("procedure", "dialysis_session_id", "INTEGER"),
    ("observation", "wellness_record_id", "INTEGER"),
    ("observation", "value_system", "TEXT"),
    ("observation", "value_code", "TEXT"),
    ("observation", "value_display", "TEXT"),
    ("observation", "code_text", "TEXT"),
    ("wellness_record", "dialysis_session_id", "INTEGER"),
    ("claim", "patient_id", "INTEGER"),
    ("claim", "encounter_id", "INTEGER"),
    ("claim", "admission_date", "TEXT"),
    ("claim", "expected_discharge_date", "TEXT"),
    ("claim", "case_type", "TEXT"),
    ("claim", "package_code", "TEXT"),
    ("claim", "package_name", "TEXT"),
    ("claim", "preauth_total", "REAL"),
    ("claim", "preauth_saved_at", "TEXT"),
    ("claim_plan", "policy_documents", "TEXT"),
    ("claim_preauth", "cancel_txn_id", "TEXT"),
    ("claim_preauth", "cancel_correlation_id", "TEXT"),
    ("claim_preauth", "cancel_requested_at", "TEXT"),
    ("claim_preauth", "cancel_reason", "TEXT"),
    ("claim_preauth", "cancel_note", "TEXT"),
    ("claim_document", "code", "TEXT"),
    ("claim_document", "stage", "TEXT NOT NULL DEFAULT 'preauth'"),
    ("claim_submission", "preauth_ref", "TEXT"),
    ("claim_preauth", "api_call_id", "TEXT"),
    ("claim_preauth", "adjudication", "TEXT"),
    ("claim_preauth", "query_note", "TEXT"),
    ("claim_submission", "api_call_id", "TEXT"),
    ("claim_submission", "adjudication", "TEXT"),
    ("claim_submission", "query_note", "TEXT"),
    ("claim_payment", "sender_code", "TEXT"),
    ("claim_payment", "workflow_id", "TEXT"),
    ("claim_document", "category", "TEXT"),
    ("claim_plan_benefit", "kind", "TEXT"),
    ("claim_plan_benefit", "extras", "TEXT"),
    ("claim_plan_benefit", "supporting_info", "TEXT"),
]


def init_db() -> None:
    """Create the schema if absent and apply any pending column migrations."""
    conn = connect()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    # the runtime key-value store lost its last consumer; drop it where it exists
    conn.execute("DROP TABLE IF EXISTS setting")
    for table, column, ddl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()


# ------------------------------------------------------------------- queries
def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Run a SELECT and return every row."""
    return connect().execute(sql, params).fetchall()


def one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    """Run a SELECT and return the first row, or ``None``."""
    return connect().execute(sql, params).fetchone()


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    """Run a SELECT and return the first column of the first row."""
    row = one(sql, params)
    return row[0] if row is not None else default


def in_transaction() -> bool:
    """True while this thread is inside a :func:`transaction` block."""
    return bool(getattr(_local, "depth", 0))


@contextlib.contextmanager
def transaction():
    """Group writes so that a failure part-way through leaves nothing behind.

        with db.transaction():
            db.insert(...)
            db.insert(...)      # if this raises, the first insert is rolled back

    Writes made through :func:`execute`, :func:`insert`, :func:`update` and
    :func:`next_number` join the open transaction instead of committing on their
    own. Nesting is allowed; only the outermost block commits.
    """
    conn = connect()
    if in_transaction():
        yield conn                      # an outer block owns the commit
        return
    _write_lock.acquire()
    _local.depth = 1
    try:
        conn.execute("BEGIN")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        _local.depth = 0
        _write_lock.release()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run one statement and return ``lastrowid``. Joins an open transaction."""
    if in_transaction():
        return connect().execute(sql, params).lastrowid
    with _write_lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    """Run one statement over many parameter sets."""
    if in_transaction():
        connect().executemany(sql, list(seq))
        return
    with _write_lock:
        conn = connect()
        conn.executemany(sql, list(seq))
        conn.commit()


def insert(table: str, values: dict[str, Any]) -> int:
    """INSERT a row from a ``{column: value}`` mapping and return its id."""
    cols = list(values.keys())
    sql = "INSERT INTO {} ({}) VALUES ({})".format(
        table, ", ".join(cols), ", ".join("?" for _ in cols)
    )
    return execute(sql, [values[c] for c in cols])


def update(table: str, row_id: int, values: dict[str, Any]) -> None:
    """UPDATE one row by id from a ``{column: value}`` mapping."""
    if not values:
        return
    cols = list(values.keys())
    sql = "UPDATE {} SET {} WHERE id = ?".format(
        table, ", ".join(f"{c} = ?" for c in cols)
    )
    execute(sql, [values[c] for c in cols] + [row_id])


# ------------------------------------------------------------------ counters
def _allocate(conn: sqlite3.Connection, name: str) -> int:
    conn.execute("INSERT INTO counter (name, value) VALUES (?, 0) "
                 "ON CONFLICT (name) DO NOTHING", (name,))
    conn.execute("UPDATE counter SET value = value + 1 WHERE name = ?", (name,))
    return conn.execute("SELECT value FROM counter WHERE name = ?",
                        (name,)).fetchone()[0]


def next_number(name: str, prefix: str, width: int = 5) -> str:
    """Atomically allocate the next human-readable number, e.g. ``MRN00042``.

    Inside a :func:`transaction` the allocation rolls back with everything else,
    so an abandoned write does not burn a number.
    """
    if in_transaction():
        return f"{prefix}{_allocate(connect(), name):0{width}d}"
    with _write_lock:
        conn = connect()
        value = _allocate(conn, name)
        conn.commit()
    return f"{prefix}{value:0{width}d}"


# ---------------------------------------------------------------- lookups
def terms(kind: str) -> list[sqlite3.Row]:
    """Every concept of one terminology ``kind``, in display order."""
    return query(
        "SELECT * FROM terminology WHERE kind = ? ORDER BY sort_order, display",
        (kind,),
    )


def term(kind: str, code: str) -> sqlite3.Row | None:
    """One concept by ``kind`` and ``code``, or ``None``."""
    return one("SELECT * FROM terminology WHERE kind = ? AND code = ?", (kind, code))


def default_org() -> sqlite3.Row | None:
    """The facility this installation represents."""
    return one("SELECT * FROM organization ORDER BY is_default DESC, id LIMIT 1")

