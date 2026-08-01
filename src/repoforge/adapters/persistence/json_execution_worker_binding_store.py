"""Private atomic persistence for execution-worker bindings (#368).

A collection of per-worker records under ``runtime-execution-workers/<worker-id>.json``
-- deliberately a collection, not a single "current worker" file: many supervisor
lifetimes can each leave workers behind (92 in the 2026-08-01 incident), and every one
of them needs its own identity record.

The registry holds only **active leases** (#424): when a lease reaches a terminal
state (``reclaimed``/``already_gone``) it is archived to
``runtime-execution-workers-history/`` and the active record is removed. The bounded
scan therefore covers the number of concurrent workers, never the number of workers
that ever existed, so the 2,000-record scan limit can never accumulate into a
deterministic fail-closed block.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.execution_worker import (
    EXECUTION_WORKER_ARCHIVE_SCHEMA_VERSION,
    EXECUTION_WORKER_BINDING_SCHEMA_VERSION,
    TERMINAL_STATES,
    ExecutionWorkerArchiveEntry,
    ExecutionWorkerBinding,
    execution_worker_archive_from_payload,
    execution_worker_archive_payload,
    execution_worker_binding_from_payload,
    execution_worker_binding_payload,
    validate_execution_worker_binding,
)
from ...ports.execution_worker_store import ExecutionWorkerBindingPage
from ...ports.locking import LockManager
from .json_state_repository import JsonStateRepository

#: Terminal leases are archived and removed; the history keeps the most recent N so
#: the archive itself stays bounded without unbounded disk growth (#424).
_HISTORY_MAX_RECORDS = 1_000
_GC_BATCH = 5_000


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


class _ExecutionWorkerArchiveCodec(StateCodec[ExecutionWorkerArchiveEntry]):
    schema_version = SchemaVersion(EXECUTION_WORKER_ARCHIVE_SCHEMA_VERSION)

    def encode(self, value: ExecutionWorkerArchiveEntry) -> dict[str, object]:
        return execution_worker_archive_payload(value)

    def decode(self, payload: dict[str, object]) -> ExecutionWorkerArchiveEntry:
        return execution_worker_archive_from_payload(dict(payload))


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
        self._history: JsonStateRepository[ExecutionWorkerArchiveEntry] = JsonStateRepository(
            state_root,
            collection="runtime-execution-workers-history",
            locks=locks,
            codec=_ExecutionWorkerArchiveCodec(),
            id_validator=validate_execution_worker_id,
            max_record_bytes=8_192,
        )
        self.root = self._records.root
        self.history_root = self._history.root

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
        if state in TERMINAL_STATES:
            # CAS-persist the terminal state first (detects a concurrent writer), then
            # archive the durable terminal lease and remove the active record (#424).
            self._records.save(
                worker_id,
                updated,
                expected_revision=envelope.revision,
            )
            self._archive_and_delete(worker_id, updated)
            return updated
        self._records.save(
            worker_id,
            updated,
            expected_revision=envelope.revision,
        )
        return updated

    def _archive_and_delete(self, worker_id: str, binding: ExecutionWorkerBinding) -> None:
        """Durably archive a terminal lease, then remove the active record (#424).

        The archive write is idempotent (crash-safe checkpoint): if we crash between
        the archive and the delete, the next terminal transition re-archives (a no-op)
        and deletes. The active record is only deleted while it is still terminal, so
        a concurrent non-terminal write is never erased.
        """
        entry = ExecutionWorkerArchiveEntry.from_binding(
            binding,
            terminated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._history.create_or_read_equal(worker_id, entry)
        self._prune_history()
        current = self._records.read(worker_id)
        if current is not None and current.value.state in TERMINAL_STATES:
            self._records.delete(worker_id)

    def _prune_history(self) -> None:
        """Bound the history: delete the oldest entries beyond the cap, when exceeded."""
        if sum(1 for _ in self.history_root.glob("*.json")) <= _HISTORY_MAX_RECORDS:
            return
        page = self._history.list_records(max_records=2_000)
        for envelope in page.records[_HISTORY_MAX_RECORDS:]:
            with contextlib.suppress(Exception):
                self._history.delete(envelope.record_id)

    def collect_terminal(self, *, max_records: int = _GC_BATCH) -> int:
        """Archive and delete terminal leases left by older releases (#424).

        Bounded per call and converges across calls: terminal files are deleted as they
        are found, so the surviving file set shrinks until only active leases remain
        and a full sweep fits inside the batch.
        """
        collected = 0
        for path in sorted(self.root.glob("*.json"))[:max_records]:
            worker_id = path.stem
            try:
                envelope = self._records.read(worker_id)
            except Exception:
                continue
            if envelope is None or envelope.value.state not in TERMINAL_STATES:
                continue
            self._archive_and_delete(worker_id, envelope.value)
            collected += 1
        return collected

    def list_archive(self, *, max_records: int = 500) -> tuple[ExecutionWorkerArchiveEntry, ...]:
        page = self._history.list_records(max_records=max_records)
        return tuple(item.value for item in page.records)

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
