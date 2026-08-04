"""PID-safe execution-worker reconciliation (#368).

Orphaned execution workers (spawned by a supervisor that died) must be reclaimed
before a new supervisor starts, or they hold operation locks and block convergence.
The reconciler proves each worker's identity and entry point before signalling, and
fails closed -- never killing by pattern -- when the process table or identity cannot
be proven.
"""

from __future__ import annotations

from pathlib import Path

from repoforge.adapters.subprocess.process_tree import ProcessIdentity
from repoforge.application.runtime.execution_worker_reconciler import (
    ExecutionWorkerReclamationReport,
    ExecutionWorkerReconciler,
)
from repoforge.domain.execution_worker import ExecutionWorkerBinding
from repoforge.ports.process_reaper import ReapOutcome

_SHA = "a" * 64
_RELEASE = "0123abc"
_RELEASE_OTHER = "9999fff"
_WORKER = "worker-0123456789ab"
_WORKER_OTHER = "worker-ffffffffffab"


def _binding(
    *,
    worker_id: str = _WORKER,
    pid: int = 4242,
    release_sha: str | None = _RELEASE,
    supervisor_pid: int = 4241,
    token: str | None = "worker-start-token",
    state: str = "running",
) -> ExecutionWorkerBinding:
    return ExecutionWorkerBinding(
        worker_id=worker_id,
        pid=pid,
        pgid=pid,
        process_start_token=token,
        generation=12,
        release_sha=release_sha,
        supervisor_pid=supervisor_pid,
        supervisor_process_identity=_SHA,
        correlation_id="c" * 24,
        started_at="2026-07-29T09:26:21+00:00",
        state=state,
    )


class _Bindings:
    def __init__(
        self,
        *bindings: ExecutionWorkerBinding,
        scan_complete: bool = True,
        unreadable_ids: tuple[str, ...] = (),
    ) -> None:
        self.records = {binding.worker_id: binding for binding in bindings}
        self.states: dict[str, list[str]] = {}
        self.scan_complete = scan_complete
        self.unreadable_ids = unreadable_ids

    def put(self, binding: ExecutionWorkerBinding) -> None:
        self.records[binding.worker_id] = binding

    def get(self, worker_id: str) -> ExecutionWorkerBinding | None:
        return self.records.get(worker_id)

    def list_page(self, *, max_records: int = 2_000):
        del max_records
        from repoforge.ports.execution_worker_store import ExecutionWorkerBindingPage

        return ExecutionWorkerBindingPage(
            records=tuple(self.records.values()),
            scan_complete=self.scan_complete,
            unreadable_ids=self.unreadable_ids,
        )

    def update_state(self, worker_id: str, state: str) -> ExecutionWorkerBinding | None:
        self.states.setdefault(worker_id, []).append(state)
        if worker_id not in self.records:
            return None
        from dataclasses import replace

        from repoforge.domain.execution_worker import (
            execution_worker_binding_from_payload,
            execution_worker_binding_payload,
        )

        updated = execution_worker_binding_from_payload(
            execution_worker_binding_payload(replace(self.records[worker_id], state=state))
        )
        self.records[worker_id] = updated
        return updated

    def list_all(self, *, max_records: int = 2_000) -> tuple[ExecutionWorkerBinding, ...]:
        del max_records
        return tuple(self.records.values())

    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        del max_records
        return 0


class _Owner:
    """Owner liveness: `process_identity(pid) == recorded identity`."""

    def __init__(self, alive_pids: set[int]) -> None:
        self.alive_pids = alive_pids

    def __call__(self, pid: int) -> str | None:
        return _SHA if pid in self.alive_pids else None


class _CommandLines:
    """`read_command_line(pid)` -> argv, or None when unreadable."""

    def __init__(self, mapping: dict[int, tuple[str, ...] | None]) -> None:
        self.mapping = mapping

    def __call__(self, pid: int) -> tuple[str, ...] | None:
        return self.mapping.get(pid)


class _Tokens:
    """`read_identity(pid)` -> a ProcessIdentity carrying the start token."""

    def __init__(self, tokens: dict[int, str | None]) -> None:
        self.tokens = tokens

    def __call__(self, pid: int) -> ProcessIdentity | None:
        token = self.tokens.get(pid)
        if token is None:
            return None
        return ProcessIdentity(pid=pid, ppid=1, start_token=token)


class _Reaper:
    def __init__(self, outcomes: list[ReapOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[object] = []

    def reap(self, binding: object) -> ReapOutcome:
        self.calls.append(binding)
        return self.outcomes.pop(0) if self.outcomes else ReapOutcome(False, False, False, "none")

    def read_start_token(self, pid: int) -> str | None:
        del pid
        return None


_EXECUTION_WORKER_ARGV = (
    "/opt/repoforge/venv/bin/python",
    "-m",
    "repoforge.interfaces.runtime.execution_worker",
    "--config",
    "/home/dev/config.toml",
    "--generation",
    "12",
)


def _reconciler(
    *,
    bindings: _Bindings,
    reaper: _Reaper,
    owner: _Owner,
    command_lines: _CommandLines,
    tokens: _Tokens,
    group_gone=None,
) -> ExecutionWorkerReconciler:
    return ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=reaper,
        owner_identity_reader=owner,
        command_line_reader=command_lines,
        identity_reader=tokens,
        process_group_gone=group_gone,
    )


def test_reconcile_leaves_workers_of_a_live_owner_alone() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner({4241}),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.inspected == 1
    assert report.reclaimed == 0
    assert reaper.calls == []


def test_registry_digest_changes_when_the_lease_set_changes() -> None:
    """The fence digest must detect any live-concern change (F-004)."""
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    owner = _Owner({4241})
    command_lines = _CommandLines({4242: _EXECUTION_WORKER_ARGV})
    tokens = _Tokens({4242: "worker-start-token"})
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=owner,
        command_lines=command_lines,
        tokens=tokens,
    )

    first = reconciler.reconcile(read_only=True)

    # A second worker registered after the first scan must change the digest.
    bindings.put(_binding(worker_id=_WORKER_OTHER, pid=4243, supervisor_pid=4244))
    second = reconciler.reconcile(read_only=True)

    assert first.registry_digest
    assert second.registry_digest != first.registry_digest

    # Terminal history does not participate: a collected terminal lease is not a
    # live concern and must not invalidate the fence.
    bindings = _Bindings(_binding(worker_id=_WORKER_OTHER, state="already_gone"))
    base = reconciler.reconcile(read_only=True)
    bindings.put(_binding(worker_id=_WORKER_OTHER, state="reclaimed"))
    changed = reconciler.reconcile(read_only=True)
    assert changed.registry_digest == base.registry_digest


