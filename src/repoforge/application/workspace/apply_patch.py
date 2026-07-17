from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from ...domain.errors import WorkspaceError
from ..context import ApplicationContext
from ..dto import to_data
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from ..idempotency import IdempotencyEffectBoundary
from .patch_input import normalize_workspace_patch

_OID = re.compile("^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_SHA = re.compile("^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class WorkspaceApplyPatchCommand:
    workspace_id: str
    patch: str
    expected_head_sha: str
    expected_workspace_fingerprint: str
    idempotency_key: str | None = None


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


def _deserialize_patch_result(value: Any) -> WorkspaceApplyPatchResult:
    return WorkspaceApplyPatchResult(
        workspace_id=str(value["workspace_id"]),
        changed_paths=tuple(str(item) for item in value["changed_paths"]),
        workspace_fingerprint=str(value["workspace_fingerprint"]),
        diff_stat=str(value["diff_stat"]),
        input_format=str(value["input_format"]),
        normalized_patch_sha256=str(value["normalized_patch_sha256"]),
        repair_actions=tuple(str(item) for item in value["repair_actions"]),
        head_sha=str(value["head_sha"]),
    )


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

        effect = IdempotencyEffectBoundary()

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
                effect.begin()
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

        return cast(
            WorkspaceApplyPatchResult,
            self.ctx.idempotent(
                "workspace_apply_patch",
                c.idempotency_key,
                {
                    "workspace_id": c.workspace_id,
                    "input_patch_sha256": audit_details["input_patch_sha256"],
                    "expected_head_sha": c.expected_head_sha,
                    "expected_workspace_fingerprint": c.expected_workspace_fingerprint,
                },
                op,
                details=audit_details,
                serialize=to_data,
                deserialize=_deserialize_patch_result,
                effect_boundary=effect,
            ),
        )
