"""Execution-worker evidence for `rf doctor` / `rf runtime ls` (#368).

Stale workers (owner supervisor gone) are counted, grouped by release, and tied to the
lock files that claim their pid -- evidence from lock-file metadata plus PID identity,
never an inference that a runtime is "stuck".
"""

from __future__ import annotations

import json

from repoforge.adapters.subprocess.process_tree import ProcessIdentity
from repoforge.application.runtime.execution_worker_report import (
    ExecutionWorkerReport,
    build_execution_worker_report,
)
from repoforge.domain.execution_worker import ExecutionWorkerBinding

_SHA = "a" * 64
_RELEASE = "0123abc"
_WORKER = "worker-0123456789ab"
_WORKER_OTHER = "worker-ffffffffffab"

_EXECUTION_WORKER_ARGV = (
    "/opt/repoforge/venv/bin/python",
    "-m",
    "repoforge.interfaces.runtime.execution_worker",
    "--config",
    "/home/dev/config.toml",
    "--generation",
    "12",
)


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
        self.scan_complete = scan_complete
        self.unreadable_ids = unreadable_ids

    def list_page(self, *, max_records: int = 2_000):
        del max_records
        from repoforge.ports.execution_worker_store import ExecutionWorkerBindingPage

        return ExecutionWorkerBindingPage(
            records=tuple(self.records.values()),
            scan_complete=self.scan_complete,
            unreadable_ids=self.unreadable_ids,
        )


class _Owner:
    def __init__(self, alive_pids: set[int]) -> None:
        self.alive_pids = alive_pids

    def __call__(self, pid: int) -> str | None:
        return _SHA if pid in self.alive_pids else None


class _CommandLines:
    def __init__(self, mapping: dict[int, tuple[str, ...] | None]) -> None:
        self.mapping = mapping

    def __call__(self, pid: int) -> tuple[str, ...] | None:
        return self.mapping.get(pid)


class _Tokens:
    def __init__(self, tokens: dict[int, str | None]) -> None:
        self.tokens = tokens

    def __call__(self, pid: int) -> ProcessIdentity | None:
        token = self.tokens.get(pid)
        if token is None:
            return None
        return ProcessIdentity(pid=pid, ppid=1, start_token=token)


def _report(
    *,
    bindings: _Bindings,
    lock_root,
    owner: _Owner,
    command_lines: _CommandLines,
    tokens: _Tokens,
    group_gone=None,
) -> ExecutionWorkerReport:
    return build_execution_worker_report(
        bindings=bindings,
        lock_root=lock_root,
        owner_identity_reader=owner,
        command_line_reader=command_lines,
        identity_reader=tokens,
        process_group_gone=group_gone,
    )