def test_reconcile_reaps_an_orphaned_execution_worker() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 1
    assert report.pids == (4242,)
    assert report.release_shas == (_RELEASE,)
    assert bindings.records[_WORKER].state == "reclaimed"
    assert len(reaper.calls) == 1


def test_reconcile_counts_an_already_gone_worker() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([ReapOutcome(False, True, False, "already gone")])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.already_gone == 1
    assert bindings.records[_WORKER].state == "already_gone"


def test_reconcile_refuses_an_unreadable_command_line() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: None}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    assert reaper.calls == []
    assert bindings.records[_WORKER].state == "refused_unproven"


def test_reconcile_refuses_a_non_execution_worker_process() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines(
            {
                4242: (
                    "/opt/repoforge/venv/bin/python",
                    "-m",
                    "repoforge.interfaces.mcp.server",
                )
            }
        ),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    assert reaper.calls == []


def test_reconcile_refuses_on_a_recycled_pid() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "a-different-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    assert reaper.calls == []


def test_reconcile_reaps_departing_release_workers_even_with_a_live_owner() -> None:
    bindings = _Bindings(
        _binding(),
        _binding(
            worker_id=_WORKER_OTHER, pid=5252, release_sha=_RELEASE_OTHER, supervisor_pid=5251
        ),
    )
    reaper = _Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner({4241, 5251}),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV, 5252: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token", 5252: "worker-start-token"}),
    )

    report = reconciler.reconcile(departing_releases=frozenset({_RELEASE}))

    assert report.reclaimed == 1
    assert report.worker_ids == (_WORKER,)
    assert bindings.records[_WORKER_OTHER].state == "running"


def test_reconcile_marks_a_worker_that_survived_kill() -> None:
    bindings = _Bindings(_binding())
    reaper = _Reaper([ReapOutcome(True, False, True, "survived SIGKILL")])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.survived_kill == 1
    assert bindings.records[_WORKER].state == "survived_kill"


def test_report_serializes_the_reclamation_evidence() -> None:
    report = ExecutionWorkerReclamationReport(
        inspected=3,
        reclaimed=1,
        already_gone=1,
        refused_unproven=1,
        survived_kill=0,
        possibly_alive_unproven=1,
        scan_complete=True,
        unreadable_record_ids=(),
        worker_ids=(_WORKER,),
        pids=(4242,),
        release_shas=(_RELEASE,),
        detail="reconciliation complete",
    )
    payload = report.as_dict()
    assert payload["inspected"] == 3
    assert payload["reclaimed"] == 1
    assert payload["possibly_alive_unproven"] == 1
    assert payload["scan_complete"] is True
    assert payload["unreadable_record_ids"] == []
    assert payload["evidence_complete"] is True
    assert payload["blocker_code"] == "STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN"
    assert payload["worker_ids"] == [_WORKER]
    assert payload["release_shas"] == [_RELEASE]


def test_reclaim_report_exposes_a_typed_blocker_code() -> None:
    """The report names the exact fail-closed gate, single-sourced across callers (#424)."""
    clean = _reconciler(
        bindings=_Bindings(_binding()),
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    ).reconcile()
    assert clean.blocker_code is None

    unreadable = _reconciler(
        bindings=_Bindings(_binding(), scan_complete=True, unreadable_ids=("worker-bad-1",)),
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    ).reconcile()
    assert unreadable.blocker_code == "EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS"

    truncated = _reconciler(
        bindings=_Bindings(_binding(), scan_complete=False),
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    ).reconcile()
    assert truncated.blocker_code == "EXECUTION_WORKER_REGISTRY_SCAN_INCOMPLETE"


