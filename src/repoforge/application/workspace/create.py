from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.operations import hash_idempotency_key
from ...domain.policy import slugify, validate_branch
from ...domain.workspace import WorkspaceRecord
from ..context import ApplicationContext, repository_policy_snapshot
from ..dto import to_data


@dataclass(frozen=True, slots=True)
class WorkspaceCreateCommand:
    repo_id: str
    task_slug: str
    base: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceCreateResult:
    workspace_id: str
    repo_id: str
    path: str
    branch: str
    base: str
    head_sha: str
    next_step: str = (
        "Inspect files, make changes, run a verification profile, then review the diff."
    )


class WorkspaceCreator:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceCreateCommand) -> WorkspaceCreateResult:
        repo = self.ctx.repo(c.repo_id)
        base = c.base or repo.default_base
        if base not in repo.allowed_base_branches:
            raise SecurityError(
                f"Base branch {base!r} is not allowlisted: {repo.allowed_base_branches}"
            )
        slug = slugify(c.task_slug)
        key_hash = hash_idempotency_key(c.idempotency_key) if c.idempotency_key else None
        suffix = key_hash[:10] if key_hash else self.ctx.ids.new_hex(10)
        workspace_id = f"{slug[:24]}-{suffix}"
        branch = f"{repo.branch_prefix}{slug}-{suffix}"
        validate_branch(branch, repo)
        root = self.ctx.config.server.workspace_root.resolve()
        destination = (root / repo.repo_id / workspace_id).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise SecurityError("Generated workspace path escaped workspace_root") from exc

        next_step = (
            "Inspect files and repository context. This repository is enrolled read-only."
            if repo.read_only
            else "Inspect files, make changes, run a verification profile, then review the diff."
        )

        def reconcile() -> WorkspaceCreateResult | None:
            if key_hash is None:
                return None
            try:
                existing = self.ctx.store.load(workspace_id)
            except Exception:
                return None
            if (
                existing.repo_id != repo.repo_id
                or existing.path != str(destination)
                or existing.branch != branch
                or existing.base != base
                or existing.metadata.get("workspace_create_idempotency") != key_hash
            ):
                raise WorkspaceError(
                    "IDEMPOTENCY_CONFLICT: deterministic workspace identity belongs to different state"
                )
            if not destination.is_dir():
                raise WorkspaceError(
                    "Workspace registry exists but its deterministic worktree is missing",
                    safe_next_action="Remove the stale workspace registry entry, then retry with a new key.",
                    unchanged_state=(
                        "The source repository and other workspaces remain unchanged.",
                    ),
                )
            return WorkspaceCreateResult(
                workspace_id,
                repo.repo_id,
                str(destination),
                branch,
                base,
                self.ctx.git.head_sha(destination),
                next_step,
            )

        def op() -> WorkspaceCreateResult:
            recovered = reconcile()
            if recovered is not None:
                return recovered
            if destination.exists():
                raise WorkspaceError(
                    f"Workspace destination already exists: {destination}",
                    safe_next_action="Inspect and remove the orphaned deterministic worktree before retrying.",
                    unchanged_state=(
                        "The source repository and existing registered workspaces remain unchanged.",
                    ),
                )
            head = self.ctx.git.create_worktree(repo, destination, branch, base)
            metadata: dict[str, object] = {
                "repository_policy_snapshot": repository_policy_snapshot(repo),
            }
            if key_hash:
                metadata["workspace_create_idempotency"] = key_hash
            record = WorkspaceRecord(
                workspace_id,
                repo.repo_id,
                str(destination),
                branch,
                base,
                repo.remote,
                self.ctx.clock.now_iso(),
                metadata=metadata,
            )
            try:
                self.ctx.store.save(record)
            except Exception as exc:
                try:
                    self.ctx.git.remove_worktree(repo, destination, branch, True)
                except Exception as cleanup_exc:
                    raise WorkspaceError(
                        f"Workspace registry save failed and compensation failed: {cleanup_exc}"
                    ) from exc
                raise
            return WorkspaceCreateResult(
                workspace_id, repo.repo_id, str(destination), branch, base, head, next_step
            )

        request = {"repo_id": c.repo_id, "task_slug": c.task_slug, "base": base}
        return cast(
            WorkspaceCreateResult,
            self.ctx.idempotent(
                "workspace_create",
                c.idempotency_key,
                request,
                op,
                details={
                    "repo_id": c.repo_id,
                    "base": base,
                    "branch": branch,
                    "workspace_id": workspace_id,
                },
                serialize=to_data,
                deserialize=lambda value: WorkspaceCreateResult(**value),
            ),
        )
