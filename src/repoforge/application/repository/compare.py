from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ...config import RepositoryConfig
from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...ports.git import GitChangedFileEvidence
from ..context import ApplicationContext
from .commit_read import validate_evidence_limit


@dataclass(frozen=True, slots=True)
class RepositoryCompareCommand:
    repo_id: str
    base_ref: str
    head_ref: str
    path_glob: str | None = None
    max_files: int = 100
    include_patch: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryCompareResult:
    repo_id: str
    requested_base_ref: str
    base_resolved_ref: str
    base_sha: str
    requested_head_ref: str
    head_resolved_ref: str
    head_sha: str
    merge_base_sha: str
    ahead: int
    behind: int
    path_glob: str | None
    files: tuple[GitChangedFileEvidence, ...]
    total_files: int
    returned_files: int
    files_truncated: bool
    additions: int
    deletions: int
    binary_files: int
    omitted_paths: int
    include_patch: bool
    patch: str | None
    patch_truncated: bool
    binary_patch_omitted: bool


def validate_evidence_glob(path_glob: str | None) -> str | None:
    if path_glob is None:
        return None
    candidate = PurePosixPath(path_glob)
    if (
        not path_glob
        or len(path_glob) > 256
        or path_glob.startswith(("/", "-", ":"))
        or any(ord(character) < 32 for character in path_glob)
        or ".." in candidate.parts
        or "\\" in path_glob
    ):
        raise SecurityError("Unsafe path_glob")
    return path_glob


class RepositoryComparer:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: RepositoryCompareCommand) -> RepositoryCompareResult:
        limit, path_glob, repo = self._validate(command)
        return self.ctx.audited(
            "repo_compare",
            {
                "repo_id": command.repo_id,
                "base_ref": command.base_ref,
                "head_ref": command.head_ref,
                "path_glob": path_glob,
                "max_files": limit,
                "include_patch": command.include_patch,
            },
            lambda: self._compare(command, repo, limit, path_glob),
        )

    def compute(self, command: RepositoryCompareCommand) -> RepositoryCompareResult:
        """Compare two commits without creating a nested audit event."""
        limit, path_glob, repo = self._validate(command)
        return self._compare(command, repo, limit, path_glob)

    def _validate(
        self,
        command: RepositoryCompareCommand,
    ) -> tuple[int, str | None, RepositoryConfig]:
        if not command.base_ref or not command.head_ref:
            raise RepoForgeError(
                "base_ref and head_ref must be non-empty committed refs",
                code=ErrorCode.REPOSITORY_REF_DISALLOWED,
            )
        return (
            validate_evidence_limit(command.max_files),
            validate_evidence_glob(command.path_glob),
            self.ctx.repo(command.repo_id),
        )

    def _compare(
        self,
        command: RepositoryCompareCommand,
        repo: RepositoryConfig,
        limit: int,
        path_glob: str | None,
    ) -> RepositoryCompareResult:
        base = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.base_ref)
        head = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.head_ref)
        evidence = self.ctx.git.compare_commits(
            repo.path,
            repo,
            base,
            head,
            path_glob,
            limit,
            command.include_patch,
        )
        return RepositoryCompareResult(
            command.repo_id,
            command.base_ref,
            base.resolved_ref,
            base.commit_sha,
            command.head_ref,
            head.resolved_ref,
            head.commit_sha,
            evidence.merge_base_sha,
            evidence.ahead,
            evidence.behind,
            path_glob,
            evidence.files,
            evidence.total_files,
            len(evidence.files),
            evidence.files_truncated,
            evidence.additions,
            evidence.deletions,
            evidence.binary_files,
            evidence.omitted_paths,
            command.include_patch,
            evidence.patch,
            evidence.patch_truncated,
            evidence.binary_patch_omitted,
        )
