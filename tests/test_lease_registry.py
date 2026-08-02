"""Phase 3: the unified shadow lease registry (role-aware, completeness-exposed).

The shadow SQLite lease table is the one registry every managed process kind
shares; ``list_page`` exposes truncation and unreadable records so a reconciler
fails closed instead of silently missing an orphan (F-008). JSON stays
authoritative; these tests prove the shadow mirrors it and reports drift.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from repoforge.adapters.persistence.json_execution_worker_binding_store import (
    JsonExecutionWorkerBindingStore,
)
from repoforge.adapters.persistence.parity import compare_lease_parity, import_active_bindings
from repoforge.adapters.persistence.sqlite_db import migrate, open_db
from repoforge.adapters.persistence.sqlite_lease_store import SqliteLeaseStore
from repoforge.domain.durable_state import Revision
from repoforge.domain.execution_worker import ExecutionWorkerBinding
from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus
from repoforge.testing import InMemoryLockManager

_SHA = "a" * 64
_WORKER = "worker-000000000001"


def _binding(*, worker_id: str = _WORKER, state: str = "running") -> ExecutionWorkerBinding:
    return ExecutionWorkerBinding(
        worker_id=worker_id,
        pid=4242,
        pgid=4242,
        process_start_token="worker-start-token",
        generation=12,
        release_sha="0123abc",
        supervisor_pid=4241,
        supervisor_process_identity=_SHA,
        correlation_id="c" * 24,
        started_at="2026-07-29T09:26:21+00:00",
        state=state,
    )


def _lease(
    *, worker_id: str = _WORKER, role: ProcessLeaseRole = ProcessLeaseRole.EXECUTION_DAEMON
) -> ProcessLease:
    return ProcessLease(
        lease_id=worker_id,
        status=ProcessLeaseStatus.RUNNING,
        role=role,
        process_identity="worker-start-token",
        pid=4242,
        started_at="2026-07-29T09:26:21+00:00",
        heartbeat_at="2026-07-29T09:26:21+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:26:22+00:00",
    )


# ---------------------------------------------------------------------------
# Domain: role field.
# ---------------------------------------------------------------------------


def test_process_lease_defaults_to_the_execution_daemon_role() -> None:
    assert _lease().role is ProcessLeaseRole.EXECUTION_DAEMON


def test_process_lease_accepts_an_explicit_role() -> None:
    lease = _lease(role=ProcessLeaseRole.OPERATION_WORKER)
    assert lease.role is ProcessLeaseRole.OPERATION_WORKER


# ---------------------------------------------------------------------------
# Schema migration: v1 databases gain the role column.
# ---------------------------------------------------------------------------


def test_v1_database_migrates_to_the_role_column(tmp_path: Path) -> None:
    path = tmp_path / "shadow.db"
    conn = open_db(path)
    # Create the v1 schema exactly as Phase 2 wrote it (no role column).
    conn.execute(
        """CREATE TABLE shadow_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE process_leases (
            lease_id          TEXT PRIMARY KEY,
            status            TEXT NOT NULL,
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
    conn.execute("INSERT INTO shadow_meta(key, value) VALUES ('schema_version', '1')")
    conn.execute(
        """INSERT INTO process_leases (
               lease_id, status, process_identity, pid, correlation_id,
               created_at, updated_at, revision
           ) VALUES ('worker-000000000001', 'running', 'tok', 4242, 'c',
                     'now', 'now', 1)"""
    )
    conn.commit()

    migrate(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(process_leases)")}
    assert "role" in columns
    version = conn.execute("SELECT value FROM shadow_meta WHERE key = 'schema_version'").fetchone()
    assert version["value"] == "2"
    # The pre-existing row reads back with the default role.
    row = conn.execute("SELECT * FROM process_leases").fetchone()
    assert row["role"] == "execution_daemon"
    conn.close()


# ---------------------------------------------------------------------------
# Registry scan: completeness is exposed, never silently truncated (F-008).
# ---------------------------------------------------------------------------


def test_list_page_reports_a_complete_scan_within_the_bound(tmp_path: Path) -> None:
    store = SqliteLeaseStore(tmp_path / "shadow.db")
    store.write_shadow(_lease(), Revision(1))

    page = store.list_page()

    assert page.scan_complete is True
    assert page.unreadable_ids == ()
    assert len(page.records) == 1


def test_list_page_exposes_truncation_instead_of_hiding_it(tmp_path: Path) -> None:
    store = SqliteLeaseStore(tmp_path / "shadow.db")
    for index in range(1, 6):
        store.write_shadow(_lease(worker_id=f"worker-{index:012x}"), Revision(1))

    page = store.list_page(max_records=3)

    assert len(page.records) == 3
    assert page.scan_complete is False


def test_list_page_filters_by_role(tmp_path: Path) -> None:
    store = SqliteLeaseStore(tmp_path / "shadow.db")
    store.write_shadow(_lease(role=ProcessLeaseRole.EXECUTION_DAEMON), Revision(1))
    store.write_shadow(
        _lease(worker_id="worker-000000000002", role=ProcessLeaseRole.OPERATION_WORKER),
        Revision(1),
    )

    page = store.list_page(role=ProcessLeaseRole.OPERATION_WORKER)

    assert [lease.lease_id for lease in page.records] == ["worker-000000000002"]
    assert page.scan_complete is True


def test_list_page_names_unreadable_records(tmp_path: Path) -> None:
    store = SqliteLeaseStore(tmp_path / "shadow.db")
    store.write_shadow(_lease(), Revision(1))
    # A row whose status is not a known process-lease status cannot be decoded.
    conn = sqlite3.connect(str(tmp_path / "shadow.db"))
    conn.execute(
        "UPDATE process_leases SET status = 'mystery', role = 'also-mystery' WHERE lease_id = ?",
        (_WORKER,),
    )
    conn.commit()
    conn.close()

    page = store.list_page()

    assert page.records == ()
    assert page.unreadable_ids == (_WORKER,)


# ---------------------------------------------------------------------------
# Parity: JSON authoritative vs SQLite shadow.
# ---------------------------------------------------------------------------


def test_parity_in_sync_when_both_sides_agree(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    bindings.put(_binding())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    import_active_bindings(bindings, shadow, now="2026-07-30T00:00:00+00:00")

    report = compare_lease_parity(bindings, shadow)

    assert report.in_sync is True
    assert report.json_count == 1
    assert report.shadow_count == 1


def test_parity_reports_a_lease_missing_from_the_shadow(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    bindings.put(_binding())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")  # shadow never mirrored

    report = compare_lease_parity(bindings, shadow)

    assert report.in_sync is False
    assert report.only_in_json == (_WORKER,)
    assert report.only_in_shadow == ()


def test_parity_reports_a_stale_shadow_lease(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    shadow.write_shadow(_lease(), Revision(1))  # shadow has a lease JSON never wrote

    report = compare_lease_parity(bindings, shadow)

    assert report.in_sync is False
    assert report.only_in_json == ()
    assert report.only_in_shadow == (_WORKER,)


def test_parity_surfaces_json_scan_truncation(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    for index in range(1, 3):
        worker_id = f"worker-{index:012x}"
        bindings.put(_binding(worker_id=worker_id))
        shadow.write_shadow(_lease(worker_id=worker_id), Revision(1))

    report = compare_lease_parity(bindings, shadow)

    # The comparison is honest about the authoritative side being truncated.
    assert report.json_scan_complete is True  # 2 records is under the scan bound


def test_import_active_bindings_mirrors_only_active_records(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    bindings.put(_binding(worker_id="worker-000000000001", state="running"))
    bindings.put(_binding(worker_id="worker-000000000002", state="already_gone"))
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")

    imported = import_active_bindings(bindings, shadow, now="2026-07-30T00:00:00+00:00")

    assert imported == 1
    page = shadow.list_page()
    assert [lease.lease_id for lease in page.records] == ["worker-000000000001"]


# ---------------------------------------------------------------------------
# Dual-write: a registered execution worker mirrors into the shadow registry.
# ---------------------------------------------------------------------------


def _fake_popen(pid: int) -> object:
    class _Process:
        def __init__(self) -> None:
            self.pid = pid

        def poll(self) -> None:
            return None

        @property
        def stdout(self):
            return None

    return _Process()


def test_registered_worker_mirrors_into_the_shadow_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker registration writes both the authoritative JSON lease and the shadow."""
    import repoforge.adapters.runtime.execution_worker as worker_module
    from repoforge.adapters.subprocess.process_tree import ProcessIdentity

    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())

    monkeypatch.setattr(worker_module.subprocess, "Popen", lambda *a, **k: _fake_popen(4242))
    monkeypatch.setattr(worker_module, "process_identity", lambda pid: _SHA)
    monkeypatch.setattr(
        worker_module,
        "read_identity",
        lambda pid: ProcessIdentity(pid=pid, ppid=1, start_token="worker-start-token"),
    )
    monkeypatch.setattr(worker_module.time, "sleep", lambda seconds: None)
    worker = worker_module.SubprocessExecutionWorker(
        Path("/tmp/config.toml"), bindings=bindings, lease_shadow=shadow
    )

    child = worker.start(
        generation=12,
        env={"REPOFORGE_RUNNING_RELEASE_SHA": "0123abc"},
        log_path=Path("/tmp/worker.log"),
        correlation_id="c" * 24,
    )

    assert child.pid == 4242
    json_ids = {b.worker_id for b in bindings.list_all()}
    assert len(json_ids) == 1
    page = shadow.list_page()
    assert len(page.records) == 1
    assert page.records[0].lease_id in json_ids
    assert page.records[0].role is ProcessLeaseRole.EXECUTION_DAEMON


def test_shadow_write_failure_never_fails_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shadow is parity evidence: a broken shadow must not refuse the worker."""
    import repoforge.adapters.runtime.execution_worker as worker_module
    from repoforge.adapters.subprocess.process_tree import ProcessIdentity

    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())

    class _BrokenShadow:
        def write_shadow(self, lease: ProcessLease, revision: Revision) -> None:
            del lease, revision
            raise RuntimeError("shadow disk full")

    monkeypatch.setattr(worker_module.subprocess, "Popen", lambda *a, **k: _fake_popen(4242))
    monkeypatch.setattr(worker_module, "process_identity", lambda pid: _SHA)
    monkeypatch.setattr(
        worker_module,
        "read_identity",
        lambda pid: ProcessIdentity(pid=pid, ppid=1, start_token="worker-start-token"),
    )
    monkeypatch.setattr(worker_module.time, "sleep", lambda seconds: None)
    worker = worker_module.SubprocessExecutionWorker(
        Path("/tmp/config.toml"), bindings=bindings, lease_shadow=_BrokenShadow()
    )

    child = worker.start(
        generation=12,
        env={"REPOFORGE_RUNNING_RELEASE_SHA": "0123abc"},
        log_path=Path("/tmp/worker.log"),
        correlation_id="c" * 24,
    )

    assert child.pid == 4242
    assert len(bindings.list_all()) == 1
