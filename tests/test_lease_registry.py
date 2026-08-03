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
from repoforge.adapters.persistence.json_process_lease_adapter import (
    JsonProcessLeaseAdapter,
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


def test_active_scan_completeness_ignores_terminal_history(tmp_path: Path) -> None:
    """2,001 terminal leases must never make the active scan fail closed.

    The re-review found a permanent fail-closed: read-only preflight blocks before
    the mutating prune pass can run, so terminal history accumulating past the scan
    bound blocks every later start forever. Active-scan completeness is computed
    over active leases only.
    """
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    for index in range(1, 2002):
        from dataclasses import replace

        leases.create(
            replace(
                _lease(worker_id=f"worker-{index:012x}"),
                status=ProcessLeaseStatus.TERMINATED,
            )
        )
    active = _lease(worker_id="worker-ffffffffffaa")  # one genuinely live lease
    leases.create(active)

    page = leases.list_active_page()

    assert [lease.lease_id for lease in page.records] == [active.lease_id]
    assert page.scan_complete is True
    assert page.unreadable_ids == ()


def test_active_scan_pages_past_deep_terminal_history(monkeypatch, tmp_path: Path) -> None:
    """The active scan pages: terminal history deeper than one page cannot falsify
    completeness, and an active lease after it is still found (F-008)."""

    # Force a small page so the test proves paging without creating 100k files:
    # a terminal backlog deeper than one scan page must be skipped, not counted.
    monkeypatch.setattr(
        "repoforge.adapters.persistence.json_process_lease_adapter._SCAN_PAGE_SIZE", 25
    )

    from dataclasses import replace

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    for index in range(1, 201):
        leases.create(
            replace(
                _lease(worker_id=f"worker-{index:012x}"),
                status=ProcessLeaseStatus.TERMINATED,
            )
        )
    active = _lease(worker_id="worker-ffffffffffaa")  # active lease after the backlog
    leases.create(active)

    page = leases.list_active_page()

    assert [lease.lease_id for lease in page.records] == [active.lease_id]
    assert page.scan_complete is True, "deep terminal history must not fail the scan closed"
    assert page.unreadable_ids == ()


def test_active_scan_stays_incomplete_when_the_active_set_outgrows_the_page(
    tmp_path: Path,
) -> None:
    """A genuinely truncated ACTIVE set (more active than max_records) still fails
    closed: only terminal history is exempt from the completeness signal (F-008)."""
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    for index in range(1, 6):
        leases.create(_lease(worker_id=f"worker-{index:012x}"))

    page = leases.list_active_page(max_records=3)

    assert len(page.records) == 3
    assert page.scan_complete is False, "an active set bigger than max_records is truncated"


def test_collect_terminal_archives_terminal_leases_out_of_the_active_scan(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    leases.create(replace(_lease(), status=ProcessLeaseStatus.TERMINATED))
    live = _lease(worker_id="worker-0000000000bb")
    leases.create(live)

    moved = leases.collect_terminal()

    assert moved == 1
    page = leases.list_active_page()
    assert [lease.lease_id for lease in page.records] == [live.lease_id]
    assert page.scan_complete is True


# ---------------------------------------------------------------------------
# Parity: JSON authoritative vs SQLite shadow.
# ---------------------------------------------------------------------------


def test_parity_in_sync_when_both_sides_agree(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    bindings.put(_binding())
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    import_active_bindings(bindings, leases, shadow, now="2026-07-30T00:00:00+00:00")

    report = compare_lease_parity(leases, shadow)

    assert report.in_sync is True
    assert report.json_count == 1
    assert report.shadow_count == 1


def test_parity_reports_a_lease_missing_from_the_shadow(tmp_path: Path) -> None:
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    leases.create(_lease())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")  # shadow never mirrored

    report = compare_lease_parity(leases, shadow)

    assert report.in_sync is False
    assert report.only_in_json == (_WORKER,)
    assert report.only_in_shadow == ()


def test_parity_reports_a_stale_shadow_lease(tmp_path: Path) -> None:
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    shadow.write_shadow(_lease(), Revision(1))  # shadow has a lease JSON never wrote

    report = compare_lease_parity(leases, shadow)

    assert report.in_sync is False
    assert report.only_in_json == ()
    assert report.only_in_shadow == (_WORKER,)


def test_parity_surfaces_json_scan_truncation(tmp_path: Path) -> None:
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    for index in range(1, 3):
        worker_id = f"worker-{index:012x}"
        leases.create(_lease(worker_id=worker_id))
        shadow.write_shadow(_lease(worker_id=worker_id), Revision(1))

    report = compare_lease_parity(leases, shadow)

    # The comparison is honest about the authoritative side being truncated.
    assert report.json_scan_complete is True  # 2 records is under the scan bound


def test_parity_detects_same_id_different_status(tmp_path: Path) -> None:
    """Same lease id on both sides but different status is not in_sync."""
    from dataclasses import replace

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    envelope = leases.create(_lease())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    # Shadow lags: still shows TERMINATING while JSON advanced to RUNNING.
    shadow.write_shadow(
        replace(envelope.value, status=ProcessLeaseStatus.TERMINATING),
        envelope.revision,
    )

    report = compare_lease_parity(leases, shadow)

    assert report.in_sync is False
    assert report.content_mismatch == (_WORKER,)
    assert report.only_in_json == ()
    assert report.only_in_shadow == ()


def test_parity_detects_same_id_different_revision(tmp_path: Path) -> None:
    """Same logical content but different revision is not in_sync."""
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    envelope = leases.create(_lease())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    # Shadow has the same payload but a stale revision number.
    shadow.write_shadow(envelope.value, Revision(envelope.revision.value + 5))

    report = compare_lease_parity(leases, shadow)

    assert report.in_sync is False
    assert report.revision_mismatch == (_WORKER,)
    assert report.content_mismatch == ()


def test_parity_fails_closed_on_shadow_truncation(tmp_path: Path) -> None:
    """A shadow row hidden behind the scan bound must fail parity, never pass.

    The false positive the review found: the visible ID sets agree while the
    shadow holds an extra record past ``max_records``, so ``in_sync`` must fail
    closed on the shadow's own ``scan_complete`` instead of trusting the page.
    """
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    for index in range(1, 4):
        worker_id = f"worker-{index:012x}"
        leases.create(_lease(worker_id=worker_id))
        shadow.write_shadow(_lease(worker_id=worker_id), Revision(1))
    # The shadow holds one more lease than the parity scan bound can see.
    shadow.write_shadow(_lease(worker_id="worker-000000000004"), Revision(1))

    report = compare_lease_parity(leases, shadow, max_records=3)

    # Visible ID sets match, yet the shadow scan is incomplete: fail closed.
    assert report.only_in_json == ()
    assert report.only_in_shadow == ()
    assert report.json_scan_complete is True
    assert report.shadow_scan_complete is False
    assert report.in_sync is False


def test_parity_fails_closed_on_shadow_unreadable_record(tmp_path: Path) -> None:
    """A malformed shadow row must fail parity, never be silently dropped."""
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    leases.create(_lease())
    shadow.write_shadow(_lease(), Revision(1))
    # Corrupt the shadow row so it cannot be decoded into a ProcessLease.
    conn = sqlite3.connect(str(tmp_path / "shadow.db"))
    conn.execute(
        "UPDATE process_leases SET status = 'garbage' WHERE lease_id = ?",
        (_WORKER,),
    )
    conn.commit()
    conn.close()

    report = compare_lease_parity(leases, shadow)

    assert report.in_sync is False
    assert report.shadow_unreadable_ids == (_WORKER,)


def test_import_active_bindings_mirrors_only_active_records(tmp_path: Path) -> None:
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    bindings.put(_binding(worker_id="worker-000000000001", state="running"))
    bindings.put(_binding(worker_id="worker-000000000002", state="already_gone"))
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")

    imported = import_active_bindings(bindings, leases, shadow, now="2026-07-30T00:00:00+00:00")

    assert imported == 1
    page = shadow.list_page()
    assert [lease.lease_id for lease in page.records] == ["worker-000000000001"]


# ---------------------------------------------------------------------------
# WorkerRegistrar: the pre-spawn intent -> RUNNING lifecycle (F-001).
# ---------------------------------------------------------------------------


def _registrar(tmp_path: Path, *, shadow: object | None = None) -> tuple[object, object]:
    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        shadow=shadow,
    )
    return registrar, leases


def test_create_intent_records_a_registered_lease_before_any_pid(tmp_path: Path) -> None:
    registrar, leases = _registrar(tmp_path)

    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    assert lease.lease_id == "worker-" + "0" * 24
    assert lease.status is ProcessLeaseStatus.REGISTERED
    assert lease.pid is None
    stored = leases.read(lease.lease_id)
    assert stored is not None and stored.value == lease
    assert revision == stored.revision


def test_record_pid_persists_the_pid_immediately(tmp_path: Path) -> None:
    registrar, leases = _registrar(tmp_path)
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    updated, _ = registrar.record_pid(lease, pid=4242, expected_revision=revision)

    assert updated.pid == 4242
    assert updated.status is ProcessLeaseStatus.REGISTERED
    assert leases.read(lease.lease_id).value.pid == 4242


def test_complete_registration_reaches_running_via_ready(tmp_path: Path) -> None:
    registrar, leases = _registrar(tmp_path)
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )
    lease, revision = registrar.record_pid(lease, pid=4242, expected_revision=revision)

    running, _ = registrar.complete_registration(
        lease, process_identity="worker-start-token", expected_revision=revision
    )

    assert running.status is ProcessLeaseStatus.RUNNING
    assert running.pid == 4242
    assert leases.read(lease.lease_id).value.status is ProcessLeaseStatus.RUNNING


def test_complete_registration_requires_a_pid(tmp_path: Path) -> None:
    registrar, _ = _registrar(tmp_path)
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    from repoforge.domain.errors import ConfigError

    with pytest.raises(ConfigError, match="PID_MISSING"):
        registrar.complete_registration(
            lease,
            process_identity="worker-start-token",
            expected_revision=revision,
        )


def test_abort_intent_terminalizes_a_registered_lease(tmp_path: Path) -> None:
    registrar, leases = _registrar(tmp_path)
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    registrar.abort_intent(
        lease,
        error_code="EXECUTION_WORKER_REGISTRATION_FAILED",
        error_message="identity could not be proven",
        expected_revision=revision,
    )

    stored = leases.read(lease.lease_id)
    assert stored.value.status is ProcessLeaseStatus.TERMINATED
    assert stored.value.error_code == "EXECUTION_WORKER_REGISTRATION_FAILED"


def test_registrar_mirrors_every_write_into_the_shadow(tmp_path: Path) -> None:
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    registrar, _ = _registrar(tmp_path, shadow=shadow)
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )
    lease, revision = registrar.record_pid(lease, pid=4242, expected_revision=revision)
    registrar.complete_registration(
        lease, process_identity="worker-start-token", expected_revision=revision
    )

    page = shadow.list_page()
    assert len(page.records) == 1
    assert page.records[0].status is ProcessLeaseStatus.RUNNING
    assert page.records[0].pid == 4242


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
    """A worker registration writes the authoritative lease, the binding, and the shadow."""
    import repoforge.adapters.runtime.execution_worker as worker_module
    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.adapters.subprocess.process_tree import ProcessIdentity
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        shadow=shadow,
    )
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
        Path("/tmp/config.toml"), bindings=bindings, registrar=registrar
    )

    child = worker.start(
        generation=12,
        env={"REPOFORGE_RUNNING_RELEASE_SHA": "0123abc"},
        log_path=Path("/tmp/worker.log"),
        correlation_id="c" * 24,
    )

    assert child.pid == 4242
    # The authoritative JSON lease is durably RUNNING with the pid before return.
    page = leases.list_all()
    assert len(page.records) == 1
    lease = page.records[0].value
    assert lease.lease_id == "worker-" + "0" * 24
    assert lease.status is ProcessLeaseStatus.RUNNING
    assert lease.pid == 4242
    # The reconciler's binding shares the lease id, so both name the same worker.
    assert {b.worker_id for b in bindings.list_all()} == {lease.lease_id}
    # The shadow mirrors the authoritative lease.
    shadow_page = shadow.list_page()
    assert len(shadow_page.records) == 1
    assert shadow_page.records[0].lease_id == lease.lease_id
    assert shadow_page.records[0].role is ProcessLeaseRole.EXECUTION_DAEMON


