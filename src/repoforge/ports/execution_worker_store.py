"""Durable per-worker registry for isolated execution workers (#368)."""

from __future__ import annotations

from typing import Protocol

from ..domain.execution_worker import ExecutionWorkerBinding


class ExecutionWorkerBindingStore(Protocol):
    def put(self, binding: ExecutionWorkerBinding) -> None:
        """Create or replace the binding for ``binding.worker_id``."""
        ...

    def get(self, worker_id: str) -> ExecutionWorkerBinding | None: ...

    def update_state(self, worker_id: str, state: str) -> ExecutionWorkerBinding | None:
        """Advance a binding's state under CAS, returning the updated binding."""
        ...

    def list_all(self, *, max_records: int = 2_000) -> tuple[ExecutionWorkerBinding, ...]:
        """All bindings, bounded; a reconciler never scans without a ceiling."""
        ...
