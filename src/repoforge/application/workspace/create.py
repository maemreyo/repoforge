from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from ...domain.auth_profile import AuthProfileSelector
from ...domain.errors import ErrorCode, SecurityError, WorkspaceError
from ...domain.operations import hash_idempotency_key
from ...domain.policy import slugify, validate_adopted_branch, validate_branch
from ...domain.workspace import WorkspaceKind, WorkspaceRecord, normalize_issue_ids
from ..context import ApplicationContext, repository_policy_snapshot
from ..dto import to_data
from ..idempotency import IdempotencyEffectBoundary
from .removal_safety import build_stale_workspaces_nudge


@dataclass(frozen=True, slots=True)
class WorkspaceCreateCommand:
    repo_id: str
    task_slug: str
    base: str | None = None
    idempotency_key: str | None = None
    issue_ids: tuple[str, ...] = ()
    # "Work on THIS branch": check out an existing branch instead of cutting a fresh
    # ai/* one. Mutually exclusive with `base` -- there is nothing to branch from when the
    # branch already exists.
    adopt_branch: str | None = None
    # "Work on the checkout the operator already has open": attach to a worktree git
    # already tracks for this repository, without creating a branch, worktree, or file.
    # Mutually exclusive with both `base` and `adopt_branch` -- there is nothing to check
    # out when the operator's own checkout is the point.
    attach_branch: str | None = None
    selector: AuthProfileSelector = field(default_factory=AuthProfileSelector)


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
    issue_ids: tuple[str, ...] = ()
    stale_workspaces: dict[str, Any] | None = None
    adopted_branch: bool = False
    # Distinct from adopted_branch: an attached workspace has no worktree RepoForge
    # created either, on top of not owning the branch.
    attached: bool = False
    # Non-blocking, and deliberately part of the result rather than a log line: adopting a
    # branch (or attaching to one) trades away isolation, and the caller acting on it must
    # be told, not the operator's terminal history.
    warnings: tuple[str, ...] = ()


def _attach_workspace_id(repo_id: str, branch: str) -> str:
    """Deterministic from (repo_id, branch) alone -- never from task_slug, an idempotency
    key, or the resolved checkout path. The path is looked up fresh on every call and can
    legitimately change (the operator moved or recreated the worktree); keying on it would
    make a moved checkout register as a second workspace instead of self-healing the
    existing one (#372's idempotent-reattach AC covers both "same checkout" and "moved")."""
    digest = hashlib.sha256(f"{repo_id}:{branch}".encode()).hexdigest()
    return f"attached-{slugify(branch)[:24]}-{digest[:10]}"


