"""Deterministic ports, failure injection, and resource-leak assertions."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from ..domain.errors import ErrorCode, RepoForgeError
from ..domain.operation_task import OperationTask
from ..domain.operation_worker import (
    OperationWorkerBinding,
    validate_operation_worker_binding,
)
from ..domain.workspace import WorkspaceRecord
from ..ports.cancellation import CancellationToken
from ..ports.command import CommandResult
from ..ports.operation_gate import GateState
from ..ports.operation_store import OperationRecordPage
from ..ports.process_reaper import ReapOutcome


class FixedClock:
    def __init__(self, value: str = "2026-01-01T00:00:00+00:00") -> None:
        self.value = value

    def now_iso(self) -> str:
        return self.value


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


class ManualBackgroundTaskRunner:
    def __init__(self) -> None:
        self._tasks: dict[str, Callable[[], None]] = {}

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._tasks))

    def submit(self, key: str, task: Callable[[], None]) -> bool:
        if key in self._tasks:
            return False
        self._tasks[key] = task
        return True

    def run(self, key: str) -> None:
        task = self._tasks.pop(key)
        task()


class SequenceIdGenerator:
    def __init__(self, values: Sequence[str] = ("0000000001",)) -> None:
        self._values = deque(values)
        self._counter = 0

    def new_hex(self, length: int = 10) -> str:
        if self._values:
            return self._values.popleft()[:length].ljust(length, "0")
        self._counter += 1
        return f"{self._counter:0{length}x}"[-length:]


class FailureInjector:
    def __init__(self) -> None:
        self._failures: dict[str, deque[BaseException]] = {}

    def fail_next(self, point: str, exception: BaseException) -> None:
        self._failures.setdefault(point, deque()).append(exception)

    def hit(self, point: str) -> None:
        failures = self._failures.get(point)
        if failures:
            raise failures.popleft()


class ScriptedCommandExecutor:
    def __init__(self, injector: FailureInjector | None = None) -> None:
        self._responses: deque[
            CommandResult | BaseException | Callable[[Sequence[str]], CommandResult]
        ] = deque()
        self.calls: list[tuple[str, ...]] = []
        self.injector = injector or FailureInjector()

    def enqueue(
        self, *responses: CommandResult | BaseException | Callable[[Sequence[str]], CommandResult]
    ) -> None:
        self._responses.extend(responses)

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit, cancel_token
        self.injector.hit("command.run")
        self.calls.append(tuple(argv))
        if not self._responses:
            return CommandResult(tuple(argv), str(cwd), 0, "", "")
        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response(argv) if callable(response) else response

    def run_bytes(
        self, argv: Sequence[str], *, cwd: Path, timeout: int | None = None, max_bytes: int
    ) -> bytes:
        return self.run(argv, cwd=cwd, timeout=timeout, output_limit=max_bytes).stdout.encode()


class InMemoryWorkspaceStore:
    def __init__(self, injector: FailureInjector | None = None) -> None:
        self.records: dict[str, WorkspaceRecord] = {}
        self.injector = injector or FailureInjector()

    def save(self, record: WorkspaceRecord) -> None:
        self.injector.hit("workspace_store.save")
        self.records[record.workspace_id] = replace(record, metadata=dict(record.metadata))

    def load(self, workspace_id: str) -> WorkspaceRecord:
        self.injector.hit("workspace_store.load")
        return self.records[workspace_id]

    def delete(self, workspace_id: str) -> None:
        self.injector.hit("workspace_store.delete")
        self.records.pop(workspace_id, None)

    def list(self) -> list[WorkspaceRecord]:
        return list(self.records.values())

    @contextmanager
    def lock(self, workspace_id: str) -> Iterator[None]:
        del workspace_id
        yield


class InMemoryOperationStore:
    def __init__(self, injector: FailureInjector | None = None) -> None:
        self.records: dict[str, OperationTask] = {}
        self.injector = injector or FailureInjector()

    def create(self, task: OperationTask) -> OperationTask:
        self.injector.hit("operation_store.create")
        if task.operation_id in self.records:
            raise RepoForgeError("Operation already exists", code=ErrorCode.ALREADY_EXISTS)
        self.records[task.operation_id] = task
        return task

    def read(self, operation_id: str) -> OperationTask | None:
        self.injector.hit("operation_store.read")
        return self.records.get(operation_id)

    def save(self, task: OperationTask, *, expected_updated_at: str) -> OperationTask:
        self.injector.hit("operation_store.save")
        current = self.records.get(task.operation_id)
        if current is None:
            raise RepoForgeError("Operation not found", code=ErrorCode.OPERATION_NOT_FOUND)
        if current.updated_at != expected_updated_at:
            raise RepoForgeError(
                "Operation changed", code=ErrorCode.OPERATION_STALE, retryable=True
            )
        self.records[task.operation_id] = task
        return task

    def list_records(self, *, max_records: int) -> OperationRecordPage:
        self.injector.hit("operation_store.list")
        values = sorted(
            self.records.values(),
            key=lambda item: (item.updated_at, item.operation_id),
            reverse=True,
        )
        return OperationRecordPage(tuple(values[:max_records]), len(values) > max_records)

    def delete(self, operation_id: str) -> None:
        self.injector.hit("operation_store.delete")
        self.records.pop(operation_id, None)


class InMemoryLockManager:
    def __init__(self) -> None:
        self._held: set[str] = set()
        self._condition = threading.Condition()

    def path_for(self, name: str) -> Path:
        return Path("/memory/locks") / f"{name}.lock"

    @contextmanager
    def lock(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Iterator[None]:
        del metadata
        with self._condition:
            if name in self._held and timeout_seconds == 0:
                raise RuntimeError(f"lock already held: {name}")
            while name in self._held:
                self._condition.wait(timeout=timeout_seconds)
                if name in self._held and timeout_seconds is not None:
                    raise RuntimeError(f"lock timeout: {name}")
            self._held.add(name)
        try:
            yield
        finally:
            with self._condition:
                self._held.remove(name)
                self._condition.notify_all()

    @property
    def held(self) -> frozenset[str]:
        return frozenset(self._held)


class InMemoryOperationGate:
    def __init__(self) -> None:
        self.state = GateState.OPEN
        self.active_reads = 0
        self.active_writes = 0

    @contextmanager
    def operation(self, operation_id: str, *, mutating: bool) -> Iterator[None]:
        del operation_id
        if self.state in {GateState.FAIL_CLOSED, GateState.DRAINING}:
            raise RuntimeError("operation rejected")
        if mutating:
            self.active_writes += 1
        else:
            self.active_reads += 1
        try:
            yield
        finally:
            if mutating:
                self.active_writes -= 1
            else:
                self.active_reads -= 1

    def begin_drain(self, *, reason: str, correlation_id: str) -> None:
        del reason, correlation_id
        self.state = GateState.DRAINING

    def fail_closed(self, *, reason: str, correlation_id: str) -> None:
        del reason, correlation_id
        self.state = GateState.FAIL_CLOSED

    def reopen(self) -> None:
        self.state = GateState.OPEN

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return self.active_reads == 0 and self.active_writes == 0

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "active_reads": self.active_reads,
            "active_writes": self.active_writes,
        }


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    worktrees: frozenset[str]
    branches: frozenset[str]
    registry_records: frozenset[str]
    temporary_files: frozenset[str]


class CleanupTracker:
    def __init__(self) -> None:
        self.worktrees: set[Path] = set()
        self.branches: set[str] = set()
        self.registry_records: set[str] = set()
        self.lock_files: set[Path] = set()
        self.temporary_files: set[Path] = set()
        self.child_pids: set[int] = set()

    @staticmethod
    def _git_lines(repo_path: Path, argv: list[str]) -> frozenset[str]:
        completed = subprocess.run(
            ["git", *argv],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            raise AssertionError(f"Cannot inspect Git cleanup state: {completed.stderr.strip()}")
        return frozenset(item for item in completed.stdout.splitlines() if item)

    @classmethod
    def capture(
        cls, *, repo_path: Path, workspace_root: Path, state_root: Path
    ) -> ResourceSnapshot:
        worktree_lines = cls._git_lines(repo_path, ["worktree", "list", "--porcelain"])
        worktrees = frozenset(
            line.removeprefix("worktree ")
            for line in worktree_lines
            if line.startswith("worktree ")
        )
        branches = cls._git_lines(
            repo_path, ["for-each-ref", "--format=%(refname:short)", "refs/heads"]
        )
        registry = frozenset(
            str(path.relative_to(state_root))
            for path in state_root.glob("workspaces/*.json")
            if path.is_file()
        )
        temporary = frozenset(
            str(path)
            for root in (workspace_root, state_root)
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and (".tmp-" in path.name or path.name.endswith(".tmp"))
        )
        return ResourceSnapshot(worktrees, branches, registry, temporary)

    @staticmethod
    def _assert_locks_released(state_root: Path) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Unix is the supported production target
            return
        for path in state_root.rglob("*.lock") if state_root.exists() else ():
            with path.open("a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    metadata = ""
                    with contextlib.suppress(OSError, json.JSONDecodeError):
                        handle.seek(0)
                        metadata = handle.read().strip()
                    raise AssertionError(f"Leaked held lock {path}: {metadata}") from exc
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def assert_no_leaks(
        cls,
        baseline: ResourceSnapshot,
        *,
        repo_path: Path,
        workspace_root: Path,
        state_root: Path,
        child_pids: Sequence[int] = (),
    ) -> None:
        current = cls.capture(
            repo_path=repo_path, workspace_root=workspace_root, state_root=state_root
        )
        leaks: dict[str, list[str] | list[int]] = {
            "worktrees": sorted(current.worktrees - baseline.worktrees),
            "branches": sorted(current.branches - baseline.branches),
            "registry_records": sorted(current.registry_records - baseline.registry_records),
            "temporary_files": sorted(current.temporary_files - baseline.temporary_files),
        }
        live_children: list[int] = []
        for pid in child_pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                live_children.append(pid)
            else:
                live_children.append(pid)
        leaks["child_pids"] = live_children
        cls._assert_locks_released(state_root)
        active = {key: value for key, value in leaks.items() if value}
        if active:
            raise AssertionError(f"Harness resource leak: {active}")

    def assert_clean(self) -> None:
        leaks = {
            "worktrees": sorted(map(str, self.worktrees)),
            "branches": sorted(self.branches),
            "registry_records": sorted(self.registry_records),
            "lock_files": sorted(map(str, self.lock_files)),
            "temporary_files": sorted(map(str, self.temporary_files)),
            "child_pids": sorted(self.child_pids),
        }
        active = {key: value for key, value in leaks.items() if value}
        if active:
            raise AssertionError(f"Harness resource leak: {active}")


class InMemoryWorkerBindingStore:
    """Deterministic in-process WorkerBindingStore for tests."""

    def __init__(self) -> None:
        self._records: dict[str, OperationWorkerBinding] = {}

    def put(self, binding: OperationWorkerBinding) -> None:
        validate_operation_worker_binding(binding)
        self._records[binding.operation_id] = binding

    def get(self, operation_id: str) -> OperationWorkerBinding | None:
        return self._records.get(operation_id)

    def delete(self, operation_id: str) -> None:
        self._records.pop(operation_id, None)

    def list_all(self, *, max_records: int = 2_000) -> tuple[OperationWorkerBinding, ...]:
        return tuple(list(self._records.values())[:max_records])


class RecordingProcessReaper:
    """ProcessReaper fake recording reap calls and returning a scripted outcome."""

    def __init__(
        self,
        *,
        outcome: ReapOutcome | None = None,
        start_tokens: dict[int, str] | None = None,
    ) -> None:
        self._outcome = outcome or ReapOutcome(
            attempted=True, reaped=True, still_alive=False, detail="reaped (fake)"
        )
        self._start_tokens = dict(start_tokens or {})
        self.reaped: list[OperationWorkerBinding] = []

    def reap(self, binding: OperationWorkerBinding) -> ReapOutcome:
        self.reaped.append(binding)
        return self._outcome

    def read_start_token(self, pid: int) -> str | None:
        return self._start_tokens.get(pid)
