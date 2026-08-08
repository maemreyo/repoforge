"""Private atomic JSON persistence for typed state envelopes."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from ...domain.durable_state import Revision, StateCodec, StateEnvelope, StatePage
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.locking import LockManager
from ..filesystem.atomic import fsync_dir
from .json_state_file_store import AtomicJsonFileStore as AtomicJsonFileStore
from .json_state_file_store import _state_error

T = TypeVar("T")


class JsonStateRepository(Generic[T]):
    """Bounded deterministic storage with private permissions and revision CAS.

    Filesystem mechanics (locking, atomic writes, bounded reads) live in
    :class:`AtomicJsonFileStore`; this class layers typed envelope encoding and
    revision compare-and-swap on top.  The compare-and-swap primitives
    (``save``, ``delete_if_revision``, ``compare_and_delete``) hold the per-record
    lock for the whole read-compare-mutate section, so a concurrent writer can
    never observe an intermediate state.
    """

    def __init__(
        self,
        state_root: Path,
        *,
        collection: str,
        locks: LockManager,
        codec: StateCodec[T],
        id_validator: Callable[[str], str],
        max_record_bytes: int = 1_000_000,
    ) -> None:
        self._files = AtomicJsonFileStore(
            state_root,
            collection=collection,
            locks=locks,
            id_validator=id_validator,
            max_record_bytes=max_record_bytes,
        )
        self.root = self._files.root
        self.collection = collection
        self._codec = codec
        self._max_record_bytes = max_record_bytes

    @staticmethod
    def _error(
        message: str,
        code: ErrorCode,
        *,
        retryable: bool = False,
    ) -> RepoForgeError:
        return _state_error(message, code, retryable=retryable)

    def _record_id(self, value: str) -> str:
        return self._files.record_id(value)

    def _path(self, record_id: str) -> Path:
        return self._files.path(record_id)

    def _encode(self, envelope: StateEnvelope[T]) -> bytes:
        payload = {
            "payload": self._codec.encode(envelope.value),
            "record_id": envelope.record_id,
            "revision": envelope.revision.value,
            "schema_version": envelope.schema_version.value,
        }
        try:
            encoded = (
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise self._error(
                "State payload is not JSON serializable", ErrorCode.STATE_INVALID
            ) from exc
        if len(encoded) > self._max_record_bytes:
            raise self._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        return encoded

    def _decode(self, data: bytes, *, expected_record_id: str) -> StateEnvelope[T]:
        if len(data) > self._max_record_bytes:
            raise self._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        try:
            raw: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error(
                "State record is not valid UTF-8 JSON", ErrorCode.STATE_CORRUPT
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "payload",
            "record_id",
            "revision",
            "schema_version",
        }:
            raise self._error(
                "State record fields do not match the envelope", ErrorCode.STATE_CORRUPT
            )
        if raw.get("record_id") != expected_record_id:
            raise self._error(
                "State record identity does not match its filename", ErrorCode.STATE_CORRUPT
            )
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise self._error("State schema version is invalid", ErrorCode.STATE_CORRUPT)
        if version != self._codec.schema_version.value:
            raise self._error(
                f"Unsupported state schema version: {version}",
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            )
        revision_raw = raw.get("revision")
        if not isinstance(revision_raw, int) or isinstance(revision_raw, bool):
            raise self._error("State revision is invalid", ErrorCode.STATE_CORRUPT)
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise self._error("State payload must be an object", ErrorCode.STATE_CORRUPT)
        try:
            value = self._codec.decode(payload)
            revision = Revision(revision_raw)
        except (TypeError, ValueError, RepoForgeError) as exc:
            raise self._error(
                "State record cannot be decoded safely", ErrorCode.STATE_CORRUPT
            ) from exc
        return StateEnvelope(expected_record_id, self._codec.schema_version, revision, value)

    def _write(self, path: Path, data: bytes) -> None:
        self._files.write_bytes(path.stem, data)

    def create(self, record_id: str, value: T) -> StateEnvelope[T]:
        safe_id = self._record_id(record_id)
        path = self._path(safe_id)
        envelope = StateEnvelope(safe_id, self._codec.schema_version, Revision(1), value)
        data = self._encode(envelope)
        with self._files.locked(safe_id, operation="create"):
            if path.exists():
                raise self._error(
                    f"State record already exists: {safe_id}", ErrorCode.ALREADY_EXISTS
                )
            self._write(path, data)
        return envelope

    def create_or_read_equal(self, record_id: str, value: T) -> StateEnvelope[T]:
        """Atomically create a record or return its equal durable envelope."""

        safe_id = self._record_id(record_id)
        path = self._path(safe_id)
        envelope = StateEnvelope(safe_id, self._codec.schema_version, Revision(1), value)
        data = self._encode(envelope)
        with self._files.locked(safe_id, operation="create_or_read_equal"):
            current = self.read(safe_id)
            if current is not None:
                if current.value == value:
                    return current
                raise self._error(
                    f"State record already exists: {safe_id}", ErrorCode.ALREADY_EXISTS
                )
            self._write(path, data)
        return envelope

    def read(self, record_id: str) -> StateEnvelope[T] | None:
        safe_id = self._record_id(record_id)
        data = self._files.read_bytes(safe_id)
        if data is None:
            return None
        return self._decode(data, expected_record_id=safe_id)

    def read_raw_envelope(self, record_id: str) -> dict[str, object] | None:
        """Read a bounded validated envelope without applying the current codec."""
        safe_id = self._record_id(record_id)
        data = self._files.read_bytes(safe_id)
        if data is None:
            return None
        if len(data) > self._max_record_bytes:
            raise self._error(
                "State record exceeds its encoded size bound", ErrorCode.STATE_TOO_LARGE
            )
        try:
            raw: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error(
                "State record is not valid UTF-8 JSON", ErrorCode.STATE_CORRUPT
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "payload",
            "record_id",
            "revision",
            "schema_version",
        }:
            raise self._error(
                "State record fields do not match the envelope", ErrorCode.STATE_CORRUPT
            )
        if raw.get("record_id") != safe_id:
            raise self._error(
                "State record identity does not match its filename", ErrorCode.STATE_CORRUPT
            )
        if not isinstance(raw.get("revision"), int) or isinstance(raw.get("revision"), bool):
            raise self._error("State revision is invalid", ErrorCode.STATE_CORRUPT)
        if not isinstance(raw.get("schema_version"), int) or isinstance(
            raw.get("schema_version"), bool
        ):
            raise self._error("State schema version is invalid", ErrorCode.STATE_CORRUPT)
        if not isinstance(raw.get("payload"), dict):
            raise self._error("State payload must be an object", ErrorCode.STATE_CORRUPT)
        return dict(raw)

    def save(
        self,
        record_id: str,
        value: T,
        *,
        expected_revision: Revision,
    ) -> StateEnvelope[T]:
        safe_id = self._record_id(record_id)
        path = self._path(safe_id)
        with self._files.locked(safe_id, operation="save"):
            current = self.read(safe_id)
            if current is None:
                raise self._error(f"State record not found: {safe_id}", ErrorCode.STATE_NOT_FOUND)
            if current.revision != expected_revision:
                raise self._error(
                    f"State record changed from revision {expected_revision.value} to {current.revision.value}",
                    ErrorCode.STATE_STALE,
                    retryable=True,
                )
            envelope = StateEnvelope(
                safe_id,
                self._codec.schema_version,
                current.revision.next(),
                value,
            )
            self._write(path, self._encode(envelope))
        return envelope

    def list_record_ids(self, *, max_records: int, offset: int = 0) -> tuple[tuple[str, ...], bool]:
        """Raw record ids in the collection, without decoding.

        Lets a caller (e.g. the process-lease adapter's active scan) enumerate the
        collection once and filter by record content, while keeping the decode
        sweep bounded by ``max_records``. ``offset`` pages the enumeration so a
        caller can skip records it does not care about (e.g. terminal history)
        without counting them against its completeness signal.
        """
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records <= 0:
            raise self._error("max_records must be a positive integer", ErrorCode.STATE_INVALID)
        return self._files.list_ids(pattern="*.json", max_records=max_records, offset=offset)

    def list_records(self, *, max_records: int) -> StatePage[T]:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= 2_000
        ):
            raise self._error("max_records must be between 1 and 2000", ErrorCode.STATE_INVALID)
        record_ids, scan_truncated = self._files.list_ids(pattern="*.json", max_records=max_records)
        records: list[StateEnvelope[T]] = []
        unreadable: list[str] = []
        for record_id in record_ids:
            # A sweep survives one bad record; `read()` of one exact id stays
            # strict, because a caller asking for that record has to hear that it
            # is unusable. Startup sweeps every store, so a raise here turns a
            # single file written by an older release into a runtime that cannot
            # start at all.
            try:
                item = self.read(record_id)
            except RepoForgeError:
                unreadable.append(record_id)
                continue
            if item is not None:
                records.append(item)
        records.sort(key=lambda item: (item.revision.value, item.record_id), reverse=True)
        return StatePage(tuple(records), scan_truncated, tuple(sorted(unreadable)))

    def delete(self, record_id: str) -> None:
        safe_id = self._record_id(record_id)
        with self._files.locked(safe_id, operation="delete"):
            self._files.delete_bytes(safe_id)

    def delete_if_revision(self, record_id: str, *, expected_revision: Revision) -> bool:
        """Delete only while the stored revision still equals ``expected_revision``.

        Atomic under the per-record lock: read, compare, and delete happen in one
        critical section, so a concurrent ``save`` between compare and delete can
        never cause a stale writer to remove a record that has since advanced.
        Returns ``False`` without deleting when the record is absent or its
        revision has moved.
        """
        safe_id = self._record_id(record_id)
        with self._files.locked(safe_id, operation="delete_if_revision"):
            current = self.read(safe_id)
            if current is None or current.revision != expected_revision:
                return False
            self._files.delete_bytes(safe_id)
            return True

    def compare_and_delete(
        self,
        record_id: str,
        *,
        expected_revision: Revision,
        expected_value: T,
    ) -> bool:
        """Delete only while the stored revision AND value still match expectations.

        Both comparisons and the delete run under one per-record lock, so a
        delete-then-recreate that reuses the same revision (an ABA sequence) can
        never cause a stale caller to remove a record it never inspected. Returns
        ``False`` without deleting when the record is absent or either the
        revision or the value has moved.
        """
        safe_id = self._record_id(record_id)
        with self._files.locked(safe_id, operation="compare_and_delete"):
            current = self.read(safe_id)
            if (
                current is None
                or current.revision != expected_revision
                or current.value != expected_value
            ):
                return False
            self._files.delete_bytes(safe_id)
            return True

    def move_to_quarantine(self, record_id: str) -> Path:
        """Move a record's raw file into a private sibling quarantine dir; never deletes.

        Used by the worker-registry repair workflow (#424): an unreadable or selected
        record is set aside with its bytes intact -- never destroyed -- so the operator
        can still prove what it described. The quarantine dir is
        ``<state_root>/<collection>-quarantine``; returns the target path even when the
        source was already gone (callers check the source first to distinguish).
        """
        safe_id = self._record_id(record_id)
        source = self._path(safe_id)
        quarantine_root = source.parent.parent / f"{self.collection}-quarantine"
        quarantine_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = quarantine_root / source.name
        with self._files.locked(safe_id, operation="quarantine"):
            if not source.is_file():
                return target
            if target.is_file() and target.read_bytes() != source.read_bytes():
                raise _state_error(
                    f"Quarantine target already contains different bytes: {target}",
                    ErrorCode.STATE_INVALID,
                )
            os.replace(source, target)
            os.chmod(target, 0o600)
            fsync_dir(source.parent)
            fsync_dir(target.parent)
        return target
