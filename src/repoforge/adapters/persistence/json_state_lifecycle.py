"""Preview-bound lifecycle administration for private JSON durable-state collections."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.durable_state import Revision, SchemaVersion
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.state_lifecycle import (
    CleanupDisposition,
    StateCleanupCandidate,
    StateCleanupPreview,
    StateCleanupReport,
    StateMigrationPreview,
    StateMigrationRecordPreview,
    StateMigrationRegistry,
    StateMigrationReport,
    StateProtection,
    StateRecordReference,
    StateRetentionPolicy,
    validate_state_collection,
)
from ...ports.locking import LockManager

FaultInjector = Callable[[str, str, int], None]

_SAFE_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ENVELOPE_FIELDS = {"payload", "record_id", "revision", "schema_version"}
_MAX_RECORDS = 2_000
_DEFAULT_MAX_RECORD_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class _RawStateRecord:
    record_id: str
    schema_version: SchemaVersion
    revision: Revision
    payload: dict[str, object]
    encoded: bytes
    checksum: str


@dataclass(frozen=True, slots=True)
class _MaterializedMigration:
    preview: StateMigrationRecordPreview
    source: _RawStateRecord
    target_bytes: bytes


class JsonStateLifecycleManager:
    """Administer schema migration and recovery without widening public tool surface."""

    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        *,
        max_record_bytes: int = _DEFAULT_MAX_RECORD_BYTES,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        if (
            not isinstance(max_record_bytes, int)
            or isinstance(max_record_bytes, bool)
            or not 64 <= max_record_bytes <= 25 * 1024 * 1024
        ):
            raise self._error(
                "max_record_bytes must be between 64 and 26214400",
                ErrorCode.STATE_INVALID,
            )
        self.root = state_root.expanduser().resolve()
        self.control_root = self.root / ".state-lifecycle"
        self.backups_root = self.control_root / "backups"
        self.journals_root = self.control_root / "journals"
        self.trash_root = self.control_root / "trash"
        self._locks = locks
        self._max_record_bytes = max_record_bytes
        self._fault_injector = fault_injector
        for directory in (
            self.root,
            self.control_root,
            self.backups_root,
            self.journals_root,
            self.trash_root,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    @staticmethod
    def _error(message: str, code: ErrorCode, *, retryable: bool = False) -> RepoForgeError:
        return RepoForgeError(
            message,
            code=code,
            retryable=retryable,
            safe_next_action=(
                "Recreate a dry-run preview from current durable state, inspect backup and journal "
                "evidence, then retry the exact reviewed lifecycle operation."
            ),
        )

    @staticmethod
    def _record_id(value: str) -> str:
        if (
            not isinstance(value, str)
            or _SAFE_RECORD_ID.fullmatch(value) is None
            or "/" in value
            or "\\" in value
        ):
            raise JsonStateLifecycleManager._error(
                "durable-state record identifier is unsafe",
                ErrorCode.STATE_INVALID,
            )
        return value

    def _collection_root(self, collection: str) -> Path:
        return self.root / validate_state_collection(collection)

    @staticmethod
    def _canonical_bytes(value: object) -> bytes:
        try:
            return json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise JsonStateLifecycleManager._error(
                "lifecycle metadata is not deterministic JSON",
                ErrorCode.STATE_INVALID,
            ) from exc

    @staticmethod
    def _pretty_bytes(value: object) -> bytes:
        try:
            return (
                json.dumps(
                    value,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise JsonStateLifecycleManager._error(
                "durable-state record is not JSON serializable",
                ErrorCode.STATE_INVALID,
            ) from exc

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @classmethod
    def _atomic_write(cls, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.tmp-",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            cls._fsync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _write_json(cls, path: Path, value: object) -> None:
        cls._atomic_write(path, cls._pretty_bytes(value))

    def _read_json(self, path: Path, *, code: ErrorCode) -> dict[str, object]:
        try:
            raw: Any = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error(f"cannot read lifecycle record {path.name}", code) from exc
        if not isinstance(raw, dict):
            raise self._error(f"lifecycle record {path.name} must be an object", code)
        return raw

    def _decode_record(self, path: Path, *, expected_record_id: str) -> _RawStateRecord:
        safe_id = self._record_id(expected_record_id)
        try:
            encoded = path.read_bytes()
        except OSError as exc:
            raise self._error(
                f"cannot read durable-state record {safe_id}",
                ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        if len(encoded) > self._max_record_bytes:
            raise self._error(
                f"durable-state record {safe_id} exceeds its size bound",
                ErrorCode.STATE_TOO_LARGE,
            )
        try:
            raw: Any = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._error(
                f"durable-state record {safe_id} is not valid UTF-8 JSON",
                ErrorCode.STATE_CORRUPT,
            ) from exc
        if not isinstance(raw, dict) or set(raw) != _ENVELOPE_FIELDS:
            raise self._error(
                f"durable-state record {safe_id} fields do not match the shared envelope",
                ErrorCode.STATE_CORRUPT,
            )
        if raw.get("record_id") != safe_id:
            raise self._error(
                f"durable-state record {safe_id} identity does not match its filename",
                ErrorCode.STATE_CORRUPT,
            )
        version = raw.get("schema_version")
        revision = raw.get("revision")
        payload = raw.get("payload")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
            or not isinstance(payload, dict)
        ):
            raise self._error(
                f"durable-state record {safe_id} envelope values are invalid",
                ErrorCode.STATE_CORRUPT,
            )
        return _RawStateRecord(
            safe_id,
            SchemaVersion(version),
            Revision(revision),
            dict(payload),
            encoded,
            self._sha256(encoded),
        )

    def _record_paths(
        self,
        collection: str,
        *,
        max_records: int,
    ) -> tuple[tuple[Path, ...], bool]:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= _MAX_RECORDS
        ):
            raise self._error(
                f"max_records must be between 1 and {_MAX_RECORDS}",
                ErrorCode.STATE_INVALID,
            )
        collection_root = self._collection_root(collection)
        paths = sorted(collection_root.glob("*.json")) if collection_root.is_dir() else []
        return tuple(paths[:max_records]), len(paths) > max_records

    @staticmethod
    def _preview_payload(
        collection: str,
        target_version: SchemaVersion,
        records: tuple[StateMigrationRecordPreview, ...],
        scan_truncated: bool,
    ) -> dict[str, object]:
        return {
            "collection": collection,
            "target_version": target_version.value,
            "scan_truncated": scan_truncated,
            "records": [
                {
                    "record_id": item.record_id,
                    "source_version": item.source_version.value,
                    "target_version": item.target_version.value,
                    "source_revision": item.source_revision,
                    "source_checksum": item.source_checksum,
                    "target_checksum": item.target_checksum,
                    "direction": item.direction.value,
                    "changed": item.changed,
                    "source_size_bytes": item.source_size_bytes,
                    "target_size_bytes": item.target_size_bytes,
                }
                for item in records
            ],
        }

    def preview_migration(
        self,
        *,
        collection: str,
        registry: StateMigrationRegistry,
        target_version: SchemaVersion,
        max_records: int = _MAX_RECORDS,
    ) -> StateMigrationPreview:
        safe_collection = validate_state_collection(collection)
        if not isinstance(registry, StateMigrationRegistry):
            raise self._error(
                "migration registry is invalid",
                ErrorCode.STATE_INVALID,
            )
        if not isinstance(target_version, SchemaVersion):
            raise self._error(
                "target_version must be a SchemaVersion",
                ErrorCode.STATE_INVALID,
            )
        paths, scan_truncated = self._record_paths(
            safe_collection,
            max_records=max_records,
        )
        previews: list[StateMigrationRecordPreview] = []
        for path in paths:
            source = self._decode_record(path, expected_record_id=path.stem)
            plan = registry.plan(
                safe_collection,
                source.schema_version,
                target_version,
            )
            if source.schema_version == target_version:
                target_bytes = source.encoded
            else:
                migrated_payload = registry.migrate_payload(plan, source.payload)
                target_bytes = self._pretty_bytes(
                    {
                        "payload": migrated_payload,
                        "record_id": source.record_id,
                        "revision": source.revision.next().value,
                        "schema_version": target_version.value,
                    }
                )
                if len(target_bytes) > self._max_record_bytes:
                    raise self._error(
                        f"migrated durable-state record {source.record_id} exceeds its size bound",
                        ErrorCode.STATE_TOO_LARGE,
                    )
            previews.append(
                StateMigrationRecordPreview(
                    record_id=source.record_id,
                    source_version=source.schema_version,
                    target_version=target_version,
                    source_revision=source.revision.value,
                    source_checksum=source.checksum,
                    target_checksum=self._sha256(target_bytes),
                    direction=plan.direction,
                    changed=source.encoded != target_bytes,
                    source_size_bytes=len(source.encoded),
                    target_size_bytes=len(target_bytes),
                )
            )
        records = tuple(previews)
        plan_digest = self._sha256(
            self._canonical_bytes(
                self._preview_payload(
                    safe_collection,
                    target_version,
                    records,
                    scan_truncated,
                )
            )
        )
        return StateMigrationPreview(
            plan_id=f"mig-{plan_digest[:24]}",
            plan_digest=plan_digest,
            collection=safe_collection,
            target_version=target_version,
            records=records,
            migrated_records=sum(item.changed for item in records),
            unchanged_records=sum(not item.changed for item in records),
            scan_truncated=scan_truncated,
        )

    def _validate_preview(self, preview: StateMigrationPreview) -> None:
        if not isinstance(preview, StateMigrationPreview):
            raise self._error(
                "migration preview is invalid",
                ErrorCode.STATE_INVALID,
            )
        digest = self._sha256(
            self._canonical_bytes(
                self._preview_payload(
                    preview.collection,
                    preview.target_version,
                    preview.records,
                    preview.scan_truncated,
                )
            )
        )
        if preview.plan_digest != digest or preview.plan_id != f"mig-{digest[:24]}":
            raise self._error(
                "migration preview digest is invalid",
                ErrorCode.STATE_INVALID,
            )
        if preview.scan_truncated:
            raise self._error(
                "migration preview is truncated and cannot be applied",
                ErrorCode.STATE_INVALID,
            )

    def _materialize(
        self,
        preview: StateMigrationPreview,
        registry: StateMigrationRegistry,
    ) -> tuple[_MaterializedMigration, ...]:
        materialized: list[_MaterializedMigration] = []
        collection_root = self._collection_root(preview.collection)
        for item in preview.records:
            source = self._decode_record(
                collection_root / f"{self._record_id(item.record_id)}.json",
                expected_record_id=item.record_id,
            )
            if not hmac.compare_digest(source.checksum, item.source_checksum):
                raise self._error(
                    f"durable-state record {item.record_id} changed after migration preview",
                    ErrorCode.STATE_STALE,
                    retryable=True,
                )
            plan = registry.plan(
                preview.collection,
                source.schema_version,
                preview.target_version,
            )
            if source.schema_version == preview.target_version:
                target_bytes = source.encoded
            else:
                target_bytes = self._pretty_bytes(
                    {
                        "payload": registry.migrate_payload(plan, source.payload),
                        "record_id": source.record_id,
                        "revision": source.revision.next().value,
                        "schema_version": preview.target_version.value,
                    }
                )
            if not hmac.compare_digest(self._sha256(target_bytes), item.target_checksum):
                raise self._error(
                    f"migration output for {item.record_id} changed since preview",
                    ErrorCode.STATE_INVALID,
                )
            materialized.append(_MaterializedMigration(item, source, target_bytes))
        return tuple(materialized)

    def _backup_dir(self, plan_id: str) -> Path:
        return self.backups_root / self._record_id(plan_id)

    def _journal_path(self, plan_id: str) -> Path:
        return self.journals_root / f"{self._record_id(plan_id)}.json"

    def _journal_report(self, raw: dict[str, object]) -> StateMigrationReport | None:
        if raw.get("phase") != "committed":
            return None
        report = raw.get("report")
        if not isinstance(report, dict):
            raise self._error("migration journal report is corrupt", ErrorCode.STATE_CORRUPT)
        try:
            return StateMigrationReport(
                plan_id=str(report["plan_id"]),
                processed=int(report["processed"]),
                migrated=int(report["migrated"]),
                unchanged=int(report["unchanged"]),
                rolled_back=bool(report["rolled_back"]),
                backup_id=(str(report["backup_id"]) if report["backup_id"] is not None else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._error(
                "migration journal report is corrupt", ErrorCode.STATE_CORRUPT
            ) from exc

    def _read_existing_report(self, preview: StateMigrationPreview) -> StateMigrationReport | None:
        journal_path = self._journal_path(preview.plan_id)
        if not journal_path.is_file():
            return None
        raw = self._read_json(journal_path, code=ErrorCode.STATE_CORRUPT)
        if raw.get("plan_digest") != preview.plan_digest:
            raise self._error(
                "migration journal identity conflicts with the reviewed preview",
                ErrorCode.STATE_INVALID,
            )
        return self._journal_report(raw)

    def _write_backup(
        self,
        preview: StateMigrationPreview,
        materialized: tuple[_MaterializedMigration, ...],
    ) -> str:
        backup_id = preview.plan_id
        backup_dir = self._backup_dir(backup_id)
        records_dir = backup_dir / "records"
        records_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(records_dir, 0o700)
        entries: list[dict[str, object]] = []
        for item in materialized:
            if not item.preview.changed:
                continue
            backup_path = records_dir / f"{item.source.record_id}.json"
            self._atomic_write(backup_path, item.source.encoded)
            entries.append(
                {
                    "record_id": item.source.record_id,
                    "source_checksum": item.source.checksum,
                    "target_checksum": item.preview.target_checksum,
                    "size_bytes": len(item.source.encoded),
                }
            )
        manifest_payload = {
            "plan_id": preview.plan_id,
            "plan_digest": preview.plan_digest,
            "collection": preview.collection,
            "target_version": preview.target_version.value,
            "records": entries,
        }
        self._write_json(
            backup_dir / "manifest.json",
            {
                **manifest_payload,
                "manifest_checksum": self._sha256(self._canonical_bytes(manifest_payload)),
            },
        )
        return backup_id

    def _write_journal(
        self,
        preview: StateMigrationPreview,
        *,
        phase: str,
        backup_id: str,
        report: StateMigrationReport | None = None,
    ) -> None:
        self._write_json(
            self._journal_path(preview.plan_id),
            {
                "plan_id": preview.plan_id,
                "plan_digest": preview.plan_digest,
                "collection": preview.collection,
                "phase": phase,
                "backup_id": backup_id,
                "report": (
                    {
                        "plan_id": report.plan_id,
                        "processed": report.processed,
                        "migrated": report.migrated,
                        "unchanged": report.unchanged,
                        "rolled_back": report.rolled_back,
                        "backup_id": report.backup_id,
                    }
                    if report is not None
                    else None
                ),
            },
        )

    def _restore_materialized(
        self,
        materialized: tuple[_MaterializedMigration, ...],
        *,
        collection: str,
    ) -> None:
        collection_root = self._collection_root(collection)
        for item in materialized:
            if item.preview.changed:
                self._atomic_write(
                    collection_root / f"{item.source.record_id}.json",
                    item.source.encoded,
                )

    def apply_migration(
        self,
        preview: StateMigrationPreview,
        *,
        registry: StateMigrationRegistry,
    ) -> StateMigrationReport:
        self._validate_preview(preview)
        existing = self._read_existing_report(preview)
        if existing is not None:
            return existing
        if preview.migrated_records == 0:
            return StateMigrationReport(
                preview.plan_id,
                len(preview.records),
                0,
                preview.unchanged_records,
                False,
                None,
            )

        with self._locks.lock(
            f"state-lifecycle-{preview.collection}",
            timeout_seconds=10,
            metadata={"operation": "migrate", "plan_id": preview.plan_id},
        ):
            existing = self._read_existing_report(preview)
            if existing is not None:
                return existing
            materialized = self._materialize(preview, registry)
            backup_id = self._write_backup(preview, materialized)
            self._write_journal(preview, phase="prepared", backup_id=backup_id)
            self._write_journal(preview, phase="applying", backup_id=backup_id)
            collection_root = self._collection_root(preview.collection)
            try:
                for index, item in enumerate(materialized):
                    if not item.preview.changed:
                        continue
                    if self._fault_injector is not None:
                        self._fault_injector(
                            "before_migration_write",
                            item.source.record_id,
                            index,
                        )
                    self._atomic_write(
                        collection_root / f"{item.source.record_id}.json",
                        item.target_bytes,
                    )
            except Exception as exc:
                rollback_error: Exception | None = None
                try:
                    self._restore_materialized(
                        materialized,
                        collection=preview.collection,
                    )
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
                report = StateMigrationReport(
                    preview.plan_id,
                    len(preview.records),
                    0,
                    preview.unchanged_records,
                    True,
                    backup_id,
                )
                self._write_journal(
                    preview,
                    phase="rolled_back",
                    backup_id=backup_id,
                    report=report,
                )
                details: dict[str, object] = {"rollback_failed": rollback_error is not None}
                raise RepoForgeError(
                    "durable-state migration failed and was rolled back",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    safe_next_action=(
                        "Inspect the private migration backup and journal, recover incomplete "
                        "migrations, then recreate the preview."
                    ),
                    details=details,
                ) from exc
            report = StateMigrationReport(
                preview.plan_id,
                len(preview.records),
                preview.migrated_records,
                preview.unchanged_records,
                False,
                backup_id,
            )
            self._write_journal(
                preview,
                phase="committed",
                backup_id=backup_id,
                report=report,
            )
            return report

    def _restore_backup(self, journal: dict[str, object]) -> None:
        plan_id = self._record_id(str(journal.get("plan_id")))
        collection = validate_state_collection(str(journal.get("collection")))
        backup_id = self._record_id(str(journal.get("backup_id")))
        backup_dir = self._backup_dir(backup_id)
        manifest = self._read_json(
            backup_dir / "manifest.json",
            code=ErrorCode.STATE_CORRUPT,
        )
        records = manifest.get("records")
        if not isinstance(records, list):
            raise self._error("migration backup manifest is corrupt", ErrorCode.STATE_CORRUPT)
        for entry in records:
            if not isinstance(entry, dict):
                raise self._error("migration backup entry is corrupt", ErrorCode.STATE_CORRUPT)
            record_id = self._record_id(str(entry.get("record_id")))
            expected_checksum = str(entry.get("source_checksum"))
            backup_bytes = (backup_dir / "records" / f"{record_id}.json").read_bytes()
            if not _SHA256.fullmatch(expected_checksum) or not hmac.compare_digest(
                self._sha256(backup_bytes),
                expected_checksum,
            ):
                raise self._error("migration backup checksum is invalid", ErrorCode.STATE_CORRUPT)
            self._atomic_write(
                self._collection_root(collection) / f"{record_id}.json",
                backup_bytes,
            )
        self._write_json(
            self._journal_path(plan_id),
            {
                **journal,
                "phase": "rolled_back",
                "report": {
                    "plan_id": plan_id,
                    "processed": len(records),
                    "migrated": 0,
                    "unchanged": 0,
                    "rolled_back": True,
                    "backup_id": backup_id,
                },
            },
        )

    def recover_incomplete_migrations(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for path in sorted(self.journals_root.glob("mig-*.json")):
            journal = self._read_json(path, code=ErrorCode.STATE_CORRUPT)
            if journal.get("phase") not in {"prepared", "applying"}:
                continue
            plan_id = self._record_id(str(journal.get("plan_id")))
            with self._locks.lock(
                f"state-lifecycle-recovery-{plan_id}",
                timeout_seconds=10,
                metadata={"operation": "recover", "plan_id": plan_id},
            ):
                self._restore_backup(journal)
            recovered.append(plan_id)
        return tuple(recovered)

    @staticmethod
    def _retention_time(value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise JsonStateLifecycleManager._error(
                f"{field} must be an ISO-8601 timestamp",
                ErrorCode.STATE_INVALID,
            ) from exc
        if parsed.tzinfo is None:
            raise JsonStateLifecycleManager._error(
                f"{field} must include a timezone offset",
                ErrorCode.STATE_INVALID,
            )
        return parsed

    @staticmethod
    def _cleanup_payload(
        *,
        collection: str,
        candidates: tuple[StateCleanupCandidate, ...],
        protected_record_ids: tuple[str, ...],
        orphan_references: tuple[tuple[str, str, str], ...],
        retained_records: int,
        retained_bytes: int,
        remaining_candidate_count: int,
        next_cursor: str | None,
    ) -> dict[str, object]:
        return {
            "collection": collection,
            "candidates": [
                {
                    "record_id": item.record_id,
                    "checksum": item.checksum,
                    "size_bytes": item.size_bytes,
                    "created_at": item.created_at,
                    "disposition": item.disposition.value,
                }
                for item in candidates
            ],
            "protected_record_ids": list(protected_record_ids),
            "orphan_references": [list(item) for item in orphan_references],
            "retained_records": retained_records,
            "retained_bytes": retained_bytes,
            "remaining_candidate_count": remaining_candidate_count,
            "next_cursor": next_cursor,
        }

    def preview_cleanup(
        self,
        *,
        collection: str,
        policy: StateRetentionPolicy,
        record_timestamps: dict[str, str],
        protections: tuple[StateProtection, ...] = (),
        references: tuple[StateRecordReference, ...] = (),
    ) -> StateCleanupPreview:
        safe_collection = validate_state_collection(collection)
        if not isinstance(policy, StateRetentionPolicy):
            raise self._error(
                "retention policy is invalid",
                ErrorCode.STATE_INVALID,
            )
        if not isinstance(record_timestamps, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in record_timestamps.items()
        ):
            raise self._error(
                "record_timestamps must map record IDs to ISO-8601 timestamps",
                ErrorCode.STATE_INVALID,
            )
        if not isinstance(protections, tuple) or not all(
            isinstance(item, StateProtection) for item in protections
        ):
            raise self._error(
                "protections must be a StateProtection tuple",
                ErrorCode.STATE_INVALID,
            )
        if not isinstance(references, tuple) or not all(
            isinstance(item, StateRecordReference) for item in references
        ):
            raise self._error(
                "references must be a StateRecordReference tuple",
                ErrorCode.STATE_INVALID,
            )

        paths, truncated = self._record_paths(safe_collection, max_records=_MAX_RECORDS)
        if truncated:
            raise self._error(
                "cleanup scan exceeds the reviewed record bound",
                ErrorCode.STATE_TOO_LARGE,
            )
        records = tuple(self._decode_record(path, expected_record_id=path.stem) for path in paths)
        ids = {record.record_id for record in records}
        if set(record_timestamps) != ids:
            raise self._error(
                "record_timestamps must cover exactly the current collection",
                ErrorCode.STATE_INVALID,
            )
        timestamps = {
            record_id: self._retention_time(value, f"created_at for {record_id}")
            for record_id, value in record_timestamps.items()
        }
        protected = {item.record_id for item in protections if item.record_id in ids}
        orphan_references: list[tuple[str, str, str]] = []
        for reference in references:
            if reference.source_record_id not in ids:
                continue
            if reference.target_record_id in ids:
                protected.add(reference.target_record_id)
            else:
                orphan_references.append(
                    (
                        reference.source_record_id,
                        reference.target_record_id,
                        reference.relation,
                    )
                )

        ordered = sorted(
            records,
            key=lambda item: (timestamps[item.record_id], item.record_id),
        )
        dispositions: dict[str, CleanupDisposition] = {}
        cutoff = self._retention_time(policy.now, "retention now") - timedelta(
            seconds=policy.retention_seconds
        )
        for record in ordered:
            if record.record_id not in protected and timestamps[record.record_id] < cutoff:
                dispositions[record.record_id] = CleanupDisposition.EXPIRED

        def remaining() -> list[_RawStateRecord]:
            return [item for item in ordered if item.record_id not in dispositions]

        while len(remaining()) > policy.max_records:
            candidate = next(
                (item for item in remaining() if item.record_id not in protected),
                None,
            )
            if candidate is None:
                break
            dispositions[candidate.record_id] = CleanupDisposition.COUNT_QUOTA

        while sum(item.encoded.__len__() for item in remaining()) > policy.max_total_bytes:
            candidate = next(
                (item for item in remaining() if item.record_id not in protected),
                None,
            )
            if candidate is None:
                break
            dispositions[candidate.record_id] = CleanupDisposition.BYTE_QUOTA

        all_candidates = tuple(
            StateCleanupCandidate(
                record_id=record.record_id,
                checksum=record.checksum,
                size_bytes=len(record.encoded),
                created_at=record_timestamps[record.record_id],
                disposition=dispositions[record.record_id],
            )
            for record in ordered
            if record.record_id in dispositions
        )
        candidates = all_candidates[: policy.batch_size]
        remaining_candidate_count = len(all_candidates) - len(candidates)
        next_cursor = candidates[-1].record_id if remaining_candidate_count else None
        retained = [item for item in ordered if item.record_id not in dispositions]
        payload = self._cleanup_payload(
            collection=safe_collection,
            candidates=candidates,
            protected_record_ids=tuple(sorted(protected)),
            orphan_references=tuple(sorted(orphan_references)),
            retained_records=len(retained),
            retained_bytes=sum(len(item.encoded) for item in retained),
            remaining_candidate_count=remaining_candidate_count,
            next_cursor=next_cursor,
        )
        digest = self._sha256(self._canonical_bytes(payload))
        return StateCleanupPreview(
            plan_id=f"clean-{digest[:24]}",
            plan_digest=digest,
            collection=safe_collection,
            candidates=candidates,
            protected_record_ids=tuple(sorted(protected)),
            orphan_references=tuple(sorted(orphan_references)),
            retained_records=len(retained),
            retained_bytes=sum(len(item.encoded) for item in retained),
            remaining_candidate_count=remaining_candidate_count,
            next_cursor=next_cursor,
        )

    def _validate_cleanup_preview(self, preview: StateCleanupPreview) -> None:
        if not isinstance(preview, StateCleanupPreview):
            raise self._error(
                "cleanup preview is invalid",
                ErrorCode.STATE_INVALID,
            )
        payload = self._cleanup_payload(
            collection=preview.collection,
            candidates=preview.candidates,
            protected_record_ids=preview.protected_record_ids,
            orphan_references=preview.orphan_references,
            retained_records=preview.retained_records,
            retained_bytes=preview.retained_bytes,
            remaining_candidate_count=preview.remaining_candidate_count,
            next_cursor=preview.next_cursor,
        )
        digest = self._sha256(self._canonical_bytes(payload))
        if preview.plan_digest != digest or preview.plan_id != f"clean-{digest[:24]}":
            raise self._error(
                "cleanup preview digest is invalid",
                ErrorCode.STATE_INVALID,
            )

    def _cleanup_report(self, raw: dict[str, object]) -> StateCleanupReport | None:
        if raw.get("phase") != "committed":
            return None
        report = raw.get("report")
        if not isinstance(report, dict):
            raise self._error("cleanup journal report is corrupt", ErrorCode.STATE_CORRUPT)
        try:
            return StateCleanupReport(
                plan_id=str(report["plan_id"]),
                processed=int(report["processed"]),
                deleted=int(report["deleted"]),
                protected=int(report["protected"]),
                retained=int(report["retained"]),
                reclaimed_bytes=int(report["reclaimed_bytes"]),
                next_cursor=(
                    str(report["next_cursor"]) if report["next_cursor"] is not None else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._error("cleanup journal report is corrupt", ErrorCode.STATE_CORRUPT) from exc

    def _write_cleanup_journal(
        self,
        preview: StateCleanupPreview,
        *,
        phase: str,
        report: StateCleanupReport | None = None,
    ) -> None:
        self._write_json(
            self._journal_path(preview.plan_id),
            {
                "plan_id": preview.plan_id,
                "plan_digest": preview.plan_digest,
                "collection": preview.collection,
                "phase": phase,
                "report": (
                    {
                        "plan_id": report.plan_id,
                        "processed": report.processed,
                        "deleted": report.deleted,
                        "protected": report.protected,
                        "retained": report.retained,
                        "reclaimed_bytes": report.reclaimed_bytes,
                        "next_cursor": report.next_cursor,
                    }
                    if report is not None
                    else None
                ),
            },
        )

    def apply_cleanup(self, preview: StateCleanupPreview) -> StateCleanupReport:
        self._validate_cleanup_preview(preview)
        journal_path = self._journal_path(preview.plan_id)
        if journal_path.is_file():
            journal = self._read_json(journal_path, code=ErrorCode.STATE_CORRUPT)
            if journal.get("plan_digest") != preview.plan_digest:
                raise self._error(
                    "cleanup journal conflicts with the reviewed preview",
                    ErrorCode.STATE_INVALID,
                )
            report = self._cleanup_report(journal)
            if report is not None:
                return report

        with self._locks.lock(
            f"state-cleanup-{preview.collection}",
            timeout_seconds=10,
            metadata={"operation": "cleanup", "plan_id": preview.plan_id},
        ):
            if journal_path.is_file():
                existing = self._read_json(journal_path, code=ErrorCode.STATE_CORRUPT)
                report = self._cleanup_report(existing)
                if report is not None:
                    return report
            trash_dir = self.trash_root / self._record_id(preview.plan_id)
            trash_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(trash_dir, 0o700)
            self._write_cleanup_journal(preview, phase="applying")
            moved: list[tuple[Path, Path]] = []
            try:
                for index, item in enumerate(preview.candidates):
                    source = self._collection_root(preview.collection) / f"{item.record_id}.json"
                    trash = trash_dir / f"{item.record_id}.json"
                    if source.is_file():
                        data = source.read_bytes()
                        if not hmac.compare_digest(self._sha256(data), item.checksum):
                            raise self._error(
                                f"durable-state record {item.record_id} changed after cleanup preview",
                                ErrorCode.STATE_STALE,
                                retryable=True,
                            )
                        if self._fault_injector is not None:
                            self._fault_injector("before_cleanup_move", item.record_id, index)
                        os.replace(source, trash)
                        self._fsync_dir(source.parent)
                        self._fsync_dir(trash.parent)
                        moved.append((source, trash))
                    elif trash.is_file():
                        if not hmac.compare_digest(self._sha256(trash.read_bytes()), item.checksum):
                            raise self._error(
                                f"cleanup trash for {item.record_id} is corrupt",
                                ErrorCode.STATE_CORRUPT,
                            )
                    else:
                        raise self._error(
                            f"durable-state record {item.record_id} disappeared after cleanup preview",
                            ErrorCode.STATE_STALE,
                            retryable=True,
                        )
            except Exception as exc:
                for source, trash in reversed(moved):
                    if trash.is_file():
                        os.replace(trash, source)
                self._write_cleanup_journal(preview, phase="rolled_back")
                if isinstance(exc, RepoForgeError):
                    raise
                raise self._error(
                    "durable-state cleanup failed and was rolled back",
                    ErrorCode.STATE_INVALID,
                ) from exc

            report = StateCleanupReport(
                plan_id=preview.plan_id,
                processed=len(preview.candidates),
                deleted=len(preview.candidates),
                protected=len(preview.protected_record_ids),
                retained=preview.retained_records,
                reclaimed_bytes=sum(item.size_bytes for item in preview.candidates),
                next_cursor=preview.next_cursor,
            )
            self._write_cleanup_journal(preview, phase="committed", report=report)
            return report
