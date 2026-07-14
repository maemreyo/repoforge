"""Semantic Git adapter; application code never builds Git argv."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from ...config import ProfileConfig, RepositoryConfig, ServerConfig
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ...ports.command import CommandExecutor, CommandResult
from ...ports.git import (
    GitActorIdentity,
    GitBaseReferences,
    GitChangedFileEvidence,
    GitCommitEvidence,
    GitComparisonEvidence,
    GitMergePreview,
    GitMergeResult,
    GitSnapshotBlob,
    ResolvedRepositoryRef,
)


@dataclass(frozen=True, slots=True)
class _RawDiffEntry:
    status_code: str
    old_mode: str
    new_mode: str
    path: str
    previous_path: str | None


@dataclass(frozen=True, slots=True)
class _CollectedEvidence:
    files: tuple[GitChangedFileEvidence, ...]
    total_files: int
    files_truncated: bool
    additions: int
    deletions: int
    binary_files: int
    omitted_paths: int
    patch: str | None
    patch_truncated: bool
    binary_patch_omitted: bool


class GitCliRepository:
    def __init__(self, executor: CommandExecutor, server: ServerConfig):
        self._executor = executor
        self.server = server

    @property
    def executor(self) -> CommandExecutor:
        return self._executor

    def is_worktree(self, path: Path) -> bool:
        result = self._executor.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=path, check=False
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def diff_stat(self, path: Path) -> str:
        return self._executor.run(["git", "diff", "--stat", "--"], cwd=path).stdout

    def current_branch(self, path: Path) -> str:
        return self._executor.run(["git", "branch", "--show-current"], cwd=path).stdout.strip()

    def head_sha(self, path: Path) -> str:
        return self._executor.run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()

    def status_porcelain(self, path: Path) -> str:
        return self._executor.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=path
        ).stdout

    def status_short_branch(self, path: Path) -> str:
        return self._executor.run(["git", "status", "--short", "--branch"], cwd=path).combined

    def remote_verbose(self, path: Path) -> str:
        return self._executor.run(["git", "remote", "-v"], cwd=path).combined

    def changed_paths(self, path: Path, repo: RepositoryConfig) -> list[str]:
        changed = []
        for cmd in (
            ["git", "diff", "--name-only", "-z", "--"],
            ["git", "diff", "--cached", "--name-only", "-z", "--"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        ):
            raw = self._executor.run_bytes(
                cmd, cwd=path, max_bytes=self.server.max_fingerprint_bytes
            ).decode("utf-8", errors="strict")
            for item in raw.split("\x00"):
                if item and item not in changed:
                    changed.append(item)
        for item in changed:
            assert_path_allowed(item, repo)
            candidate = path / item
            if candidate.is_symlink():
                raise SecurityError(f"Changed symlinks are not allowed: {item}")
            index = self._executor.run(
                ["git", "ls-files", "-s", "--", item], cwd=path, check=False
            ).stdout.strip()
            head = self._executor.run(
                ["git", "ls-tree", "HEAD", "--", item], cwd=path, check=False
            ).stdout.strip()
            modes = {
                entry.split(maxsplit=1)[0]
                for entry in (index, head)
                if entry and entry.split(maxsplit=1)
            }
            if modes.intersection({"120000", "160000"}):
                raise SecurityError(f"Symlink or submodule changes are not allowed: {item}")
        return changed

    def untracked_paths(self, path: Path, repo: RepositoryConfig) -> list[str]:
        raw = self._executor.run_bytes(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        ).decode("utf-8", errors="strict")
        return [assert_path_allowed(x, repo) for x in raw.split("\x00") if x]

    def is_tracked_path(self, path: Path, relative_path: str) -> bool:
        result = self._executor.run(
            ["git", "ls-files", "--error-unmatch", "--", relative_path],
            cwd=path,
            check=False,
            output_limit=1_024,
        )
        return result.returncode == 0 and result.stdout.strip() == relative_path

    def fingerprint(self, path: Path) -> str:
        digest = hashlib.sha256()
        digest.update(self.head_sha(path).encode())
        diff = self._executor.run_bytes(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=path,
            timeout=self.server.verification_timeout_seconds,
            max_bytes=self.server.max_fingerprint_bytes,
        )
        digest.update(diff)
        raw = self._executor.run_bytes(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        )
        total = len(diff) + len(raw)
        for name in sorted(x for x in raw.split(b"\x00") if x):
            relative = name.decode("utf-8", errors="strict")
            file_path = path / relative
            digest.update(b"\x00UNTRACKED\x00" + name + b"\x00")
            if file_path.is_symlink():
                data = os.readlink(file_path).encode()
                total += len(data)
                digest.update(data)
            elif file_path.is_file():
                with file_path.open("rb") as h:
                    for chunk in iter(lambda: h.read(1024 * 1024), b""):
                        total += len(chunk)
                        if total > self.server.max_fingerprint_bytes:
                            raise WorkspaceError(
                                "Working-tree fingerprint exceeds configured max_fingerprint_bytes"
                            )
                        digest.update(chunk)
        return digest.hexdigest()

    def change_metrics(self, path: Path, repo: RepositoryConfig) -> dict[str, Any]:
        changed = self.changed_paths(path, repo)
        numstat = self._executor.run(
            ["git", "diff", "--numstat", "HEAD", "--"], cwd=path, check=False
        ).stdout
        added = deleted = binary = 0
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            if "-" in parts[:2]:
                binary += 1
                continue
            try:
                added += int(parts[0])
                deleted += int(parts[1])
            except ValueError:
                pass
        total = sum(
            (path / r).stat().st_size
            for r in changed
            if (path / r).is_file() and (not (path / r).is_symlink())
        )
        files = len(changed)
        lines = added + deleted
        return {
            "changed_files": files,
            "added_lines": added,
            "deleted_lines": deleted,
            "diff_lines": lines,
            "binary_files": binary,
            "total_current_bytes": total,
            "limits": {
                "max_changed_files": repo.max_changed_files,
                "max_diff_lines": repo.max_diff_lines,
                "max_total_changed_bytes": repo.max_total_changed_bytes,
            },
            "within_limits": files <= repo.max_changed_files
            and lines <= repo.max_diff_lines
            and (total <= repo.max_total_changed_bytes),
        }

    def enforce_change_budget(self, path: Path, repo: RepositoryConfig) -> dict[str, Any]:
        m = self.change_metrics(path, repo)
        v = []
        if m["changed_files"] > repo.max_changed_files:
            v.append(f"changed files {m['changed_files']} > {repo.max_changed_files}")
        if m["diff_lines"] > repo.max_diff_lines:
            v.append(f"diff lines {m['diff_lines']} > {repo.max_diff_lines}")
        if m["total_current_bytes"] > repo.max_total_changed_bytes:
            v.append(
                f"changed file bytes {m['total_current_bytes']} > {repo.max_total_changed_bytes}"
            )
        if v:
            raise WorkspaceError(
                "Change budget exceeded: "
                + "; ".join(v)
                + ". Split the task or raise the explicit repository limits in config."
            )
        return m

    def ensure_clean(self, path: Path, *, context: str) -> None:
        if self.status_porcelain(path).strip():
            raise WorkspaceError(f"Working tree must be clean before {context}")

    def ahead_of_base(self, path: Path, remote: str, base: str) -> int:
        return int(
            self._executor.run(
                ["git", "rev-list", "--count", f"{remote}/{base}..HEAD"], cwd=path
            ).stdout.strip()
            or "0"
        )

    def _commit_ref(self, path: Path, ref: str, *, check: bool = True) -> str | None:
        result = self._executor.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            cwd=path,
            check=False,
            output_limit=256,
        )
        if result.returncode != 0:
            if check:
                raise CommandError(f"Git commit reference is unavailable: {ref}")
            return None
        return result.stdout.strip()

    def _is_ancestor(self, path: Path, ancestor_sha: str, descendant_sha: str) -> bool:
        result = self._executor.run(
            ["git", "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
            cwd=path,
            check=False,
            output_limit=256,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise CommandError(result.combined or "Unable to inspect Git ancestry")

    def inspect_base_references(
        self, path: Path, remote: str, base: str, *, fetch_remote: bool
    ) -> GitBaseReferences:
        local_ref = f"refs/heads/{base}"
        remote_ref = f"refs/remotes/{remote}/{base}"
        local_sha = self._commit_ref(path, local_ref)
        assert local_sha is not None
        last_known_remote = self._commit_ref(path, remote_ref, check=False)
        remote_available = True
        remote_error_code: str | None = None
        if fetch_remote:
            result = self._executor.run(
                [
                    "git",
                    "fetch",
                    "--prune",
                    remote,
                    f"+refs/heads/{base}:{remote_ref}",
                ],
                cwd=path,
                check=False,
                timeout=self.server.verification_timeout_seconds,
                output_limit=2048,
            )
            if result.returncode != 0:
                remote_available = False
                remote_error_code = "REMOTE_BASE_UNAVAILABLE"
        remote_sha = (
            self._commit_ref(path, remote_ref, check=False)
            if remote_available
            else last_known_remote
        )
        if remote_sha is None:
            remote_available = False
            remote_error_code = "REMOTE_BASE_UNAVAILABLE"
            relation = "unavailable"
        elif local_sha == remote_sha:
            relation = "equal"
        elif self._is_ancestor(path, local_sha, remote_sha):
            relation = "local_behind_remote"
        elif self._is_ancestor(path, remote_sha, local_sha):
            relation = "local_ahead_remote"
        else:
            relation = "diverged"
        return GitBaseReferences(
            local_sha,
            remote_sha,
            remote_available,
            remote_error_code,
            relation,
        )

    def ahead_behind(self, path: Path, left_sha: str, right_sha: str) -> tuple[int, int]:
        raw = self._executor.run(
            ["git", "rev-list", "--left-right", "--count", f"{left_sha}...{right_sha}"],
            cwd=path,
            output_limit=128,
        ).stdout.split()
        if len(raw) != 2:
            raise CommandError("Git returned invalid ahead/behind counts")
        try:
            return int(raw[0]), int(raw[1])
        except ValueError as exc:
            raise CommandError("Git returned non-numeric ahead/behind counts") from exc

    def merge_base(self, path: Path, left_sha: str, right_sha: str) -> str:
        return self._executor.run(
            ["git", "merge-base", left_sha, right_sha],
            cwd=path,
            output_limit=256,
        ).stdout.strip()

    @staticmethod
    def _bounded_policy_paths(raw_paths: list[str], repo: RepositoryConfig) -> list[str]:
        paths: list[str] = []
        for raw in raw_paths:
            if not raw:
                continue
            try:
                normalized = assert_path_allowed(raw, repo)
            except SecurityError:
                continue
            if normalized not in paths:
                paths.append(normalized)
        return sorted(paths)

    def changed_paths_between(
        self,
        path: Path,
        repo: RepositoryConfig,
        older_sha: str,
        newer_sha: str,
    ) -> list[str]:
        if older_sha == newer_sha:
            return []
        raw = self._executor.run_bytes(
            ["git", "diff", "--name-only", "-z", older_sha, newer_sha, "--"],
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        ).decode("utf-8", errors="strict")
        return self._bounded_policy_paths(raw.split("\x00"), repo)

    def preview_merge(self, path: Path, repo: RepositoryConfig, target_sha: str) -> GitMergePreview:
        head = self.head_sha(path)
        merge_base = self.merge_base(path, head, target_sha)
        if self._is_ancestor(path, target_sha, head):
            return GitMergePreview(target_sha, merge_base, (), True)
        result = self._executor.run(
            [
                "git",
                "merge-tree",
                "--write-tree",
                "--name-only",
                "-z",
                "--no-messages",
                head,
                target_sha,
            ],
            cwd=path,
            check=False,
            output_limit=self.server.max_fingerprint_bytes,
        )
        if result.returncode not in {0, 1}:
            raise CommandError(result.combined or "Unable to compute merge preview")
        fields = result.stdout.split("\x00")
        if not fields or not fields[0]:
            raise CommandError("Git returned an invalid merge preview")
        raw_paths = [field for field in fields[1:] if field] if result.returncode == 1 else []
        conflicts = self._bounded_policy_paths(raw_paths, repo)
        return GitMergePreview(target_sha, merge_base, tuple(conflicts), False)

    def merge_no_ff(self, path: Path, repo: RepositoryConfig, target_sha: str) -> GitMergeResult:
        head = self.head_sha(path)
        if self._is_ancestor(path, target_sha, head):
            return GitMergeResult("current", head, ())
        result = self._executor.run(
            ["git", "merge", "--no-ff", "--no-edit", target_sha],
            cwd=path,
            check=False,
            timeout=self.server.verification_timeout_seconds,
            output_limit=self.server.max_tool_output_chars,
        )
        if result.returncode == 0:
            return GitMergeResult("refreshed", self.head_sha(path), ())
        raw = self._executor.run_bytes(
            ["git", "diff", "--name-only", "--diff-filter=U", "-z", "--"],
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        ).decode("utf-8", errors="strict")
        raw_paths = [item for item in raw.split("\x00") if item]
        merge_in_progress = self._commit_ref(path, "MERGE_HEAD", check=False) is not None
        if not merge_in_progress:
            raise CommandError(result.combined or "Workspace base merge failed")
        conflicts = self._bounded_policy_paths(raw_paths, repo)
        aborted = self._executor.run(
            ["git", "merge", "--abort"],
            cwd=path,
            check=False,
            output_limit=2048,
        )
        if aborted.returncode != 0:
            raise WorkspaceError(
                "Workspace refresh conflict could not be aborted cleanly",
                safe_next_action="Inspect the isolated workspace before any further mutation.",
            )
        if raw_paths and not conflicts:
            raise SecurityError("Workspace refresh conflicts touch only denied repository paths")
        return GitMergeResult("conflict", self.head_sha(path), tuple(conflicts))

    def reset_hard(self, path: Path, target_sha: str) -> None:
        self._executor.run(["git", "reset", "--hard", target_sha], cwd=path, output_limit=2048)

    def list_files(
        self, path: Path, repo: RepositoryConfig, max_entries: int
    ) -> tuple[list[str], bool]:
        raw = self._executor.run_bytes(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        ).decode("utf-8", errors="strict")
        entries = []
        for value in raw.split("\x00"):
            if not value:
                continue
            try:
                allowed = assert_path_allowed(value, repo)
            except SecurityError:
                continue
            if allowed not in entries:
                entries.append(allowed)
            if len(entries) >= max_entries:
                break
        return (entries, len(entries) >= max_entries)

    def root_files(self, path: Path, repo: RepositoryConfig) -> list[str]:
        raw = self._executor.run_bytes(
            ["git", "ls-files", "-z", "--", "*"],
            cwd=path,
            max_bytes=min(self.server.max_fingerprint_bytes, 2000000),
        )
        out = []
        for item in raw.split(b"\x00"):
            if item:
                name = item.decode("utf-8", errors="strict")
                if "/" not in name:
                    with contextlib.suppress(SecurityError):
                        out.append(assert_path_allowed(name, repo))
        return sorted(out)

    def recent_commits(self, path: Path, limit: int) -> list[dict[str, str]]:
        output = self._executor.run(
            [
                "git",
                "log",
                f"-{limit}",
                "--date=iso-strict",
                "--pretty=format:%H%x09%ad%x09%an%x09%s",
            ],
            cwd=path,
        ).stdout
        result = []
        for line in output.splitlines():
            values = [*line.split("\t", 3), "", "", "", ""][:4]
            result.append(dict(zip(("sha", "date", "author", "subject"), values, strict=False)))
        return result

    @staticmethod
    def _raise_ref_error(message: str, code: ErrorCode) -> NoReturn:
        raise RepoForgeError(
            message,
            code=code,
            unchanged_state=("The source clone and repository references were not modified.",),
        )

    @staticmethod
    def _tag_name(requested: str) -> str | None:
        name = requested.removeprefix("refs/tags/")
        if requested.startswith("refs/") and not requested.startswith("refs/tags/"):
            return None
        if (
            not name
            or len(name) > 200
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", name) is None
            or ".." in name
            or "@{" in name
            or name.endswith((".", "/", ".lock"))
            or any(part in {"", ".", ".."} or part.startswith(".") for part in name.split("/"))
        ):
            return None
        return name

    def _commit_is_reviewed(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
    ) -> bool:
        allowed_branches = tuple(dict.fromkeys((repo.default_base, *repo.allowed_base_branches)))
        for branch in allowed_branches:
            branch_ref = f"refs/heads/{branch}"
            exists = self._executor.run(
                ["git", "rev-parse", "--verify", "--end-of-options", f"{branch_ref}^{{commit}}"],
                cwd=path,
                check=False,
                output_limit=512,
            )
            if exists.returncode != 0:
                continue
            ancestry = self._executor.run(
                ["git", "merge-base", "--is-ancestor", commit_sha, branch_ref],
                cwd=path,
                check=False,
                output_limit=512,
            )
            if ancestry.returncode == 0:
                return True
            if ancestry.returncode != 1:
                raise CommandError(
                    ancestry.combined or "Unable to validate repository ref ancestry"
                )
        return False

    def resolve_snapshot_ref(
        self, path: Path, repo: RepositoryConfig, ref: str | None
    ) -> ResolvedRepositoryRef:
        allowed_branches = tuple(dict.fromkeys((repo.default_base, *repo.allowed_base_branches)))
        requested = ref or repo.default_base
        if (
            not requested
            or len(requested) > 256
            or any(ord(character) < 32 for character in requested)
        ):
            self._raise_ref_error(
                "Repository ref is empty, too long, or contains control characters",
                ErrorCode.REPOSITORY_REF_DISALLOWED,
            )

        if requested in allowed_branches:
            resolved_ref = f"refs/heads/{requested}"
            result = self._executor.run(
                ["git", "rev-parse", "--verify", "--end-of-options", f"{resolved_ref}^{{commit}}"],
                cwd=path,
                check=False,
                output_limit=512,
            )
            if result.returncode != 0:
                self._raise_ref_error(
                    f"Repository ref not found: {requested}",
                    ErrorCode.REPOSITORY_REF_NOT_FOUND,
                )
            return ResolvedRepositoryRef(resolved_ref, result.stdout.strip())

        tag_name = self._tag_name(requested)
        if tag_name is not None:
            tag_ref = f"refs/tags/{tag_name}"
            tag = self._executor.run(
                ["git", "rev-parse", "--verify", "--end-of-options", f"{tag_ref}^{{commit}}"],
                cwd=path,
                check=False,
                output_limit=512,
            )
            if tag.returncode == 0:
                commit_sha = tag.stdout.strip()
                if not self._commit_is_reviewed(path, repo, commit_sha):
                    self._raise_ref_error(
                        f"Repository ref is outside reviewed base history: {requested}",
                        ErrorCode.REPOSITORY_REF_EXTERNAL,
                    )
                return ResolvedRepositoryRef(tag_ref, commit_sha)
            if requested.startswith("refs/tags/"):
                self._raise_ref_error(
                    f"Repository ref not found: {requested}",
                    ErrorCode.REPOSITORY_REF_NOT_FOUND,
                )

        full_object_id = re.fullmatch(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?", requested)
        if full_object_id is not None:
            result = self._executor.run(
                ["git", "rev-parse", "--verify", "--end-of-options", f"{requested}^{{commit}}"],
                cwd=path,
                check=False,
                output_limit=512,
            )
            if result.returncode != 0:
                self._raise_ref_error(
                    f"Repository ref not found: {requested}",
                    ErrorCode.REPOSITORY_REF_NOT_FOUND,
                )
            commit_sha = result.stdout.strip()
            if not self._commit_is_reviewed(path, repo, commit_sha):
                self._raise_ref_error(
                    f"Repository ref is outside reviewed base history: {requested}",
                    ErrorCode.REPOSITORY_REF_EXTERNAL,
                )
            return ResolvedRepositoryRef(commit_sha, commit_sha)

        if re.fullmatch(r"[0-9a-fA-F]{4,63}", requested) is not None:
            self._raise_ref_error(
                f"Abbreviated repository ref is ambiguous: {requested}",
                ErrorCode.REPOSITORY_REF_AMBIGUOUS,
            )
        if requested.startswith(("refs/remotes/", f"{repo.remote}/")):
            self._raise_ref_error(
                f"External repository ref is not allowed: {requested}",
                ErrorCode.REPOSITORY_REF_EXTERNAL,
            )
        self._raise_ref_error(
            f"Repository ref form is not allowed: {requested}",
            ErrorCode.REPOSITORY_REF_DISALLOWED,
        )

    @staticmethod
    def _parse_tree_entry(raw: bytes) -> tuple[str, str, str, str]:
        try:
            metadata, encoded_path = raw.split(b"\t", 1)
            mode, object_type, object_sha = metadata.decode("ascii").split(" ", 2)
            relative_path = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SecurityError("Committed snapshot contains an invalid Git tree entry") from exc
        return mode, object_type, object_sha, relative_path

    def list_snapshot_files(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
        max_entries: int,
    ) -> tuple[list[str], bool]:
        raw = self._executor.run_bytes(
            ["git", "ls-tree", "-r", "-z", "--full-tree", commit_sha],
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        )
        entries: list[str] = []
        for item in raw.split(b"\x00"):
            if not item:
                continue
            mode, object_type, _, relative_path = self._parse_tree_entry(item)
            if mode in {"120000", "160000"} or object_type != "blob":
                continue
            try:
                normalized = assert_path_allowed(relative_path, repo)
            except SecurityError:
                continue
            entries.append(normalized)
        entries.sort()
        return entries[:max_entries], len(entries) > max_entries

    def read_snapshot_blob(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
        relative_path: str,
    ) -> GitSnapshotBlob:
        normalized = assert_path_allowed(relative_path, repo)
        raw = self._executor.run_bytes(
            ["git", "ls-tree", "-z", commit_sha, "--", f":(literal){normalized}"],
            cwd=path,
            max_bytes=min(self.server.max_fingerprint_bytes, 1_000_000),
        )
        entries = [item for item in raw.split(b"\x00") if item]
        if not entries:
            raise RepoForgeError(
                f"File not found in committed snapshot: {normalized}",
                code=ErrorCode.NOT_FOUND,
            )
        mode, object_type, object_sha, returned_path = self._parse_tree_entry(entries[0])
        if returned_path != normalized:
            raise SecurityError("Git returned a different path than the requested literal path")
        if mode == "120000":
            raise SecurityError(f"Reading symlink entries is not allowed: {normalized}")
        if mode == "160000":
            raise SecurityError(f"Reading gitlink entries is not allowed: {normalized}")
        if object_type != "blob" or not mode.startswith("100"):
            raise SecurityError(f"Unsupported committed object type for path: {normalized}")
        size_result = self._executor.run(
            ["git", "cat-file", "-s", object_sha],
            cwd=path,
            output_limit=128,
        )
        try:
            size_bytes = int(size_result.stdout.strip())
        except ValueError as exc:
            raise CommandError("Git returned an invalid committed blob size") from exc
        if size_bytes > self.server.max_file_bytes:
            raise SecurityError(
                f"File size {size_bytes} exceeds max_file_bytes={self.server.max_file_bytes}"
            )
        data = self._executor.run_bytes(
            ["git", "cat-file", "blob", object_sha],
            cwd=path,
            max_bytes=self.server.max_file_bytes,
        )
        return GitSnapshotBlob(normalized, object_sha, mode, size_bytes, data)

    def search_snapshot(
        self,
        path: Path,
        repo: RepositoryConfig,
        commit_sha: str,
        query: str,
        path_glob: str | None,
        max_results: int,
    ) -> tuple[list[str], bool]:
        argv = [
            "git",
            "grep",
            "-n",
            "-I",
            "-F",
            "--full-name",
            "-e",
            query,
            commit_sha,
            "--",
        ]
        if path_glob:
            argv.append(f":(glob){path_glob}")
        result = self._executor.run(
            argv,
            cwd=path,
            check=False,
            output_limit=self.server.max_fingerprint_bytes,
        )
        if result.returncode == 1:
            return [], False
        if result.returncode != 0:
            raise CommandError(result.combined or "Committed snapshot search failed")
        executor_truncated = "characters omitted" in result.stdout
        pattern = re.compile(rf"^{re.escape(commit_sha)}:(.*?):(\d+):(.*)$")
        matches: list[str] = []
        for line in result.stdout.splitlines():
            parsed = pattern.match(line)
            if parsed is None:
                if executor_truncated:
                    continue
                raise CommandError("Git returned an invalid committed snapshot search result")
            raw_path, line_number, content = parsed.groups()
            try:
                normalized = assert_path_allowed(raw_path, repo)
            except SecurityError:
                continue
            matches.append(f"{normalized}:{line_number}:{content}")
        matches.sort()
        return matches[:max_results], executor_truncated or len(matches) > max_results

    @staticmethod
    def _raise_evidence_error(message: str, code: ErrorCode) -> NoReturn:
        raise RepoForgeError(
            message,
            code=code,
            unchanged_state=(
                "The source clone, working tree, and Git references were not modified.",
            ),
        )

    @classmethod
    def _decode_git_path(cls, raw: bytes) -> str:
        try:
            value = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            cls._raise_evidence_error(
                "Git returned a non-UTF-8 committed path",
                ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
            )
            raise AssertionError("unreachable") from exc
        if not value:
            cls._raise_evidence_error(
                "Git returned an empty committed path",
                ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
            )
        return value

    @classmethod
    def _parse_raw_diff(cls, raw: bytes) -> list[_RawDiffEntry]:
        tokens = raw.split(b"\x00")
        entries: list[_RawDiffEntry] = []
        index = 0
        while index < len(tokens):
            header = tokens[index]
            index += 1
            if not header:
                continue
            try:
                fields = header.decode("ascii", errors="strict").split()
            except UnicodeDecodeError as exc:
                cls._raise_evidence_error(
                    "Git returned a malformed raw diff header",
                    ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                )
                raise AssertionError("unreachable") from exc
            if len(fields) != 5 or not fields[0].startswith(":"):
                cls._raise_evidence_error(
                    "Git returned a malformed raw diff record",
                    ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                )
            if index >= len(tokens):
                cls._raise_evidence_error(
                    "Git raw diff omitted a path",
                    ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                )
            old_mode = fields[0][1:]
            new_mode = fields[1]
            status_code = fields[4]
            first_path = cls._decode_git_path(tokens[index])
            index += 1
            previous_path: str | None = None
            current_path = first_path
            if status_code[:1] in {"R", "C"}:
                if index >= len(tokens):
                    cls._raise_evidence_error(
                        "Git rename/copy record omitted its destination path",
                        ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                    )
                previous_path = first_path
                current_path = cls._decode_git_path(tokens[index])
                index += 1
            entries.append(
                _RawDiffEntry(status_code, old_mode, new_mode, current_path, previous_path)
            )
        return entries

    @classmethod
    def _parse_numstat(
        cls,
        raw: bytes,
    ) -> dict[tuple[str | None, str], tuple[int | None, int | None, bool]]:
        tokens = raw.split(b"\x00")
        stats: dict[tuple[str | None, str], tuple[int | None, int | None, bool]] = {}
        index = 0
        while index < len(tokens):
            header = tokens[index]
            index += 1
            if not header:
                continue
            parts = header.split(b"\t", 2)
            if len(parts) != 3:
                cls._raise_evidence_error(
                    "Git returned a malformed numstat record",
                    ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                )
            added_raw, deleted_raw, path_raw = parts
            previous_path: str | None = None
            if path_raw:
                current_path = cls._decode_git_path(path_raw)
            else:
                if index + 1 >= len(tokens):
                    cls._raise_evidence_error(
                        "Git rename/copy numstat omitted paths",
                        ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                    )
                previous_path = cls._decode_git_path(tokens[index])
                current_path = cls._decode_git_path(tokens[index + 1])
                index += 2
            binary = added_raw == b"-" or deleted_raw == b"-"
            try:
                additions = None if binary else int(added_raw)
                deletions = None if binary else int(deleted_raw)
            except ValueError as exc:
                cls._raise_evidence_error(
                    "Git returned non-numeric file statistics",
                    ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                )
                raise AssertionError("unreachable") from exc
            stats[(previous_path, current_path)] = (additions, deletions, binary)
        return stats

    @classmethod
    def _status_name(cls, status_code: str) -> str:
        names = {
            "A": "added",
            "M": "modified",
            "D": "deleted",
            "R": "renamed",
            "C": "copied",
            "T": "type_changed",
            "U": "unmerged",
        }
        name = names.get(status_code[:1])
        if name is None:
            cls._raise_evidence_error(
                f"Git returned an unsupported changed-file status: {status_code}",
                ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
            )
        return name

    def _diff_bytes(
        self,
        path: Path,
        base_sha: str | None,
        head_sha: str,
        output_kind: str,
    ) -> bytes:
        if output_kind not in {"--raw", "--numstat"}:
            raise ValueError("Unsupported evidence diff output kind")
        if base_sha is None:
            argv = [
                "git",
                "diff-tree",
                "--root",
                "--no-commit-id",
                "-r",
                "-z",
                output_kind,
                "-M",
                "-C",
                head_sha,
                "--",
            ]
        else:
            argv = [
                "git",
                "diff",
                output_kind,
                "-z",
                "-M",
                "-C",
                base_sha,
                head_sha,
                "--",
            ]
        return self._executor.run_bytes(
            argv,
            cwd=path,
            max_bytes=self.server.max_fingerprint_bytes,
        )

    @staticmethod
    def _glob_matches(path: str, pattern: str) -> bool:
        return fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)

    def _patch_for_files(
        self,
        path: Path,
        base_sha: str | None,
        head_sha: str,
        files: tuple[GitChangedFileEvidence, ...],
    ) -> tuple[str, bool]:
        paths = sorted(
            {
                candidate
                for item in files
                if not item.binary
                for candidate in (item.previous_path, item.path)
                if candidate is not None
            }
        )
        if not paths:
            return "", False
        if base_sha is None:
            argv = [
                "git",
                "diff-tree",
                "--root",
                "--no-commit-id",
                "-r",
                "--patch",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "-M",
                "-C",
                head_sha,
                "--",
            ]
        else:
            argv = [
                "git",
                "diff",
                "--patch",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "-M",
                "-C",
                base_sha,
                head_sha,
                "--",
            ]
        argv.extend(f":(literal){candidate}" for candidate in paths)
        result = self._executor.run(
            argv,
            cwd=path,
            output_limit=self.server.max_tool_output_chars,
        )
        return result.stdout, result.stdout_truncated

    def _collect_evidence(
        self,
        path: Path,
        repo: RepositoryConfig,
        base_sha: str | None,
        head_sha: str,
        path_glob: str | None,
        max_files: int,
        include_patch: bool,
    ) -> _CollectedEvidence:
        raw_entries = self._parse_raw_diff(self._diff_bytes(path, base_sha, head_sha, "--raw"))
        numstats = self._parse_numstat(self._diff_bytes(path, base_sha, head_sha, "--numstat"))
        visible: list[GitChangedFileEvidence] = []
        omitted_paths = 0
        for entry in raw_entries:
            if {entry.old_mode, entry.new_mode}.intersection({"120000", "160000"}):
                omitted_paths += 1
                continue
            try:
                current = assert_path_allowed(entry.path, repo)
                previous = (
                    assert_path_allowed(entry.previous_path, repo)
                    if entry.previous_path is not None
                    else None
                )
            except SecurityError:
                omitted_paths += 1
                continue
            if path_glob and not (
                self._glob_matches(current, path_glob)
                or (previous is not None and self._glob_matches(previous, path_glob))
            ):
                continue
            stat = numstats.get((entry.previous_path, entry.path))
            if stat is None:
                self._raise_evidence_error(
                    f"Git statistics did not match changed path: {current}",
                    ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
                )
            additions, deletions, binary = stat
            visible.append(
                GitChangedFileEvidence(
                    self._status_name(entry.status_code),
                    current,
                    previous,
                    additions,
                    deletions,
                    binary,
                )
            )
        visible.sort(key=lambda item: (item.path, item.previous_path or ""))
        returned = tuple(visible[:max_files])
        patch: str | None = None
        patch_truncated = False
        if include_patch:
            patch, patch_truncated = self._patch_for_files(
                path,
                base_sha,
                head_sha,
                returned,
            )
        return _CollectedEvidence(
            returned,
            len(visible),
            len(visible) > max_files,
            sum(item.additions or 0 for item in visible),
            sum(item.deletions or 0 for item in visible),
            sum(1 for item in visible if item.binary),
            omitted_paths,
            patch,
            patch_truncated,
            any(item.binary for item in visible),
        )

    def _commit_metadata(
        self,
        path: Path,
        commit_sha: str,
    ) -> tuple[
        str,
        tuple[str, ...],
        GitActorIdentity,
        GitActorIdentity,
        str,
        str,
        bool,
    ]:
        fixed = self._executor.run(
            [
                "git",
                "show",
                "-s",
                "--no-show-signature",
                "--format=%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
                commit_sha,
            ],
            cwd=path,
            output_limit=32_000,
        )
        if fixed.stdout_truncated:
            self._raise_evidence_error(
                "Git commit identity exceeded its bounded parser contract",
                ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
            )
        parts = fixed.stdout.rstrip("\n").split("\x00")
        if len(parts) != 8:
            self._raise_evidence_error(
                "Git returned malformed commit identity metadata",
                ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
            )
        (
            tree_sha,
            parents_raw,
            author_name,
            author_email,
            author_date,
            committer_name,
            committer_email,
            committer_date,
        ) = parts
        subject_result = self._executor.run(
            ["git", "show", "-s", "--no-show-signature", "--format=%s", commit_sha],
            cwd=path,
            output_limit=min(4_000, self.server.max_tool_output_chars),
        )
        body_result = self._executor.run(
            ["git", "show", "-s", "--no-show-signature", "--format=%b", commit_sha],
            cwd=path,
            output_limit=max(1_000, min(20_000, self.server.max_tool_output_chars)),
        )
        return (
            tree_sha,
            tuple(parent for parent in parents_raw.split() if parent),
            GitActorIdentity(author_name, author_email, author_date),
            GitActorIdentity(committer_name, committer_email, committer_date),
            subject_result.stdout.rstrip("\n"),
            body_result.stdout.rstrip("\n"),
            subject_result.stdout_truncated or body_result.stdout_truncated,
        )

    def read_commit_evidence(
        self,
        path: Path,
        repo: RepositoryConfig,
        snapshot: ResolvedRepositoryRef,
        max_files: int,
        include_patch: bool,
    ) -> GitCommitEvidence:
        (
            tree_sha,
            parent_shas,
            author,
            committer,
            subject,
            body,
            message_truncated,
        ) = self._commit_metadata(path, snapshot.commit_sha)
        comparison_parent = parent_shas[0] if parent_shas else None
        collected = self._collect_evidence(
            path,
            repo,
            comparison_parent,
            snapshot.commit_sha,
            None,
            max_files,
            include_patch,
        )
        return GitCommitEvidence(
            tree_sha,
            parent_shas,
            comparison_parent,
            author,
            committer,
            subject,
            body,
            message_truncated,
            collected.files,
            collected.total_files,
            collected.files_truncated,
            collected.additions,
            collected.deletions,
            collected.binary_files,
            collected.omitted_paths,
            collected.patch,
            collected.patch_truncated,
            collected.binary_patch_omitted,
        )

    def compare_commits(
        self,
        path: Path,
        repo: RepositoryConfig,
        base: ResolvedRepositoryRef,
        head: ResolvedRepositoryRef,
        path_glob: str | None,
        max_files: int,
        include_patch: bool,
    ) -> GitComparisonEvidence:
        merge_base = self._executor.run(
            ["git", "merge-base", base.commit_sha, head.commit_sha],
            cwd=path,
            check=False,
            output_limit=512,
        )
        if merge_base.returncode == 1:
            shallow = self._executor.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=path,
                check=False,
                output_limit=64,
            )
            code = (
                ErrorCode.REPOSITORY_HISTORY_INCOMPLETE
                if shallow.returncode == 0 and shallow.stdout.strip() == "true"
                else ErrorCode.REPOSITORY_HISTORIES_UNRELATED
            )
            self._raise_evidence_error(
                "Reviewed commits do not have an available merge base",
                code,
            )
        if merge_base.returncode != 0:
            raise CommandError(merge_base.combined or "Unable to calculate merge base")
        counts = self._executor.run(
            [
                "git",
                "rev-list",
                "--left-right",
                "--count",
                f"{base.commit_sha}...{head.commit_sha}",
            ],
            cwd=path,
            output_limit=128,
        )
        try:
            behind_text, ahead_text = counts.stdout.split()
            behind = int(behind_text)
            ahead = int(ahead_text)
        except (ValueError, TypeError) as exc:
            self._raise_evidence_error(
                "Git returned malformed ahead/behind counts",
                ErrorCode.REPOSITORY_EVIDENCE_PARSE_FAILED,
            )
            raise AssertionError("unreachable") from exc
        collected = self._collect_evidence(
            path,
            repo,
            base.commit_sha,
            head.commit_sha,
            path_glob,
            max_files,
            include_patch,
        )
        return GitComparisonEvidence(
            merge_base.stdout.strip(),
            ahead,
            behind,
            collected.files,
            collected.total_files,
            collected.files_truncated,
            collected.additions,
            collected.deletions,
            collected.binary_files,
            collected.omitted_paths,
            collected.patch,
            collected.patch_truncated,
            collected.binary_patch_omitted,
        )

    def search(
        self,
        path: Path,
        repo: RepositoryConfig,
        query: str,
        path_glob: str | None,
        max_results: int,
    ) -> tuple[list[str], bool]:
        argv = ["git", "grep", "--untracked", "-n", "-I", "-F", "-e", query, "--"]
        if path_glob:
            argv.append(path_glob)
        r = self._executor.run(argv, cwd=path, check=False)
        if r.returncode not in (0, 1):
            raise CommandError(r.combined)
        matches = []
        for line in r.stdout.splitlines():
            try:
                assert_path_allowed(line.split(":", 1)[0], repo)
            except SecurityError:
                continue
            matches.append(line)
            if len(matches) >= max_results:
                break
        return (matches, len(matches) >= max_results)

    @staticmethod
    def _bound(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return (text, False)
        half = max(1, limit // 2)
        return (
            f"{text[:half]}\n\n... <{len(text) - 2 * half} characters omitted> ...\n\n{text[-half:]}",
            True,
        )

    def diff(self, path: Path, repo: RepositoryConfig, *, staged: bool) -> dict[str, Any]:
        changed = self.changed_paths(path, repo)
        stat_args = ["git", "diff", "--no-ext-diff", "--stat"]
        diff_args = ["git", "diff", "--no-ext-diff"]
        if staged:
            stat_args.append("--cached")
            diff_args.append("--cached")
        stat = self._executor.run([*stat_args, "--"], cwd=path).stdout
        parts = [self._executor.run([*diff_args, "--"], cwd=path).stdout]
        untracked = []
        if not staged:
            untracked = self.untracked_paths(path, repo)
            for rel in untracked:
                fp = resolve_workspace_path(path, rel, repo)
                if not fp.is_file() or fp.is_symlink():
                    continue
                if fp.stat().st_size > self.server.max_file_bytes:
                    parts.append(f"\nUntracked file omitted because it is too large: {rel}\n")
                    continue
                if b"\x00" in fp.read_bytes():
                    parts.append(f"\nBinary untracked file omitted: {rel}\n")
                    continue
                r = self._executor.run(
                    ["git", "diff", "--no-index", "--", "/dev/null", rel],
                    cwd=path,
                    check=False,
                )
                if r.returncode not in (0, 1):
                    raise CommandError(r.combined)
                parts.append(r.stdout)
        text, truncated = self._bound(
            "\n".join(x for x in parts if x), self.server.max_tool_output_chars
        )
        return {
            "changed_paths": changed,
            "change_metrics": self.change_metrics(path, repo),
            "untracked_paths": untracked,
            "stat": stat,
            "diff": text,
            "truncated": truncated,
        }

    def run_profile(
        self, path: Path, profile: ProfileConfig
    ) -> tuple[list[CommandResult], str, dict[str, Any]]:
        timeout = profile.timeout_seconds or self.server.verification_timeout_seconds
        results = [self._executor.run(c, cwd=path, timeout=timeout) for c in profile.commands]
        return (results, self.fingerprint(path), {})

    def restore_paths(
        self, path: Path, repo: RepositoryConfig, relative_paths: list[str]
    ) -> tuple[list[str], list[str]]:
        restored = []
        removed = []
        for rel in relative_paths:
            candidate = resolve_workspace_path(path, rel, repo)
            tracked = (
                self._executor.run(
                    ["git", "ls-files", "--error-unmatch", "--", rel],
                    cwd=path,
                    check=False,
                ).returncode
                == 0
            )
            if tracked:
                self._executor.run(
                    [
                        "git",
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        rel,
                    ],
                    cwd=path,
                )
                restored.append(rel)
            elif candidate.exists():
                if candidate.is_symlink() or not candidate.is_file():
                    raise SecurityError(f"Only untracked regular files can be removed: {rel}")
                candidate.unlink()
                removed.append(rel)
        return (restored, removed)

    def create_worktree(
        self, repo: RepositoryConfig, destination: Path, branch: str, base: str
    ) -> str:
        if repo.fetch_before_workspace:
            self._executor.run(["git", "fetch", "--prune", repo.remote, base], cwd=repo.path)
        base_ref = base if (repo.read_only or not repo.publish_enabled) else f"{repo.remote}/{base}"
        self._executor.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch,
                str(destination),
                base_ref,
            ],
            cwd=repo.path,
            timeout=self.server.verification_timeout_seconds,
        )
        return self.head_sha(destination)

    def remove_worktree(
        self, repo: RepositoryConfig, path: Path, branch: str, delete_branch: bool
    ) -> bool:
        self._executor.run(
            ["git", "worktree", "remove", str(path)],
            cwd=repo.path,
            timeout=self.server.verification_timeout_seconds,
        )
        if delete_branch:
            self._executor.run(["git", "branch", "-D", branch], cwd=repo.path)
        return delete_branch

    def commit(self, path: Path, message: str) -> tuple[str, str]:
        self._executor.run(["git", "add", "--all", "--"], cwd=path)
        if not self._executor.run(
            ["git", "diff", "--cached", "--name-only", "--"], cwd=path
        ).stdout.strip():
            raise WorkspaceError("No staged changes remain after git add")
        self._executor.run(["git", "commit", "-m", message], cwd=path)
        head = self.head_sha(path)
        show = self._executor.run(
            ["git", "show", "-1", "--stat", "--oneline", "--decorate"], cwd=path
        ).stdout
        return (head, show)

    def commit_summary(self, path: Path) -> str:
        return self._executor.run(
            ["git", "show", "-1", "--stat", "--oneline", "--decorate"], cwd=path
        ).stdout

    def push(self, path: Path, remote: str, branch: str, timeout: int) -> CommandResult:
        return self._executor.run(
            ["git", "push", "--set-upstream", remote, f"HEAD:refs/heads/{branch}"],
            cwd=path,
            timeout=timeout,
        )

    def upstream_name(self, path: Path) -> str | None:
        r = self._executor.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=path,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else None

    def upstream_sha(self, path: Path) -> str:
        return self._executor.run(["git", "rev-parse", "@{u}"], cwd=path).stdout.strip()

    def apply_patch(self, path: Path, patch: str) -> None:
        self._executor.run(
            ["git", "apply", "--check", "--whitespace=error-all", "-"],
            cwd=path,
            input_text=patch,
        )
        self._executor.run(["git", "apply", "--whitespace=fix", "-"], cwd=path, input_text=patch)

    def reverse_patch(self, path: Path, patch: str) -> None:
        self._executor.run(
            ["git", "apply", "-R", "--whitespace=nowarn", "-"],
            cwd=path,
            input_text=patch,
            check=False,
        )

    def remote_url(self, path: Path, remote: str) -> CommandResult:
        return self._executor.run(["git", "remote", "get-url", remote], cwd=path, check=False)

    def verify_base(self, path: Path, remote: str, base: str) -> CommandResult:
        return self._executor.run(
            ["git", "rev-parse", "--verify", f"refs/remotes/{remote}/{base}"],
            cwd=path,
            check=False,
        )
