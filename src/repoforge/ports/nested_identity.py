"""Read-only discovery and resolution boundaries for nested identities."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.nested_identity import (
    NestedAccess,
    NestedResourceCandidate,
    NestedResourceTarget,
)
from ..domain.repository_identity import ActorClass, AuthLease

_MAX_DEPTH = 32
_MAX_RESOURCES = 256
_MAX_OUTPUT_BYTES = 1_048_576
_MAX_TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class NestedDiscoveryRequest:
    root: Path
    primary_endpoint: str
    submodule_access: NestedAccess = NestedAccess.READ
    lfs_access: NestedAccess = NestedAccess.READ
    include_lfs: bool = False
    max_depth: int = 8
    max_resources: int = 64
    max_output_bytes: int = 262_144
    command_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("root must be an absolute Path")
        if not isinstance(self.primary_endpoint, str) or not self.primary_endpoint:
            raise ValueError("primary_endpoint is required")
        if not isinstance(self.submodule_access, NestedAccess):
            raise ValueError("submodule_access must be a NestedAccess")
        if not isinstance(self.lfs_access, NestedAccess):
            raise ValueError("lfs_access must be a NestedAccess")
        if not isinstance(self.include_lfs, bool):
            raise ValueError("include_lfs must be boolean")
        for value, name, maximum, allow_zero in (
            (self.max_depth, "max_depth", _MAX_DEPTH, True),
            (self.max_resources, "max_resources", _MAX_RESOURCES, False),
            (self.max_output_bytes, "max_output_bytes", _MAX_OUTPUT_BYTES, False),
            (
                self.command_timeout_seconds,
                "command_timeout_seconds",
                _MAX_TIMEOUT_SECONDS,
                False,
            ),
        ):
            minimum = 0 if allow_zero else 1
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be between {minimum} and {maximum}")


class NestedResourceDiscovery(Protocol):
    def discover(self, request: NestedDiscoveryRequest) -> tuple[NestedResourceCandidate, ...]: ...


class NestedTargetResolver(Protocol):
    def resolve(self, candidate: NestedResourceCandidate) -> NestedResourceTarget: ...


class NestedLeaseProvider(Protocol):
    def acquire(
        self,
        *,
        operation_id: str,
        actor_class: ActorClass,
        target: NestedResourceTarget,
        profile_id: str,
        capability_ids: tuple[str, ...],
        config_revision: str,
        policy_revision: str,
        now: str,
    ) -> AuthLease: ...


__all__ = [
    "NestedDiscoveryRequest",
    "NestedLeaseProvider",
    "NestedResourceDiscovery",
    "NestedTargetResolver",
]