def test_reconciler_reaps_a_real_orphaned_execution_worker(tmp_path) -> None:
    """Real chain: spawn an execution worker, orphan its owner, reconcile reaps it."""
    import os
    import time

    from conftest import create_forge_environment

    from repoforge.adapters.persistence import JsonExecutionWorkerBindingStore
    from repoforge.adapters.runtime.execution_worker import SubprocessExecutionWorker
    from repoforge.adapters.runtime.state_store import process_identity
    from repoforge.adapters.subprocess.os_process_reaper import OsProcessReaper
    from repoforge.adapters.subprocess.process_tree import read_command_line, read_identity
    from repoforge.bootstrap import build_configuration_store
    from repoforge.domain.execution_worker import (
        execution_worker_binding_from_payload,
        execution_worker_binding_payload,
    )
    from repoforge.testing import InMemoryLockManager
    from repoforge.testing.fakes import InMemoryWorkerRegistrar

    env = create_forge_environment(tmp_path)
    home = tmp_path / "home"
    build_configuration_store(
        env.config_path, state_root=home / ".local/state/repoforge"
    ).import_legacy(
        env.config_path.read_text(encoding="utf-8"),
        env.config_path.read_text(encoding="utf-8"),
        created_at="2026-07-29T00:00:00+00:00",
    )
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    spawner = SubprocessExecutionWorker(
        env.config_path, bindings=bindings, registrar=InMemoryWorkerRegistrar()
    )
    child = spawner.start(
        1,
        env=dict(os.environ, HOME=str(home)),
        log_path=tmp_path / "worker.log",
        correlation_id="c" * 24,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if spawner.is_alive(child):
                break
            time.sleep(0.1)
        if not spawner.is_alive(child):
            log_tail = (tmp_path / "worker.log").read_text(encoding="utf-8", errors="replace")[
                -2000:
            ]
            raise AssertionError(f"the real execution worker did not start: {log_tail}")

        binding = next(item for item in bindings.list_all() if item.pid == child.pid)
        assert binding.state == "running"
        orphaned = execution_worker_binding_from_payload(
            execution_worker_binding_payload(binding)
            | {"supervisor_pid": 99999999, "supervisor_process_identity": "a" * 64}
        )
        bindings.put(orphaned)

        reconciler = ExecutionWorkerReconciler(
            bindings=bindings,
            reaper=OsProcessReaper(),
            owner_identity_reader=process_identity,
            command_line_reader=read_command_line,
            identity_reader=read_identity,
        )
        report = reconciler.reconcile()

        assert report.reclaimed == 1
        # The reclaimed lease is archived and removed from the active registry (#424).
        assert bindings.get(orphaned.worker_id) is None
        archived = bindings.list_archive()
        assert any(item.worker_id == orphaned.worker_id for item in archived)
        assert spawner.is_alive(child) is False
    finally:
        spawner.terminate(child, grace_seconds=3)


def test_reconcile_skips_terminal_state_bindings() -> None:
    """Only `running` bindings are live concerns; terminal ones are history (#420)."""
    bindings = _Bindings(
        _binding(state="reclaimed"),
        _binding(worker_id=_WORKER_OTHER, pid=5252, state="already_gone"),
    )
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV, 5252: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token", 5252: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.inspected == 0
    assert report.reclaimed == 0
    assert reaper.calls == []


def test_reconcile_never_reaps_a_tokenless_binding() -> None:
    """A binding without a start token can never prove PID-reuse safety (#420)."""
    bindings = _Bindings(_binding(token=None))
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
        group_gone=lambda pgid: False,
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    assert report.possibly_alive_unproven == 1
    assert reaper.calls == []
    assert bindings.records[_WORKER].state == "refused_unproven"


def test_reconcile_marks_provably_gone_unclassified_as_already_gone() -> None:
    """An unclassifiable worker whose process AND group are proven gone is safe (#420)."""
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: None}),
        tokens=_Tokens({4242: None}),
        group_gone=lambda pgid: True,
    )

    report = reconciler.reconcile()

    assert report.already_gone == 1
    assert report.possibly_alive_unproven == 0
    assert reaper.calls == []


def test_reconcile_reports_possibly_alive_unproven() -> None:
    """An unclassifiable worker that may still run must be flagged, not ignored (#420)."""
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: None}),
        tokens=_Tokens({4242: "worker-start-token"}),
        group_gone=lambda pgid: False,
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    assert report.possibly_alive_unproven == 1
    assert reaper.calls == []


def test_reconcile_reports_an_incomplete_registry_scan() -> None:
    bindings = _Bindings(_binding(), scan_complete=False)
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.scan_complete is False


def test_reconcile_flags_unreadable_records_as_incomplete_evidence() -> None:
    """An unreadable registry record hides an orphan like a truncation does (#420)."""
    bindings = _Bindings(
        _binding(), scan_complete=True, unreadable_ids=("worker-bad-1", "worker-bad-2")
    )
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.scan_complete is True
    assert report.unreadable_record_ids == ("worker-bad-1", "worker-bad-2")
    assert report.evidence_complete is False


def test_reconcile_re_evaluates_a_refused_binding_that_is_still_alive() -> None:
    """A previously refused worker that may still run blocks every later pass (#420)."""
    bindings = _Bindings(_binding(state="refused_unproven", token=None))
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
        group_gone=lambda pgid: False,
    )

    report = reconciler.reconcile()

    assert report.possibly_alive_unproven == 1
    assert report.evidence_complete is True
    assert reaper.calls == []


def test_reconcile_marks_a_refused_binding_whose_process_left_as_already_gone() -> None:
    """A refused worker whose process AND group are now gone becomes already_gone."""
    bindings = _Bindings(_binding(state="refused_unproven", token=None))
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: None}),
        tokens=_Tokens({4242: None}),
        group_gone=lambda pgid: True,
    )

    report = reconciler.reconcile()

    assert report.already_gone == 1
    assert report.possibly_alive_unproven == 0
    assert bindings.records[_WORKER].state == "already_gone"


def test_reconcile_read_only_does_not_reap_or_mark() -> None:
    """A read-only pass answers the blocker questions with no side effects (#424)."""
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile(read_only=True)

    assert report.evidence_complete is True
    assert report.possibly_alive_unproven == 0
    assert reaper.calls == []
    assert bindings.records[_WORKER].state == "running"
    assert bindings.states == {}


def test_reconcile_read_only_flags_a_survived_kill_lease_as_blocker() -> None:
    """A previous kill that failed stays a blocker in read-only preflight (#424)."""
    bindings = _Bindings(_binding(state="survived_kill"))
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile(read_only=True)

    assert report.survived_kill == 1
    assert reaper.calls == []
    assert bindings.records[_WORKER].state == "survived_kill"


def test_restarter_blocks_on_possibly_alive_unproven() -> None:
    """Not killing is not safety: an unproven worker blocks the replacement start."""
    from repoforge.adapters.activation.build import SupervisorRestarter

    class _Reconciler:
        def reconcile(self, *, departing_releases=frozenset(), read_only: bool = False):
            del departing_releases, read_only
            return ExecutionWorkerReclamationReport(
                inspected=1,
                reclaimed=0,
                already_gone=0,
                refused_unproven=1,
                survived_kill=0,
                possibly_alive_unproven=1,
                scan_complete=True,
                unreadable_record_ids=(),
                worker_ids=(_WORKER,),
                pids=(4242,),
                release_shas=(_RELEASE,),
                detail="worker identity unproven",
            )

    restarter = SupervisorRestarter(
        control=None,
        runtime=None,
        launcher=None,
        config_path=Path("/tmp/config.toml"),
        correlation_id="c" * 24,
        worker_reconciler=_Reconciler(),
    )
    ok, detail, evidence = restarter._reclaim_departing(_RELEASE)

    assert ok is False
    assert "STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN" in detail
    assert evidence is not None and evidence["possibly_alive_unproven"] == 1


