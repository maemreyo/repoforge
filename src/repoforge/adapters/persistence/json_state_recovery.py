"""Secret-safe integrity, backup, and restore workflows for JSON durable state."""

from __future__ import annotations

import hmac
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from ...domain.durable_state import SchemaVersion
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.state_lifecycle import (
    IntegritySeverity,
    StateBackupPreview,
    StateBackupRecord,
    StateBackupReport,
    StateIntegrityFinding,
    StateIntegrityReport,
    StateRecordReference,
    StateRestorePreview,
    StateRestoreReport,
    validate_state_collection,
)
from ...ports.locking import LockManager
from .json_state_lifecycle import FaultInjector, JsonStateLifecycleManager

_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BACKUP_ID = re.compile(r"^backup-[a-f0-9]{24}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_BACKUP_FORMAT_VERSION = 1
_BACKUP_MANIFEST_FIELDS = {
    "format_version",
    "backup_id",
    "collection",
    "destination_id",
    "records",
    "total_bytes",
    "manifest_checksum",
}
_BACKUP_RECORD_FIELDS = {
    "record_id",
    "checksum",
    "size_bytes",
    "schema_version",
    "revision",
}
_MAX_RECORDS = 2_000
_MAX_FINDINGS = 2_000
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


class JsonStateRecoveryManager(JsonStateLifecycleManager):
    """Validate and recover durable state without exposing payloads in result contracts."""

    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        *,
        max_record_bytes: int = 1_000_000,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        super().__init__(
            state_root,
            locks,
            max_record_bytes=max_record_bytes,
            fault_injector=fault_injector,
        )

    @staticmethod
    def _recovery_error(
        message: str,
        code: ErrorCode,
        *,
        retryable: bool = False,
    ) -> RepoForgeError:
        return RepoForgeError(
            message,
            code=code,
            retryable=retryable,
            safe_next_action=(
                "Re-run integrity inspection or recreate the checksum-bound backup/restore preview "
                "from current private durable state before applying changes."
            ),
        )

    @classmethod
    def _identity(cls, value: str, *, code: ErrorCode) -> str:
        if not isinstance(value, str) or _SAFE_IDENTITY.fullmatch(value) is None:
            raise cls._recovery_error("durable-state destination identity is invalid", code)
        return value

    @staticmethod
    def _bounded_positive_int(
        value: int,
        *,
        field: str,
        maximum: int,
        code: ErrorCode,
    ) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
            raise JsonStateRecoveryManager._recovery_error(
                f"{field} must be between 1 and {maximum}", code
            )
        return value

    @staticmethod
    def _manifest_int(
        value: object,
        *,
        field: str,
        minimum: int = 0,
        maximum: int = 1 << 50,
    ) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise JsonStateRecoveryManager._recovery_error(
                f"backup manifest {field} is invalid",
                ErrorCode.STATE_INVALID,
            )
        return value

    @classmethod
    def _supported_versions(
        cls,
        value: tuple[SchemaVersion, ...],
    ) -> tuple[SchemaVersion, ...]:
        if (
            not isinstance(value, tuple)
            or not value
            or not all(isinstance(item, SchemaVersion) for item in value)
        ):
            raise cls._recovery_error(
                "supported_versions must be a non-empty SchemaVersion tuple",
                ErrorCode.STATE_INVALID,
            )
        return tuple(sorted(set(value), key=lambda item: item.value))

    @classmethod
    def _references(
        cls,
        value: tuple[StateRecordReference, ...],
    ) -> tuple[StateRecordReference, ...]:
        if not isinstance(value, tuple) or not all(
            isinstance(item, StateRecordReference) for item in value
        ):
            raise cls._recovery_error(
                "references must be a StateRecordReference tuple",
                ErrorCode.STATE_INVALID,
            )
        return tuple(
            sorted(
                set(value),
                key=lambda item: (
                    item.source_record_id,
                    item.target_record_id,
                    item.relation,
                ),
            )
        )

    @staticmethod
    def _severity_order(value: IntegritySeverity) -> int:
        return {
            IntegritySeverity.ERROR: 0,
            IntegritySeverity.WARNING: 1,
            IntegritySeverity.INFO: 2,
        }[value]

    def inspect_integrity(
        self,
        *,
        collection: str,
        supported_versions: tuple[SchemaVersion, ...],
        references: tuple[StateRecordReference, ...] = (),
        max_records: int = _MAX_RECORDS,
        max_total_bytes: int = 1 << 40,
        max_findings: int = 100,
    ) -> StateIntegrityReport:
        safe_collection = validate_state_collection(collection)
        quota_records = self._bounded_positive_int(
            max_records,
            field="max_records",
            maximum=_MAX_RECORDS,
            code=ErrorCode.STATE_INVALID,
        )
        quota_bytes = self._bounded_positive_int(
            max_total_bytes,
            field="max_total_bytes",
            maximum=1 << 50,
            code=ErrorCode.STATE_INVALID,
        )
        findings_limit = self._bounded_positive_int(
            max_findings,
            field="max_findings",
            maximum=_MAX_FINDINGS,
            code=ErrorCode.STATE_INVALID,
        )
        if (
            not isinstance(supported_versions, tuple)
            or not supported_versions
            or not all(isinstance(item, SchemaVersion) for item in supported_versions)
        ):
            raise self._recovery_error(
                "supported_versions must be a non-empty SchemaVersion tuple",
                ErrorCode.STATE_INVALID,
            )
        if not isinstance(references, tuple) or not all(
            isinstance(item, StateRecordReference) for item in references
        ):
            raise self._recovery_error(
                "references must be a StateRecordReference tuple",
                ErrorCode.STATE_INVALID,
            )

        paths, scan_truncated = self._record_paths(
            safe_collection,
            max_records=_MAX_RECORDS,
        )
        supported = {item.value for item in supported_versions}
        findings: list[StateIntegrityFinding] = []
        total_bytes = 0
        observed_ids = {path.stem for path in paths}
        valid_ids: set[str] = set()
        for path in paths:
            with suppress(OSError):
                total_bytes += path.stat().st_size
            try:
                record = self._decode_record(path, expected_record_id=path.stem)
            except RepoForgeError:
                findings.append(
                    StateIntegrityFinding(
                        IntegritySeverity.ERROR,
                        "CORRUPT_RECORD",
                        path.stem,
                        "The record is unreadable or violates the shared envelope.",
                    )
                )
                continue
            valid_ids.add(record.record_id)
            if record.schema_version.value not in supported:
                findings.append(
                    StateIntegrityFinding(
                        IntegritySeverity.ERROR,
                        "UNSUPPORTED_SCHEMA",
                        record.record_id,
                        f"Schema version {record.schema_version.value} is not supported.",
                    )
                )

        if scan_truncated or len(paths) > quota_records:
            findings.append(
                StateIntegrityFinding(
                    IntegritySeverity.ERROR,
                    "RECORD_QUOTA_EXCEEDED",
                    None,
                    "The collection exceeds its reviewed record quota.",
                )
            )
        if total_bytes > quota_bytes:
            findings.append(
                StateIntegrityFinding(
                    IntegritySeverity.ERROR,
                    "BYTE_QUOTA_EXCEEDED",
                    None,
                    "The collection exceeds its reviewed byte quota.",
                )
            )
        for reference in references:
            if (
                reference.source_record_id in observed_ids
                and reference.target_record_id not in valid_ids
            ):
                findings.append(
                    StateIntegrityFinding(
                        IntegritySeverity.ERROR,
                        "MISSING_REFERENCE",
                        reference.source_record_id,
                        f"Reference {reference.relation} targets a missing or corrupt record.",
                    )
                )

        findings.sort(
            key=lambda item: (
                self._severity_order(item.severity),
                item.code,
                item.record_id or "",
                item.message,
            )
        )
        findings_truncated = len(findings) > findings_limit
        bounded = tuple(findings[:findings_limit])
        return StateIntegrityReport(
            collection=safe_collection,
            scanned_records=len(paths),
            total_bytes=total_bytes,
            findings=bounded,
            findings_truncated=findings_truncated,
            healthy=not any(item.severity is IntegritySeverity.ERROR for item in findings),
        )

    @staticmethod
    def _backup_manifest_payload(
        *,
        backup_id: str | None,
        collection: str,
        destination_id: str,
        records: tuple[StateBackupRecord, ...],
        total_bytes: int,
    ) -> dict[str, object]:
        return {
            "format_version": 1,
            "backup_id": backup_id,
            "collection": collection,
            "destination_id": destination_id,
            "records": [
                {
                    "record_id": item.record_id,
                    "checksum": item.checksum,
                    "size_bytes": item.size_bytes,
                    "schema_version": item.schema_version.value,
                    "revision": item.revision,
                }
                for item in records
            ],
            "total_bytes": total_bytes,
        }

    def preview_backup(
        self,
        *,
        collection: str,
        destination_id: str,
        max_records: int = _MAX_RECORDS,
        max_total_bytes: int = 1 << 40,
    ) -> StateBackupPreview:
        safe_collection = validate_state_collection(collection)
        safe_destination = self._identity(
            destination_id,
            code=ErrorCode.STATE_INVALID,
        )
        records_limit = self._bounded_positive_int(
            max_records,
            field="max_records",
            maximum=_MAX_RECORDS,
            code=ErrorCode.STATE_INVALID,
        )
        bytes_limit = self._bounded_positive_int(
            max_total_bytes,
            field="max_total_bytes",
            maximum=1 << 50,
            code=ErrorCode.STATE_INVALID,
        )
        paths, truncated = self._record_paths(safe_collection, max_records=_MAX_RECORDS)
        if truncated or len(paths) > records_limit:
            raise self._recovery_error(
                "backup exceeds its reviewed record quota",
                ErrorCode.STATE_TOO_LARGE,
            )
        records: list[StateBackupRecord] = []
        total_bytes = 0
        for path in paths:
            try:
                record = self._decode_record(path, expected_record_id=path.stem)
            except RepoForgeError as exc:
                raise self._recovery_error(
                    f"backup source record {path.stem} is corrupt",
                    ErrorCode.STATE_INVALID,
                ) from exc
            total_bytes += len(record.encoded)
            records.append(
                StateBackupRecord(
                    record_id=record.record_id,
                    checksum=record.checksum,
                    size_bytes=len(record.encoded),
                    schema_version=record.schema_version,
                    revision=record.revision.value,
                )
            )
        if total_bytes > bytes_limit:
            raise self._recovery_error(
                "backup exceeds its reviewed byte quota",
                ErrorCode.STATE_TOO_LARGE,
            )
        ordered = tuple(sorted(records, key=lambda item: item.record_id))
        seed_payload = self._backup_manifest_payload(
            backup_id=None,
            collection=safe_collection,
            destination_id=safe_destination,
            records=ordered,
            total_bytes=total_bytes,
        )
        seed = self._sha256(self._canonical_bytes(seed_payload))
        backup_id = f"backup-{seed[:24]}"
        manifest_payload = self._backup_manifest_payload(
            backup_id=backup_id,
            collection=safe_collection,
            destination_id=safe_destination,
            records=ordered,
            total_bytes=total_bytes,
        )
        checksum = self._sha256(self._canonical_bytes(manifest_payload))
        return StateBackupPreview(
            backup_id=backup_id,
            manifest_checksum=checksum,
            collection=safe_collection,
            destination_id=safe_destination,
            records=ordered,
            total_bytes=total_bytes,
        )

    def _validate_backup_preview(self, preview: StateBackupPreview) -> None:
        if not isinstance(preview, StateBackupPreview):
            raise self._recovery_error("backup preview is invalid", ErrorCode.STATE_INVALID)
        payload = self._backup_manifest_payload(
            backup_id=preview.backup_id,
            collection=preview.collection,
            destination_id=preview.destination_id,
            records=preview.records,
            total_bytes=preview.total_bytes,
        )
        checksum = self._sha256(self._canonical_bytes(payload))
        if not hmac.compare_digest(checksum, preview.manifest_checksum):
            raise self._recovery_error(
                "backup preview checksum is invalid", ErrorCode.STATE_INVALID
            )

    @staticmethod
    def _resolved_destination(path: Path) -> Path:
        return path.expanduser().resolve()

    def _validate_partial_backup_destination(
        self,
        destination: Path,
        preview: StateBackupPreview,
        *,
        repair: bool,
    ) -> None:
        if not destination.exists():
            return
        if not destination.is_dir():
            raise self._recovery_error(
                "backup destination must be a directory",
                ErrorCode.STATE_INVALID,
            )
        for child in destination.iterdir():
            if child.name not in {"manifest.json", "records"}:
                raise self._recovery_error(
                    "backup destination contains unrelated entries",
                    ErrorCode.STATE_INVALID,
                )
        records_root = destination / "records"
        if not records_root.exists():
            return
        if not records_root.is_dir():
            raise self._recovery_error(
                "backup records path must be a directory",
                ErrorCode.STATE_INVALID,
            )
        expected = {item.record_id: item for item in preview.records}
        for child in records_root.iterdir():
            if not child.is_file() or child.suffix != ".json" or child.stem not in expected:
                raise self._recovery_error(
                    "backup destination contains an unrelated record",
                    ErrorCode.STATE_INVALID,
                )
            data = child.read_bytes()
            if (
                not hmac.compare_digest(self._sha256(data), expected[child.stem].checksum)
                and not repair
            ):
                raise self._recovery_error(
                    f"partial backup record {child.stem} does not match the reviewed preview",
                    ErrorCode.STATE_INVALID,
                )

    def apply_backup(
        self,
        preview: StateBackupPreview,
        *,
        destination_root: Path,
        repair: bool = False,
    ) -> StateBackupReport:
        self._validate_backup_preview(preview)
        destination = self._resolved_destination(destination_root)
        if (
            destination.name != preview.destination_id
            or destination == self.root
            or self.root in destination.parents
        ):
            raise self._recovery_error(
                "backup destination path does not match its bound identity or is inside source state",
                ErrorCode.STATE_INVALID,
            )
        if not isinstance(repair, bool):
            raise self._recovery_error("repair must be a boolean", ErrorCode.STATE_INVALID)
        manifest_path = destination / "manifest.json"
        if manifest_path.is_file() and not repair:
            existing = self._read_backup(destination)
            if existing == preview:
                return StateBackupReport(
                    preview.backup_id,
                    len(preview.records),
                    preview.total_bytes,
                    preview.destination_id,
                )
            raise self._recovery_error(
                "backup destination contains a different manifest",
                ErrorCode.STATE_INVALID,
            )
        self._validate_partial_backup_destination(destination, preview, repair=repair)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination, 0o700)
        records_root = destination / "records"
        records_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(records_root, 0o700)
        with self._locks.lock(
            f"state-backup-{preview.backup_id}",
            timeout_seconds=10,
            metadata={"operation": "backup", "backup_id": preview.backup_id},
        ):
            for index, item in enumerate(preview.records):
                source = self._collection_root(preview.collection) / f"{item.record_id}.json"
                try:
                    data = source.read_bytes()
                except OSError as exc:
                    raise self._recovery_error(
                        f"backup source record {item.record_id} is unavailable",
                        ErrorCode.STATE_INVALID,
                    ) from exc
                if not hmac.compare_digest(self._sha256(data), item.checksum):
                    raise self._recovery_error(
                        f"backup source record {item.record_id} changed after preview",
                        ErrorCode.STATE_INVALID,
                        retryable=True,
                    )
                target = records_root / f"{item.record_id}.json"
                if target.is_file() and hmac.compare_digest(
                    self._sha256(target.read_bytes()), item.checksum
                ):
                    continue
                if self._fault_injector is not None:
                    self._fault_injector("before_backup_write", item.record_id, index)
                self._atomic_write(target, data)
            manifest_payload = self._backup_manifest_payload(
                backup_id=preview.backup_id,
                collection=preview.collection,
                destination_id=preview.destination_id,
                records=preview.records,
                total_bytes=preview.total_bytes,
            )
            self._write_json(
                manifest_path,
                {
                    **manifest_payload,
                    "manifest_checksum": preview.manifest_checksum,
                },
            )
        return StateBackupReport(
            preview.backup_id,
            len(preview.records),
            preview.total_bytes,
            preview.destination_id,
        )

    def _read_backup(self, backup_root: Path) -> StateBackupPreview:
        root = self._resolved_destination(backup_root)
        try:
            manifest_data = (root / "manifest.json").read_bytes()
        except OSError as exc:
            raise self._recovery_error(
                "backup manifest is unavailable or corrupt",
                ErrorCode.STATE_INVALID,
            ) from exc
        if len(manifest_data) > _MAX_MANIFEST_BYTES:
            raise self._recovery_error(
                "backup manifest exceeds its reviewed size bound",
                ErrorCode.STATE_INVALID,
            )
        try:
            raw: Any = json.loads(manifest_data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._recovery_error(
                "backup manifest is unavailable or corrupt",
                ErrorCode.STATE_INVALID,
            ) from exc
        if not isinstance(raw, dict) or set(raw) != _BACKUP_MANIFEST_FIELDS:
            raise self._recovery_error(
                "backup manifest fields do not match format version 1",
                ErrorCode.STATE_INVALID,
            )
        format_version = raw.get("format_version")
        if (
            not isinstance(format_version, int)
            or isinstance(format_version, bool)
            or format_version != _BACKUP_FORMAT_VERSION
        ):
            raise self._recovery_error(
                f"unsupported backup manifest format version: {format_version!r}",
                ErrorCode.STATE_INVALID,
            )
        backup_id_raw = raw.get("backup_id")
        collection_raw = raw.get("collection")
        destination_raw = raw.get("destination_id")
        checksum_raw = raw.get("manifest_checksum")
        records_raw = raw.get("records")
        if (
            not isinstance(backup_id_raw, str)
            or _BACKUP_ID.fullmatch(backup_id_raw) is None
            or not isinstance(collection_raw, str)
            or not isinstance(destination_raw, str)
            or not isinstance(checksum_raw, str)
            or _SHA256.fullmatch(checksum_raw) is None
            or not isinstance(records_raw, list)
            or len(records_raw) > _MAX_RECORDS
        ):
            raise self._recovery_error(
                "backup manifest fields are invalid",
                ErrorCode.STATE_INVALID,
            )
        backup_id = backup_id_raw
        collection = validate_state_collection(collection_raw)
        destination_id = self._identity(destination_raw, code=ErrorCode.STATE_INVALID)
        manifest_checksum = checksum_raw
        total_bytes = self._manifest_int(raw.get("total_bytes"), field="total_bytes")
        if root.name != destination_id:
            raise self._recovery_error(
                "backup destination identity is invalid",
                ErrorCode.STATE_INVALID,
            )

        records: list[StateBackupRecord] = []
        seen_ids: set[str] = set()
        for item in records_raw:
            if not isinstance(item, dict) or set(item) != _BACKUP_RECORD_FIELDS:
                raise self._recovery_error(
                    "backup record manifest entry fields are invalid",
                    ErrorCode.STATE_INVALID,
                )
            record_id_raw = item.get("record_id")
            checksum = item.get("checksum")
            schema_version_raw = item.get("schema_version")
            if (
                not isinstance(record_id_raw, str)
                or not isinstance(checksum, str)
                or _SHA256.fullmatch(checksum) is None
                or not isinstance(schema_version_raw, int)
                or isinstance(schema_version_raw, bool)
            ):
                raise self._recovery_error(
                    "backup record manifest entry is invalid",
                    ErrorCode.STATE_INVALID,
                )
            record_id = self._record_id(record_id_raw)
            if record_id in seen_ids:
                raise self._recovery_error(
                    f"backup manifest contains duplicate record {record_id}",
                    ErrorCode.STATE_INVALID,
                )
            seen_ids.add(record_id)
            size_bytes = self._manifest_int(item.get("size_bytes"), field="record size")
            revision = self._manifest_int(
                item.get("revision"),
                field="record revision",
                minimum=1,
            )
            try:
                schema_version = SchemaVersion(schema_version_raw)
            except ValueError as exc:
                raise self._recovery_error(
                    "backup record schema version is invalid",
                    ErrorCode.STATE_INVALID,
                ) from exc
            record = StateBackupRecord(
                record_id=record_id,
                checksum=checksum,
                size_bytes=size_bytes,
                schema_version=schema_version,
                revision=revision,
            )
            record_path = root / "records" / f"{record.record_id}.json"
            try:
                decoded = self._decode_record(record_path, expected_record_id=record.record_id)
            except RepoForgeError as exc:
                raise self._recovery_error(
                    f"backup record {record.record_id} is missing or corrupt",
                    ErrorCode.STATE_INVALID,
                ) from exc
            if (
                len(decoded.encoded) != record.size_bytes
                or not hmac.compare_digest(decoded.checksum, record.checksum)
                or decoded.schema_version != record.schema_version
                or decoded.revision.value != record.revision
            ):
                raise self._recovery_error(
                    f"backup record {record.record_id} does not match its manifest metadata",
                    ErrorCode.STATE_INVALID,
                )
            records.append(record)

        ordered = tuple(sorted(records, key=lambda item: item.record_id))
        seed_payload = self._backup_manifest_payload(
            backup_id=None,
            collection=collection,
            destination_id=destination_id,
            records=ordered,
            total_bytes=total_bytes,
        )
        expected_backup_id = f"backup-{self._sha256(self._canonical_bytes(seed_payload))[:24]}"
        if backup_id != expected_backup_id:
            raise self._recovery_error(
                "backup identity does not match its manifest contents",
                ErrorCode.STATE_INVALID,
            )
        payload = self._backup_manifest_payload(
            backup_id=backup_id,
            collection=collection,
            destination_id=destination_id,
            records=ordered,
            total_bytes=total_bytes,
        )
        if not hmac.compare_digest(self._sha256(self._canonical_bytes(payload)), manifest_checksum):
            raise self._recovery_error(
                "backup manifest checksum is invalid", ErrorCode.STATE_INVALID
            )
        if sum(item.size_bytes for item in ordered) != total_bytes:
            raise self._recovery_error(
                "backup manifest byte total is invalid", ErrorCode.STATE_INVALID
            )
        return StateBackupPreview(
            backup_id,
            manifest_checksum,
            collection,
            destination_id,
            ordered,
            total_bytes,
        )

    def _validate_restore_references(
        self,
        backup: StateBackupPreview,
        references: tuple[StateRecordReference, ...],
    ) -> None:
        backup_ids = {item.record_id for item in backup.records}
        collection_root = self._collection_root(backup.collection)
        for reference in references:
            if reference.source_record_id not in backup_ids:
                continue
            if reference.target_record_id in backup_ids:
                continue
            target = collection_root / f"{reference.target_record_id}.json"
            try:
                self._decode_record(target, expected_record_id=reference.target_record_id)
            except RepoForgeError as exc:
                raise self._recovery_error(
                    f"restore reference {reference.relation} targets a missing or corrupt record",
                    ErrorCode.STATE_INVALID,
                ) from exc

    @staticmethod
    def _restore_payload(
        *,
        backup: StateBackupPreview,
        destination_id: str,
        supported_versions: tuple[SchemaVersion, ...],
        references: tuple[StateRecordReference, ...],
        conflicts: tuple[tuple[str, str], ...],
        overwrite: bool,
    ) -> dict[str, object]:
        return {
            "backup_id": backup.backup_id,
            "manifest_checksum": backup.manifest_checksum,
            "collection": backup.collection,
            "destination_id": destination_id,
            "records": [
                {
                    "record_id": item.record_id,
                    "checksum": item.checksum,
                    "size_bytes": item.size_bytes,
                    "schema_version": item.schema_version.value,
                    "revision": item.revision,
                }
                for item in backup.records
            ],
            "supported_versions": [item.value for item in supported_versions],
            "references": [
                {
                    "source_record_id": item.source_record_id,
                    "target_record_id": item.target_record_id,
                    "relation": item.relation,
                }
                for item in references
            ],
            "conflicts": [list(item) for item in conflicts],
            "total_bytes": backup.total_bytes,
            "overwrite": overwrite,
        }

    def preview_restore(
        self,
        *,
        backup_root: Path,
        destination_id: str,
        overwrite: bool,
        supported_versions: tuple[SchemaVersion, ...],
        references: tuple[StateRecordReference, ...] = (),
        max_records: int = _MAX_RECORDS,
        max_total_bytes: int = 1 << 40,
    ) -> StateRestorePreview:
        safe_destination = self._identity(destination_id, code=ErrorCode.STATE_INVALID)
        root_identity = self._identity(self.root.name, code=ErrorCode.STATE_INVALID)
        if safe_destination != root_identity:
            raise self._recovery_error(
                "restore destination identity does not match the current state root",
                ErrorCode.STATE_INVALID,
            )
        normalized_versions = self._supported_versions(supported_versions)
        normalized_references = self._references(references)
        if not isinstance(overwrite, bool):
            raise self._recovery_error("overwrite must be a boolean", ErrorCode.STATE_INVALID)
        records_limit = self._bounded_positive_int(
            max_records,
            field="max_records",
            maximum=_MAX_RECORDS,
            code=ErrorCode.STATE_INVALID,
        )
        bytes_limit = self._bounded_positive_int(
            max_total_bytes,
            field="max_total_bytes",
            maximum=1 << 50,
            code=ErrorCode.STATE_INVALID,
        )
        backup = self._read_backup(backup_root)
        if len(backup.records) > records_limit or backup.total_bytes > bytes_limit:
            raise self._recovery_error(
                "restore exceeds its reviewed quota", ErrorCode.STATE_TOO_LARGE
            )
        supported = {item.value for item in normalized_versions}
        unsupported = sorted(
            {
                item.schema_version.value
                for item in backup.records
                if item.schema_version.value not in supported
            }
        )
        if unsupported:
            raise self._recovery_error(
                f"Unsupported state schema version: {unsupported[0]}",
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            )
        self._validate_restore_references(backup, normalized_references)
        conflicts: list[tuple[str, str]] = []
        collection_root = self._collection_root(backup.collection)
        for item in backup.records:
            existing = collection_root / f"{item.record_id}.json"
            if existing.is_file() and not hmac.compare_digest(
                self._sha256(existing.read_bytes()), item.checksum
            ):
                conflicts.append((item.record_id, "different_existing_record"))
        ordered_conflicts = tuple(sorted(conflicts))
        payload = self._restore_payload(
            backup=backup,
            destination_id=safe_destination,
            supported_versions=normalized_versions,
            references=normalized_references,
            conflicts=ordered_conflicts,
            overwrite=overwrite,
        )
        digest = self._sha256(self._canonical_bytes(payload))
        return StateRestorePreview(
            restore_id=f"restore-{digest[:24]}",
            plan_digest=digest,
            backup_id=backup.backup_id,
            manifest_checksum=backup.manifest_checksum,
            collection=backup.collection,
            destination_id=safe_destination,
            records=backup.records,
            supported_versions=normalized_versions,
            references=normalized_references,
            conflicts=ordered_conflicts,
            total_bytes=backup.total_bytes,
            overwrite=overwrite,
        )

    def _validate_restore_preview(
        self, preview: StateRestorePreview, backup: StateBackupPreview
    ) -> None:
        if not isinstance(preview, StateRestorePreview):
            raise self._recovery_error("restore preview is invalid", ErrorCode.STATE_INVALID)
        root_identity = self._identity(self.root.name, code=ErrorCode.STATE_INVALID)
        normalized_versions = self._supported_versions(preview.supported_versions)
        normalized_references = self._references(preview.references)
        if preview.destination_id != root_identity:
            raise self._recovery_error(
                "restore destination identity does not match the current state root",
                ErrorCode.STATE_INVALID,
            )
        if (
            preview.backup_id != backup.backup_id
            or preview.manifest_checksum != backup.manifest_checksum
            or preview.collection != backup.collection
            or preview.records != backup.records
            or preview.total_bytes != backup.total_bytes
            or preview.supported_versions != normalized_versions
            or preview.references != normalized_references
        ):
            raise self._recovery_error(
                "backup changed after restore preview", ErrorCode.STATE_INVALID
            )
        supported = {item.value for item in normalized_versions}
        if any(item.schema_version.value not in supported for item in backup.records):
            raise self._recovery_error(
                "restore preview contains an unsupported schema version",
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            )
        self._validate_restore_references(backup, normalized_references)
        payload = self._restore_payload(
            backup=backup,
            destination_id=preview.destination_id,
            supported_versions=normalized_versions,
            references=normalized_references,
            conflicts=preview.conflicts,
            overwrite=preview.overwrite,
        )
        digest = self._sha256(self._canonical_bytes(payload))
        if preview.plan_digest != digest or preview.restore_id != f"restore-{digest[:24]}":
            raise self._recovery_error("restore preview digest is invalid", ErrorCode.STATE_INVALID)

    def _restore_report(self, raw: dict[str, object]) -> StateRestoreReport | None:
        if raw.get("phase") != "committed":
            return None
        report = raw.get("report")
        if not isinstance(report, dict):
            raise self._recovery_error("restore journal report is corrupt", ErrorCode.STATE_CORRUPT)
        try:
            return StateRestoreReport(
                restore_id=str(report["restore_id"]),
                restored_records=int(report["restored_records"]),
                replaced_records=int(report["replaced_records"]),
                total_bytes=int(report["total_bytes"]),
                backup_id=str(report["backup_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._recovery_error(
                "restore journal report is corrupt", ErrorCode.STATE_CORRUPT
            ) from exc

    def _write_restore_journal(
        self,
        preview: StateRestorePreview,
        *,
        phase: str,
        created_record_ids: tuple[str, ...],
        replaced_record_ids: tuple[str, ...],
        report: StateRestoreReport | None = None,
    ) -> None:
        self._write_json(
            self._journal_path(preview.restore_id),
            {
                "restore_id": preview.restore_id,
                "plan_digest": preview.plan_digest,
                "backup_id": preview.backup_id,
                "collection": preview.collection,
                "phase": phase,
                "created_record_ids": list(created_record_ids),
                "replaced_record_ids": list(replaced_record_ids),
                "report": (
                    {
                        "restore_id": report.restore_id,
                        "restored_records": report.restored_records,
                        "replaced_records": report.replaced_records,
                        "total_bytes": report.total_bytes,
                        "backup_id": report.backup_id,
                    }
                    if report is not None
                    else None
                ),
            },
        )

    def _rollback_restore(
        self,
        *,
        restore_id: str,
        collection: str,
        created_record_ids: tuple[str, ...],
        replaced_record_ids: tuple[str, ...],
    ) -> None:
        collection_root = self._collection_root(collection)
        destination_backup = self.backups_root / restore_id / "destination"
        for record_id in created_record_ids:
            path = collection_root / f"{record_id}.json"
            path.unlink(missing_ok=True)
        for record_id in replaced_record_ids:
            backup_path = destination_backup / f"{record_id}.json"
            try:
                original = backup_path.read_bytes()
            except OSError as exc:
                raise self._recovery_error(
                    f"destination backup for {record_id} is unavailable",
                    ErrorCode.STATE_CORRUPT,
                ) from exc
            self._atomic_write(collection_root / f"{record_id}.json", original)

    def apply_restore(
        self,
        preview: StateRestorePreview,
        *,
        backup_root: Path,
    ) -> StateRestoreReport:
        backup = self._read_backup(backup_root)
        self._validate_restore_preview(preview, backup)
        if preview.conflicts and not preview.overwrite:
            raise self._recovery_error(
                "restore has unresolved destination conflicts",
                ErrorCode.ALREADY_EXISTS,
            )
        journal_path = self._journal_path(preview.restore_id)
        if journal_path.is_file():
            journal = self._read_json(journal_path, code=ErrorCode.STATE_CORRUPT)
            if journal.get("plan_digest") != preview.plan_digest:
                raise self._recovery_error(
                    "restore journal conflicts with the reviewed preview",
                    ErrorCode.ALREADY_EXISTS,
                )
            report = self._restore_report(journal)
            if report is not None:
                return report

        with self._locks.lock(
            f"state-restore-{preview.destination_id}",
            timeout_seconds=10,
            metadata={"operation": "restore", "restore_id": preview.restore_id},
        ):
            if journal_path.is_file():
                existing_journal = self._read_json(journal_path, code=ErrorCode.STATE_CORRUPT)
                existing_report = self._restore_report(existing_journal)
                if existing_report is not None:
                    return existing_report
            collection_root = self._collection_root(preview.collection)
            collection_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(collection_root, 0o700)
            destination_backup = self.backups_root / preview.restore_id / "destination"
            destination_backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination_backup, 0o700)
            created: list[str] = []
            replaced: list[str] = []
            for item in preview.records:
                destination = collection_root / f"{item.record_id}.json"
                if not destination.is_file():
                    created.append(item.record_id)
                    continue
                current = destination.read_bytes()
                if hmac.compare_digest(self._sha256(current), item.checksum):
                    continue
                if not preview.overwrite:
                    raise self._recovery_error(
                        f"restore destination record {item.record_id} conflicts",
                        ErrorCode.ALREADY_EXISTS,
                    )
                self._atomic_write(destination_backup / f"{item.record_id}.json", current)
                replaced.append(item.record_id)
            created_ids = tuple(sorted(created))
            replaced_ids = tuple(sorted(replaced))
            self._write_restore_journal(
                preview,
                phase="applying",
                created_record_ids=created_ids,
                replaced_record_ids=replaced_ids,
            )
            try:
                for index, item in enumerate(preview.records):
                    source = (
                        self._resolved_destination(backup_root)
                        / "records"
                        / f"{item.record_id}.json"
                    )
                    data = source.read_bytes()
                    destination = collection_root / f"{item.record_id}.json"
                    if destination.is_file() and hmac.compare_digest(
                        self._sha256(destination.read_bytes()), item.checksum
                    ):
                        continue
                    if self._fault_injector is not None:
                        self._fault_injector("before_restore_write", item.record_id, index)
                    self._atomic_write(destination, data)
            except Exception as exc:
                self._rollback_restore(
                    restore_id=preview.restore_id,
                    collection=preview.collection,
                    created_record_ids=created_ids,
                    replaced_record_ids=replaced_ids,
                )
                self._write_restore_journal(
                    preview,
                    phase="rolled_back",
                    created_record_ids=created_ids,
                    replaced_record_ids=replaced_ids,
                )
                raise self._recovery_error(
                    "durable-state restore failed and was rolled back",
                    ErrorCode.STATE_PERSISTENCE_FAILED,
                ) from exc
            report = StateRestoreReport(
                restore_id=preview.restore_id,
                restored_records=len(preview.records),
                replaced_records=len(replaced_ids),
                total_bytes=preview.total_bytes,
                backup_id=preview.backup_id,
            )
            self._write_restore_journal(
                preview,
                phase="committed",
                created_record_ids=created_ids,
                replaced_record_ids=replaced_ids,
                report=report,
            )
            return report

    def recover_incomplete_restores(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for path in sorted(self.journals_root.glob("restore-*.json")):
            journal = self._read_json(path, code=ErrorCode.STATE_CORRUPT)
            if journal.get("phase") != "applying":
                continue
            restore_id = self._record_id(str(journal.get("restore_id")))
            collection = validate_state_collection(str(journal.get("collection")))
            created_raw = journal.get("created_record_ids")
            replaced_raw = journal.get("replaced_record_ids")
            if not isinstance(created_raw, list) or not isinstance(replaced_raw, list):
                raise self._recovery_error(
                    "restore journal recovery metadata is corrupt",
                    ErrorCode.STATE_CORRUPT,
                )
            created = tuple(self._record_id(str(item)) for item in created_raw)
            replaced = tuple(self._record_id(str(item)) for item in replaced_raw)
            with self._locks.lock(
                f"state-restore-recovery-{restore_id}",
                timeout_seconds=10,
                metadata={"operation": "restore_recovery", "restore_id": restore_id},
            ):
                self._rollback_restore(
                    restore_id=restore_id,
                    collection=collection,
                    created_record_ids=created,
                    replaced_record_ids=replaced,
                )
                self._write_json(path, {**journal, "phase": "rolled_back"})
            recovered.append(restore_id)
        return tuple(recovered)
