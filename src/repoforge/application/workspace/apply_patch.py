from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ...domain.errors import WorkspaceError
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from .patch_input import normalize_workspace_patch

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
    input_format: str
    normalized_patch_sha256: str
    repair_actions: tuple[str, ...]
    head_sha: str


class WorkspacePatchApplier:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceApplyPatchCommand) -> WorkspaceApplyPatchResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if not _OID.fullmatch(c.expected_head_sha):
            raise ValueError("expected_head_sha must be a lowercase 40/64 hex Git object id")
        if not _SHA.fullmatch(c.expected_workspace_fingerprint):
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")
        audit_details: dict[str, object] = {
            "workspace_id": c.workspace_id,
            "input_patch_sha256": hashlib.sha256(c.patch.encode("utf-8")).hexdigest(),
        }

        def op() -> WorkspaceApplyPatchResult:
            with self.ctx.locks.lock(c.workspace_id):
                actual_head = self.ctx.git.head_sha(path)
                if actual_head != c.expected_head_sha:
                    raise WorkspaceError(
                        f"HEAD changed: expected {c.expected_head_sha}, got {actual_head}"
                    )
                before_lookup = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    path,
                )
                before = before_lookup.fingerprint
                if before != c.expected_workspace_fingerprint:
                    raise WorkspaceError(
                        "Workspace changed since it was inspected; refresh status before applying patch"
                    )
                normalized, changed = normalize_workspace_patch(
                    workspace_root=path,
                    repository=repo,
                    server=self.ctx.config.server,
                    filesystem=self.ctx.filesystem,
                    patch=c.patch,
                )
                audit_details.update(
                    {
                        "input_format": normalized.input_format,
                        "normalized_patch_sha256": normalized.normalized_sha256,
                        "repair_actions": list(normalized.repair_actions),
                        "changed_paths": list(changed),
                        "fingerprint_source": before_lookup.source,
                        "fingerprint_duration_ms": before_lookup.duration_ms,
                    }
                )
                self.ctx.git.apply_patch(path, normalized.patch)
                try:
                    actual_changed = tuple(self.ctx.git.changed_paths(path, repo))
                    self.ctx.git.enforce_change_budget(path, repo)
                except Exception:
                    self.ctx.git.reverse_patch(path, normalized.patch)
                    rollback_lookup = prime_fingerprint(
                        self.ctx.fingerprint_cache,
                        c.workspace_id,
                        self.ctx.git,
                        path,
                    )
                    if rollback_lookup.fingerprint != before:
                        raise WorkspaceError(
                            "Patch violated policy and rollback did not fully restore the workspace"
                        ) from None
                    raise

                after_lookup = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    c.workspace_id,
                    self.ctx.git,
                    path,
                )
                after = after_lookup.fingerprint
                audit_details["post_mutation_fingerprint_duration_ms"] = after_lookup.duration_ms
                return WorkspaceApplyPatchResult(
                    c.workspace_id,
                    actual_changed,
                    after,
                    self.ctx.git.diff_stat(path),
                    normalized.input_format,
                    normalized.normalized_sha256,
                    normalized.repair_actions,
                    self.ctx.git.head_sha(path),
                )

        return self.ctx.audited(
            "workspace_apply_patch",
            audit_details,
            op,
        )
