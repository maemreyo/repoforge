"""Protected local runtime control protocol."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.runtime import ControlRequest, ControlResponse, RestartHistoryRecord, RuntimeRecord


class RuntimeControlClient(Protocol):
    def request(
        self, request: ControlRequest, *, timeout_seconds: float = 10.0
    ) -> ControlResponse: ...


class RuntimeControlServer(Protocol):
    def start(self, handler: Callable[[ControlRequest], ControlResponse]) -> None: ...
    def close(self) -> None: ...
    def is_serving(self) -> bool:
        """Whether control requests are still being accepted at all.

        Part of the boundary because runtime health has to report it: the health record is
        written by a loop that can outlive the one serving control requests, so a runtime
        could otherwise claim `healthy` with no control plane at all (#322).

        True as long as *something* can still answer a request -- an implementation backed
        by a pool of workers may keep this `True` even at reduced capacity, which is exactly
        why this alone must never drive a health boolean: use `is_healthy()` for that.
        """
        ...

    def is_healthy(self) -> bool:
        """Whether the control plane is serving at its full configured capacity.

        Stricter than `is_serving()`: an implementation with a fixed-size worker pool that
        has lost some (but not all) of its workers is still `is_serving() == True` -- it can
        still answer requests -- but is running at a persistent capacity loss with no
        self-healing, which is exactly the state a health check must not call `healthy`
        (#448 Slice 1 partial-worker health semantics).
        """
        ...

    def serving_diagnostic(self) -> str: ...


class RuntimeStore(Protocol):
    def read(self) -> RuntimeRecord | None: ...
    def write(self, record: RuntimeRecord) -> None: ...
    def clear(self, *, expected_pid: int | None = None) -> None: ...
    def reconcile(self) -> RuntimeRecord | None: ...

    def peek_restart_evidence(self) -> tuple[int, str | None] | None:
        """Read `restarts_total`/`last_restart_at` straight off disk, bypassing
        pid-liveness self-heal -- for one-time restart-history-ledger migration
        seeding only, never for anything claiming current process liveness (#448
        Slice 4).
        """
        ...


class RestartHistoryStore(Protocol):
    """A durable restart-history ledger, deliberately separate from `RuntimeStore`.

    `RuntimeStore.read()` self-heals to `None` whenever the recorded process is no
    longer live -- correct for "is this claim about a running process still true,"
    but wrong for restart history, which stays true regardless of whether the
    process that produced it is still alive. This port exists so restart counters
    survive process replacement instead of being reset as collateral damage of that
    liveness check (#448 Slice 4).
    """

    def read(self) -> RestartHistoryRecord | None: ...
    def write(self, record: RestartHistoryRecord) -> None: ...

    def record_restart(
        self,
        *,
        incarnation_id: str,
        reason: str | None,
        occurred_at: str,
        event_id: str,
    ) -> RestartHistoryRecord:
        """Atomically increment `restarts_total` for one logical restart, exactly once.

        Guards the whole read-modify-write against overlapping writers (the exact
        shape of a supervisor handoff), and is idempotent on `event_id` so a caller
        that replays the same logical restart (e.g. retrying after an unconfirmed
        write) never double-counts it (#448 Slice 4).
        """
        ...

    def seed_if_missing(
        self,
        *,
        restarts_total: int,
        last_restart_at: str | None,
        incarnation_id: str,
        occurred_at: str,
    ) -> RestartHistoryRecord:
        """Initialize the ledger from legacy `RuntimeRecord` evidence, ONLY if it does
        not already exist -- a ledger that already exists always wins (#448 Slice 4
        migration)."""
        ...


class RuntimeLauncher(Protocol):
    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int: ...
    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool: ...