def test_restarter_blocks_on_an_incomplete_scan() -> None:
    from repoforge.adapters.activation.build import SupervisorRestarter

    class _Reconciler:
        def reconcile(self, *, departing_releases=frozenset(), read_only: bool = False):
            del departing_releases, read_only
            return ExecutionWorkerReclamationReport(
                inspected=0,
                reclaimed=0,
                already_gone=0,
                refused_unproven=0,
                survived_kill=0,
                possibly_alive_unproven=0,
                scan_complete=False,
                unreadable_record_ids=(),
                worker_ids=(),
                pids=(),
                release_shas=(),
                detail="scan truncated",
            )

    restarter = SupervisorRestarter(
        control=None,
        runtime=None,
        launcher=None,
        config_path=Path("/tmp/config.toml"),
        correlation_id="c" * 24,
        worker_reconciler=_Reconciler(),
    )
    ok, detail, evidence = restarter._reclaim_departing(_RELEASE)

    assert ok is False
    assert "EXECUTION_WORKER_REGISTRY_SCAN_INCOMPLETE" in detail
    assert evidence is not None and evidence["scan_complete"] is False


def test_restarter_blocks_on_unreadable_registry_records() -> None:
    """An unreadable record is incomplete evidence: the replacement must not start."""
    from repoforge.adapters.activation.build import SupervisorRestarter

    class _Reconciler:
        def reconcile(self, *, departing_releases=frozenset(), read_only: bool = False):
            del departing_releases, read_only
            return ExecutionWorkerReclamationReport(
                inspected=0,
                reclaimed=0,
                already_gone=0,
                refused_unproven=0,
                survived_kill=0,
                possibly_alive_unproven=0,
                scan_complete=True,
                unreadable_record_ids=("worker-bad-1",),
                worker_ids=(),
                pids=(),
                release_shas=(),
                detail="registry contains unreadable records",
            )

    restarter = SupervisorRestarter(
        control=None,
        runtime=None,
        launcher=None,
        config_path=Path("/tmp/config.toml"),
        correlation_id="c" * 24,
        worker_reconciler=_Reconciler(),
    )
    ok, detail, evidence = restarter._reclaim_departing(_RELEASE)

    assert ok is False
    assert "EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS" in detail
    assert evidence is not None and evidence["unreadable_record_ids"] == ["worker-bad-1"]
    assert evidence["evidence_complete"] is False


def test_restarter_preflight_is_read_only_and_gated() -> None:
    """The handoff preflight uses a read-only reconcile and applies the same gates."""
    from repoforge.adapters.activation.build import SupervisorRestarter

    calls: list[bool] = []

    class _Reconciler:
        def reconcile(self, *, departing_releases=frozenset(), read_only: bool = False):
            del departing_releases
            calls.append(read_only)
            return ExecutionWorkerReclamationReport(
                inspected=0,
                reclaimed=0,
                already_gone=0,
                refused_unproven=0,
                survived_kill=0,
                possibly_alive_unproven=0,
                scan_complete=True,
                unreadable_record_ids=("worker-bad-1",),
                worker_ids=(),
                pids=(),
                release_shas=(),
                detail="registry contains unreadable records",
            )

    restarter = SupervisorRestarter(
        control=None,
        runtime=None,
        launcher=None,
        config_path=Path("/tmp/config.toml"),
        correlation_id="c" * 24,
        worker_reconciler=_Reconciler(),
    )
    ok, detail, evidence = restarter.preflight_reclaim(_RELEASE)

    assert ok is False
    assert "EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS" in detail
    assert calls == [True]
    assert evidence is not None and evidence["evidence_complete"] is False


def test_terminate_marks_reclaimed_only_after_confirmed_death(tmp_path) -> None:
    """`reclaimed` records verified death, not the intent to terminate (#420)."""
    import os
    import time

    from conftest import create_forge_environment

    from repoforge.adapters.persistence import JsonExecutionWorkerBindingStore
    from repoforge.adapters.runtime.execution_worker import SubprocessExecutionWorker
    from repoforge.bootstrap import build_configuration_store
    from repoforge.testing import InMemoryLockManager
    from repoforge.testing.fakes import InMemoryWorkerRegistrar

    env = create_forge_environment(tmp_path)
    home = tmp_path / "home"
    build_configuration_store(
        env.config_path, state_root=home / ".local/state/repoforge"
    ).import_legacy(
        env.config_path.read_text(encoding="utf-8"),
        env.config_path.read_text(encoding="utf-8"),
        created_at="2026-07-29T00:00:00+00:00",
    )
    bindings = JsonExecutionWorkerBindingStore(tmp_path / "state", InMemoryLockManager())
    spawner = SubprocessExecutionWorker(
        env.config_path, bindings=bindings, registrar=InMemoryWorkerRegistrar()
    )
    child = spawner.start(
        1,
        env=dict(os.environ, HOME=str(home)),
        log_path=tmp_path / "worker.log",
        correlation_id="c" * 24,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not spawner.is_alive(child):
            time.sleep(0.1)
        assert spawner.is_alive(child), "the real execution worker did not start"

        spawner.terminate(child, grace_seconds=3)

        # `reclaimed` verifies death; the terminal lease is archived and removed (#424).
        archived = bindings.list_archive()
        assert any(item.pid == child.pid and item.state == "reclaimed" for item in archived)
        assert (
            bindings.get(next((item.worker_id for item in archived if item.pid == child.pid), ""))
            is None
        )
        assert spawner.is_alive(child) is False
    finally:
        spawner.terminate(child, grace_seconds=1)


# ---------------------------------------------------------------------------
# Hotfix 1: ProcessLease containment gate.
# ---------------------------------------------------------------------------


def _lease_envelope(lease, *, revision: int = 1):
    from repoforge.domain.durable_state import Revision, SchemaVersion, StateEnvelope

    return StateEnvelope(
        record_id=lease.lease_id,
        schema_version=SchemaVersion(1),
        value=lease,
        revision=Revision(revision),
    )


def _lease_store(leases):

    class _LeaseStore:
        def __init__(self, records):
            self._records = records

        def list_page(self, *, role=None, max_records=2_000):
            from repoforge.ports.process_lease_store import ProcessLeasePage

            if role is not None:
                filtered = tuple(lease for lease in self._records if lease.role is role)
            else:
                filtered = self._records
            return ProcessLeasePage(records=filtered, scan_complete=True, unreadable_ids=())

        def read(self, lease_id):

            for lease in self._records:
                if lease.lease_id == lease_id:
                    return _lease_envelope(lease, revision=1)
            return None

        def save(self, lease, *, expected_revision):
            return _lease_envelope(lease, revision=expected_revision.value + 1)

    return _LeaseStore(leases)


def test_startup_blocks_on_registered_lease_without_pid() -> None:
    """A REGISTERED ProcessLease with no pid is an incomplete safety concern.

    Pre-spawn crash window: parent died after create_intent and never reached
    record_pid. The reconciler must surface this as a blocker before a
    replacement can be started.
    """
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.REGISTERED,
        process_identity=None,
        pid=None,
        started_at=None,
        heartbeat_at=None,
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        role=ProcessLeaseRole.EXECUTION_DAEMON,
    )
    bindings = _Bindings()
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({}),
        identity_reader=_Tokens({}),
        leases=_lease_store([lease]),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_incomplete == 1
    assert report.blocker_code == "PROCESS_LEASE_INCOMPLETE"