def test_shadow_write_failure_never_fails_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shadow is parity evidence: a broken shadow must not refuse the worker."""
    import repoforge.adapters.runtime.execution_worker as worker_module
    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.adapters.subprocess.process_tree import ProcessIdentity
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    class _BrokenShadow:
        def write_shadow(self, lease: ProcessLease, revision: Revision) -> None:
            del lease, revision
            raise RuntimeError("shadow disk full")

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        shadow=_BrokenShadow(),
    )
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
        Path("/tmp/config.toml"), bindings=bindings, registrar=registrar
    )

    child = worker.start(
        generation=12,
        env={"REPOFORGE_RUNNING_RELEASE_SHA": "0123abc"},
        log_path=Path("/tmp/worker.log"),
        correlation_id="c" * 24,
    )

    assert child.pid == 4242
    assert len(bindings.list_all()) == 1
    assert len(leases.list_all().records) == 1


def test_record_pid_failure_reaps_the_spawned_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed durable pid write after Popen must reap the child and raise typed.

    The process is alive the instant Popen returns; if the pid cannot be persisted
    the lease stays REGISTERED without a pid and the process must not be left
    running untraceable. The adapter reaps via the group-aware reaper and raises
    the typed registration error so the supervisor fails closed.
    """
    import repoforge.adapters.runtime.execution_worker as worker_module
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.domain.errors import ExecutionWorkerRegistrationError
    from repoforge.ports.process_reaper import ReapOutcome
    from repoforge.testing import FixedClock, SequenceIdGenerator

    class _FailingPidLeases(JsonProcessLeaseAdapter):
        def save(self, lease, *, expected_revision):
            del lease, expected_revision
            raise OSError("durable pid write failed")

    class _ReapingReaper:
        def __init__(self) -> None:
            self.reaped_pids: list[int] = []

        def reap(self, binding: object) -> ReapOutcome:
            pid = getattr(binding, "child_pid", 0)
            self.reaped_pids.append(int(pid))
            return ReapOutcome(
                attempted=True, reaped=True, still_alive=False, detail="reaped via SIGKILL"
            )

        def read_start_token(self, pid: int) -> str | None:
            del pid
            return None

    leases = _FailingPidLeases(tmp_path / "leases", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    reaper = _ReapingReaper()
    monkeypatch.setattr(worker_module.subprocess, "Popen", lambda *a, **k: _fake_popen(4242))
    monkeypatch.setattr(worker_module, "process_identity", lambda pid: _SHA)
    monkeypatch.setattr(worker_module.time, "sleep", lambda seconds: None)
    worker = worker_module.SubprocessExecutionWorker(
        Path("/tmp/config.toml"),
        bindings=bindings,
        registrar=registrar,
        reaper=reaper,
    )

    with pytest.raises(ExecutionWorkerRegistrationError, match="REGISTRATION_FAILED"):
        worker.start(
            generation=12,
            env={},
            log_path=Path("/tmp/worker.log"),
            correlation_id="c" * 24,
        )

    assert reaper.reaped_pids == [4242]
    assert len(bindings.list_all()) == 0


# ---------------------------------------------------------------------------
# Admission epoch (P1-3): the registrar refuses spawns while admission is fenced.
# ---------------------------------------------------------------------------


def test_admission_epoch_round_trips_through_the_json_store(tmp_path: Path) -> None:
    from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from repoforge.ports.admission_epoch import ADMISSION_OPEN

    store = JsonAdmissionEpochStore(tmp_path / "state")

    epoch, state = store.read()
    assert epoch == 1
    assert state == ADMISSION_OPEN
    store.close()
    epoch, state = store.read()
    assert state != ADMISSION_OPEN
    reopened = store.open_next()
    assert reopened == epoch + 1
    assert store.read() == (reopened, ADMISSION_OPEN)


def test_registrar_refuses_a_new_spawn_while_admission_is_closed(tmp_path: Path) -> None:
    """P1-3: once a restarter has fenced admission, no new intent may be created."""
    from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.domain.errors import ConfigError
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    epochs = JsonAdmissionEpochStore(tmp_path / "state")
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        epochs=epochs,
    )
    epochs.close()

    with pytest.raises(ConfigError, match="WORKER_ADMISSION_REFUSED"):
        registrar.create_intent(role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24)

    assert len(leases.list_all().records) == 0


