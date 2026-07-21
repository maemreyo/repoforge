"""Private atomic persistence for durable pull-request check watches."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.operation_task import validate_operation_id
from ...domain.pr_check_watch import (
    PR_CHECK_WATCH_SCHEMA_VERSION,
    PrCheckWatch,
    PrCheckWatchOutcome,
    PrCheckWatchUntil,
    validate_pr_check_watch,
)
from ...ports.locking import LockManager
from ...ports.pr_check_watch_store import PrCheckWatchPage

_FORBIDDEN_KEYS = {
    "api_key",
    "access_token",
    "authorization",
    "body",
    "content",
    "credential",
    "credentials",
    "diff",
    "environment",
    "password",
    "patch",
    "secret",
    "stderr",
    "stdout",
    "token",
}
_EXPECTED_FIELDS = {
    "operation_id",
    "workspace_id",
    "branch",
    "pr_number",
    "pushed_sha",
    "workspace_fingerprint",
    "remote_version",
    "stability_version",
    "until",
    "include_failure_evidence",
    "timeout_seconds",
    "poll_count",
    "pass_count",
    "fail_count",
    "pending_count",
    "skipping_count",
    "selectors",
    "failed_selectors",
    "evidence_references",
    "next_delay_seconds",
    "provider_error_code",
    "outcome",
    "created_at",
    "updated_at",
    "deadline_at",
    "schema_version",
}


class JsonPrCheckWatchStore:
    def __init__(self, state_root: Path, locks: LockManager):
        self.root = state_root.expanduser().resolve() / "pr-check-watches"
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
                "Inspect the latest operation/watch state and local state-root health before retrying."
            ),
        )

    def _path(self, operation_id: str) -> Path:
        return self.root / f"{validate_operation_id(operation_id)}.json"

    @staticmethod
    def _assert_safe(value: object, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                if normalized in _FORBIDDEN_KEYS:
                    raise JsonPrCheckWatchStore._error(
                        f"PR check watch contains forbidden field {path}{key}",
                        code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
                    )
                JsonPrCheckWatchStore._assert_safe(item, f"{path}{key}.")
        elif isinstance(value, list):
            for item in value:
                JsonPrCheckWatchStore._assert_safe(item, path)

    @staticmethod
    def _payload(watch: PrCheckWatch) -> dict[str, Any]:
        normalized = validate_pr_check_watch(watch)
        payload = asdict(normalized)
        payload["until"] = normalized.until.value
        payload["outcome"] = normalized.outcome.value
        JsonPrCheckWatchStore._assert_safe(payload)
        return payload

    @staticmethod
    def _encode(watch: PrCheckWatch) -> bytes:
        return (
            json.dumps(
                JsonPrCheckWatchStore._payload(watch),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def encode_for_test(watch: PrCheckWatch) -> bytes:
        return JsonPrCheckWatchStore._encode(watch)

    @staticmethod
    def _decode(data: bytes, *, expected_operation_id: str) -> PrCheckWatch:
        try:
            raw: Any = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonPrCheckWatchStore._error(
                "PR check watch is not valid UTF-8 JSON",
                code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
            ) from exc
        if not isinstance(raw, dict):
            raise JsonPrCheckWatchStore._error(
                "PR check watch must be a JSON object",
                code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
            )
        JsonPrCheckWatchStore._assert_safe(raw)
        version = raw.get("schema_version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != PR_CHECK_WATCH_SCHEMA_VERSION
        ):
            raise JsonPrCheckWatchStore._error(
                f"Unsupported PR check watch schema version: {version!r}",
                code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
            )
        if set(raw) != _EXPECTED_FIELDS:
            raise JsonPrCheckWatchStore._error(
                f"PR check watch fields do not match schema version {PR_CHECK_WATCH_SCHEMA_VERSION}",
                code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
            )
        if raw.get("operation_id") != expected_operation_id:
            raise JsonPrCheckWatchStore._error(
                "PR check watch identity does not match its filename",
                code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
            )
        try:
            watch = PrCheckWatch(
                operation_id=str(raw["operation_id"]),
                workspace_id=str(raw["workspace_id"]),
                branch=str(raw["branch"]),
                pr_number=raw["pr_number"],
                pushed_sha=str(raw["pushed_sha"]),
                workspace_fingerprint=str(raw["workspace_fingerprint"]),
                remote_version=str(raw["remote_version"]),
                stability_version=str(raw["stability_version"]),
                until=PrCheckWatchUntil(raw["until"]),
                include_failure_evidence=raw["include_failure_evidence"],
                timeout_seconds=raw["timeout_seconds"],
                poll_count=raw["poll_count"],
                pass_count=raw["pass_count"],
                fail_count=raw["fail_count"],
                pending_count=raw["pending_count"],
                skipping_count=raw["skipping_count"],
                selectors=tuple(raw["selectors"]),
                failed_selectors=tuple(raw["failed_selectors"]),
                evidence_references=tuple(raw["evidence_references"]),
                next_delay_seconds=raw["next_delay_seconds"],
                provider_error_code=raw["provider_error_code"],
                outcome=PrCheckWatchOutcome(raw["outcome"]),
                created_at=str(raw["created_at"]),
                updated_at=str(raw["updated_at"]),
                deadline_at=str(raw["deadline_at"]),
                schema_version=raw["schema_version"],
            )
            return validate_pr_check_watch(watch)
        except (KeyError, TypeError, ValueError, RepoForgeError) as exc:
            raise JsonPrCheckWatchStore._error(
                "PR check watch cannot be decoded safely",
                code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
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

    def _write(self, path: Path, data: bytes) -> None:
        try:
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
                self._fsync_dir(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise self._error(
                f"Cannot persist PR check watch {path.name}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc

    def create(self, watch: PrCheckWatch) -> PrCheckWatch:
        normalized = validate_pr_check_watch(watch)
        path = self._path(normalized.operation_id)
        with self._locks.lock(
            f"pr-check-watch-{normalized.operation_id}",
            timeout_seconds=5,
            metadata={"operation": "create"},
        ):
            if path.exists():
                raise self._error(
                    f"PR check watch already exists: {normalized.operation_id}",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            self._write(path, self._encode(normalized))
        return normalized

    def read(self, operation_id: str) -> PrCheckWatch | None:
        path = self._path(operation_id)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise self._error(
                f"Cannot read PR check watch {operation_id}",
                code=ErrorCode.STATE_PERSISTENCE_FAILED,
                retryable=True,
            ) from exc
        return self._decode(data, expected_operation_id=operation_id)

    def save(
        self,
        watch: PrCheckWatch,
        *,
        expected_updated_at: str,
    ) -> PrCheckWatch:
        normalized = validate_pr_check_watch(watch)
        path = self._path(normalized.operation_id)
        with self._locks.lock(
            f"pr-check-watch-{normalized.operation_id}",
            timeout_seconds=5,
            metadata={"operation": "save"},
        ):
            current = self.read(normalized.operation_id)
            if current is None:
                raise self._error(
                    f"PR check watch not found: {normalized.operation_id}",
                    code=ErrorCode.PR_CHECK_WATCH_STATE_CORRUPT,
                )
            if current.updated_at != expected_updated_at:
                raise self._error(
                    "PR check watch changed after the reviewed timestamp",
                    code=ErrorCode.PR_CHECK_WATCH_STALE,
                    retryable=True,
                )
            self._write(path, self._encode(normalized))
        return normalized

    def list_records(self, *, max_records: int) -> PrCheckWatchPage:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= 2_000
        ):
            raise self._error(
                "max_records must be between 1 and 2000",
                code=ErrorCode.PR_CHECK_WATCH_INVALID,
            )
        paths = sorted(self.root.glob("op-*.json"))
        records: list[PrCheckWatch] = []
        for path in paths[:max_records]:
            record = self.read(path.stem)
            if record is not None:
                records.append(record)
        records.sort(
            key=lambda item: (item.updated_at, item.operation_id),
            reverse=True,
        )
        return PrCheckWatchPage(
            tuple(records),
            len(paths) > max_records,
        )

    def delete(self, operation_id: str) -> None:
        path = self._path(operation_id)
        with self._locks.lock(
            f"pr-check-watch-{operation_id}",
            timeout_seconds=5,
            metadata={"operation": "delete"},
        ):
            existed = path.exists()
            try:
                path.unlink(missing_ok=True)
                if existed:
                    self._fsync_dir(path.parent)
            except OSError as exc:
                raise self._error(
                    f"Cannot delete PR check watch {operation_id}",
                    code=ErrorCode.STATE_PERSISTENCE_FAILED,
                    retryable=True,
                ) from exc