def test_read_only_preflight_blocks_on_a_registered_intent_that_can_claim_later() -> None:
    """P1-3 admission race: a REGISTERED/READY intent is a durable fence member.

    The re-review repro: the intent exists (REGISTERED) before the final fence;
    the final preflight sees it as a live concern; a child that claims READY after
    the incumbent is stopped must never be possible. The fence member blocks the
    stop -- REGISTERED counts as incomplete even when it carries a pid.
    """
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.REGISTERED,
        process_identity=None,
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        role=ProcessLeaseRole.EXECUTION_DAEMON,
    )
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=_lease_store([lease]),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_incomplete == 1
    assert report.blocker_code == "PROCESS_LEASE_INCOMPLETE"


def test_startup_blocks_on_process_lease_without_binding() -> None:
    """An active ProcessLease without a matching binding diverges from the registry."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id="worker-aaaaaaaaaaaa",
        status=ProcessLeaseStatus.RUNNING,
        process_identity="worker-start-token",
        pid=9999,
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        role=ProcessLeaseRole.EXECUTION_DAEMON,
    )
    bindings = _Bindings()
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({}),
        identity_reader=_Tokens({}),
        leases=_lease_store([lease]),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_binding_divergence == 1
    assert report.blocker_code == "PROCESS_LEASE_BINDING_DIVERGENCE"


def test_process_lease_terminalized_and_archived_with_binding() -> None:
    """Marking a binding reclaimed advances the matching ProcessLease to TERMINATED."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        process_identity="worker-start-token",
        pid=4242,
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        role=ProcessLeaseRole.EXECUTION_DAEMON,
    )
    recorded_saves: list[object] = []

    class _RecordingLeaseStore:
        def list_page(self, *, role=None, max_records=2_000):
            from repoforge.ports.process_lease_store import ProcessLeasePage

            return ProcessLeasePage(records=(lease,), scan_complete=True, unreadable_ids=())

        def read(self, lease_id):
            if lease_id == lease.lease_id:
                return _lease_envelope(lease, revision=3)
            return None

        def save(self, lease, *, expected_revision):
            recorded_saves.append(lease)
            return _lease_envelope(lease, revision=expected_revision.value + 1)

    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=_RecordingLeaseStore(),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 1
    # RUNNING -> TERMINATING -> TERMINATED.
    statuses = [getattr(lease_candidate, "status", None) for lease_candidate in recorded_saves]
    assert ProcessLeaseStatus.TERMINATING in statuses
    assert ProcessLeaseStatus.TERMINATED in statuses


def test_single_authority_reclaim_terminalizes_the_lease_without_a_binding() -> None:
    """A binding-backed reclaim must terminalize the authoritative lease (P0-1).

    The re-review found the split-brain: a worker was reclaimed and its binding
    archived while its ProcessLease stayed RUNNING -- the lease then blocked the
    next release forever. The authoritative lease must advance to TERMINATED with
    the binding, so the two can never diverge.
    """
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        pgid=4242,
        process_start_token="worker-start-token",
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    saved_statuses: list[object] = []

    class _LeaseStore:
        def list_page(self, *, role=None, max_records=2_000):
            from repoforge.ports.process_lease_store import ProcessLeasePage

            del role, max_records
            return ProcessLeasePage(records=(lease,), scan_complete=True, unreadable_ids=())

        def read(self, lease_id):
            if lease_id == lease.lease_id:
                return _lease_envelope(lease, revision=1)
            return None

        def save(self, candidate, *, expected_revision):
            saved_statuses.append(candidate.status)
            return _lease_envelope(candidate, revision=expected_revision.value + 1)

    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=_LeaseStore(),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 1
    assert ProcessLeaseStatus.TERMINATED in saved_statuses


