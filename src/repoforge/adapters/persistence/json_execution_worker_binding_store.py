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
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ...domain.durable_state import SchemaVersion, StateCodec
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.execution_worker import (
    EXECUTION_WORKER_ARCHIVE_SCHEMA_VERSION,
    EXECUTION_WORKER_BINDING_SCHEMA_VERSION,
    TERMINAL_STATES,
    ExecutionWorkerArchiveEntry,
    ExecutionWorkerBinding,
    ExecutionWorkerQuarantineReceipt,
    execution_worker_archive_from_payload,
    execution_worker_archive_payload,
    execution_worker_binding_from_payload,
    execution_worker_binding_payload,
    validate_execution_worker_binding,
    validate_execution_worker_quarantine_receipt,
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
        try:
            self._history.create_or_read_equal(worker_id, entry)
        except RepoForgeError as exc:
            if exc.code is not ErrorCode.ALREADY_EXISTS:
                raise
            # Crash checkpoint: the archive half of a previous attempt completed and
            # the process died before the active delete. The retry re-stamps
            # terminated_at, so the payload differs only by that timestamp -- an
            # existing entry is exactly the durable checkpoint, not a conflict.
            # A worker_id is unique per pid + start token, so it is archived at most
            # once and an existing entry can never belong to a different worker.
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

    @property
    def quarantine_root(self) -> Path:
        return self._records.root.parent / "runtime-execution-workers-quarantine"

    def inspect_record(self, worker_id: str) -> dict[str, object] | None:
        """Bounded evidence about one registry file, or None when it does not exist.

        Works for unreadable files too: the raw bytes are read directly (bounded),
        the sha256 is reported, and pid/state are recovered only when the envelope is
        parseable -- so the operator can still prove whether the described process
        lives before quarantining (#424).
        """
        return self._probe_record(worker_id)

    def quarantine_record(
        self, worker_id: str, *, reason: str
    ) -> ExecutionWorkerQuarantineReceipt | None:
        """Move one registry file into the private quarantine with a durable receipt.

        Never deletes: the bytes are moved (under the record lock) to the quarantine
        directory and a receipt records where they went, the content digest, why, and
        the pid they described when the metadata was parseable (#424).

        The receipt is written BEFORE the move (F-011): a crash between the two can
        then never leave quarantined bytes with no evidence of what/why they were
        set aside -- the worst case is a receipt whose move never happened, which a
        re-run completes and overwrites. If the move itself fails, the receipt is
        removed so no false "quarantined at <path>" claim survives.
        """
        info = self._probe_record(worker_id)
        if info is None:
            return None
        safe_id = str(info["worker_id"])
        target = self.quarantine_root / f"{safe_id}.json"
        raw_pid = info["pid"]
        receipt = ExecutionWorkerQuarantineReceipt(
            worker_id=safe_id,
            quarantine_path=str(target),
            digest=str(info["sha256"]),
            reason=reason,
            quarantined_at=datetime.now(timezone.utc).isoformat(),
            pid=raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None,
            parseable=bool(info["parseable"]),
        )
        validate_execution_worker_quarantine_receipt(receipt)
        receipt_path = target.parent / f"{safe_id}.receipt.json"
        self._write_quarantine_receipt(receipt_path, receipt)
        try:
            self._records.move_to_quarantine(safe_id)
        except BaseException:
            with contextlib.suppress(OSError):
                receipt_path.unlink()
            raise
        return receipt

    @staticmethod
    def _write_quarantine_receipt(path: Path, receipt: ExecutionWorkerQuarantineReceipt) -> None:
        """Persist the receipt durably (atomic replace + fsync) before the move."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n"
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            with tmp.open("wb") as handle:
                handle.write(payload.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def list_quarantined(self) -> tuple[ExecutionWorkerQuarantineReceipt, ...]:
        """Every durable quarantine receipt, oldest first."""
        if not self.quarantine_root.is_dir():
            return ()
        receipts: list[ExecutionWorkerQuarantineReceipt] = []
        for path in sorted(self.quarantine_root.glob("*.receipt.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                pid = raw.get("pid")
                receipt = ExecutionWorkerQuarantineReceipt(
                    worker_id=str(raw["worker_id"]),
                    quarantine_path=str(raw["quarantine_path"]),
                    digest=str(raw["digest"]),
                    reason=str(raw["reason"]),
                    quarantined_at=str(raw["quarantined_at"]),
                    pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                    parseable=bool(raw.get("parseable")),
                )
                validate_execution_worker_quarantine_receipt(receipt)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            receipts.append(receipt)
        return tuple(receipts)

    def _probe_record(self, worker_id: str) -> dict[str, object] | None:
        safe_id = validate_execution_worker_id(worker_id)
        path = self._records.root / f"{safe_id}.json"
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RepoForgeError(f"Cannot read state record {path.name}") from exc
        digest = hashlib.sha256(data).hexdigest()
        info: dict[str, object] = {
            "worker_id": safe_id,
            "path": str(path),
            "bytes": len(data),
            "sha256": digest,
            "parseable": False,
            "pid": None,
            "state": None,
        }
        try:
            envelope = self._records.read_raw_envelope(safe_id)
        except RepoForgeError:
            envelope = None
        if envelope is not None:
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                raw_pid = payload.get("pid")
                raw_state = payload.get("state")
                if isinstance(raw_pid, int) and not isinstance(raw_pid, bool):
                    info["pid"] = raw_pid
                if isinstance(raw_state, str):
                    info["state"] = raw_state
                info["parseable"] = True
        return info
