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
    spawner = SubprocessExecutionWorker(env.config_path, bindings=bindings)
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
    spawner = SubprocessExecutionWorker(env.config_path, bindings=bindings)
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