class WorkspaceCreator:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def _resolve_attach_target(self, repo: Any, attach: str) -> tuple[Path, str]:
        """Find the one worktree git already tracks for `repo.path` that has `attach`
        checked out, and return its path and HEAD sha. Reads only -- never creates a
        branch, worktree, or file (#372's discovery boundary and AC)."""
        validate_adopted_branch(attach, repo)
        entries = self.ctx.git.list_worktrees(repo.path)
        matches = [entry for entry in entries if not entry.detached and entry.branch == attach]
        if not matches:
            detached_count = sum(1 for entry in entries if entry.detached)
            raise WorkspaceError(
                f"ATTACH_BRANCH_NOT_FOUND: no worktree of {repo.repo_id!r} has {attach!r} "
                "checked out",
                details={
                    "known_branches": sorted({entry.branch for entry in entries if entry.branch}),
                    "detached_worktree_count": detached_count,
                },
                safe_next_action=(
                    "Check out the branch in a worktree of this repository first, or pass "
                    "the exact branch name it is already checked out on."
                ),
                unchanged_state=("No branch, worktree, or file was created.",),
            )
        if len(matches) > 1:
            # Git itself refuses to check out one branch into two worktrees, so this
            # should not be reachable in practice; treated as ambiguous rather than
            # silently picking one if the repository's git state ever disagrees.
            raise WorkspaceError(
                f"ATTACH_BRANCH_AMBIGUOUS: {attach!r} matches more than one worktree",
                details={"candidate_paths": [entry.path for entry in matches]},
                unchanged_state=("No branch, worktree, or file was created.",),
            )
        entry = matches[0]
        resolved = Path(entry.path)
        if entry.prunable or not resolved.is_dir():
            raise WorkspaceError(
                f"ATTACH_WORKTREE_MISSING: the checkout git recorded for {attach!r} no "
                f"longer exists at {entry.path}",
                safe_next_action=(
                    "Run `git worktree prune` in the repository, or recreate the checkout, "
                    "then retry."
                ),
                unchanged_state=("No branch, worktree, or file was created.",),
            )
        return resolved.resolve(), entry.head_sha or self.ctx.git.head_sha(resolved)

    def execute(self, c: WorkspaceCreateCommand) -> WorkspaceCreateResult:
        repo = self.ctx.repo(c.repo_id)
        issue_ids = normalize_issue_ids(c.issue_ids)
        adopt = c.adopt_branch.strip() if c.adopt_branch else None
        attach = c.attach_branch.strip() if c.attach_branch else None
        if sum(1 for value in (adopt, attach, c.base) if value) > 1:
            raise WorkspaceError(
                "ADOPT_BASE_CONFLICT: adopt_branch, attach_branch, and base each describe a "
                "different way to get a workspace; pass exactly one."
            )
        slug = slugify(c.task_slug)
        key_hash = hash_idempotency_key(c.idempotency_key) if c.idempotency_key else None
        warnings: tuple[str, ...] = ()
        attach_head_sha: str | None = None

        if attach:
            destination, attach_head_sha = self._resolve_attach_target(repo, attach)
            branch = attach
            base = attach
            workspace_id = _attach_workspace_id(repo.repo_id, attach)
            warnings = (
                f"ATTACHED_SHARED: this workspace operates directly on the operator's own "
                f"checkout of {attach!r} at {destination}. RepoForge created neither the "
                "worktree nor the branch and will not remove either; the checkout can "
                "change, move, or disappear outside of anything this workspace does.",
            )
        else:
            suffix = key_hash[:10] if key_hash else self.ctx.ids.new_hex(10)
            workspace_id = f"{slug[:24]}-{suffix}"
            if adopt:
                # The operator named this branch explicitly, so the ai/* convention and the
                # base allowlist do not apply -- neither describes a branch that already
                # exists. What still applies is refused, not warned: see
                # validate_adopted_branch.
                validate_adopted_branch(adopt, repo)
                branch = adopt
                # `base` records what the workspace sits on; for an adopted branch that is
                # the branch itself, and claiming `main` here would misreport what a diff
                # is against.
                base = adopt
                warnings = (
                    f"ADOPTED_BRANCH: this workspace works directly on {adopt!r} instead of "
                    f"a fresh {repo.branch_prefix}* branch. Commits land on a branch that is "
                    "not exclusively owned by this workspace, so isolation is reduced: "
                    "anything else using it (your editor, another worktree, a teammate) sees "
                    "these changes, and `workspace_remove` will not delete it.",
                )
            else:
                base = c.base or repo.default_base
                if base not in repo.allowed_base_branches:
                    raise SecurityError(
                        f"Base branch {base!r} is not allowlisted: {repo.allowed_base_branches}"
                    )
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
        boundary = IdempotencyEffectBoundary()

        def reconcile() -> WorkspaceCreateResult | None:
            if attach:
                try:
                    existing = self.ctx.store.load(workspace_id)
                except Exception:
                    return None
                if (
                    existing.repo_id != repo.repo_id
                    or existing.kind is not WorkspaceKind.ATTACHED_SHARED
                ):
                    raise WorkspaceError(
                        "IDEMPOTENCY_CONFLICT: deterministic attach identity belongs to "
                        "different state"
                    )
                if existing.branch != branch or existing.path != str(destination):
                    # Self-heal, not a conflict: the operator's checkout moved or was
                    # recreated at a new path (#372's "moved" case) while the branch
                    # identity -- and therefore workspace_id -- is unchanged. "Shared means
                    # shared": this is an observation to reconcile, not a pre-refusal.
                    existing.branch = branch
                    existing.base = base
                    existing.path = str(destination)
                    self.ctx.store.save(existing)
                return WorkspaceCreateResult(
                    workspace_id,
                    repo.repo_id,
                    existing.path,
                    existing.branch,
                    existing.base,
                    attach_head_sha or self.ctx.git.head_sha(Path(existing.path)),
                    next_step,
                    tuple(existing.metadata.get("issue_ids", ())),
                    attached=True,
                    warnings=warnings,
                )
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
                tuple(existing.metadata.get("issue_ids", ())),
                adopted_branch=existing.kind is WorkspaceKind.ADOPTED_WORKTREE,
                warnings=warnings,
            )

        def op() -> WorkspaceCreateResult:
            recovered = reconcile()
            if recovered is not None:
                return recovered
            if not attach and destination.exists():
                raise WorkspaceError(
                    f"Workspace destination already exists: {destination}",
                    safe_next_action="Inspect and remove the orphaned deterministic worktree before retrying.",
                    unchanged_state=(
                        "The source repository and existing registered workspaces remain unchanged.",
                    ),
                )
            # Both intents kept: the effect boundary opens BEFORE any worktree exists
            # (#234), and which worktree call runs depends on adoption (#281). Attach
            # creates nothing, so no worktree call runs at all.
            boundary.begin()
            if attach:
                assert attach_head_sha is not None
                head = attach_head_sha
            else:
                head = (
                    self.ctx.git.adopt_worktree(repo, destination, branch)
                    if adopt
                    else self.ctx.git.create_worktree(repo, destination, branch, base)
                )
            try:
                identity_gateway = self.ctx.commit_identities
                if identity_gateway is None:
                    raise WorkspaceError(
                        "Commit identity gateway is unavailable; workspace identity cannot be pinned",
                        code=ErrorCode.COMMIT_IDENTITY_UNRESOLVED,
                    )
                commit_identity = identity_gateway.resolve_policy(
                    destination,
                    repo.commit_identity,
                )
                commit_config = identity_gateway.config_snapshot(destination)
                metadata: dict[str, object] = {
                    "repository_policy_snapshot": repository_policy_snapshot(repo),
                    "workspace_base_sha": head,
                    "task_slug": c.task_slug,
                    "commit_identity_policy": commit_identity.durable_payload(),
                    "commit_identity_config_digest": commit_config.digest,
                    "commit_identity_config_snapshot": commit_config.safe_payload(),
                }
                if issue_ids:
                    metadata["issue_ids"] = list(issue_ids)
                if key_hash:
                    metadata["workspace_create_idempotency"] = key_hash
                kind = (
                    WorkspaceKind.ATTACHED_SHARED
                    if attach
                    else WorkspaceKind.ADOPTED_WORKTREE
                    if adopt
                    else WorkspaceKind.MANAGED_WORKTREE
                )
                record = WorkspaceRecord(
                    workspace_id,
                    repo.repo_id,
                    str(destination),
                    branch,
                    base,
                    repo.remote,
                    self.ctx.clock.now_iso(),
                    metadata=metadata,
                    kind=kind,
                )
                self.ctx.store.save(record)
            except Exception as exc:
                if not attach:
                    try:
                        self.ctx.git.remove_worktree(repo, destination, branch, not bool(adopt))
                    except Exception as cleanup_exc:
                        raise WorkspaceError(
                            "Workspace identity initialization or registry save failed and "
                            f"compensation failed: {cleanup_exc}"
                        ) from exc
                boundary.rollback()
                raise
            return WorkspaceCreateResult(
                workspace_id,
                repo.repo_id,
                str(destination),
                branch,
                base,
                head,
                next_step,
                issue_ids,
                adopted_branch=bool(adopt),
                attached=bool(attach),
                warnings=warnings,
            )

        request = {
            "repo_id": c.repo_id,
            "task_slug": c.task_slug,
            "base": base,
            "issue_ids": list(issue_ids),
            "adopt_branch": adopt,
            "attach_branch": attach,
            "selector": c.selector.payload(),
        }
        result = cast(
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
                    "issue_ids": list(issue_ids),
                },
                serialize=to_data,
                deserialize=lambda value: WorkspaceCreateResult(**value),
                effect_boundary=boundary,
            ),
        )
        # Computed fresh on every call (even an idempotent cache hit) since workspace
        # staleness across the whole repository changes over time independent of this
        # specific create call's cached result.
        nudge = build_stale_workspaces_nudge(self.ctx)
        return replace(result, stale_workspaces=nudge) if nudge is not None else result
