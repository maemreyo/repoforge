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


def _error(message: str, code: ErrorCode = ErrorCode.STATE_MIGRATION_INVALID) -> RepoForgeError:
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