def test_registrar_stamps_the_open_epoch_onto_a_new_intent(tmp_path: Path) -> None:
    from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    epochs = JsonAdmissionEpochStore(tmp_path / "state")
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        epochs=epochs,
    )

    lease, _revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    assert lease.admission_epoch == 1


def test_create_intent_holds_the_admission_lock_across_read_and_create(
    tmp_path: Path,
) -> None:
    """P1-3: the OPEN check and the intent create are one atomic, locked section.

    A restarter fencing the epoch in another process must never interleave
    between the registrar's OPEN read and its lease write: either the intent
    lands before the fence (a fence member the restarter waits on) or the read
    observes CLOSING and the spawn is refused -- never a REGISTERED intent
    created from a stale OPEN read after the fence.
    """
    from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.ports.worker_registrar import WORKER_ADMISSION_LOCK
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    epochs = JsonAdmissionEpochStore(tmp_path / "state")
    shared_locks = InMemoryLockManager()
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        epochs=epochs,
        locks=shared_locks,
        admission_timeout_seconds=2.0,
    )

    created: list[ProcessLease] = []

    def spawn() -> None:
        lease, _ = registrar.create_intent(
            role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
        )
        created.append(lease)

    with shared_locks.lock(WORKER_ADMISSION_LOCK):
        # The restarter holds the fence; a spawn must not complete underneath it.
        import threading

        thread = threading.Thread(target=spawn)
        thread.start()
        thread.join(timeout=0.2)
        assert created == [], "a spawn completed while the admission fence was held"
    thread.join(timeout=2.0)
    assert len(created) == 1, "the spawn must proceed once the fence is released"
    assert created[0].admission_epoch == 1


