"""Pure construction of a metadata-only diagnostics bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...domain.redaction import redact_data

_EXCLUSIONS = (
    "configuration bodies",
    "repository file content",
    "patches and pull request bodies",
    "process environment and credentials",
    "runtime log content",
)


def build_diagnostics_bundle(
    *,
    created_at: str,
    config_path: Path,
    accepted: dict[str, Any] | None,
    active: dict[str, Any] | None,
    runtime: dict[str, Any],
    capabilities: dict[str, Any] | None,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    """Return bounded diagnostic metadata with recursive credential redaction."""
    payload = {
        "schema_version": 1,
        "created_at": created_at,
        "config": {
            "source_path": str(config_path),
            "accepted": accepted,
            "active": active,
        },
        "runtime": runtime,
        "capabilities": capabilities or {"status": "unavailable"},
        "metrics": metrics,
        "exclusions": list(_EXCLUSIONS),
    }
    redacted = redact_data(payload)
    assert isinstance(redacted, dict)
    return redacted