def test_report_is_empty_when_every_owner_is_alive(tmp_path) -> None:
    report = _report(
        bindings=_Bindings(_binding()),
        lock_root=tmp_path / "locks",
        owner=_Owner({4241}),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    assert report.stale_execution_worker_count == 0
    assert report.worker_pids == []
    assert report.reclamation_safe is True


def test_report_counts_stale_workers_by_release_and_pid(tmp_path) -> None:
    report = _report(
        bindings=_Bindings(_binding(), _binding(worker_id=_WORKER_OTHER, pid=5252)),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV, 5252: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token", 5252: "worker-start-token"}),
    )

    assert report.stale_execution_worker_count == 2
    assert report.worker_pids == [4242, 5252]
    assert report.workers_by_release[_RELEASE] == [_WORKER, _WORKER_OTHER]
    assert report.owner_supervisor_state == {_WORKER: "dead", _WORKER_OTHER: "dead"}


def test_report_lists_locks_held_by_stale_worker_pids(tmp_path) -> None:
    lock_root = tmp_path / "locks"
    lock_root.mkdir()
    (lock_root / "runtime-single-instance.lock").write_text(
        json.dumps({"pid": 4242, "acquired_at_ns": 1}), encoding="utf-8"
    )
    (lock_root / "operations-op-123.lock").write_text(
        json.dumps({"pid": 5252, "acquired_at_ns": 1}), encoding="utf-8"
    )
    (lock_root / "unrelated.lock").write_text(
        json.dumps({"pid": 9999, "acquired_at_ns": 1}), encoding="utf-8"
    )

    report = _report(
        bindings=_Bindings(_binding(), _binding(worker_id=_WORKER_OTHER, pid=5252)),
        lock_root=lock_root,
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV, 5252: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token", 5252: "worker-start-token"}),
    )

    assert report.locks_held[_WORKER] == ["runtime-single-instance.lock"]
    assert report.locks_held[_WORKER_OTHER] == ["operations-op-123.lock"]


def test_report_flags_reclamation_unsafe_for_an_unprovable_worker(tmp_path) -> None:
    report = _report(
        bindings=_Bindings(_binding()),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: None}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    assert report.stale_execution_worker_count == 1
    assert report.reclamation_safe is False


def test_report_serializes_all_evidence_fields() -> None:
    report = ExecutionWorkerReport(
        stale_execution_worker_count=1,
        workers_by_release={_RELEASE: [_WORKER]},
        worker_pids=[4242],
        owner_supervisor_state={_WORKER: "dead"},
        locks_held={_WORKER: ["runtime-single-instance.lock"]},
        reclamation_safe=True,
        scan_complete=True,
        unreadable_record_ids=(),
        orphaned_group_without_leader=(),
        containment_unproven=False,
        detail="1 stale execution worker(s)",
    )
    payload = report.as_dict()
    assert payload["stale_execution_worker_count"] == 1
    assert payload["workers_by_release"] == {_RELEASE: [_WORKER]}
    assert payload["worker_pids"] == [4242]
    assert payload["locks_held"] == {_WORKER: ["runtime-single-instance.lock"]}
    assert payload["unreadable_record_count"] == 0
    assert payload["unreadable_record_ids_sample"] == []
    assert payload["unreadable_record_ids_truncated"] is False
    assert payload["orphaned_group_without_leader"] == []
    assert payload["containment_unproven"] is False


def test_report_bounds_unreadable_ids_in_the_json_payload(tmp_path) -> None:
    """The doctor payload carries a count + ≤8 sample, never the full id list (#424)."""
    ids = tuple(f"worker-bad-{i}" for i in range(2_000))
    report = _report(
        bindings=_Bindings(unreadable_ids=ids),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({}),
        tokens=_Tokens({}),
    )
    payload = report.as_dict()
    assert payload["unreadable_record_count"] == 2_000
    assert len(payload["unreadable_record_ids_sample"]) == 8
    assert payload["unreadable_record_ids_truncated"] is True
    assert report.reclamation_safe is False


def test_report_does_not_show_terminal_bindings_as_stale(tmp_path) -> None:
    """Reclaimed/already_gone bindings are history, never stale live workers (#420)."""
    report = _report(
        bindings=_Bindings(
            _binding(state="reclaimed"),
            _binding(worker_id=_WORKER_OTHER, pid=5252, state="already_gone"),
        ),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV, 5252: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token", 5252: "worker-start-token"}),
    )

    assert report.stale_execution_worker_count == 0
    assert report.reclamation_safe is True


def test_report_does_not_show_a_gone_process_as_stale(tmp_path) -> None:
    """A binding whose process already exited is a stale record, not a stale worker."""
    report = _report(
        bindings=_Bindings(_binding()),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: None}),
    )

    assert report.stale_execution_worker_count == 0


def test_report_flags_an_incomplete_scan_as_unsafe(tmp_path) -> None:
    report = _report(
        bindings=_Bindings(scan_complete=False),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({}),
        tokens=_Tokens({}),
    )

    assert report.scan_complete is False
    assert report.reclamation_safe is False


def test_report_flags_unreadable_records_as_unsafe(tmp_path) -> None:
    """An unreadable record is incomplete evidence for reclamation (#420)."""
    report = _report(
        bindings=_Bindings(unreadable_ids=("worker-bad-1",)),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({}),
        tokens=_Tokens({}),
    )

    assert report.scan_complete is True
    assert report.unreadable_record_ids == ("worker-bad-1",)
    assert report.reclamation_safe is False


def test_report_flags_a_tokenless_binding_as_unsafe(tmp_path) -> None:
    """A tokenless binding cannot prove PID-reuse safety: reclamation is never safe."""
    report = _report(
        bindings=_Bindings(_binding(token=None)),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: "worker-start-token"}),
    )

    assert report.stale_execution_worker_count == 1
    assert report.reclamation_safe is False


def test_report_flags_an_orphaned_group_without_leader_as_unsafe(tmp_path) -> None:
    """Leader gone + live group members: the group may still hold locks (#424)."""
    report = _report(
        bindings=_Bindings(_binding()),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: None}),
        group_gone=lambda pgid: False,
    )

    assert report.stale_execution_worker_count == 0
    assert report.orphaned_group_without_leader == (_WORKER,)
    assert report.reclamation_safe is False


def test_report_treats_leader_and_group_gone_as_a_stale_record(tmp_path) -> None:
    """Leader AND group gone proves the worker is history, not a live concern."""
    report = _report(
        bindings=_Bindings(_binding()),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: None}),
        group_gone=lambda pgid: True,
    )

    assert report.stale_execution_worker_count == 0
    assert report.orphaned_group_without_leader == ()
    assert report.reclamation_safe is True


def test_report_flags_unprovable_containment_as_unsafe(tmp_path) -> None:
    """No group probe available: containment is unproven, so reclamation is unsafe."""
    report = _report(
        bindings=_Bindings(_binding()),
        lock_root=tmp_path / "locks",
        owner=_Owner(set()),
        command_lines=_CommandLines({4242: _EXECUTION_WORKER_ARGV}),
        tokens=_Tokens({4242: None}),
        group_gone=None,
    )

    assert report.containment_unproven is True
    assert report.reclamation_safe is False
    assert report.reclamation_safe is False
