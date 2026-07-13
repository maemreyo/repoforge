from __future__ import annotations

from pathlib import Path
from typing import Protocol

from repoforge.audit import AuditLogger
from repoforge.config import load_config
from repoforge.runner import CommandRunner
from repoforge.service import CodingService
from repoforge.state import StateStore
from repoforge.workspace_create import (
    WorkspaceCreateCommand,
    WorkspaceCreator,
    WorkspaceCreatorPorts,
)


class WorkspaceCreatorEnvironment(Protocol):
    config_path: Path


def test_coding_service_uses_explicit_dependencies(tmp_path: Path) -> None:
    # Given: a valid config and concrete dependencies supplied by the composition root.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{repo}"
''',
        encoding="utf-8",
    )
    config = load_config(config_path)
    runner = CommandRunner(config.server)
    state = StateStore(config.server.state_root)
    audit = AuditLogger(config.server.state_root)

    # When: application service composition receives the adapters.
    service = CodingService(config, runner=runner, state=state, audit=audit)

    # Then: it uses exactly the supplied adapters without changing its public API.
    assert service.runner is runner
    assert service.state is state
    assert service.audit is audit


def test_workspace_creator_creates_and_registers_isolated_worktree(
    forge_env: WorkspaceCreatorEnvironment,
) -> None:
    # Given: a configured local Git repository and injected production adapters.
    config = load_config(forge_env.config_path)
    runner = CommandRunner(config.server)
    state = StateStore(config.server.state_root)
    creator = WorkspaceCreator(
        WorkspaceCreatorPorts(
            runner=runner,
            state=state,
            workspace_root=config.server.workspace_root,
            verification_timeout_seconds=config.server.verification_timeout_seconds,
        )
    )

    # When: the typed application command creates a workspace.
    repository = config.repositories["demo"]
    plan = creator.plan(repository, WorkspaceCreateCommand("demo", "Improve developer experience"))
    created = creator.execute(repository, plan)

    # Then: the branch and persistent registry agree on the isolated workspace.
    record = state.load(created.workspace_id)
    assert created.branch.startswith("ai/improve-developer-experience-")
    assert record.path == str(created.path)
    assert record.branch == created.branch
