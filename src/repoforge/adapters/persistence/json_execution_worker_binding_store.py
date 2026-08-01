"""Private atomic persistence for execution-worker bindings (#368).

A collection of per-worker records under ``runtime-execution-workers/<worker-id>.json``
-- deliberately a collection, not a single "current worker" file: many supervisor
lifetimes can each leave workers behind (92 in the 2026-08-01 incident), and every one
of them needs its own identity record.
"""

from __future__ import annotations

from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.execution_worker import (
    EXECUTION_WORKER_BINDING_SCHEMA_VERSION,
    ExecutionWorkerBinding,
    execution_worker_binding_from_payload,
    execution_worker_binding_payload,
    validate_execution_worker_binding,
)
from ...ports.execution_worker_store import ExecutionWorkerBindingPage
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository


def validate_execution_worker_id(worker_id: str) -> str:
    validate_execution_worker_binding(
        # The id validator only enforces the id shape; construct a minimal binding.
        ExecutionWorkerBinding(
            worker_id=worker_id,
            pid=1,
            pgid=1,
            process_start_token="unused-id-check-token",
            generation=1,
            release_sha=None,
            supervisor_pid=1,
            supervisor_process_identity="a" * 64,
            correlation_id="x",
            started_at="x",
            state="running",
        )
    )
    return worker_id


class _ExecutionWorkerBindingCodec(StateCodec[ExecutionWorkerBinding]):
    schema_version = SchemaVersion(EXECUTION_WORKER_BINDING_SCHEMA_VERSION)

    def encode(self, value: ExecutionWorkerBinding) -> dict[str, object]:
        return execution_worker_binding_payload(value)

    def decode(self, payload: dict[str, object]) -> ExecutionWorkerBinding:
        return execution_worker_binding_from_payload(dict(payload))


class JsonExecutionWorkerBindingStore:
    def __init__(self, state_root: Path, locks: LockManager) -> None:
        self._records: JsonStateRepository[ExecutionWorkerBinding] = JsonStateRepository(
            state_root,
            collection="runtime-execution-workers",
            locks=locks,
            codec=_ExecutionWorkerBindingCodec(),
            id_validator=validate_execution_worker_id,
            max_record_bytes=8_192,
        )
        self.root = self._records.root

    def put(self, binding: ExecutionWorkerBinding) -> None:
        validate_execution_worker_binding(binding)
        current = self._records.read(binding.worker_id)
        if current is None:
            self._records.create(binding.worker_id, binding)
            return
        self._records.save(
            binding.worker_id,
            binding,
            expected_revision=current.revision,
        )

    def get(self, worker_id: str) -> ExecutionWorkerBinding | None:
        envelope = self._records.read(worker_id)
        return envelope.value if envelope is not None else None

    def update_state(self, worker_id: str, state: str) -> ExecutionWorkerBinding | None:
        envelope = self._records.read(worker_id)
        if envelope is None:
            return None
        updated = execution_worker_binding_from_payload(
            execution_worker_binding_payload(envelope.value) | {"state": state}
        )
        self._records.save(
            worker_id,
            updated,
            expected_revision=envelope.revision,
        )
        return updated

    def list_all(self, *, max_records: int = 2_000) -> tuple[ExecutionWorkerBinding, ...]:
        page = self._records.list_records(max_records=max_records)
        return tuple(item.value for item in page.records)

    def list_page(self, *, max_records: int = 2_000) -> ExecutionWorkerBindingPage:
        """Scan the registry with truncation and unreadable-record evidence exposed."""
        page = self._records.list_records(max_records=max_records)
        return ExecutionWorkerBindingPage(
            records=tuple(item.value for item in page.records),
            scan_complete=not page.scan_truncated,
            unreadable_ids=page.unreadable_record_ids,
        )