def test_create_intent_fails_closed_when_the_admission_lock_times_out(
    tmp_path: Path,
) -> None:
    """P1-3: a wedged admission holder surfaces a typed LOCK_TIMEOUT, never a block."""
    from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.domain.errors import ConfigError
    from repoforge.ports.worker_registrar import WORKER_ADMISSION_LOCK
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    epochs = JsonAdmissionEpochStore(tmp_path / "state")
    shared_locks = InMemoryLockManager()
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        epochs=epochs,
        locks=shared_locks,
        admission_timeout_seconds=0.05,
    )

    with shared_locks.lock(WORKER_ADMISSION_LOCK), pytest.raises(ConfigError, match="LOCK_TIMEOUT"):
        registrar.create_intent(role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24)

    assert len(leases.list_all().records) == 0, "no intent may be created on a timeout"


def test_terminal_process_leases_are_archived_and_removed(tmp_path: Path) -> None:
    """A TERMINATED lease is archived durably and removed from the active scan."""
    from dataclasses import replace

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    envelope = leases.create(_lease())
    terminal = leases.save(
        replace(_lease(), status=ProcessLeaseStatus.TERMINATED),
        expected_revision=envelope.revision,
    )

    assert leases.archive_terminal(_WORKER, expected_revision=terminal.revision) is True

    active = leases.list_active_page()
    assert [lease.lease_id for lease in active.records] == []
    stored = leases.read(_WORKER)
    assert stored is not None and stored.value.status is ProcessLeaseStatus.TERMINATED
    assert leases.collect_terminal() == 0


