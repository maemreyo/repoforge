"""Private checksum-framed, content-addressed persistence for normalized evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    MAX_EVIDENCE_ARTIFACT_BYTES,
    EvidenceArtifactRef,
    EvidenceItem,
    EvidenceMeasure,
    EvidenceProvenance,
    EvidenceQuery,
    EvidenceScope,
    EvidenceSnapshot,
    EvidenceSourceKind,
    EvidenceStatus,
    evidence_matches_query,
    evidence_payload,
    evidence_status_for,
    mark_evidence_conflicts,
    validate_evidence_id,
    validate_evidence_item,
)
from ...ports.evidence_store import EvidencePage, EvidenceRetentionReport
from ...ports.locking import LockManager

_FRAME_VERSION = 1
_FRAME_FIELDS = {"frame_version", "payload_sha256", "evidence"}
_ITEM_FIELDS = {
    "evidence_id",
    "source_kind",
    "provider_id",
    "provider_version",
    "provenance",
    "scope",
    "snapshot",
    "summary",
    "coverage",
    "confidence",
    "status",
    "conflict_group",
    "content_digest",
    "created_at",
    "expires_at",
    "schema_version",
}
_PROVENANCE_FIELDS = {"source_reference", "provider_run_id", "artifact"}
_ARTIFACT_FIELDS = {"digest", "media_type", "size_bytes", "required"}
_SCOPE_FIELDS = {"paths", "symbols", "flows", "tests"}
_SNAPSHOT_FIELDS = {
    "snapshot_id",
    "repo_id",
    "workspace_id",
    "head_sha",
    "workspace_fingerprint",
    "config_generation",
    "policy_hash",
}
_MEASURE_FIELDS = {"value", "reason"}
_SHA64 = re.compile(r"^[a-f0-9]{64}$")
_DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_ITEMS = 2_000
_MAX_ITEM_FRAME_BYTES = 64 * 1024


class JsonEvidenceStore:
    """Store normalized summaries separately from optional provider artifact bytes."""

    def __init__(
        self,
        state_root: Path,
        locks: LockManager,
        *,
        max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
        max_artifact_bytes: int = MAX_EVIDENCE_ARTIFACT_BYTES,
        max_items: int = _DEFAULT_MAX_ITEMS,
    ) -> None:
        for field, value in (
            ("max_total_bytes", max_total_bytes),
            ("max_artifact_bytes", max_artifact_bytes),
            ("max_items", max_items),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise self._error(
                    f"{field} must be a positive integer",
                    code=ErrorCode.EVIDENCE_INVALID,
                )
        if max_artifact_bytes > MAX_EVIDENCE_ARTIFACT_BYTES:
            raise self._error(
                "max_artifact_bytes exceeds the reviewed domain bound",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        self.root = state_root.expanduser().resolve() / "evidence"
        self.items_root = self.root / "items"
        self.artifacts_root = self.root / "artifacts"
        self._locks = locks
        self.max_total_bytes = max_total_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.max_items = max_items
        self._process_lock = threading.RLock()
        for directory in (self.root, self.items_root, self.artifacts_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    @staticmethod
    def _error(
        message: str,
        *,
        code: ErrorCode,
        retryable: bool = False,
    ) -> RepoForgeError:
        return RepoForgeError(
            message,
            code=code,
            retryable=retryable,
            safe_next_action=(
                "Inspect private evidence IDs, checksums, permissions, quotas, and protected references; "
                "then retry from normalized provider output."
            ),
        )

    def _item_path(self, evidence_id: str) -> Path:
        return self.items_root / f"{validate_evidence_id(evidence_id)}.json"

    def _artifact_path(self, digest: str) -> Path:
        if not isinstance(digest, str) or _SHA64.fullmatch(digest) is None:
            raise self._error(
                "artifact digest must be a lowercase SHA-256",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        return self.artifacts_root / f"{digest}.blob"

    @staticmethod
    def _canonical_payload_bytes(payload: object) -> bytes:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    @staticmethod
    def _frame(item: EvidenceItem) -> dict[str, object]:
        normalized = validate_evidence_item(item)
        payload = evidence_payload(normalized)
        return {
            "frame_version": _FRAME_VERSION,
            "payload_sha256": hashlib.sha256(
                JsonEvidenceStore._canonical_payload_bytes(payload)
            ).hexdigest(),
            "evidence": payload,
        }

    @staticmethod
    def _encode(item: EvidenceItem) -> bytes:
        encoded = (
            json.dumps(
                JsonEvidenceStore._frame(item),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_ITEM_FRAME_BYTES:
            raise JsonEvidenceStore._error(
                "evidence item frame exceeds the reviewed size bound",
                code=ErrorCode.EVIDENCE_QUOTA_EXCEEDED,
            )
        return encoded

    @staticmethod
    def encode_for_test(item: EvidenceItem) -> bytes:
        """Return deterministic bytes for persistence-contract tests."""

        return JsonEvidenceStore._encode(item)

    @staticmethod
    def _string_tuple(value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{field} must be a string list")
        return tuple(value)

    @staticmethod
    def _decode_measure(value: object, field: str) -> EvidenceMeasure:
        if not isinstance(value, dict) or set(value) != _MEASURE_FIELDS:
            raise TypeError(f"{field} fields do not match schema version 1")
        return EvidenceMeasure(value=value["value"], reason=value["reason"])

    @staticmethod
    def _decode_artifact(value: object) -> EvidenceArtifactRef | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != _ARTIFACT_FIELDS:
            raise TypeError("artifact fields do not match schema version 1")
        return EvidenceArtifactRef(
            digest=value["digest"],
            media_type=value["media_type"],
            size_bytes=value["size_bytes"],
            required=value["required"],
        )

    @staticmethod
    def _decode_item(payload: object, *, expected_evidence_id: str) -> EvidenceItem:
        if not isinstance(payload, dict):
            raise TypeError("evidence payload must be an object")
        version = payload.get("schema_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != EVIDENCE_SCHEMA_VERSION
        ):
            raise JsonEvidenceStore._error(
                f"Unsupported evidence schema version: {version!r}",
                code=ErrorCode.EVIDENCE_SCHEMA_UNSUPPORTED,
            )
        if set(payload) != _ITEM_FIELDS:
            raise TypeError("evidence fields do not match schema version 1")
        if payload.get("evidence_id") != expected_evidence_id:
            raise TypeError("evidence identity does not match its filename")
        provenance = payload["provenance"]
        scope = payload["scope"]
        snapshot = payload["snapshot"]
        if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_FIELDS:
            raise TypeError("provenance fields do not match schema version 1")
        if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
            raise TypeError("scope fields do not match schema version 1")
        if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
            raise TypeError("snapshot fields do not match schema version 1")
        item = EvidenceItem(
            evidence_id=payload["evidence_id"],
            source_kind=EvidenceSourceKind(payload["source_kind"]),
            provider_id=payload["provider_id"],
            provider_version=payload["provider_version"],
            provenance=EvidenceProvenance(
                source_reference=provenance["source_reference"],
                provider_run_id=provenance["provider_run_id"],
                artifact=JsonEvidenceStore._decode_artifact(provenance["artifact"]),
            ),
            scope=EvidenceScope(
                paths=JsonEvidenceStore._string_tuple(scope["paths"], "paths"),
                symbols=JsonEvidenceStore._string_tuple(scope["symbols"], "symbols"),
                flows=JsonEvidenceStore._string_tuple(scope["flows"], "flows"),
                tests=JsonEvidenceStore._string_tuple(scope["tests"], "tests"),
            ),
            snapshot=EvidenceSnapshot(
                snapshot_id=snapshot["snapshot_id"],
                repo_id=snapshot["repo_id"],
                workspace_id=snapshot["workspace_id"],
                head_sha=snapshot["head_sha"],
                workspace_fingerprint=snapshot["workspace_fingerprint"],
                config_generation=snapshot["config_generation"],
                policy_hash=snapshot["policy_hash"],
            ),
            summary=payload["summary"],
            coverage=JsonEvidenceStore._decode_measure(payload["coverage"], "coverage"),
            confidence=JsonEvidenceStore._decode_measure(payload["confidence"], "confidence"),
            status=EvidenceStatus(payload["status"]),
            conflict_group=payload["conflict_group"],
            content_digest=payload["content_digest"],
            created_at=payload["created_at"],
            expires_at=payload["expires_at"],
            schema_version=payload["schema_version"],
        )
        return validate_evidence_item(item)

    @staticmethod
    def _decode(data: bytes, *, expected_evidence_id: str) -> EvidenceItem:
        if len(data) > _MAX_ITEM_FRAME_BYTES:
            raise JsonEvidenceStore._error(
                "evidence frame exceeds the reviewed size bound",
                code=ErrorCode.EVIDENCE_CORRUPT,
            )
        try:
            frame: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonEvidenceStore._error(
                "evidence frame is not valid UTF-8 JSON",
                code=ErrorCode.EVIDENCE_CORRUPT,
            ) from exc
        if not isinstance(frame, dict) or set(frame) != _FRAME_FIELDS:
            raise JsonEvidenceStore._error(
                "evidence frame fields are invalid",
                code=ErrorCode.EVIDENCE_CORRUPT,
            )
        frame_version = frame.get("frame_version")
        if frame_version != _FRAME_VERSION or isinstance(frame_version, bool):
            raise JsonEvidenceStore._error(
                f"Unsupported evidence frame version: {frame_version!r}",
                code=ErrorCode.EVIDENCE_SCHEMA_UNSUPPORTED,
            )
        payload = frame.get("evidence")
        checksum = frame.get("payload_sha256")
        if not isinstance(checksum, str) or not hmac.compare_digest(
            checksum,
            hashlib.sha256(JsonEvidenceStore._canonical_payload_bytes(payload)).hexdigest(),
        ):
            raise JsonEvidenceStore._error(
                "evidence checksum does not match its payload",
                code=ErrorCode.EVIDENCE_CORRUPT,
            )
        try:
            return JsonEvidenceStore._decode_item(
                payload,
                expected_evidence_id=expected_evidence_id,
            )
        except RepoForgeError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise JsonEvidenceStore._error(
                "evidence payload cannot be decoded safely",
                code=ErrorCode.EVIDENCE_CORRUPT,
            ) from exc

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

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            JsonEvidenceStore._fsync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _total_bytes(self) -> int:
        total = 0
        for root, pattern in (
            (self.items_root, "*.json"),
            (self.artifacts_root, "*.blob"),
        ):
            for path in root.glob(pattern):
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    continue
        return total

    def _assert_quota(self, *, additional_bytes: int, additional_items: int) -> None:
        current_items = sum(1 for _path in self.items_root.glob("ev-*.json"))
        if current_items + additional_items > self.max_items:
            raise self._error(
                "evidence item quota exhausted",
                code=ErrorCode.EVIDENCE_QUOTA_EXCEEDED,
            )
        if self._total_bytes() + additional_bytes > self.max_total_bytes:
            raise self._error(
                "evidence byte quota exhausted",
                code=ErrorCode.EVIDENCE_QUOTA_EXCEEDED,
            )

    def _validate_artifact(self, item: EvidenceItem, artifact: bytes | None) -> bytes | None:
        if artifact is None:
            return None
        if not isinstance(artifact, bytes):
            raise self._error(
                "provider artifact must be bytes",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        reference = item.provenance.artifact
        if reference is None:
            raise self._error(
                "provider artifact bytes require a typed artifact reference",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        if len(artifact) > self.max_artifact_bytes or len(artifact) != reference.size_bytes:
            raise self._error(
                "provider artifact size does not match its bounded reference",
                code=ErrorCode.EVIDENCE_ARTIFACT_DIGEST_MISMATCH,
            )
        digest = hashlib.sha256(artifact).hexdigest()
        if not hmac.compare_digest(digest, reference.digest):
            raise self._error(
                "provider artifact digest does not match its reference",
                code=ErrorCode.EVIDENCE_ARTIFACT_DIGEST_MISMATCH,
            )
        return artifact

    def create(self, item: EvidenceItem, *, artifact: bytes | None = None) -> EvidenceItem:
        normalized = validate_evidence_item(item)
        artifact_bytes = self._validate_artifact(normalized, artifact)
        encoded = self._encode(normalized)
        item_path = self._item_path(normalized.evidence_id)
        artifact_ref = normalized.provenance.artifact
        artifact_path = (
            self._artifact_path(artifact_ref.digest) if artifact_ref is not None else None
        )
        with (
            self._process_lock,
            self._locks.lock(
                "evidence-store",
                timeout_seconds=5,
                metadata={"operation": "create", "evidence_id": normalized.evidence_id},
            ),
        ):
            if item_path.exists():
                existing = self.read(normalized.evidence_id)
                if existing != normalized:
                    raise self._error(
                        "content-addressed evidence identity maps to different persisted data",
                        code=ErrorCode.EVIDENCE_CORRUPT,
                    )
                if (
                    artifact_bytes is not None
                    and artifact_path is not None
                    and artifact_ref is not None
                ):
                    if artifact_path.exists():
                        persisted = artifact_path.read_bytes()
                        if not hmac.compare_digest(
                            hashlib.sha256(persisted).hexdigest(),
                            artifact_ref.digest,
                        ):
                            raise self._error(
                                "persisted provider artifact digest is corrupt",
                                code=ErrorCode.EVIDENCE_CORRUPT,
                            )
                    else:
                        self._assert_quota(
                            additional_bytes=len(artifact_bytes),
                            additional_items=0,
                        )
                        self._atomic_write(artifact_path, artifact_bytes)
                return normalized

            artifact_delta = 0
            if (
                artifact_bytes is not None
                and artifact_path is not None
                and not artifact_path.exists()
            ):
                artifact_delta = len(artifact_bytes)
            self._assert_quota(
                additional_bytes=len(encoded) + artifact_delta,
                additional_items=1,
            )
            try:
                if artifact_delta and artifact_path is not None and artifact_bytes is not None:
                    self._atomic_write(artifact_path, artifact_bytes)
                self._atomic_write(item_path, encoded)
            except OSError as exc:
                raise self._error(
                    f"cannot persist evidence {normalized.evidence_id}",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                ) from exc
        return normalized

    def read(self, evidence_id: str) -> EvidenceItem | None:
        path = self._item_path(evidence_id)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise self._error(
                f"cannot read evidence {evidence_id}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        return self._decode(data, expected_evidence_id=evidence_id)

    def read_artifact(self, digest: str) -> bytes:
        path = self._artifact_path(digest)
        if not path.is_file():
            raise self._error(
                f"provider artifact is missing: {digest}",
                code=ErrorCode.EVIDENCE_ARTIFACT_MISSING,
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise self._error(
                f"cannot read provider artifact {digest}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        if len(data) > self.max_artifact_bytes or not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), digest
        ):
            raise self._error(
                "provider artifact is corrupt or exceeds its reviewed bound",
                code=ErrorCode.EVIDENCE_CORRUPT,
            )
        return data

    def query(
        self,
        query: EvidenceQuery,
        *,
        current_snapshot: EvidenceSnapshot | None = None,
        now: str | None = None,
    ) -> EvidencePage:
        if (current_snapshot is None) != (now is None):
            raise self._error(
                "current_snapshot and now must be supplied together for staleness evaluation",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        items: list[EvidenceItem] = []
        paths = sorted(self.items_root.glob("ev-*.json"))
        if len(paths) > self.max_items:
            raise self._error(
                "evidence store contains more items than its reviewed scan bound",
                code=ErrorCode.EVIDENCE_QUOTA_EXCEEDED,
            )
        for path in paths:
            item = self.read(path.stem)
            if item is None:
                continue
            if current_snapshot is not None and now is not None:
                item = replace(
                    item,
                    status=evidence_status_for(
                        item,
                        current_snapshot=current_snapshot,
                        now=now,
                    ),
                )
            items.append(item)
        items = list(mark_evidence_conflicts(tuple(items)))
        filtered = [item for item in items if evidence_matches_query(item, query)]
        filtered.sort(key=lambda item: (item.created_at, item.evidence_id), reverse=True)
        start = 0
        if query.cursor is not None:
            for index, item in enumerate(filtered):
                if item.evidence_id == query.cursor:
                    start = index + 1
                    break
            else:
                raise self._error(
                    "query cursor is not present in the deterministic result set",
                    code=ErrorCode.EVIDENCE_INVALID,
                )
        selected = tuple(filtered[start : start + query.limit])
        has_more = start + len(selected) < len(filtered)
        next_cursor = selected[-1].evidence_id if selected and has_more else None
        return EvidencePage(selected, next_cursor, False)

    @staticmethod
    def _retention_time(value: str, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise JsonEvidenceStore._error(
                f"{field} must be an ISO-8601 timestamp",
                code=ErrorCode.EVIDENCE_INVALID,
            ) from exc
        if parsed.tzinfo is None:
            raise JsonEvidenceStore._error(
                f"{field} must include a timezone offset",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        return parsed

    def _remaining_items(self) -> list[EvidenceItem]:
        records: list[EvidenceItem] = []
        for path in sorted(self.items_root.glob("ev-*.json")):
            record = self.read(path.stem)
            if record is not None:
                records.append(record)
        return records

    def _garbage_collect_artifacts(self, items: list[EvidenceItem]) -> int:
        referenced = {
            item.provenance.artifact.digest
            for item in items
            if item.provenance.artifact is not None
        }
        deleted = 0
        for path in sorted(self.artifacts_root.glob("*.blob")):
            if path.stem not in referenced:
                path.unlink(missing_ok=True)
                deleted += 1
        if deleted:
            self._fsync_dir(self.artifacts_root)
        return deleted

    def prune(
        self,
        *,
        now: str,
        retention_seconds: int,
        max_items: int,
        max_total_bytes: int,
        protected_evidence_ids: tuple[str, ...] = (),
    ) -> EvidenceRetentionReport:
        if (
            not isinstance(retention_seconds, int)
            or isinstance(retention_seconds, bool)
            or retention_seconds < 0
            or not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or max_items < 1
            or not isinstance(max_total_bytes, int)
            or isinstance(max_total_bytes, bool)
            or max_total_bytes < 1
            or not isinstance(protected_evidence_ids, tuple)
        ):
            raise self._error(
                "evidence retention bounds are invalid",
                code=ErrorCode.EVIDENCE_INVALID,
            )
        protected = {validate_evidence_id(item) for item in protected_evidence_ids}
        cutoff = self._retention_time(now, "retention now") - timedelta(seconds=retention_seconds)
        deleted_age = 0
        deleted_count = 0
        deleted_bytes = 0
        deleted_artifacts = 0
        with (
            self._process_lock,
            self._locks.lock(
                "evidence-store",
                timeout_seconds=5,
                metadata={"operation": "prune"},
            ),
        ):
            items = self._remaining_items()
            by_id = {item.evidence_id: item for item in items}
            protected_existing = protected & set(by_id)

            for item in sorted(items, key=lambda value: (value.created_at, value.evidence_id)):
                if item.evidence_id in protected_existing:
                    continue
                if self._retention_time(item.created_at, "created_at") < cutoff:
                    self._item_path(item.evidence_id).unlink(missing_ok=True)
                    deleted_age += 1

            items = self._remaining_items()
            while len(items) > max_items:
                candidate = next(
                    (
                        item
                        for item in sorted(
                            items,
                            key=lambda value: (value.created_at, value.evidence_id),
                        )
                        if item.evidence_id not in protected_existing
                    ),
                    None,
                )
                if candidate is None:
                    break
                self._item_path(candidate.evidence_id).unlink(missing_ok=True)
                deleted_count += 1
                items = [item for item in items if item.evidence_id != candidate.evidence_id]

            deleted_artifacts += self._garbage_collect_artifacts(items)
            total_bytes = self._total_bytes()
            while total_bytes > max_total_bytes:
                candidate = next(
                    (
                        item
                        for item in sorted(
                            items,
                            key=lambda value: (value.created_at, value.evidence_id),
                        )
                        if item.evidence_id not in protected_existing
                    ),
                    None,
                )
                if candidate is None:
                    break
                self._item_path(candidate.evidence_id).unlink(missing_ok=True)
                deleted_bytes += 1
                items = [item for item in items if item.evidence_id != candidate.evidence_id]
                deleted_artifacts += self._garbage_collect_artifacts(items)
                total_bytes = self._total_bytes()
            self._fsync_dir(self.items_root)
            self._fsync_dir(self.root)

        return EvidenceRetentionReport(
            deleted_for_age=deleted_age,
            deleted_for_count=deleted_count,
            deleted_for_bytes=deleted_bytes,
            deleted_artifacts=deleted_artifacts,
            protected_items=len(protected_existing),
            remaining_items=len(items),
            total_bytes=total_bytes,
        )