def test_same_worker_id_same_state_changed_pid_changes_registry_digest() -> None:
    """Hotfix 3: digest must invalidate when pid/start_token of a live binding changes."""
    bindings = _Bindings(_binding())
    reaper = _Reaper([])
    reconciler = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    base = reconciler.reconcile(read_only=True).registry_digest

    # Same worker_id + state but pid changed (the same logical worker could not be
    # the same process). The handoff fence must detect this.
    bindings = _Bindings(_binding(pid=5500))
    reconciler2 = _reconciler(
        bindings=bindings,
        reaper=reaper,
        owner=_Owner(set()),
        command_lines=_CommandLines({5500: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({5500: "worker-start-token"}),
    )
    changed = reconciler2.reconcile(read_only=True).registry_digest

    assert base
    assert base != changed


# ---------------------------------------------------------------------------
# Re-review: single-authority lifecycle (P0-1) and bidirectional containment.
# ---------------------------------------------------------------------------


class _RecordingLeaseStore:
    """Lease store that records every save and can fail on demand (F-010 tests)."""

    def __init__(self, *leases, failing: bool = False) -> None:
        from repoforge.domain.process_lease import ProcessLease

        self._records: dict[str, object] = {
            lease.lease_id: _lease_envelope(lease) for lease in leases
        }
        self.saved: list[ProcessLease] = []
        self.archived: list[str] = []
        self.failing = failing

    def list_page(self, *, role=None, max_records=2_000):
        from repoforge.ports.process_lease_store import ProcessLeasePage

        del max_records
        values = [env.value for env in self._records.values()]
        if role is not None:
            values = [lease for lease in values if lease.role is role]
        return ProcessLeasePage(records=tuple(values), scan_complete=True, unreadable_ids=())

    def read(self, lease_id):
        return self._records.get(lease_id)

    def save(self, lease, *, expected_revision):
        if self.failing:
            raise OSError("disk full")
        self.saved.append(lease)
        envelope = _lease_envelope(lease, revision=expected_revision.value + 1)
        self._records[lease.lease_id] = envelope
        return envelope

    def archive_terminal(self, lease_id, *, expected_revision):
        del expected_revision
        self.archived.append(lease_id)
        return True


def test_survived_kill_keeps_process_lease_killed_and_active() -> None:
    """A process proven alive after SIGKILL is never recorded as TERMINATED (P0)."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, False, True, "survived SIGKILL")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.survived_kill == 1
    statuses = [candidate.status for candidate in store.saved]
    assert ProcessLeaseStatus.KILLED in statuses
    assert ProcessLeaseStatus.TERMINATED not in statuses
    assert store.archived == []
    assert bindings.get(_WORKER).state == "survived_kill"


def test_refused_unproven_keeps_process_lease_unproven_and_active() -> None:
    """An unprovable worker is recorded as UNPROVEN, never as TERMINATED (P0)."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    statuses = [candidate.status for candidate in store.saved]
    assert ProcessLeaseStatus.UNPROVEN in statuses
    assert ProcessLeaseStatus.TERMINATED not in statuses
    assert store.archived == []
    assert bindings.get(_WORKER).state == "refused_unproven"


def test_later_proof_of_death_terminalizes_killed_lease() -> None:
    """A KILLED lease becomes TERMINATED only on a later proof of death."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.KILLED,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings(_binding(state="survived_kill"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({}),
        identity_reader=_Tokens({}),
        process_group_gone=lambda pgid: True,
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.already_gone == 1
    statuses = [candidate.status for candidate in store.saved]
    assert ProcessLeaseStatus.TERMINATED in statuses
    assert store.archived == [_WORKER]


def test_active_binding_without_active_process_lease_is_divergence() -> None:
    """A running binding with no lease at all diverges from the registry."""
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=_lease_store([]),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_binding_divergence == 1
    assert report.blocker_code == "PROCESS_LEASE_BINDING_DIVERGENCE"


def test_active_binding_with_terminal_process_lease_is_divergence() -> None:
    """A running binding whose lease is already terminal is a split-brain."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.TERMINATED,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=_lease_store([lease]),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_binding_divergence == 1
    assert report.blocker_code == "PROCESS_LEASE_BINDING_DIVERGENCE"


def test_process_identity_mismatch_is_divergence() -> None:
    """A lease and binding naming different start tokens diverge."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="lease-identity",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="lease-token-a",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    bindings = _Bindings(_binding(state="running", token="binding-token-b"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "binding-token-b"}),
        leases=_lease_store([lease]),
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_binding_divergence == 1
    assert report.blocker_code == "PROCESS_LEASE_BINDING_DIVERGENCE"


def test_persistence_failure_is_reported_not_suppressed() -> None:
    """A reaped worker whose lease write fails is reported, never claimed reclaimed."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease, failing=True)
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 0
    assert report.persistence_failures == 1
    assert report.persistence_failure_ids == (_WORKER,)
    assert report.blocker_code == "PROCESS_LEASE_PERSISTENCE_FAILURE"


def _real_spawner(tmp_path: Path, *, shadow: object | None = None):
    """A real worker wired to the real registrar + JSON lease registry (F-010)."""

    from conftest import create_forge_environment

    from repoforge.adapters.persistence import JsonExecutionWorkerBindingStore
    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.adapters.runtime.execution_worker import SubprocessExecutionWorker
    from repoforge.application.runtime.worker_lifecycle import WorkerLifecycleStore
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.bootstrap import build_configuration_store
    from repoforge.testing import FixedClock, InMemoryLockManager, SequenceIdGenerator

    env = create_forge_environment(tmp_path)
    home = tmp_path / "home"
    build_configuration_store(
        env.config_path, state_root=home / ".local/state/repoforge"
    ).import_legacy(
        env.config_path.read_text(encoding="utf-8"),
        env.config_path.read_text(encoding="utf-8"),
        created_at="2026-07-29T00:00:00+00:00",
    )
    root = tmp_path / "state"
    bindings = JsonExecutionWorkerBindingStore(root, InMemoryLockManager())
    leases = JsonProcessLeaseAdapter(root, InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        shadow=shadow,
    )
    lifecycle = WorkerLifecycleStore(
        bindings=bindings,
        leases=leases,
        shadow=shadow,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )
    spawner = SubprocessExecutionWorker(
        env.config_path,
        bindings=bindings,
        registrar=registrar,
        lifecycle=lifecycle,
        state_root=root,
    )
    return env, home, spawner, bindings, leases


def _wait_alive(spawner, child) -> None:
    import time

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not spawner.is_alive(child):
        time.sleep(0.1)
    assert spawner.is_alive(child), "the real execution worker did not start"


def test_normal_terminate_terminalizes_and_archives_process_lease(tmp_path) -> None:
    """Normal termination advances the canonical ProcessLease to TERMINATED (P0)."""
    import os

    from repoforge.domain.process_lease import ProcessLeaseStatus

    _env, home, spawner, bindings, leases = _real_spawner(tmp_path)
    child = spawner.start(
        1,
        env=dict(os.environ, HOME=str(home)),
        log_path=tmp_path / "worker.log",
        correlation_id="c" * 24,
    )
    try:
        _wait_alive(spawner, child)

        spawner.terminate(child, grace_seconds=3)

        archived = bindings.list_archive()
        worker_id = next((item.worker_id for item in archived if item.pid == child.pid), "")
        assert worker_id
        stored = leases.read(worker_id)
        assert stored is not None
        assert stored.value.status is ProcessLeaseStatus.TERMINATED
        active = leases.list_active_page()
        assert all(lease.lease_id != worker_id for lease in active.records)
    finally:
        spawner.terminate(child, grace_seconds=1)


def test_normal_terminate_updates_shadow(tmp_path) -> None:
    """Normal termination leaves parity in sync: the shadow row follows the JSON.

    The SQLite shadow mirrors the canonical lease writes; when the terminal lease
    is archived out of the JSON active store, the shadow row is removed with it,
    so ``compare_lease_parity`` stays in sync after every worker lifetime instead
    of reporting a permanent ``only_in_shadow`` drift (review F-010).
    """
    import os

    from repoforge.adapters.persistence.parity import compare_lease_parity
    from repoforge.adapters.persistence.sqlite_lease_store import SqliteLeaseStore

    shadow = SqliteLeaseStore(tmp_path / "state" / "shadow.db")
    _env, home, spawner, bindings, leases = _real_spawner(tmp_path, shadow=shadow)
    child = spawner.start(
        1,
        env=dict(os.environ, HOME=str(home)),
        log_path=tmp_path / "worker.log",
        correlation_id="c" * 24,
    )
    try:
        _wait_alive(spawner, child)

        spawner.terminate(child, grace_seconds=3)

        archived = bindings.list_archive()
        worker_id = next((item.worker_id for item in archived if item.pid == child.pid), "")
        assert worker_id
        # The shadow row is removed with the JSON archive, not left as a stale
        # TERMINATED record that makes parity drift forever.
        assert shadow.list_all() == [], "the shadow row must be removed with the archive"
        report = compare_lease_parity(leases, shadow)
        assert report.in_sync is True, report.as_dict()
    finally:
        spawner.terminate(child, grace_seconds=1)


def test_binding_less_active_lease_is_reclaimed_from_the_lease_alone() -> None:
    """P0-1: a provable RUNNING lease with no binding is reclaimed, not divergence.

    The canonical ProcessLease is the safety authority; the binding is a derived
    projection a crash or store failure can lose. A RUNNING lease whose binding is
    gone is still a real worker holding locks, so the reconciler proves it from the
    lease (dead owner, exact entry point, matching start token) and reaps it --
    otherwise a lost binding strands the orphan forever, recreating the 2026-08-01
    deadlock.
    """
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id="worker-bbbbbbbbbbbb",
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        pgid=4242,
        process_start_token="worker-start-token",
        owner_pid=4241,
        owner_process_identity="a" * 64,
        release_sha="0123abc",
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings()  # no binding projection at all
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 1
    assert report.process_lease_binding_divergence == 0
    statuses = [candidate.status for candidate in store.saved]
    assert ProcessLeaseStatus.TERMINATED in statuses
    assert store.archived == ["worker-bbbbbbbbbbbb"]


def test_binding_less_active_lease_still_blocks_when_unprovable() -> None:
    """P0-1: an unprovable binding-less lease stays a divergence (fail closed)."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id="worker-bbbbbbbbbbbb",
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=9999,
        pgid=9999,
        process_start_token="worker-start-token",
        owner_pid=4241,
        owner_process_identity="a" * 64,
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings()
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({}),  # entry point cannot be proven
        identity_reader=_Tokens({9999: None}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.refused_unproven == 1
    assert report.possibly_alive_unproven == 1
    assert report.process_lease_binding_divergence == 1
    # The more specific blocker wins: an unprovable possibly-alive worker is a
    # stronger reason to refuse than the divergence itself.
    assert report.blocker_code == "STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN"


def test_binding_less_lease_of_a_live_owner_is_left_alone() -> None:
    """P0-1: a binding-less lease whose owner supervisor is alive is never touched."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id="worker-bbbbbbbbbbbb",
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        pgid=4242,
        process_start_token="worker-start-token",
        owner_pid=4241,
        owner_process_identity="a" * 64,
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings()
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner_identity_reader=_Owner({4241}),  # owner is alive (returns _SHA)
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 0
    assert report.refused_unproven == 0
    assert store.saved == [], "a lease with a live owner must never be terminalized"


# ---------------------------------------------------------------------------
# Re-review: lifecycle write ordering, containment truthfulness, maintenance
# reporting (F-010).
# ---------------------------------------------------------------------------


def test_lease_failure_keeps_recovery_projection_intact() -> None:
    """A lease write failure must leave the binding projection untouched (P0).

    The canonical lease is persisted FIRST; the binding is only terminalized when
    the canonical terminal checkpoint landed. A lease CAS failure therefore never
    deletes/archives the recovery projection the next pass re-applies through --
    the binding must stay active so the worker is still discoverable and the
    outcome can be re-applied (review F-010).
    """
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease, failing=True)
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 0
    assert report.persistence_failures == 1
    assert report.persistence_failure_ids == (_WORKER,)
    # The recovery projection survives the lease failure: still active, still
    # terminalizable by the next pass once the store heals.
    assert bindings.get(_WORKER).state == "running"


def test_missing_canonical_lease_is_a_persistence_failure() -> None:
    """A reaped worker whose canonical lease is MISSING is never a success (P0).

    In production the canonical ProcessLease is authoritative; a binding whose
    lease does not exist cannot be terminalized as ``reclaimed`` -- that would be
    a false success the registry never recorded. It is reported as
    PROCESS_LEASE_MISSING and the binding stays active for recovery.
    """
    bindings = _Bindings(_binding(state="running"))
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=_RecordingLeaseStore(),  # wired, but the lease does not exist
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 0
    assert report.persistence_failures == 1
    assert report.persistence_failure_ids == (_WORKER,)
    assert report.detail, "the detail must explain the missing canonical lease"
    assert bindings.get(_WORKER).state == "running", "the projection stays for recovery"


def test_unwired_lease_store_is_not_success() -> None:
    """A lifecycle with no canonical lease store never reports persisted (P0).

    The strict (non-binding-only) lifecycle is what production wires; without a
    canonical lease store it must fail closed -- ``persisted=False`` -- instead of
    reporting a terminal outcome the registry never recorded. The binding-only
    opt-out is an explicit flag only embedders without any lease authority set.
    """
    from repoforge.application.runtime.worker_lifecycle import WorkerLifecycleStore

    bindings = _Bindings(_binding(state="running"))
    lifecycle = WorkerLifecycleStore(
        bindings=bindings,
        leases=None,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    result = lifecycle.apply_outcome(_WORKER, "reclaimed")

    assert result.persisted is False
    assert "PROCESS_LEASE_STORE_UNWIRED" in result.detail
    assert bindings.get(_WORKER).state == "running", "no projection was touched"


def test_terminal_binding_left_by_crash_is_debt_not_divergence() -> None:
    """A terminal binding with no lease is maintenance debt, not a divergence.

    A binding terminalized by a crash before its archive describes no process
    that may hold locks, so it must never block a read-only preflight as a
    live-process divergence (review F-010).
    """
    bindings = _Bindings(_binding(state="reclaimed"))
    reconciler = _reconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )
    reconciler._leases = _lease_store([])

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_binding_divergence == 0
    assert report.terminal_binding_debt == 1
    assert report.blocker_code is None, "archive debt must not block a preflight"


def test_running_lease_missing_pid_is_incomplete() -> None:
    """A RUNNING lease without a pid cannot prove its process: fail closed."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=None,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    bindings = _Bindings(_binding(state="running"))
    reconciler = _reconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )
    reconciler._leases = _lease_store([lease])

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_incomplete == 1
    assert report.process_lease_binding_divergence == 0


def test_running_lease_missing_start_token_is_incomplete() -> None:
    """A RUNNING lease without a start token cannot prove PID reuse safety."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.RUNNING,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token=None,
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    bindings = _Bindings(_binding(state="running"))
    reconciler = _reconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )
    reconciler._leases = _lease_store([lease])

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_incomplete == 1
    assert report.process_lease_binding_divergence == 0


