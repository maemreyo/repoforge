"""Bind a compiled context pack to every input that can change what it contains.

A pack is only trustworthy if a caller can tell that two packs are the same pack. That is
what `pack_hash` is for: it changes when any input that could change the compiled content
changes, and stays identical when none of them do. A cached or replayed pack whose hash
still matches is provably the same governance snapshot.

The inputs are taken as parameters rather than read here. `config_generation` in particular
lives on the runtime record, not on the application context, so a compiler that reached for
it would have to know about runtime state to hash a value. Keeping this pure also makes the
determinism and sensitivity properties testable without a repository at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...domain.operations import request_fingerprint

COMPILER_SCHEMA_VERSION = 1
"""Bumped when the compiler's output shape changes for identical inputs.

Without it, upgrading the compiler would silently reuse packs built by the previous one
under a hash that no longer describes them.
"""

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_FOCUS_PATHS = 100
_MAX_PATH_LENGTH = 4096


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256")
    return value


def _positive(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _focus_paths(values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize to a sorted unique set.

    `focus_paths` selects which rules and skills apply, so it is a set: the same paths in a
    different order, or listed twice, describe the same pack and must not produce a
    different hash.
    """

    if len(values) > _MAX_FOCUS_PATHS:
        raise ValueError(f"focus_paths exceeds the {_MAX_FOCUS_PATHS}-entry bound")
    normalized: set[str] = set()
    for item in values:
        if not isinstance(item, str) or not item or len(item) > _MAX_PATH_LENGTH:
            raise ValueError(f"focus_paths contains an invalid path: {item!r}")
        normalized.add(item)
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class ContextPackBinding:
    """Everything a compiled pack's identity depends on.

    ``code_snapshot_sha`` is the workspace tree's identity -- callers pass the workspace
    fingerprint, which covers uncommitted state, because a pack compiled against a dirty
    tree is not the same pack as one compiled against its commit.

    ``constitution_sha`` already aggregates the digests of every file the constitution was
    compiled from (see :mod:`repoforge.application.rules.constitution`), so those source
    digests are bound through it rather than carried a second time -- a separate copy could
    disagree with the hash that produced it.
    """

    code_snapshot_sha: str
    constitution_sha: str
    config_generation: int
    task_revision: int
    policy_digest: str
    focus_paths: tuple[str, ...] = ()
    compiler_schema_version: int = field(default=COMPILER_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "code_snapshot_sha", _digest(self.code_snapshot_sha, "code_snapshot_sha")
        )
        object.__setattr__(
            self, "constitution_sha", _digest(self.constitution_sha, "constitution_sha")
        )
        object.__setattr__(self, "policy_digest", _digest(self.policy_digest, "policy_digest"))
        object.__setattr__(
            self, "config_generation", _positive(self.config_generation, "config_generation")
        )
        object.__setattr__(self, "task_revision", _positive(self.task_revision, "task_revision"))
        object.__setattr__(
            self,
            "compiler_schema_version",
            _positive(self.compiler_schema_version, "compiler_schema_version"),
        )
        object.__setattr__(self, "focus_paths", _focus_paths(self.focus_paths))

    def as_dict(self) -> dict[str, object]:
        """The exact binding inputs, for both hashing and public reporting."""

        return {
            "code_snapshot_sha": self.code_snapshot_sha,
            "constitution_sha": self.constitution_sha,
            "config_generation": self.config_generation,
            "task_revision": self.task_revision,
            "policy_digest": self.policy_digest,
            "focus_paths": list(self.focus_paths),
            "compiler_schema_version": self.compiler_schema_version,
        }

    @property
    def pack_hash(self) -> str:
        """Canonical hash over every binding input.

        Uses the repository's existing canonical-JSON fingerprint rather than a second
        hashing convention, so key order and formatting cannot drift between the two.
        """

        return request_fingerprint(self.as_dict())


__all__ = ["COMPILER_SCHEMA_VERSION", "ContextPackBinding"]