def test_archive_terminal_refuses_a_live_lease(tmp_path: Path) -> None:
    """archive_terminal must never move a live (non-terminal) lease."""
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    envelope = leases.create(_lease())

    assert leases.archive_terminal(_WORKER, expected_revision=envelope.revision) is False

    assert leases.list_active_page().records
    assert leases.read(_WORKER) is not None


def test_parent_killed_after_popen_before_record_pid(tmp_path: Path) -> None:
    """A worker whose parent died before record_pid claims the pid-less lease (P0)."""
    import os

    from repoforge.application.runtime.child_lease_claim import (
        claim_child_lease,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    shadow = SqliteLeaseStore(tmp_path / "shadow.db")
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        shadow=shadow,
    )
    # The parent wrote the REGISTERED intent, then died before record_pid.
    lease, _ = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )
    assert lease.pid is None

    claim = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=999_999_999,
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha="0123abc",
        identity="d" * 64,
        start_token="child-start-token",
        now="2026-07-30T00:00:00+00:00",
    )

    assert claim.claimed is True
    stored = leases.read(lease.lease_id)
    assert stored is not None
    assert stored.value.status is ProcessLeaseStatus.RUNNING
    assert stored.value.pid == os.getpid()
    binding = bindings.get(lease.lease_id)
    assert binding is not None and binding.state == "running"
    assert any(item[0].lease_id == lease.lease_id for item in shadow.list_all())


