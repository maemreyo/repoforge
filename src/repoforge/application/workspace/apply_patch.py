from __future__ import annotations

import re
from dataclasses import dataclass

from ...domain.errors import WorkspaceError
from ...domain.policy import validate_patch
from ..context import ApplicationContext

_OID = re.compile("^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_SHA = re.compile("^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceApplyPatchCommand:
    workspace_id: str
    patch: str
    expected_head_sha: str
    expected_workspace_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceApplyPatchResult:
    workspace_id: str
    changed_paths: tuple[str, ...]
    workspace_fingerprint: str
    diff_stat: str


class WorkspacePatchApplier:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceApplyPatchCommand) -> WorkspaceApplyPatchResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if not _OID.fullmatch(c.expected_head_sha):
            raise ValueError("expected_head_sha must be a lowercase 40/64 hex Git object id")
        if not _SHA.fullmatch(c.expected_workspace_fingerprint):
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")
        changed = validate_patch(
            c.patch, repo, max_chars=self.ctx.config.server.max_tool_output_chars * 4
        )

        def op() -> WorkspaceApplyPatchResult:
            with self.ctx.locks.lock(c.workspace_id):
                actual_head = self.ctx.git.head_sha(path)
                if actual_head != c.expected_head_sha:
                    raise WorkspaceError(
                        f"HEAD changed: expected {c.expected_head_sha}, got {actual_head}"
                    )
                before = self.ctx.git.fingerprint(path)
                if before != c.expected_workspace_fingerprint:
                    raise WorkspaceError(
                        "Workspace changed since it was inspected; refresh status before applying patch"
                    )
                self.ctx.git.apply_patch(path, c.patch)
                try:
                    self.ctx.git.changed_paths(path, repo)
                except Exception:
                    self.ctx.git.reverse_patch(path, c.patch)
                    if self.ctx.git.fingerprint(path) != before:
                        raise WorkspaceError(
                            "Patch violated policy and rollback did not fully restore the workspace"
                        ) from None
                    raise
                return WorkspaceApplyPatchResult(
                    c.workspace_id,
                    changed,
                    self.ctx.git.fingerprint(path),
                    self.ctx.git.diff_stat(path),
                )

        return self.ctx.audited(
            "workspace_apply_patch",
            {"workspace_id": c.workspace_id, "changed_paths": list(changed)},
            op,
        )
