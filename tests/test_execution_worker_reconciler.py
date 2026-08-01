"""PID-safe execution-worker reconciliation (#368).

Orphaned execution workers (spawned by a supervisor that died) must be reclaimed
before a new supervisor starts, or they hold operation locks and block convergence.
The reconciler proves each worker's identity and entry point before signalling, and
fails closed -- never killing by pattern -- when the process table or identity cannot
be proven.
"""

from __future__ import annotations

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
        state="running",
    )


class _Bindings:
    def __init__(self, *bindings: ExecutionWorkerBinding) -> None:
        self.records = {binding.worker_id: binding for binding in bindings}
        self.states: dict[str, list[str]] = {}

    def put(self, binding: ExecutionWorkerBinding) -> None:
        self.records[binding.worker_id] = binding

    def get(self, worker_id: str) -> ExecutionWorkerBinding | None:
        return self.records.get(worker_id)

    def update_state(self, worker_id: str, state: str) -> ExecutionWorkerBinding | None:
        self.states.setdefault(worker_id, []).append(state)
        if worker_id not in self.records:
            return None
        from repoforge.domain.execution_worker import (
            execution_worker_binding_from_payload,
            execution_worker_binding_payload,
        )

        updated = execution_worker_binding_from_payload(
            execution_worker_binding_payload(self.records[worker_id]) | {"state": state}
        )
        self.records[worker_id] = updated
        return updated

    def list_all(self, *, max_records: int = 2_000) -> tuple[ExecutionWorkerBinding, ...]:
        del max_records
        return tuple(self.records.values())


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
    """`read_identity(pid)` -> a ProcessIdentity-like with a start_token."""

    def __init__(self, tokens: dict[int, str | None]) -> None:
        self.tokens = tokens

    def __call__(self, pid: int) -> object | None:
        token = self.tokens.get(pid)
        if token is None:
            return None
        return _Identity(token)


class _Identity:
    def __init__(self, start_token: str) -> None:
        self.start_token = start_token


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
) -> ExecutionWorkerReconciler:
    return ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=reaper,
        owner_identity_reader=owner,
        command_line_reader=command_lines,
        identity_reader=tokens,
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
        worker_ids=(_WORKER,),
        pids=(4242,),
        release_shas=(_RELEASE,),
        detail="reconciliation complete",
    )
    payload = report.as_dict()
    assert payload["inspected"] == 3
    assert payload["reclaimed"] == 1
    assert payload["worker_ids"] == [_WORKER]
    assert payload["release_shas"] == [_RELEASE]