def test_child_claim_acks_while_supervisor_alive_and_self_terminates_on_terminal(
    tmp_path: Path,
) -> None:
    """The claim never races a live parent and self-terminates a superseded worker."""
    import os

    from repoforge.application.runtime.child_lease_claim import (
        EXIT_SUPERSEDED,
        claim_child_lease,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    ack = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=os.getpid(),
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha=None,
        identity="d" * 64,
        start_token="child-start-token",
        now="2026-07-30T00:00:00+00:00",
    )

    assert ack.acked is True
    assert ack.exit_code is None
    assert leases.read(lease.lease_id).value.pid is None

    registrar.abort_intent(
        lease,
        error_code="EXECUTION_WORKER_REGISTRATION_FAILED",
        error_message="superseded",
        expected_revision=revision,
    )
    superseded = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=999_999_999,
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha=None,
        identity="d" * 64,
        start_token="child-start-token",
        now="2026-07-30T00:00:00+00:00",
    )

    assert superseded.claimed is False
    assert superseded.exit_code == EXIT_SUPERSEDED


# ---------------------------------------------------------------------------
# Re-review: child-side claim is fail-closed and crash-window safe (F-001 P0).
# ---------------------------------------------------------------------------


def test_reused_supervisor_pid_with_wrong_identity_is_treated_as_dead(
    tmp_path: Path,
) -> None:
    """A reused supervisor pid whose identity no longer matches is a DEAD owner.

    ``os.kill(pid, 0)`` alone cannot tell a live supervisor from an unrelated
    process that reused its pid. When a supervisor identity was recorded, the pid
    is only treated as alive when its CURRENT identity still matches -- anything
    else is dead, or the worker would skip the claim and run as an invisible
    orphan (review F-001 P0).
    """
    import os

    from repoforge.application.runtime.child_lease_claim import claim_child_lease
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    lease, _ = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    claim = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=os.getpid(),  # the pid EXISTS...
        supervisor_process_identity="0" * 64,  # ...but it is not the recorded owner
        generation=12,
        release_sha=None,
        identity="d" * 64,
        start_token="child-start-token",
        now="2026-07-30T00:00:00+00:00",
        supervisor_identity_reader=lambda pid: "f" * 64,  # a different process now
    )

    assert claim.claimed is True, "a reused pid is a dead owner, not a live one"
    stored = leases.read(lease.lease_id)
    assert stored is not None and stored.value.status is ProcessLeaseStatus.RUNNING


def test_dead_supervisor_and_missing_lease_self_terminates(tmp_path: Path) -> None:
    """A dead supervisor with NO lease must self-terminate, never run invisible.

    "The parent owns the lifecycle" stops being true the moment the parent is
    provably dead and no lease exists: no reconciler can ever discover this
    process through a record that does not exist, so running on creates an
    invisible orphan (review F-001 P0).
    """

    from repoforge.application.runtime.child_lease_claim import (
        EXIT_ORPHANED,
        claim_child_lease,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )

    claim = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id="worker-000000000000000000000000",
        supervisor_pid=999_999_999,
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha=None,
        identity="d" * 64,
        start_token="child-start-token",
        now="2026-07-30T00:00:00+00:00",
    )

    assert claim.claimed is False
    assert claim.acked is False
    assert claim.exit_code == EXIT_ORPHANED
    assert bindings.list_all() == ()


