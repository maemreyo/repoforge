"""Durable per-worker registry for isolated execution workers (#368)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.execution_worker import ExecutionWorkerBinding


@dataclass(frozen=True, slots=True)
class ExecutionWorkerBindingPage:
    records: tuple[ExecutionWorkerBinding, ...]
    scan_complete: bool
    unreadable_ids: tuple[str, ...]


class ExecutionWorkerBindingStore(Protocol):
    def put(self, binding: ExecutionWorkerBinding) -> None:
        """Create or replace the binding for ``binding.worker_id``."""
        ...

    def get(self, worker_id: str) -> ExecutionWorkerBinding | None: ...

    def update_state(self, worker_id: str, state: str) -> ExecutionWorkerBinding | None:
        """Advance a binding's state under CAS, returning the updated binding."""
        ...

    def list_page(self, *, max_records: int = 2_000) -> ExecutionWorkerBindingPage:
        """A bounded scan; ``scan_complete`` is False when the registry was truncated.

        A reconciler must fail closed on an incomplete scan: an orphan past the limit
        would be invisible and a replacement could start on incomplete evidence.
        """
        ...

    def collect_terminal(self, *, max_records: int = 5_000) -> int:
        """Archive and delete terminal leases left by older releases.

        Keeps the active registry bounded by the number of concurrent workers so the
        bounded scan can never overflow into a permanent fail-closed block (#424).
        """
        ...
