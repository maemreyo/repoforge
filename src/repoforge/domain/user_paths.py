"""Deterministic user-owned RepoForge path contracts."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, fields
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/repoforge/config.toml").expanduser()
DEFAULT_WORKSPACE_ROOT = "~/.local/share/repoforge/workspaces"
DEFAULT_STATE_ROOT = "~/.local/state/repoforge"


@dataclass(frozen=True, slots=True)
class RepoForgePaths:
    config_file: Path
    state_root: Path
    generation_root: Path
    onboarding_root: Path
    workspace_registry: Path
    audit_log: Path
    metrics_file: Path
    runtime_log: Path
    diagnostics_root: Path

    def payload(self) -> dict[str, dict[str, object]]:
        payload: dict[str, dict[str, object]] = {}
        override = os.environ.get("REPOFORGE_CONFIG")
        override_path = Path(override).expanduser().resolve() if override else None
        for item in fields(self):
            path = getattr(self, item.name)
            if not isinstance(path, Path):
                continue
            entry: dict[str, object] = {
                "path": str(path),
                "exists": path.exists(),
                "kind": "directory" if path.is_dir() else "file" if path.is_file() else "missing",
            }
            if item.name == "config_file":
                entry["overridden_by"] = (
                    "REPOFORGE_CONFIG"
                    if override_path is not None and override_path == path
                    else None
                )
            payload[item.name] = entry
        return payload


def resolve_repoforge_paths(
    config_path: str | Path,
    *,
    state_root: str | Path | None = None,
) -> RepoForgePaths:
    """Resolve paths without creating or mutating any of them."""
    config_file = Path(config_path).expanduser().resolve()
    resolved_state = Path(state_root or DEFAULT_STATE_ROOT).expanduser().resolve()
    digest = hashlib.sha256(str(config_file).encode("utf-8")).hexdigest()[:16]
    lock_root = resolved_state / "config-locks" / digest
    return RepoForgePaths(
        config_file=config_file,
        state_root=resolved_state,
        generation_root=lock_root / "generations-v3",
        onboarding_root=resolved_state / "onboarding",
        workspace_registry=resolved_state / "workspaces.json",
        audit_log=resolved_state / "audit.jsonl",
        metrics_file=resolved_state / "metrics.json",
        runtime_log=lock_root / "managed-runtime.log",
        diagnostics_root=lock_root / "diagnostics",
    )
