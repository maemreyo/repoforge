from __future__ import annotations

from dataclasses import dataclass

from ...config import RepositoryConfig
from ...domain.ci_evidence import sanitize_ci_text
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.git import GitActorIdentity, GitChangedFileEvidence
from ..context import ApplicationContext

_MAX_EVIDENCE_FILES = 500


@dataclass(frozen=True, slots=True)
class RepositoryCommitReadCommand:
    repo_id: str
    ref: str
    max_files: int = 100
    include_patch: bool = False


@dataclass(frozen=True, slots=True)
class RepositoryCommitReadResult:
    repo_id: str
    requested_ref: str
    resolved_ref: str
    commit_sha: str
    tree_sha: str
    parent_shas: tuple[str, ...]
    comparison_parent_sha: str | None
    author: GitActorIdentity
    committer: GitActorIdentity
    identity_truncated: bool
    identity_redacted: bool
    subject: str
    body: str
    message_truncated: bool
    message_redacted: bool
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


def validate_evidence_limit(max_files: int) -> int:
    if (
        not isinstance(max_files, int)
        or isinstance(max_files, bool)
        or not 1 <= max_files <= _MAX_EVIDENCE_FILES
    ):
        raise RepoForgeError(
            f"max_files must be between 1 and {_MAX_EVIDENCE_FILES}",
            code=ErrorCode.REPOSITORY_EVIDENCE_LIMIT_INVALID,
            unchanged_state=("No repository, workspace, or Git reference was modified.",),
        )
    return max_files


class RepositoryCommitReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def _sanitize_identity(
        self,
        identity: GitActorIdentity,
        repo: RepositoryConfig,
    ) -> tuple[GitActorIdentity, bool, bool]:
        limit = max(1, min(512, self.ctx.config.server.max_tool_output_chars))
        name = sanitize_ci_text(identity.name, repo, max_chars=limit)
        email = sanitize_ci_text(identity.email, repo, max_chars=limit)
        return (
            GitActorIdentity(name.text, email.text, identity.date),
            name.truncated or email.truncated,
            name.redacted or email.redacted,
        )

    def execute(self, command: RepositoryCommitReadCommand) -> RepositoryCommitReadResult:
        limit, repo = self._validate(command)
        return self.ctx.audited(
            "repo_commit_read",
            {
                "repo_id": command.repo_id,
                "ref": command.ref,
                "max_files": limit,
                "include_patch": command.include_patch,
            },
            lambda: self._read(command, repo, limit),
        )

    def compute(self, command: RepositoryCommitReadCommand) -> RepositoryCommitReadResult:
        """Read one commit without creating a nested audit event."""
        limit, repo = self._validate(command)
        return self._read(command, repo, limit)

    def _validate(
        self,
        command: RepositoryCommitReadCommand,
    ) -> tuple[int, RepositoryConfig]:
        if not command.ref or any(ord(character) < 32 for character in command.ref):
            raise RepoForgeError(
                "ref must be a non-empty bounded committed ref",
                code=ErrorCode.REPOSITORY_REF_DISALLOWED,
            )
        return validate_evidence_limit(command.max_files), self.ctx.repo(command.repo_id)

    def _read(
        self,
        command: RepositoryCommitReadCommand,
        repo: RepositoryConfig,
        limit: int,
    ) -> RepositoryCommitReadResult:
        snapshot = self.ctx.git.resolve_snapshot_ref(repo.path, repo, command.ref)
        evidence = self.ctx.git.read_commit_evidence(
            repo.path,
            repo,
            snapshot,
            limit,
            command.include_patch,
        )
        author, author_truncated, author_redacted = self._sanitize_identity(
            evidence.author,
            repo,
        )
        committer, committer_truncated, committer_redacted = self._sanitize_identity(
            evidence.committer,
            repo,
        )
        subject = sanitize_ci_text(
            evidence.subject,
            repo,
            max_chars=min(4_000, self.ctx.config.server.max_tool_output_chars),
        )
        body = sanitize_ci_text(
            evidence.body,
            repo,
            max_chars=min(20_000, self.ctx.config.server.max_tool_output_chars),
        )
        return RepositoryCommitReadResult(
            command.repo_id,
            command.ref,
            snapshot.resolved_ref,
            snapshot.commit_sha,
            evidence.tree_sha,
            evidence.parent_shas,
            evidence.comparison_parent_sha,
            author,
            committer,
            author_truncated or committer_truncated,
            author_redacted or committer_redacted,
            subject.text,
            body.text,
            evidence.message_truncated or subject.truncated or body.truncated,
            subject.redacted or body.redacted,
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
