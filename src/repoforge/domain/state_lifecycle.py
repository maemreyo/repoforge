"""Typed durable-state migration, retention, integrity, and recovery contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .durable_state import SchemaVersion
from .errors import ErrorCode, RepoForgeError

JsonObject = dict[str, object]
MigrationTransform = Callable[[JsonObject], JsonObject]

_SAFE_COLLECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_MIGRATION_PAYLOAD_BYTES = 1_000_000


def _error(message: str, code: ErrorCode = ErrorCode.STATE_INVALID) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        safe_next_action=(
            "Review the registered adjacent schema steps and exact durable-state preview before retrying."
        ),
    )


def validate_state_collection(value: str) -> str:
    if not isinstance(value, str) or _SAFE_COLLECTION.fullmatch(value) is None:
        raise _error("durable-state collection name is invalid")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error("migration transform returned a non-JSON payload") from exc
    if len(encoded) > _MAX_MIGRATION_PAYLOAD_BYTES:
        raise _error("migration transform exceeded the bounded payload size")
    return encoded


class MigrationDirection(str, Enum):
    FORWARD = "forward"
    REVERSE = "reverse"


@dataclass(frozen=True, slots=True)
class StateMigrationStep:
    collection: str
    from_version: SchemaVersion
    to_version: SchemaVersion
    forward: MigrationTransform
    reverse: MigrationTransform | None = None

    def __post_init__(self) -> None:
        validate_state_collection(self.collection)
        if self.to_version.value != self.from_version.value + 1:
            raise _error("migration steps must connect exactly one adjacent schema version")
        if not callable(self.forward):
            raise _error("forward migration transform must be callable")
        if self.reverse is not None and not callable(self.reverse):
            raise _error("reverse migration transform must be callable")


@dataclass(frozen=True, slots=True)
class StateMigrationPlan:
    collection: str
    current_version: SchemaVersion
    target_version: SchemaVersion
    direction: MigrationDirection
    steps: tuple[StateMigrationStep, ...]
    plan_digest: str

    def __post_init__(self) -> None:
        validate_state_collection(self.collection)
        if not isinstance(self.direction, MigrationDirection):
            raise _error("migration direction is invalid")
        if not isinstance(self.steps, tuple):
            raise _error("migration steps must be an immutable tuple")
        if not isinstance(self.plan_digest, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.plan_digest
        ):
            raise _error("migration plan digest must be a lowercase SHA-256")


def _plan_payload(
    collection: str,
    current_version: SchemaVersion,
    target_version: SchemaVersion,
    direction: MigrationDirection,
    steps: tuple[StateMigrationStep, ...],
) -> dict[str, object]:
    return {
        "collection": collection,
        "current_version": current_version.value,
        "target_version": target_version.value,
        "direction": direction.value,
        "steps": [
            {
                "from_version": step.from_version.value,
                "to_version": step.to_version.value,
                "reverse_available": step.reverse is not None,
            }
            for step in steps
        ],
    }


def _plan_digest(
    collection: str,
    current_version: SchemaVersion,
    target_version: SchemaVersion,
    direction: MigrationDirection,
    steps: tuple[StateMigrationStep, ...],
) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            _plan_payload(collection, current_version, target_version, direction, steps)
        )
    ).hexdigest()


class StateMigrationRegistry:
    """Deterministic registry of adjacent schema transforms by collection."""

    def __init__(self, steps: tuple[StateMigrationStep, ...] = ()) -> None:
        if not isinstance(steps, tuple):
            raise _error("migration registry steps must be an immutable tuple")
        by_edge: dict[tuple[str, int], StateMigrationStep] = {}
        for step in steps:
            if not isinstance(step, StateMigrationStep):
                raise _error("migration registry contains an invalid step")
            key = (step.collection, step.from_version.value)
            if key in by_edge:
                raise _error(
                    f"duplicate migration edge for {step.collection} schema {step.from_version.value}"
                )
            by_edge[key] = step
        self._steps = tuple(
            sorted(
                steps,
                key=lambda item: (
                    item.collection,
                    item.from_version.value,
                    item.to_version.value,
                ),
            )
        )
        self._by_edge = by_edge

    @property
    def steps(self) -> tuple[StateMigrationStep, ...]:
        return self._steps

    def latest_version(self, collection: str) -> SchemaVersion | None:
        safe_collection = validate_state_collection(collection)
        versions = [
            step.to_version.value for step in self._steps if step.collection == safe_collection
        ]
        return SchemaVersion(max(versions)) if versions else None

    def plan(
        self,
        collection: str,
        current_version: SchemaVersion,
        target_version: SchemaVersion,
    ) -> StateMigrationPlan:
        safe_collection = validate_state_collection(collection)
        if not isinstance(current_version, SchemaVersion) or not isinstance(
            target_version, SchemaVersion
        ):
            raise _error("migration versions must be SchemaVersion values")

        direction = (
            MigrationDirection.REVERSE
            if target_version.value < current_version.value
            else MigrationDirection.FORWARD
        )
        if current_version == target_version:
            steps: tuple[StateMigrationStep, ...] = ()
            return StateMigrationPlan(
                safe_collection,
                current_version,
                target_version,
                MigrationDirection.FORWARD,
                steps,
                _plan_digest(
                    safe_collection,
                    current_version,
                    target_version,
                    MigrationDirection.FORWARD,
                    steps,
                ),
            )

        latest = self.latest_version(safe_collection)
        if latest is None:
            raise _error(f"no migration steps are registered for {safe_collection}")
        if current_version.value > latest.value:
            raise _error(
                f"Unsupported state schema version: {current_version.value}",
                ErrorCode.STATE_SCHEMA_UNSUPPORTED,
            )

        selected: list[StateMigrationStep] = []
        if direction is MigrationDirection.FORWARD:
            for version in range(current_version.value, target_version.value):
                step = self._by_edge.get((safe_collection, version))
                if step is None:
                    raise _error(
                        f"migration path for {safe_collection} skips schema version {version}"
                    )
                selected.append(step)
        else:
            for version in range(current_version.value - 1, target_version.value - 1, -1):
                step = self._by_edge.get((safe_collection, version))
                if step is None:
                    raise _error(
                        f"reverse migration path for {safe_collection} skips schema version {version + 1}"
                    )
                if step.reverse is None:
                    raise _error(
                        f"reverse migration is not registered for {safe_collection} "
                        f"schema {step.to_version.value} -> {step.from_version.value}"
                    )
                selected.append(step)

        steps = tuple(selected)
        return StateMigrationPlan(
            safe_collection,
            current_version,
            target_version,
            direction,
            steps,
            _plan_digest(
                safe_collection,
                current_version,
                target_version,
                direction,
                steps,
            ),
        )

    def migrate_payload(
        self,
        plan: StateMigrationPlan,
        payload: JsonObject,
    ) -> JsonObject:
        if not isinstance(plan, StateMigrationPlan):
            raise _error("migration plan is invalid")
        expected_digest = _plan_digest(
            plan.collection,
            plan.current_version,
            plan.target_version,
            plan.direction,
            plan.steps,
        )
        if plan.plan_digest != expected_digest:
            raise _error("migration plan digest does not match its registered path")
        if not isinstance(payload, dict):
            raise _error("migration payload must be an object")

        current = dict(payload)
        _canonical_bytes(current)
        for step in plan.steps:
            transform = (
                step.forward if plan.direction is MigrationDirection.FORWARD else step.reverse
            )
            if transform is None:
                raise _error("reverse migration transform is missing")
            try:
                first = transform(dict(current))
                second = transform(dict(current))
            except RepoForgeError:
                raise
            except Exception as exc:
                raise _error("migration transform raised an exception") from exc
            if not isinstance(first, dict) or not isinstance(second, dict):
                raise _error("migration transform must return an object")
            first_bytes = _canonical_bytes(first)
            if first_bytes != _canonical_bytes(second):
                raise _error("migration transform is not deterministic")
            current = dict(first)
        return current


@dataclass(frozen=True, slots=True)
class StateMigrationRecordPreview:
    record_id: str
    source_version: SchemaVersion
    target_version: SchemaVersion
    source_revision: int
    source_checksum: str
    target_checksum: str
    direction: MigrationDirection
    changed: bool
    source_size_bytes: int
    target_size_bytes: int


@dataclass(frozen=True, slots=True)
class StateMigrationPreview:
    plan_id: str
    plan_digest: str
    collection: str
    target_version: SchemaVersion
    records: tuple[StateMigrationRecordPreview, ...]
    migrated_records: int
    unchanged_records: int
    scan_truncated: bool = False


@dataclass(frozen=True, slots=True)
class StateMigrationReport:
    plan_id: str
    processed: int
    migrated: int
    unchanged: int
    rolled_back: bool
    backup_id: str | None


class CleanupDisposition(str, Enum):
    EXPIRED = "expired"
    COUNT_QUOTA = "count_quota"
    BYTE_QUOTA = "byte_quota"


@dataclass(frozen=True, slots=True)
class StateRetentionPolicy:
    now: str
    retention_seconds: int
    max_records: int
    max_total_bytes: int
    batch_size: int = 100

    def __post_init__(self) -> None:
        _parse_timestamp(self.now, "retention now")
        for field, value, minimum, maximum in (
            ("retention_seconds", self.retention_seconds, 0, 10 * 365 * 24 * 60 * 60),
            ("max_records", self.max_records, 1, 2_000_000),
            ("max_total_bytes", self.max_total_bytes, 1, 1 << 50),
            ("batch_size", self.batch_size, 1, 2_000),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise _retention_error(f"{field} is outside its reviewed bound")


@dataclass(frozen=True, slots=True)
class StateProtection:
    record_id: str
    reason: str

    def __post_init__(self) -> None:
        _validate_lifecycle_record_id(self.record_id)
        _validate_reason(self.reason, "protection reason")


@dataclass(frozen=True, slots=True)
class StateRecordReference:
    source_record_id: str
    target_record_id: str
    relation: str

    def __post_init__(self) -> None:
        _validate_lifecycle_record_id(self.source_record_id)
        _validate_lifecycle_record_id(self.target_record_id)
        _validate_reason(self.relation, "reference relation")


@dataclass(frozen=True, slots=True)
class StateCleanupCandidate:
    record_id: str
    checksum: str
    size_bytes: int
    created_at: str
    disposition: CleanupDisposition


@dataclass(frozen=True, slots=True)
class StateCleanupPreview:
    plan_id: str
    plan_digest: str
    collection: str
    candidates: tuple[StateCleanupCandidate, ...]
    protected_record_ids: tuple[str, ...]
    orphan_references: tuple[tuple[str, str, str], ...]
    retained_records: int
    retained_bytes: int
    remaining_candidate_count: int
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class StateCleanupReport:
    plan_id: str
    processed: int
    deleted: int
    protected: int
    retained: int
    reclaimed_bytes: int
    next_cursor: str | None


def _retention_error(message: str, code: ErrorCode = ErrorCode.STATE_INVALID) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        safe_next_action=(
            "Rebuild a bounded cleanup preview from current checksums, references, protections, "
            "timestamps, and reviewed quotas."
        ),
    )


def _parse_timestamp(value: str, field: str) -> object:
    from datetime import datetime

    if not isinstance(value, str) or len(value) > 64:
        raise _retention_error(f"{field} must be a bounded ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _retention_error(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise _retention_error(f"{field} must include a timezone offset")
    return parsed


def _validate_lifecycle_record_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
    ):
        raise _retention_error("lifecycle record identifier is invalid")
    return value


def _validate_reason(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None
    ):
        raise _retention_error(f"{field} is invalid")
    return value


class IntegritySeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True, order=True)
class StateIntegrityFinding:
    severity: IntegritySeverity
    code: str
    record_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class StateIntegrityReport:
    collection: str
    scanned_records: int
    total_bytes: int
    findings: tuple[StateIntegrityFinding, ...]
    findings_truncated: bool
    healthy: bool


@dataclass(frozen=True, slots=True)
class StateBackupRecord:
    record_id: str
    checksum: str
    size_bytes: int
    schema_version: SchemaVersion
    revision: int


@dataclass(frozen=True, slots=True)
class StateBackupPreview:
    backup_id: str
    manifest_checksum: str
    collection: str
    destination_id: str
    records: tuple[StateBackupRecord, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class StateBackupReport:
    backup_id: str
    copied_records: int
    total_bytes: int
    destination_id: str


@dataclass(frozen=True, slots=True)
class StateRestorePreview:
    restore_id: str
    plan_digest: str
    backup_id: str
    manifest_checksum: str
    collection: str
    destination_id: str
    records: tuple[StateBackupRecord, ...]
    conflicts: tuple[tuple[str, str], ...]
    total_bytes: int
    overwrite: bool


@dataclass(frozen=True, slots=True)
class StateRestoreReport:
    restore_id: str
    restored_records: int
    replaced_records: int
    total_bytes: int
    backup_id: str
