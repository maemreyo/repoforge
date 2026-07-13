"""Typed application use case for creating isolated Git workspaces."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import RepositoryConfig
from .errors import SecurityError, WorkspaceError
from .ports import CommandExecutor, WorkspaceStore
from .security import slugify, validate_branch
from .state import WorkspaceRecord, utc_now


@dataclass(frozen=True, slots=True)
class WorkspaceCreateCommand:
    """Validated caller intent for one repository workspace."""

    repo_id: str
    task_slug: str
    base: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceCreateResult:
    """Created workspace identity and Git state."""

    workspace_id: str
    repo_id: str
    path: Path
    branch: str
    base: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class WorkspaceCreatePlan:
    """Validated workspace identity and destination prepared for creation."""

    workspace_id: str
    repo_id: str
    destination: Path
    branch: str
    base: str


@dataclass(frozen=True, slots=True)
class WorkspaceCreatorPorts:
    """Constrained adapters required to create and register a workspace."""

    runner: CommandExecutor
    state: WorkspaceStore
    workspace_root: Path
    verification_timeout_seconds: int


class WorkspaceCreator:
    """Creates one validated, registered Git worktree."""

    def __init__(self, ports: WorkspaceCreatorPorts) -> None:
        self._ports: WorkspaceCreatorPorts = ports

    def plan(self, repo: RepositoryConfig, command: WorkspaceCreateCommand) -> WorkspaceCreatePlan:
        """Validate caller intent and prepare one workspace destination."""
        if command.repo_id != repo.repo_id:
            raise SecurityError("Workspace command repository does not match configured repository")
        base = command.base or repo.default_base
        if base not in repo.allowed_base_branches:
            raise SecurityError(
                f"Base branch {base!r} is not allowlisted: {repo.allowed_base_branches}"
            )
        slug = slugify(command.task_slug)
        suffix = uuid.uuid4().hex[:10]
        workspace_id = f"{slug[:24]}-{suffix}"
        branch = f"{repo.branch_prefix}{slug}-{suffix}"
        validate_branch(branch, repo)
        workspace_root = self._ports.workspace_root.resolve()
        destination = (workspace_root / repo.repo_id / workspace_id).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _ = destination.relative_to(workspace_root)
        except ValueError as exc:
            raise SecurityError("Generated workspace path escaped workspace_root") from exc
        return WorkspaceCreatePlan(workspace_id, repo.repo_id, destination, branch, base)

    def execute(self, repo: RepositoryConfig, plan: WorkspaceCreatePlan) -> WorkspaceCreateResult:
        """Create and register a previously validated workspace plan."""
        if plan.repo_id != repo.repo_id:
            raise SecurityError("Workspace plan repository does not match configured repository")
        if plan.destination.exists():
            raise WorkspaceError(f"Workspace destination already exists: {plan.destination}")
        if repo.fetch_before_workspace:
            _ = self._ports.runner.run(
                ["git", "fetch", "--prune", repo.remote, plan.base], cwd=repo.path
            )
        base_ref = f"{repo.remote}/{plan.base}"
        _ = self._ports.runner.run(
            ["git", "worktree", "add", "-b", plan.branch, str(plan.destination), base_ref],
            cwd=repo.path,
            timeout=self._ports.verification_timeout_seconds,
        )
        self._ports.state.save(
            WorkspaceRecord(
                workspace_id=plan.workspace_id,
                repo_id=repo.repo_id,
                path=str(plan.destination),
                branch=plan.branch,
                base=plan.base,
                remote=repo.remote,
                created_at=utc_now(),
            )
        )
        head_sha = self._ports.runner.run(
            ["git", "rev-parse", "HEAD"], cwd=plan.destination
        ).stdout.strip()
        return WorkspaceCreateResult(
            workspace_id=plan.workspace_id,
            repo_id=plan.repo_id,
            path=plan.destination,
            branch=plan.branch,
            base=plan.base,
            head_sha=head_sha,
        )
