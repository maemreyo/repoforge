"""Shared SQLite connection factory with WAL mode for shadow storage.

Phase 2 shadow persistence: SQLite mirrors authoritative JSON state for parity
checking. JSON is authoritative; SQLite is the read-only shadow.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SHADOW_SCHEMA_VERSION = 2


def _dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, object]:
    cols = cursor.description
    return {col[0]: row[idx] for idx, col in enumerate(cols)} if cols else {}


def open_db(path: Path) -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with the dict row factory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = _dict_factory  # type: ignore[assignment]
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _current_schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM shadow_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or not isinstance(row.get("value"), str) or not row["value"].isdigit():
        return 0
    return int(row["value"])


def migrate(conn: sqlite3.Connection) -> None:
    """Create shadow tables if they do not exist and record schema version."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shadow_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS process_leases (
            lease_id          TEXT PRIMARY KEY,
            status            TEXT NOT NULL,
            role              TEXT NOT NULL DEFAULT 'execution_daemon',
            process_identity  TEXT,
            pid               INTEGER,
            started_at        TEXT,
            heartbeat_at      TEXT,
            correlation_id    TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            error_code        TEXT,
            error_message     TEXT,
            revision          INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runtime_transitions (
            transition_id          TEXT PRIMARY KEY,
            status                 TEXT NOT NULL,
            target_generation      INTEGER NOT NULL,
            config_generation      INTEGER,
            correlation_id         TEXT NOT NULL,
            started_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL,
            completed_at           TEXT,
            error_code             TEXT,
            error_message          TEXT,
            previous_transition_id TEXT,
            revision               INTEGER NOT NULL
        )"""
    )
    if _current_schema_version(conn) < 2:
        _columns = {row["name"] for row in conn.execute("PRAGMA table_info(process_leases)")}
        if "role" not in _columns:
            conn.execute(
                "ALTER TABLE process_leases ADD COLUMN "
                "role TEXT NOT NULL DEFAULT 'execution_daemon'"
            )
    conn.execute(
        "INSERT OR IGNORE INTO shadow_meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SHADOW_SCHEMA_VERSION)),
    )
    conn.execute(
        "UPDATE shadow_meta SET value = ? WHERE key = 'schema_version'",
        (str(SHADOW_SCHEMA_VERSION),),
    )
    # Close the implicit transaction the DML above opened: the next writer uses
    # BEGIN IMMEDIATE and would otherwise fail on the dangling transaction.
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction context manager: commits on success, rolls back on error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