def test_unprovable_child_identity_self_terminates(tmp_path: Path) -> None:
    """An identity the worker cannot prove records the pid, then self-terminates.

    Continuing to run with no PID-reuse proof and no binding makes the worker an
    un-reclaimable permanent blocker; the safe resolution is to record the
    diagnostic pid and exit (review F-001 P0).
    """
    import os

    from repoforge.application.runtime.child_lease_claim import (
        EXIT_IDENTITY_UNPROVABLE,
        claim_child_lease,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    lease, _ = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    claim = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=999_999_999,
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha=None,
        identity=None,  # the worker cannot prove itself
        start_token=None,
        now="2026-07-30T00:00:00+00:00",
    )

    assert claim.exit_code == EXIT_IDENTITY_UNPROVABLE
    # The diagnostic pid is recorded so the lease stays discoverable...
    stored = leases.read(lease.lease_id)
    assert stored is not None and stored.value.pid == os.getpid()
    # ...but no binding is written and the worker must not start working.
    assert bindings.list_all() == ()


def test_child_claim_stops_at_ready_until_the_binding_is_written(
    tmp_path: Path,
) -> None:
    """The claim advances REGISTERED -> READY only; RUNNING waits for the binding.

    A crash between the claim and the binding write must leave READY-with-pid --
    recoverable from the canonical lease -- never a RUNNING lease with no
    projection the recovery path cannot reconstruct (review F-001 P0).
    """
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    lease, _ = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )

    claimed, claimed_revision = registrar.claim_intent(
        lease.lease_id,
        process_identity="d" * 64,
        pid=4242,
        pgid=4242,
        process_start_token="child-start-token",
    )

    assert claimed.status is ProcessLeaseStatus.READY, "claim stops at READY"
    assert claimed.pid == 4242
    assert claimed.lease_id == lease.lease_id

    running, _ = registrar.complete_claim(claimed, expected_revision=claimed_revision)
    assert running.status is ProcessLeaseStatus.RUNNING
    stored = leases.read(lease.lease_id)
    assert stored is not None and stored.value.status is ProcessLeaseStatus.RUNNING


def test_running_lease_ack_requires_matching_start_token(tmp_path: Path) -> None:
    """A RUNNING lease is acked only when its start token matches this worker.

    A different token means the lease was reused by another process; acking would
    run a duplicate, so the worker self-terminates with the lease-conflict exit
    (review F-001 P0).
    """
    import os
    from dataclasses import replace

    from repoforge.application.runtime.child_lease_claim import (
        EXIT_LEASE_CONFLICT,
        claim_child_lease,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, SequenceIdGenerator

    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
    )
    lease, revision = registrar.create_intent(
        role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
    )
    with_pid, pid_revision = registrar.record_pid(
        lease, pid=os.getpid(), expected_revision=revision
    )
    registrar.complete_registration(
        with_pid,
        process_identity="d" * 64,
        expected_revision=pid_revision,
        owner_pid=os.getpid(),
        owner_process_identity="0" * 64,
    )
    running = leases.read(lease.lease_id)
    assert running is not None and running.value.status is ProcessLeaseStatus.RUNNING
    leases.save(
        replace(running.value, process_start_token="lease-token-a"),
        expected_revision=running.revision,
    )

    conflict = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=999_999_999,
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha=None,
        identity="d" * 64,
        start_token="child-token-b",  # not the lease's token
        now="2026-07-30T00:00:00+00:00",
    )

    assert conflict.exit_code == EXIT_LEASE_CONFLICT
    assert conflict.acked is False

    # A matching token is acknowledged: the worker is already registered.
    ack = claim_child_lease(
        leases=leases,
        registrar=registrar,
        bindings=bindings,
        lease_id=lease.lease_id,
        supervisor_pid=999_999_999,
        supervisor_process_identity="0" * 64,
        generation=12,
        release_sha=None,
        identity="d" * 64,
        start_token="lease-token-a",
        now="2026-07-30T00:00:00+00:00",
    )
    assert ack.acked is True
    assert ack.exit_code is None
