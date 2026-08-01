"""Shadow SQLite ProcessLease store for parity checking.

JSON is authoritative. This store mirrors writes into SQLite so the parity
checker can diff the two views. No production safety gate reads from SQLite.
"""

from __future__ import annotations

from pathlib import Path

from ...domain.durable_state import Revision
from ...domain.process_lease import ProcessLease
from .sqlite_db import migrate, open_db, transaction


class SqliteLeaseStore:
    """Shadow-only ProcessLease persistence in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._conn = open_db(db_path)
        migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ---- shadow write API (mirrors JSON authoritative writes) ----

    def write_shadow(self, lease: ProcessLease, revision: Revision) -> None:
        """Upsert a lease into the shadow store."""
        row = {
            "lease_id": lease.lease_id,
            "status": lease.status.value,
            "process_identity": lease.process_identity,
            "pid": lease.pid,
            "started_at": lease.started_at,
            "heartbeat_at": lease.heartbeat_at,
            "correlation_id": lease.correlation_id,
            "created_at": lease.created_at,
            "updated_at": lease.updated_at,
            "error_code": lease.error_code,
            "error_message": lease.error_message,
            "revision": revision.value,
        }
        with transaction(self._conn) as conn:
            conn.execute(
                """INSERT INTO process_leases (
                       lease_id, status, process_identity, pid,
                       started_at, heartbeat_at, correlation_id,
                       created_at, updated_at, error_code,
                       error_message, revision
                   ) VALUES (
                       :lease_id, :status, :process_identity, :pid,
                       :started_at, :heartbeat_at, :correlation_id,
                       :created_at, :updated_at, :error_code,
                       :error_message, :revision
                   ) ON CONFLICT(lease_id) DO UPDATE SET
                       status          = excluded.status,
                       process_identity = excluded.process_identity,
                       pid             = excluded.pid,
                       started_at      = excluded.started_at,
                       heartbeat_at    = excluded.heartbeat_at,
                       correlation_id  = excluded.correlation_id,
                       created_at      = excluded.created_at,
                       updated_at      = excluded.updated_at,
                       error_code      = excluded.error_code,
                       error_message   = excluded.error_message,
                       revision        = excluded.revision
                """,
                row,
            )

    def delete_shadow(self, lease_id: str) -> None:
        """Delete a lease from the shadow store."""
        with transaction(self._conn) as conn:
            conn.execute(
                "DELETE FROM process_leases WHERE lease_id = ?",
                (lease_id,),
            )

    def list_shadow(self) -> list[tuple[ProcessLease, Revision]]:
        """Return every shadow lease with its revision for parity checking."""
        rows = self._conn.execute("SELECT * FROM process_leases ORDER BY lease_id").fetchall()
        result: list[tuple[ProcessLease, Revision]] = []
        for row in rows:
            lease = ProcessLease(
                lease_id=row["lease_id"],
                status=row["status"],
                process_identity=row["process_identity"],
                pid=row["pid"],
                started_at=row["started_at"],
                heartbeat_at=row["heartbeat_at"],
                correlation_id=row["correlation_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                error_code=row["error_code"],
                error_message=row["error_message"],
            )
            result.append((lease, Revision(row["revision"])))
        return result
