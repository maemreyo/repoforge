"""Shadow SQLite WAL persistence for RuntimeTransitionStore (Phase 2).

JSON is authoritative. This store mirrors writes into SQLite so the parity
checker can diff the two views. No production safety gate reads from SQLite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...domain.runtime_transition import RuntimeTransition, RuntimeTransitionStatus
from ...domain.versioning import Revision
from .sqlite_db import migrate, open_db, transaction


class SqliteTransitionStore:
    """Shadow SQLite store for RuntimeTransition. Not the primary store —
    JSON is authoritative. This is write-only-shadow for parity checking.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn: sqlite3.Connection = open_db(db_path)
        migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    # ---- shadow write API (mirrors JSON authoritative writes) ----

    def write_shadow(self, transition: RuntimeTransition, revision: Revision) -> None:
        """Upsert a RuntimeTransition into the shadow table."""
        row = {
            "transition_id": transition.transition_id,
            "status": transition.status.value,
            "target_generation": transition.target_generation,
            "config_generation": transition.config_generation,
            "correlation_id": transition.correlation_id,
            "started_at": transition.started_at,
            "updated_at": transition.updated_at,
            "completed_at": transition.completed_at,
            "error_code": transition.error_code,
            "error_message": transition.error_message,
            "previous_transition_id": transition.previous_transition_id,
            "revision": revision.value,
        }
        with transaction(self._conn) as conn:
            conn.execute(
                """INSERT INTO runtime_transitions (
                       transition_id, status, target_generation,
                       config_generation, correlation_id, started_at,
                       updated_at, completed_at, error_code,
                       error_message, previous_transition_id, revision
                   ) VALUES (
                       :transition_id, :status, :target_generation,
                       :config_generation, :correlation_id, :started_at,
                       :updated_at, :completed_at, :error_code,
                       :error_message, :previous_transition_id, :revision
                   ) ON CONFLICT(transition_id) DO UPDATE SET
                       status               = excluded.status,
                       target_generation    = excluded.target_generation,
                       config_generation    = excluded.config_generation,
                       correlation_id       = excluded.correlation_id,
                       started_at           = excluded.started_at,
                       updated_at           = excluded.updated_at,
                       completed_at         = excluded.completed_at,
                       error_code           = excluded.error_code,
                       error_message        = excluded.error_message,
                       previous_transition_id = excluded.previous_transition_id,
                       revision             = excluded.revision
                """,
                row,
            )

    def delete_shadow(self, transition_id: str) -> None:
        """Delete a RuntimeTransition from the shadow table."""
        with transaction(self._conn) as conn:
            conn.execute(
                "DELETE FROM runtime_transitions WHERE transition_id = ?",
                (transition_id,),
            )

    def list_shadow(self) -> list[tuple[RuntimeTransition, Revision]]:
        """Read all transitions from the shadow table for parity checking."""
        rows = self._conn.execute(
            "SELECT * FROM runtime_transitions ORDER BY updated_at DESC"
        ).fetchall()
        result: list[tuple[RuntimeTransition, Revision]] = []
        for row in rows:
            rt = RuntimeTransition(
                transition_id=row["transition_id"],
                status=RuntimeTransitionStatus(row["status"]),
                target_generation=row["target_generation"],
                config_generation=row["config_generation"],
                correlation_id=row["correlation_id"],
                started_at=row["started_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
                error_code=row["error_code"],
                error_message=row["error_message"],
                previous_transition_id=row["previous_transition_id"],
            )
            result.append((rt, Revision(row["revision"])))
        return result