def test_refused_binding_with_killed_lease_is_divergent() -> None:
    """refused_unproven <-> KILLED claims two histories for one worker."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.KILLED,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    bindings = _Bindings(_binding(state="refused_unproven"))
    reconciler = _reconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )
    reconciler._leases = _lease_store([lease])

    report = reconciler.reconcile(read_only=True)

    assert report.process_lease_binding_divergence == 1


def test_survived_kill_binding_with_unproven_lease_is_divergent() -> None:
    """survived_kill <-> UNPROVEN claims two histories for one worker."""
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.UNPROVEN,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        started_at=None,
        heartbeat_at=None,
        process_start_token="worker-start-token",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    bindings = _Bindings(_binding(state="survived_kill"))
    reconciler = _reconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )
    reconciler._leases = _lease_store([lease])

    report = reconciler.reconcile(read_only=True)

    # The matrix divergence is reported; the blocker code is the higher-priority
    # survived-kill gate (a possibly-alive worker is a stronger reason to refuse).
    assert report.process_lease_binding_divergence == 1
    assert report.blocker_code == "STALE_EXECUTION_WORKER_RECLAMATION_FAILED"


def test_ready_lease_without_binding_is_reclaimed_from_the_lease_alone() -> None:
    """A child that crashed mid-claim (READY-with-pid) is reclaimed, not stranded.

    The canonical lease carries pid, pgid, start token, and owner identity, so a
    READY lease whose binding was never written is proven and reaped from the
    lease alone once its dead owner no longer owns it (review F-001 P0: recovery
    never depends on the projection).
    """
    from repoforge.domain.process_lease import ProcessLease, ProcessLeaseRole, ProcessLeaseStatus

    lease = ProcessLease(
        lease_id=_WORKER,
        status=ProcessLeaseStatus.READY,
        role=ProcessLeaseRole.EXECUTION_DAEMON,
        process_identity="worker-start-token",
        pid=4242,
        pgid=4242,
        process_start_token="worker-start-token",
        owner_pid=4241,
        owner_process_identity="a" * 64,
        release_sha=_RELEASE,
        started_at="2026-07-30T00:00:00+00:00",
        heartbeat_at="2026-07-30T00:00:00+00:00",
        correlation_id="c" * 24,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
    )
    store = _RecordingLeaseStore(lease)
    bindings = _Bindings()  # the claim crashed before writing the binding
    reconciler = ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=_Reaper([ReapOutcome(True, True, False, "reaped via SIGTERM")]),
        owner_identity_reader=_Owner(set()),
        command_line_reader=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        identity_reader=_Tokens({4242: "worker-start-token"}),
        leases=store,
        now_iso=lambda: "2026-07-30T00:00:00+00:00",
    )

    report = reconciler.reconcile()

    assert report.reclaimed == 1
    assert report.process_lease_incomplete == 0, "the mid-claim crash is recovered"
    assert report.process_lease_binding_divergence == 0
    statuses = [candidate.status for candidate in store.saved]
    assert ProcessLeaseStatus.TERMINATED in statuses
    assert store.archived == [_WORKER]


class _FailingBindings(_Bindings):
    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        del max_records
        raise OSError("disk full")


def test_maintenance_failures_are_reported_not_suppressed() -> None:
    """A terminal-collection failure is evidence, never a silent no-op."""
    bindings = _FailingBindings(_binding())
    reconciler = _reconciler(
        bindings=bindings,
        reaper=_Reaper([]),
        owner=_Owner({4241}),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    report = reconciler.reconcile()

    assert report.maintenance_failures == 1
    assert report.maintenance_failure_ids == ("binding-collect",)
    assert "maintenance failures=1" in report.detail
