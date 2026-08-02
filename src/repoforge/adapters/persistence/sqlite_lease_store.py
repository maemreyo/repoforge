"""Shadow SQLite ProcessLease store — the unified lease registry (shadow).

JSON is authoritative. This store mirrors writes into SQLite so the parity
checker can diff the two views, and provides the one unified, role-aware lease
table every managed process kind (execution daemon, operation worker, tunnel
child) shares -- with pagination completeness so a reconciler fails closed on a
partial scan instead of silently missing an orphan (F-008). No production safety
gate reads from SQLite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.durable_state import Revision
from ...domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus
from ...ports.process_lease_store import ProcessLeasePage
from .sqlite_db import migrate, open_db, transaction

_SHADOW_LIMIT = 2_000


class SqliteLeaseStore:
    """Shadow-only unified ProcessLease persistence in SQLite."""

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
            "role": lease.role.value,
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
                       lease_id, status, role, process_identity, pid,
                       started_at, heartbeat_at, correlation_id,
                       created_at, updated_at, error_code,
                       error_message, revision
                   ) VALUES (
                       :lease_id, :status, :role, :process_identity, :pid,
                       :started_at, :heartbeat_at, :correlation_id,
                       :created_at, :updated_at, :error_code,
                       :error_message, :revision
                   ) ON CONFLICT(lease_id) DO UPDATE SET
                       status           = excluded.status,
                       role             = excluded.role,
                       process_identity = excluded.process_identity,
                       pid              = excluded.pid,
                       started_at       = excluded.started_at,
                       heartbeat_at     = excluded.heartbeat_at,
                       correlation_id   = excluded.correlation_id,
                       created_at       = excluded.created_at,
                       updated_at       = excluded.updated_at,
                       error_code       = excluded.error_code,
                       error_message    = excluded.error_message,
                       revision         = excluded.revision
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

    # ---- unified registry scan API ----

    def list_page(
        self,
        *,
        role: ProcessLeaseRole | None = None,
        max_records: int = _SHADOW_LIMIT,
    ) -> ProcessLeasePage:
        """A bounded scan with completeness exposed; never a silent truncation.

        The row count is known before the page is taken, so ``scan_complete``
        truthfully reports whether leases beyond ``max_records`` exist -- the
        operation-worker scan that once returned a bare tuple and hid orphans past
        the limit now exposes the truncation to its caller (F-008).
        """
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records <= 0:
            raise ValueError("max_records must be a positive integer")
        params: tuple[object, ...] = (max_records + 1,)
        where = ""
        if role is not None:
            where = "WHERE role = ?"
            params = (role.value, max_records + 1)
        rows = self._conn.execute(
            f"SELECT * FROM process_leases {where} ORDER BY lease_id LIMIT ?",
            params,
        ).fetchall()
        truncated = len(rows) > max_records
        rows = rows[:max_records]
        records: list[ProcessLease] = []
        unreadable: list[str] = []
        for row in rows:
            # The row factory produces dicts at runtime; converting at the decode
            # boundary keeps `.get` usable and the values `Any` (arbitrary SQLite
            # data), which is the honest type for a dynamically decoded row.
            data: dict[str, Any] = dict(row)
            try:
                records.append(_row_to_lease(data))
            except (KeyError, TypeError, ValueError):
                lease_id = data.get("lease_id")
                if isinstance(lease_id, str):
                    unreadable.append(lease_id)
        return ProcessLeasePage(
            records=tuple(records),
            scan_complete=not truncated,
            unreadable_ids=tuple(sorted(unreadable)),
        )

    def list_all(
        self, *, role: ProcessLeaseRole | None = None, max_records: int = _SHADOW_LIMIT
    ) -> list[tuple[ProcessLease, Revision]]:
        """Every shadow lease with its revision, for parity checking."""
        page = self.list_page(role=role, max_records=max_records)
        result: list[tuple[ProcessLease, Revision]] = []
        for lease in page.records:
            row = self._conn.execute(
                "SELECT revision FROM process_leases WHERE lease_id = ?",
                (lease.lease_id,),
            ).fetchone()
            result.append((lease, Revision(row["revision"]) if row is not None else Revision(1)))
        return result


def _row_to_lease(data: dict[str, Any]) -> ProcessLease:
    """Decode one shadow row; raises ValueError on a malformed record.

    The status and role columns are converted to their enums explicitly so an
    unknown value raises ``ValueError`` (treated as an unreadable record) instead
    of passing a raw string through to the domain validator's ``assert_never``.
    """
    status = ProcessLeaseStatus(str(data["status"]))
    role = ProcessLeaseRole(str(data["role"]))
    return ProcessLease(
        lease_id=str(data["lease_id"]),
        status=status,
        role=role,
        process_identity=_optional_str(data.get("process_identity")),
        pid=_optional_int(data.get("pid")),
        started_at=_optional_str(data.get("started_at")),
        heartbeat_at=_optional_str(data.get("heartbeat_at")),
        correlation_id=str(data["correlation_id"]),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
        error_code=_optional_str(data.get("error_code")),
        error_message=_optional_str(data.get("error_message")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid integer value: {value!r}")
    return value
