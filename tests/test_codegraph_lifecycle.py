from __future__ import annotations

from pathlib import Path

from conftest import TEST_CONFIG_GENERATION, ForgeEnvironment

from repoforge.adapters.codegraph.lifecycle import CodeGraphCleanupResult, CodeGraphLifecycle
from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config


class Projection:
    def __init__(self) -> None:
        self.disposed: list[str] = []
        self.cleaned: list[tuple[frozenset[str], int]] = []

    def dispose_workspace(self, workspace_id: str) -> None:
        self.disposed.append(workspace_id)

    def cleanup_workspaces(
        self,
        active_workspace_ids: frozenset[str],
        *,
        limit: int,
    ) -> tuple[int, int, int]:
        self.cleaned.append((active_workspace_ids, limit))
        return (2, 1, 1)


def test_lifecycle_delegates_bounded_cleanup_and_disposal() -> None:
    projection = Projection()
    lifecycle = CodeGraphLifecycle(projection)

    result = lifecycle.startup_cleanup(frozenset({"active"}), limit=7)
    lifecycle.dispose_workspace("workspace-1")

    assert result == CodeGraphCleanupResult(2, 1, 1)
    assert projection.cleaned == [(frozenset({"active"}), 7)]
    assert projection.disposed == ["workspace-1"]


def test_workspace_remove_disposes_provider_state(
    forge_env: ForgeEnvironment,
) -> None:
    projection = Projection()
    lifecycle = CodeGraphLifecycle(projection)
    config = load_config(forge_env.config_path)
    service = CodingService(
        config,
        application=build_application(
            config,
            overrides=AdapterOverrides(provider_workspace_lifecycle=lifecycle),
            config_generation=TEST_CONFIG_GENERATION,
        ),
    )
    workspace_id = service.workspace_create("demo", "provider lifecycle removal")["workspace_id"]

    service.workspace_remove(workspace_id)

    assert projection.disposed == [workspace_id]


def test_lifecycle_never_accepts_worktree_paths() -> None:
    projection = Projection()
    lifecycle = CodeGraphLifecycle(projection)

    lifecycle.dispose_workspace("workspace-1")

    assert projection.disposed == ["workspace-1"]
    assert not any(isinstance(item, Path) for item in projection.disposed)
