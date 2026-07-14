"""Private checksum-framed persistence for sanitized workflow recordings."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.workflow_recording import (
    MAX_WORKFLOW_RECORD_BYTES,
    WORKFLOW_RECORDING_SCHEMA_VERSION,
    WorkflowArgumentCategory,
    WorkflowArgumentSummary,
    WorkflowEvent,
    WorkflowFinalOutcome,
    WorkflowMetrics,
    WorkflowRecording,
    WorkflowResultCategory,
    WorkflowStateTransition,
    canonical_workflow_recording_bytes,
    validate_workflow_recording,
    workflow_recording_payload,
)
from ...ports.locking import LockManager
from ...ports.workflow_recording_store import (
    WorkflowRecordingPage,
    WorkflowRetentionReport,
)

_FRAME_VERSION = 1
_RECORDING_ID = re.compile(r"^wr-[a-f0-9]{24}$")
_FIXTURE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\.json$")
_FRAME_FIELDS = {"frame_version", "payload_sha256", "recording"}
_RECORD_FIELDS = {
    "recording_id",
    "scenario_id",
    "server_instructions_hash",
    "tool_surface_hash",
    "capability_flags",
    "events",
    "final_outcome",
    "metrics",
    "created_at",
    "truncated",
    "truncation_reason",
    "schema_version",
}
_EVENT_FIELDS = {
    "timestamp_offset_ms",
    "tool_inventory_ids",
    "selected_tool_id",
    "arguments",
    "result_category",
    "stable_error_code",
    "workspace_ref",
    "task_ref",
    "snapshot_ref",
    "next_action_ids",
    "state_transition",
    "arguments_truncated",
    "result_truncated",
}
_ARGUMENT_FIELDS = {"name", "category", "value_hash", "truncated"}
_METRIC_FIELDS = {"tool_calls", "duration_ms", "retry_count", "error_count"}


class JsonWorkflowRecordingStore:
    def __init__(self, state_root: Path, locks: LockManager):
        self.root = state_root.expanduser().resolve() / "workflow-recordings"
        self._locks = locks
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

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
                "Inspect the private recording store, discard corrupt frames, and retry with typed sanitized evidence."
            ),
        )

    @staticmethod
    def _validate_recording_id(recording_id: str) -> str:
        if not isinstance(recording_id, str) or _RECORDING_ID.fullmatch(recording_id) is None:
            raise JsonWorkflowRecordingStore._error(
                "workflow recording id has an invalid format",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            )
        return recording_id

    def _path(self, recording_id: str) -> Path:
        return self.root / f"{self._validate_recording_id(recording_id)}.json"

    @staticmethod
    def _frame(recording: WorkflowRecording) -> dict[str, Any]:
        normalized = validate_workflow_recording(recording)
        payload = workflow_recording_payload(normalized)
        payload_bytes = canonical_workflow_recording_bytes(normalized)
        return {
            "frame_version": _FRAME_VERSION,
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "recording": payload,
        }

    @staticmethod
    def _encode(recording: WorkflowRecording) -> bytes:
        encoded = (
            json.dumps(
                JsonWorkflowRecordingStore._frame(recording),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_WORKFLOW_RECORD_BYTES:
            raise JsonWorkflowRecordingStore._error(
                f"workflow recording exceeds {MAX_WORKFLOW_RECORD_BYTES} encoded bytes",
                code=ErrorCode.WORKFLOW_RECORD_TOO_LARGE,
            )
        return encoded

    @staticmethod
    def encode_for_test(recording: WorkflowRecording) -> bytes:
        return JsonWorkflowRecordingStore._encode(recording)

    @staticmethod
    def _list_of_strings(value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{field} must be a string list")
        return tuple(value)

    @staticmethod
    def _decode_argument(raw: object) -> WorkflowArgumentSummary:
        if not isinstance(raw, dict) or set(raw) != _ARGUMENT_FIELDS:
            raise TypeError("workflow argument fields do not match schema version 1")
        return WorkflowArgumentSummary(
            name=raw["name"],
            category=WorkflowArgumentCategory(raw["category"]),
            value_hash=raw["value_hash"],
            truncated=raw["truncated"],
        )

    @staticmethod
    def _decode_event(raw: object) -> WorkflowEvent:
        if not isinstance(raw, dict) or set(raw) != _EVENT_FIELDS:
            raise TypeError("workflow event fields do not match schema version 1")
        arguments_raw = raw["arguments"]
        if not isinstance(arguments_raw, list):
            raise TypeError("workflow event arguments must be a list")
        return WorkflowEvent(
            timestamp_offset_ms=raw["timestamp_offset_ms"],
            tool_inventory_ids=JsonWorkflowRecordingStore._list_of_strings(
                raw["tool_inventory_ids"], "tool_inventory_ids"
            ),
            selected_tool_id=raw["selected_tool_id"],
            arguments=tuple(
                JsonWorkflowRecordingStore._decode_argument(item) for item in arguments_raw
            ),
            result_category=WorkflowResultCategory(raw["result_category"]),
            stable_error_code=raw["stable_error_code"],
            workspace_ref=raw["workspace_ref"],
            task_ref=raw["task_ref"],
            snapshot_ref=raw["snapshot_ref"],
            next_action_ids=JsonWorkflowRecordingStore._list_of_strings(
                raw["next_action_ids"], "next_action_ids"
            ),
            state_transition=WorkflowStateTransition(raw["state_transition"]),
            arguments_truncated=raw["arguments_truncated"],
            result_truncated=raw["result_truncated"],
        )

    @staticmethod
    def _decode_recording(raw: object, *, expected_recording_id: str) -> WorkflowRecording:
        if not isinstance(raw, dict):
            raise TypeError("workflow recording must be an object")
        version = raw.get("schema_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != WORKFLOW_RECORDING_SCHEMA_VERSION
        ):
            raise JsonWorkflowRecordingStore._error(
                f"Unsupported workflow recording schema version: {version!r}",
                code=ErrorCode.WORKFLOW_RECORD_SCHEMA_UNSUPPORTED,
            )
        if set(raw) != _RECORD_FIELDS:
            raise TypeError("workflow recording fields do not match schema version 1")
        if raw.get("recording_id") != expected_recording_id:
            raise TypeError("workflow recording identity does not match its filename")
        events_raw = raw["events"]
        metrics_raw = raw["metrics"]
        if not isinstance(events_raw, list):
            raise TypeError("workflow recording events must be a list")
        if not isinstance(metrics_raw, dict) or set(metrics_raw) != _METRIC_FIELDS:
            raise TypeError("workflow metrics fields do not match schema version 1")
        recording = WorkflowRecording(
            recording_id=raw["recording_id"],
            scenario_id=raw["scenario_id"],
            server_instructions_hash=raw["server_instructions_hash"],
            tool_surface_hash=raw["tool_surface_hash"],
            capability_flags=JsonWorkflowRecordingStore._list_of_strings(
                raw["capability_flags"], "capability_flags"
            ),
            events=tuple(JsonWorkflowRecordingStore._decode_event(item) for item in events_raw),
            final_outcome=WorkflowFinalOutcome(raw["final_outcome"]),
            metrics=WorkflowMetrics(
                tool_calls=metrics_raw["tool_calls"],
                duration_ms=metrics_raw["duration_ms"],
                retry_count=metrics_raw["retry_count"],
                error_count=metrics_raw["error_count"],
            ),
            created_at=raw["created_at"],
            truncated=raw["truncated"],
            truncation_reason=raw["truncation_reason"],
            schema_version=raw["schema_version"],
        )
        return validate_workflow_recording(recording)

    @staticmethod
    def _decode(data: bytes, *, expected_recording_id: str) -> WorkflowRecording:
        if len(data) > MAX_WORKFLOW_RECORD_BYTES:
            raise JsonWorkflowRecordingStore._error(
                "workflow recording frame exceeds the reviewed size bound",
                code=ErrorCode.WORKFLOW_RECORD_TOO_LARGE,
            )
        try:
            frame: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonWorkflowRecordingStore._error(
                "workflow recording frame is not valid UTF-8 JSON",
                code=ErrorCode.WORKFLOW_RECORD_CORRUPT,
            ) from exc
        if not isinstance(frame, dict) or set(frame) != _FRAME_FIELDS:
            raise JsonWorkflowRecordingStore._error(
                "workflow recording frame fields are invalid",
                code=ErrorCode.WORKFLOW_RECORD_CORRUPT,
            )
        frame_version = frame.get("frame_version")
        if frame_version != _FRAME_VERSION or isinstance(frame_version, bool):
            raise JsonWorkflowRecordingStore._error(
                f"Unsupported workflow recording frame version: {frame_version!r}",
                code=ErrorCode.WORKFLOW_RECORD_SCHEMA_UNSUPPORTED,
            )
        payload = frame.get("recording")
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        checksum = frame.get("payload_sha256")
        if not isinstance(checksum, str) or not hmac.compare_digest(
            checksum, hashlib.sha256(canonical).hexdigest()
        ):
            raise JsonWorkflowRecordingStore._error(
                "workflow recording checksum does not match its payload",
                code=ErrorCode.WORKFLOW_RECORD_CORRUPT,
            )
        try:
            return JsonWorkflowRecordingStore._decode_recording(
                payload,
                expected_recording_id=expected_recording_id,
            )
        except RepoForgeError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise JsonWorkflowRecordingStore._error(
                "workflow recording cannot be decoded safely",
                code=ErrorCode.WORKFLOW_RECORD_CORRUPT,
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
            JsonWorkflowRecordingStore._fsync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, recording: WorkflowRecording) -> WorkflowRecording:
        normalized = validate_workflow_recording(recording)
        data = self._encode(normalized)
        path = self._path(normalized.recording_id)
        with self._locks.lock(
            f"workflow-recording-{normalized.recording_id}",
            timeout_seconds=5,
            metadata={"operation": "create"},
        ):
            if path.exists():
                raise self._error(
                    f"workflow recording already exists: {normalized.recording_id}",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            try:
                self._atomic_write(path, data)
            except OSError as exc:
                raise self._error(
                    f"cannot persist workflow recording {normalized.recording_id}",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                ) from exc
        return normalized

    def read(self, recording_id: str) -> WorkflowRecording | None:
        path = self._path(recording_id)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise self._error(
                f"cannot read workflow recording {recording_id}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        return self._decode(data, expected_recording_id=recording_id)

    def list_records(self, *, max_records: int) -> WorkflowRecordingPage:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= 2_000
        ):
            raise self._error(
                "max_records must be between 1 and 2000",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            )
        paths = sorted(self.root.glob("wr-*.json"))
        records: list[WorkflowRecording] = []
        for path in paths:
            record = self.read(path.stem)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: (item.created_at, item.recording_id), reverse=True)
        return WorkflowRecordingPage(tuple(records[:max_records]), len(records) > max_records)

    def export_fixture(
        self,
        recording_id: str,
        fixture_root: Path,
        fixture_name: str,
    ) -> Path:
        if _FIXTURE_NAME.fullmatch(fixture_name) is None:
            raise self._error(
                "workflow fixture name must be one safe .json filename",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            )
        recording = self.read(recording_id)
        if recording is None:
            raise self._error(
                f"workflow recording not found: {recording_id}",
                code=ErrorCode.WORKFLOW_RECORD_NOT_FOUND,
            )
        root = fixture_root.expanduser().resolve()
        destination = root / fixture_name
        try:
            self._atomic_write(destination, self._encode(recording))
        except OSError as exc:
            raise self._error(
                f"cannot export workflow fixture {fixture_name}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        return destination

    @staticmethod
    def _timestamp(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise JsonWorkflowRecordingStore._error(
                "retention now must be an ISO-8601 timestamp",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            ) from exc
        if parsed.tzinfo is None:
            raise JsonWorkflowRecordingStore._error(
                "retention now must include a timezone offset",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            )
        return parsed

    def prune(
        self,
        *,
        now: str,
        retention_seconds: int,
        max_records: int,
        max_total_bytes: int,
    ) -> WorkflowRetentionReport:
        if (
            not isinstance(retention_seconds, int)
            or isinstance(retention_seconds, bool)
            or retention_seconds < 0
            or not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records < 1
            or not isinstance(max_total_bytes, int)
            or isinstance(max_total_bytes, bool)
            or max_total_bytes < 1
        ):
            raise self._error(
                "workflow retention bounds are invalid",
                code=ErrorCode.WORKFLOW_RECORD_INVALID,
            )
        cutoff = self._timestamp(now) - timedelta(seconds=retention_seconds)
        deleted_age = 0
        deleted_count = 0
        deleted_bytes = 0
        with self._locks.lock(
            "workflow-recording-retention",
            timeout_seconds=5,
            metadata={"operation": "prune"},
        ):
            entries: list[tuple[WorkflowRecording, Path, int]] = []
            for path in sorted(self.root.glob("wr-*.json")):
                recording = self.read(path.stem)
                if recording is not None:
                    entries.append((recording, path, path.stat().st_size))
            entries.sort(key=lambda item: (item[0].created_at, item[0].recording_id))

            retained: list[tuple[WorkflowRecording, Path, int]] = []
            for item in entries:
                if self._timestamp(item[0].created_at) < cutoff:
                    item[1].unlink(missing_ok=True)
                    deleted_age += 1
                else:
                    retained.append(item)

            while len(retained) > max_records:
                _recording, path, _size = retained.pop(0)
                path.unlink(missing_ok=True)
                deleted_count += 1

            total_bytes = sum(item[2] for item in retained)
            while retained and total_bytes > max_total_bytes:
                _recording, path, size = retained.pop(0)
                path.unlink(missing_ok=True)
                total_bytes -= size
                deleted_bytes += 1
            self._fsync_dir(self.root)

        return WorkflowRetentionReport(
            deleted_for_age=deleted_age,
            deleted_for_count=deleted_count,
            deleted_for_bytes=deleted_bytes,
            remaining_records=len(retained),
            total_bytes=total_bytes,
        )
