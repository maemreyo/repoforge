"""Semantic capability classification for reviewed resolved configurations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

import tomli as tomllib

from .errors import ConfigError


class CapabilityDeltaKind(str, Enum):
    """The activation-relevant relationship between two resolved configurations."""

    EQUIVALENT = "equivalent"
    EXPANSION = "expansion"
    RESTRICTION = "restriction"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class CapabilityDelta:
    """Deterministic classification with canonical policy snapshots for auditability."""

    kind: CapabilityDeltaKind
    before: str
    after: str


def _policy_snapshot(lock_text: str) -> str:
    try:
        parsed: Any = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Cannot classify invalid resolved configuration: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("Resolved configuration must be a TOML table")
    lock = parsed.pop("repoforge_lock", None)
    if not isinstance(lock, dict):
        raise ConfigError("Resolved configuration has no repoforge_lock table")
    lock.pop("generation", None)
    lock.pop("source_sha256", None)
    parsed["repoforge_lock"] = lock
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def classify_capability_delta(current_lock: str, candidate_lock: str) -> CapabilityDelta:
    """Classify reviewed policy semantics without treating formatting as a change."""
    before = _policy_snapshot(current_lock)
    after = _policy_snapshot(candidate_lock)
    if before == after:
        return CapabilityDelta(CapabilityDeltaKind.EQUIVALENT, before, after)
    try:
        before_value: Any = json.loads(before)
        after_value: Any = json.loads(after)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Cannot classify resolved configuration: {exc}") from exc
    before_atoms = _atoms(before_value)
    after_atoms = _atoms(after_value)
    if before_atoms < after_atoms:
        return CapabilityDelta(CapabilityDeltaKind.EXPANSION, before, after)
    if after_atoms < before_atoms:
        return CapabilityDelta(CapabilityDeltaKind.RESTRICTION, before, after)
    return CapabilityDelta(CapabilityDeltaKind.INCOMPATIBLE, before, after)


def _atoms(value: Any, prefix: str = "") -> frozenset[str]:
    if isinstance(value, dict):
        return frozenset().union(
            *(_atoms(item, f"{prefix}.{key}" if prefix else key) for key, item in value.items())
        )
    if isinstance(value, list):
        return frozenset().union(*(_atoms(item, prefix) for item in value))
    return frozenset({f"{prefix}={json.dumps(value, sort_keys=True, ensure_ascii=False)}"})
