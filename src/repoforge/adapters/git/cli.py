"""Semantic Git adapter; application code never builds Git argv."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from pathlib import Path
from typing import Any, NoReturn

from ...config import ProfileConfig, RepositoryConfig, ServerConfig
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError, WorkspaceError
from ...domain.policy import assert_path_allowed, resolve_workspace_path
from ...ports.command import CommandExecutor, CommandResult
from ...ports.git import GitSnapshotBlob, ResolvedRepositoryRef


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

    def resolve_snapshot_ref(
        self, path: Path, repo: RepositoryConfig, ref: str | None
    ) -> ResolvedRepositoryRef:
        allowed_branches = tuple(dict.fromkeys((repo.default_base, *repo.allowed_base_branches)))
        requested = ref or repo.default_base
        if not requested or any(ord(character) < 32 for character in requested):
            self._raise_ref_error(
                "Repository ref is empty or contains control characters",
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
            reachable = False
            for branch in allowed_branches:
                branch_ref = f"refs/heads/{branch}"
                exists = self._executor.run(
                    [
                        "git",
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        f"{branch_ref}^{{commit}}",
                    ],
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
                    reachable = True
                    break
                if ancestry.returncode != 1:
                    raise CommandError(
                        ancestry.combined or "Unable to validate repository ref ancestry"
                    )
            if not reachable:
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
