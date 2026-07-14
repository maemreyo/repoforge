"""Workspace-bound patch inspection, policy validation, and normalization."""

from __future__ import annotations

from pathlib import Path

from ...config import RepositoryConfig, ServerConfig
from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.patches import PatchNormalizationResult, inspect_patch, normalize_patch
from ...domain.policy import assert_path_allowed, resolve_workspace_path, validate_patch
from ...ports.filesystem import FileSystem


def normalize_workspace_patch(
    *,
    workspace_root: Path,
    repository: RepositoryConfig,
    server: ServerConfig,
    filesystem: FileSystem,
    patch: str,
) -> tuple[PatchNormalizationResult, tuple[str, ...]]:
    """Normalize one reviewed patch against the exact current workspace snapshot."""
    inspection = inspect_patch(patch)
    approved_paths = tuple(assert_path_allowed(item, repository) for item in inspection.paths)

    def read_file(relative_path: str) -> str | None:
        approved = assert_path_allowed(relative_path, repository)
        candidate = resolve_workspace_path(workspace_root, approved, repository)
        if not filesystem.exists(candidate):
            return None
        if filesystem.is_symlink(candidate) or not filesystem.is_file(candidate):
            raise SecurityError(
                f"Patch input may read only policy-approved regular files: {approved}"
            )
        size = filesystem.size(candidate)
        if size > server.max_file_bytes:
            raise RepoForgeError(
                f"Patch target exceeds max_file_bytes: {approved}",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Use a smaller reviewed target or adjust the repository policy outside this operation.",
                unchanged_state=("The workspace tree, index, and HEAD were not modified.",),
                details={
                    "target_path": approved,
                    "size_bytes": size,
                    "max_file_bytes": server.max_file_bytes,
                },
            )
        try:
            return filesystem.read_text(candidate)
        except (OSError, UnicodeError) as exc:
            raise RepoForgeError(
                f"Patch target is not readable UTF-8 text: {approved}",
                code=ErrorCode.PATCH_PARSE_FAILED,
                safe_next_action="Use workspace_write_file only for reviewed UTF-8 content or choose another allowed path.",
                unchanged_state=("The workspace tree, index, and HEAD were not modified.",),
                details={"target_path": approved},
            ) from exc

    normalized = normalize_patch(patch, read_file)
    canonical_paths = validate_patch(
        normalized.patch,
        repository,
        max_chars=server.max_tool_output_chars * 4,
    )
    if set(canonical_paths) != set(approved_paths):
        # Moves and canonical add/delete pairs may reorder paths, but may never introduce authority.
        unexpected = sorted(set(canonical_paths) - set(approved_paths))
        if unexpected:
            raise SecurityError(
                f"Normalized patch introduced paths absent from the reviewed input: {unexpected}"
            )
    return normalized, canonical_paths
