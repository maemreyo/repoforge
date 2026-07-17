"""Repository-configured operation-level policy for the unified mutation tool."""

from __future__ import annotations

from typing import Final

from .errors import ConfigError

MUTATION_OPS: Final[tuple[str, ...]] = (
    "replace_text",
    "write",
    "create",
    "delete",
    "move",
    "apply_patch",
    "restore",
)


def validate_allowed_mutation_ops(
    values: tuple[str, ...],
    repo_id: str,
) -> tuple[str, ...]:
    """Validate and canonicalize one repository's explicit mutation allowlist."""

    if len(set(values)) != len(values):
        raise ConfigError(f"repositories.{repo_id}.allowed_mutation_ops contains duplicates")
    unsupported = sorted(set(values) - set(MUTATION_OPS))
    if unsupported:
        raise ConfigError(
            f"repositories.{repo_id}.allowed_mutation_ops contains unsupported ops: {unsupported}"
        )
    return tuple(op for op in MUTATION_OPS if op in values)
