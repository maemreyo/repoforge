"""Typed application use case for applying unified diffs in Git workspace directories.

All safety invariants from CodingService.workspace_apply_patch are preserved:
- validate_patch bounds, path, symlink/gitlink policy rejection
- expected HEAD OID format validation (40 or 64 lowercase hex)
- workspace fingerprint SHA format validation (64 lowercase hex)
- State lock, stale HEAD rejection, stale fingerprint rejection
- git apply --check with whitespace error, application with whitespace fix
- Post-apply changed-path policy validation (symlinks, gitlinks, denied paths)
- Best-effort reverse-patch rollback if post-apply policy fails
- Detectable incomplete rollback via pre-apply vs post-rollback fingerprint comparison
- Fresh fingerprint and diff stat in the result
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .config import RepositoryConfig
from .errors import SecurityError, WorkspaceError
from .ports import CommandExecutor, WorkspaceStore
from .security import assert_path_allowed

_GIT_OID_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceApplyPatchCommand:
    """Validated caller intent to apply one unified diff to a registered workspace."""

    workspace_id: str
    patch: str
    expected_head_sha: str
    expected_workspace_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceApplyPatchResult:
    """Applied patch identity, changed paths, fresh fingerprint, and diff statistic."""

    workspace_id: str
    changed_paths: tuple[str, ...]
    workspace_fingerprint: str
    diff_stat: str


@dataclass(frozen=True, slots=True)
class WorkspaceApplyPatchPorts:
    """Constrained adapters required to apply a patch in a workspace."""

    state: WorkspaceStore
    runner: CommandExecutor
    max_fingerprint_bytes: int
    verification_timeout_seconds: int


# ---------------------------------------------------------------------------
# Module-level helpers (reused by the use case and callable for test harness)
# ---------------------------------------------------------------------------


def head_sha(runner: CommandExecutor, workspace_path: Path) -> str:
    """Return the current HEAD SHA of the workspace worktree."""
    return runner.run(["git", "rev-parse", "HEAD"], cwd=workspace_path).stdout.strip()


def workspace_fingerprint(
    runner: CommandExecutor,
    workspace_path: Path,
    *,
    max_fingerprint_bytes: int,
    verification_timeout_seconds: int,
) -> str:
    """Compute a SHA-256 digest of the working-tree state (HEAD + diff + untracked content).

    Matches the algorithm in ``CodingService._fingerprint`` to guarantee interchangeability.
    """
    digest = hashlib.sha256()
    digest.update(head_sha(runner, workspace_path).encode())
    diff = runner.run_bytes(
        ["git", "diff", "--binary", "HEAD", "--"],
        cwd=workspace_path,
        timeout=verification_timeout_seconds,
        max_bytes=max_fingerprint_bytes,
    )
    digest.update(diff)
    untracked_raw = runner.run_bytes(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace_path,
        max_bytes=max_fingerprint_bytes,
    )
    total = len(diff) + len(untracked_raw)
    for raw_name in sorted(item for item in untracked_raw.split(b"\x00") if item):
        relative = raw_name.decode("utf-8", errors="strict")
        file_path = workspace_path / relative
        digest.update(b"\x00UNTRACKED\x00" + raw_name + b"\x00")
        if file_path.is_symlink():
            target = os.readlink(file_path)
            encoded = target.encode("utf-8")
            total += len(encoded)
            digest.update(encoded)
        elif file_path.is_file():
            with file_path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > max_fingerprint_bytes:
                        raise WorkspaceError(
                            "Working-tree fingerprint exceeds configured max_fingerprint_bytes"
                        )
                    digest.update(chunk)
    return digest.hexdigest()


def _assert_changed_paths_allowed(
    runner: CommandExecutor,
    workspace_path: Path,
    repo: RepositoryConfig,
    *,
    max_fingerprint_bytes: int,
) -> None:
    """Verify that every changed, staged, or untracked path respects repository policy.

    Raises ``SecurityError`` if any path is denied, is a symlink, or has symlink/gitlink
    (120000/160000) mode in the index or HEAD tree.
    """
    commands = (
        ["git", "diff", "--name-only", "-z", "--"],
        ["git", "diff", "--cached", "--name-only", "-z", "--"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    )
    changed: list[str] = []
    for cmd in commands:
        raw_bytes = runner.run_bytes(cmd, cwd=workspace_path, max_bytes=max_fingerprint_bytes)
        raw = raw_bytes.decode("utf-8", errors="strict")
        for item in raw.split("\x00"):
            if item and item not in changed:
                changed.append(item)
    for item in changed:
        _ = assert_path_allowed(item, repo)
        candidate = workspace_path / item
        if candidate.is_symlink():
            raise SecurityError(f"Changed symlinks are not allowed: {item}")
        index_entry = runner.run(
            ["git", "ls-files", "-s", "--", item], cwd=workspace_path, check=False
        ).stdout.strip()
        head_entry = runner.run(
            ["git", "ls-tree", "HEAD", "--", item], cwd=workspace_path, check=False
        ).stdout.strip()
        modes = {
            entry.split(maxsplit=1)[0]
            for entry in (index_entry, head_entry)
            if entry and entry.split(maxsplit=1)
        }
        if modes.intersection({"120000", "160000"}):
            raise SecurityError(f"Symlink or submodule changes are not allowed: {item}")


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


class WorkspacePatchApplier:
    """Applies a unified diff to a registered workspace.

    Owns the locked critical section:
    1. Format-validate ``expected_head_sha`` and ``expected_workspace_fingerprint``.
    2. Verify HEAD SHA has not advanced (stale-head rejection).
    3. Verify workspace fingerprint matches the caller snapshot (stale-fingerprint rejection).
    4. Snapshot pre-apply fingerprint for rollback verification.
    5. ``git apply --check`` with whitespace-as-error.
    6. ``git apply --whitespace=fix`` — actual application.
    7. Post-apply changed-path policy validation.
    8. If policy fails, best-effort reverse-patch rollback.
       After rollback, compare fingerprint to the pre-apply snapshot. A
       mismatch means rollback itself was incomplete — raise a separate
       WorkspaceError so the caller knows the workspace is in an
       inconsistent state rather than silently re-raising the original
       policy exception.
    9. Fresh fingerprint and ``diff --stat`` response.
    """

    def __init__(self, ports: WorkspaceApplyPatchPorts) -> None:
        self._ports: WorkspaceApplyPatchPorts = ports

    def execute(
        self,
        repo: RepositoryConfig,
        workspace_path: Path,
        command: WorkspaceApplyPatchCommand,
        changed_paths: tuple[str, ...],
    ) -> WorkspaceApplyPatchResult:
        """Format-validate, apply the patch, verify policy, roll back on violation.

        Parameters
        ----------
        repo:
            Repository configuration governing path policy.
        workspace_path:
            Resolved absolute path to the worktree directory.
        command:
            Caller intent — ``expected_head_sha`` and
            ``expected_workspace_fingerprint`` are validated here.
        changed_paths:
            Paths extracted from the patch header by ``validate_patch``.

        Returns
        -------
        WorkspaceApplyPatchResult
            The fresh fingerprint already reflects the applied change.

        Raises
        ------
        ValueError
            ``expected_head_sha`` or ``expected_workspace_fingerprint`` have
            invalid format (messages match the existing ``CodingService``
            facade exactly).
        WorkspaceError
            HEAD or fingerprint changed since the caller's snapshot, or
            rollback failed to fully restore the workspace.
        SecurityError
            Post-apply policy violation (rollback already performed).
        CommandError
            ``git apply --check`` rejected the patch.
        """
        # -- Format validation (pre-lock, deterministic, matches facade)
        if not _GIT_OID_RE.fullmatch(command.expected_head_sha):
            raise ValueError("expected_head_sha must be a lowercase 40/64 hex Git object id")
        if not _SHA256_RE.fullmatch(command.expected_workspace_fingerprint):
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")

        with self._ports.state.lock(command.workspace_id):
            actual_head = head_sha(self._ports.runner, workspace_path)
            if actual_head != command.expected_head_sha:
                raise WorkspaceError(
                    f"HEAD changed: expected {command.expected_head_sha}, got {actual_head}"
                )
            actual_fingerprint = workspace_fingerprint(
                self._ports.runner,
                workspace_path,
                max_fingerprint_bytes=self._ports.max_fingerprint_bytes,
                verification_timeout_seconds=self._ports.verification_timeout_seconds,
            )
            if actual_fingerprint != command.expected_workspace_fingerprint:
                raise WorkspaceError(
                    "Workspace changed since it was inspected; refresh status before applying patch"
                )
            # Snapshot: the exact workspace state before modification.
            # Used after rollback to confirm the revert was complete.
            pre_apply_fingerprint = actual_fingerprint

            # --check rejects whitespace errors and structural problems
            _ = self._ports.runner.run(
                ["git", "apply", "--check", "--whitespace=error-all", "-"],
                cwd=workspace_path,
                input_text=command.patch,
            )
            # Apply with whitespace fix
            _ = self._ports.runner.run(
                ["git", "apply", "--whitespace=fix", "-"],
                cwd=workspace_path,
                input_text=command.patch,
            )
            try:
                _assert_changed_paths_allowed(
                    self._ports.runner,
                    workspace_path,
                    repo,
                    max_fingerprint_bytes=self._ports.max_fingerprint_bytes,
                )
            except Exception:
                # Best-effort reverse-patch rollback: a patch that violates
                # post-apply policy must not leave the workspace in a
                # partially unsafe state.  The broad ``except Exception``
                # is intentional — ANY validation failure (SecurityError
                # from denied path, symlink, gitlink) must trigger
                # rollback because the patch has already been written to
                # the working tree.  Narrowing the catch would risk
                # leaving the workspace in a compromised state when an
                # unexpected checker error occurs.
                _ = self._ports.runner.run(
                    ["git", "apply", "-R", "--whitespace=nowarn", "-"],
                    cwd=workspace_path,
                    input_text=command.patch,
                    check=False,
                )
                # Detect incomplete rollback by comparing fingerprints.
                post_rollback_fingerprint = workspace_fingerprint(
                    self._ports.runner,
                    workspace_path,
                    max_fingerprint_bytes=self._ports.max_fingerprint_bytes,
                    verification_timeout_seconds=self._ports.verification_timeout_seconds,
                )
                if post_rollback_fingerprint != pre_apply_fingerprint:
                    # The original policy violation is now secondary to the
                    # rollback failure — the workspace is in an inconsistent
                    # state regardless of the original SecurityError.
                    raise WorkspaceError(
                        "Rollback did not fully restore workspace after policy violation; workspace may be in an inconsistent state \u2014 report this defect"
                    ) from None
                # Bare re-raise preserves the original policy exception
                # (normally SecurityError) with its full traceback.
                raise

            new_fingerprint = workspace_fingerprint(
                self._ports.runner,
                workspace_path,
                max_fingerprint_bytes=self._ports.max_fingerprint_bytes,
                verification_timeout_seconds=self._ports.verification_timeout_seconds,
            )
            diff_stat = self._ports.runner.run(
                ["git", "diff", "--stat", "--"],
                cwd=workspace_path,
            ).stdout

            return WorkspaceApplyPatchResult(
                workspace_id=command.workspace_id,
                changed_paths=changed_paths,
                workspace_fingerprint=new_fingerprint,
                diff_stat=diff_stat,
            )
