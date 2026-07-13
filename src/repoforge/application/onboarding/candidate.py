"""Candidate generation smoke tests isolated from production state."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from ...config import load_config


def smoke_candidate(
    resolved_text: str, repo_ids: tuple[str, ...], *, state_root: Path
) -> tuple[dict[str, object], ...]:
    from ..service import CodingService

    del state_root
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="repoforge-onboarding-smoke-") as directory:
        root = Path(directory)
        resolved = root / "resolved.toml"
        resolved.write_text(resolved_text, encoding="utf-8")
        config = load_config(resolved)
        config = replace(
            config,
            server=replace(
                config.server, workspace_root=root / "workspaces", state_root=root / "state"
            ),
        )
        service = CodingService(config)
        for repo_id in repo_ids:
            workspace = service.workspace_create(repo_id, "onboarding-smoke")
            workspace_id = str(workspace["workspace_id"])
            try:
                service.repo_context(repo_id)
                service.workspace_status(workspace_id)
                service.workspace_tree(workspace_id, 50)
                service.workspace_diff(workspace_id)
            finally:
                service.workspace_remove(workspace_id, delete_local_branch=True)
            results.append({"ok": True, "repo_id": repo_id})
    return tuple(results)
