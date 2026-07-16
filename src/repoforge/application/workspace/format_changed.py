"""Run one reviewed formatter over server-derived changed paths only."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ...domain.errors import ConfigError, ErrorCode, RepoForgeError, SecurityError
from ..context import ApplicationContext
from ..fingerprint_cache import prime_fingerprint, read_fingerprint
from .hygiene_common import select_formatter, select_policy_paths


@dataclass(frozen=True, slots=True)
class WorkspaceFormatChangedCommand:
    workspace_id: str
    expected_fingerprint: str
    formatter_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceFormatChangedResult:
    workspace_id: str
    formatter_id: str
    formatter_contract_hash: str
    selected_paths: list[str]
    modified_paths: list[str]
    changed_paths: list[str]
    unexpected_paths: list[str]
    environment_identity: str
    output_truncated: bool
    fingerprint_before: str
    fingerprint_after: str
    fingerprint_changed: bool
    head_sha: str
    verification_invalidated: bool
    next_safe_actions: list[dict[str, object]]


def _file_digest(workspace: Path, relative_path: str) -> str:
    candidate = workspace / relative_path
    if not candidate.exists() and not candidate.is_symlink():
        return "<missing>"
    if candidate.is_symlink():
        return "<symlink>"
    if not candidate.is_file():
        return "<non-regular>"
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_state(workspace: Path, paths: list[str]) -> dict[str, str]:
    return {path: _file_digest(workspace, path) for path in paths}


class WorkspaceChangedFormatter:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: WorkspaceFormatChangedCommand) -> WorkspaceFormatChangedResult:
        _, repo, _ = self.ctx.workspace(command.workspace_id)
        policy = select_formatter(repo, command.formatter_id, allow_unavailable=False)
        hygiene = self.ctx.hygiene
        if policy is None or hygiene is None:  # pragma: no cover - guarded above/bootstrap.
            raise ConfigError("Reviewed formatter adapter is unavailable")
        audit_details: dict[str, object] = {
            "workspace_id": command.workspace_id,
            "formatter_id": policy.formatter_id,
            "formatter_contract_hash": policy.contract_hash,
        }

        def operation() -> WorkspaceFormatChangedResult:
            with self.ctx.locks.lock(command.workspace_id):
                record, locked_repo, workspace = self.ctx.workspace(command.workspace_id)
                locked_policy = select_formatter(
                    locked_repo,
                    policy.formatter_id,
                    allow_unavailable=False,
                )
                assert locked_policy is not None
                before = read_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                ).fingerprint
                if command.expected_fingerprint != before:
                    raise RepoForgeError(
                        "Workspace changed since formatter remediation was reviewed: "
                        f"expected {command.expected_fingerprint}, current {before}",
                        code=ErrorCode.STALE_STATE,
                        retryable=True,
                        safe_next_action=(
                            "Refresh workspace_hygiene_status and retry with its exact workspace_fingerprint."
                        ),
                    )
                before_paths = self.ctx.git.changed_paths(workspace, locked_repo)
                selected = select_policy_paths(
                    before_paths,
                    repo=locked_repo,
                    policy=locked_policy,
                )
                approved: list[str] = []
                for path in selected:
                    candidate = workspace / path
                    if candidate.is_symlink():
                        raise SecurityError(f"Formatter cannot operate on a symlink: {path}")
                    if candidate.is_file():
                        approved.append(path)
                audit_details["selected_path_count"] = len(approved)
                audit_details["selected_paths_digest"] = hashlib.sha256(
                    "\n".join(approved).encode("utf-8")
                ).hexdigest()
                before_states = _path_state(workspace, before_paths)
                if approved:
                    receipt = hygiene.format_paths(
                        workspace,
                        locked_policy,
                        tuple(approved),
                    )
                else:
                    receipt = hygiene.format_paths(workspace, locked_policy, ())
                after_paths = self.ctx.git.changed_paths(workspace, locked_repo)
                after_states = _path_state(workspace, after_paths)
                touched = sorted(
                    path
                    for path in set(before_states) | set(after_states)
                    if before_states.get(path) != after_states.get(path)
                )
                unexpected = sorted(set(touched) - set(approved))
                after_receipt = prime_fingerprint(
                    self.ctx.fingerprint_cache,
                    command.workspace_id,
                    self.ctx.git,
                    workspace,
                )
                after = after_receipt.fingerprint
                fingerprint_changed = after != before
                verification_invalidated = False
                if fingerprint_changed and record.last_verification is not None:
                    record.last_verification = None
                    self.ctx.store.save(record)
                    verification_invalidated = True
                audit_details["modified_path_count"] = len(touched)
                audit_details["outcome"] = "unexpected_mutation" if unexpected else "completed"
                if unexpected:
                    raise SecurityError(
                        "Formatter changed paths outside the reviewed changed-path scope",
                        details={
                            "unexpected_path_count": len(unexpected),
                            "unexpected_paths_digest": hashlib.sha256(
                                "\n".join(unexpected).encode("utf-8")
                            ).hexdigest(),
                        },
                        unchanged_state=(
                            "No commit, configuration generation, or remote state changed; the reported workspace paths may have changed.",
                        ),
                        safe_next_action=(
                            "Inspect and explicitly restore unexpected workspace paths before retrying."
                        ),
                    )
                actions: list[dict[str, object]] = []
                if fingerprint_changed:
                    actions.append(
                        {
                            "action": "workspace_run_diagnostic",
                            "reason": "Formatter remediation changed the workspace fingerprint; rerun the narrow GREEN diagnostic.",
                            "required": True,
                        }
                    )
                else:
                    actions.append(
                        {
                            "action": "continue",
                            "reason": "Formatter was a no-op and preserved the exact workspace fingerprint.",
                            "required": False,
                        }
                    )
                return WorkspaceFormatChangedResult(
                    workspace_id=command.workspace_id,
                    formatter_id=locked_policy.formatter_id,
                    formatter_contract_hash=locked_policy.contract_hash,
                    selected_paths=approved,
                    modified_paths=touched,
                    changed_paths=sorted(after_paths),
                    unexpected_paths=[],
                    environment_identity=receipt.environment_identity,
                    output_truncated=receipt.output_truncated,
                    fingerprint_before=before,
                    fingerprint_after=after,
                    fingerprint_changed=fingerprint_changed,
                    head_sha=self.ctx.git.head_sha(workspace),
                    verification_invalidated=verification_invalidated,
                    next_safe_actions=actions,
                )

        return self.ctx.audited(
            "workspace_format_changed",
            audit_details,
            operation,
            mutating=True,
        )
