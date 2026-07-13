"""Core repository and isolated-worktree operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

from .audit import AuditLogger
from .config import AppConfig, ProfileConfig, RepositoryConfig
from .errors import CommandError, ConfigError, SecurityError, WorkspaceError
from .ports import AuditSink, CommandExecutor, WorkspaceStore
from .runner import CommandResult, CommandRunner
from .security import (
    assert_path_allowed,
    resolve_workspace_path,
    validate_branch,
    validate_patch,
)
from .state import StateStore, VerificationReceipt, WorkspaceRecord, utc_now
from .workspace_create import WorkspaceCreateCommand, WorkspaceCreator, WorkspaceCreatorPorts
from .workspace_file_read import (
    WorkspaceFileReadCommand,
    WorkspaceFileReader,
    WorkspaceFileReadPorts,
)
from .workspace_file_write import (
    WorkspaceFileWriteCommand,
    WorkspaceFileWritePorts,
    WorkspaceFileWriter,
)

T = TypeVar("T")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_GIT_OID_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")


class CodingService:
    def __init__(
        self,
        config: AppConfig,
        *,
        runner: CommandExecutor | None = None,
        state: WorkspaceStore | None = None,
        audit: AuditSink | None = None,
    ):
        self.config = config
        self.config.server.workspace_root.mkdir(parents=True, exist_ok=True)
        self.config.server.state_root.mkdir(parents=True, exist_ok=True)
        self.runner: CommandExecutor = runner or CommandRunner(config.server)
        self.state: WorkspaceStore = state or StateStore(config.server.state_root)
        self.audit: AuditSink = audit or AuditLogger(config.server.state_root)

    def _audit_call(self, action: str, details: dict[str, Any], operation: Callable[[], T]) -> T:
        try:
            result = operation()
        except Exception as exc:
            # Do not persist command output or file content in the audit log. The caller still
            # receives the complete exception, while the local audit trail records only its type.
            self.audit.record(
                action,
                success=False,
                details={**details, "error_type": type(exc).__name__},
            )
            raise
        self.audit.record(action, success=True, details=details)
        return result

    def _repo(self, repo_id: str) -> RepositoryConfig:
        try:
            repo = self.config.repositories[repo_id]
        except KeyError as exc:
            raise ConfigError(f"Unknown repository id: {repo_id}") from exc
        # Worktree/common-dir layouts can use a .git file, so only existence is required.
        if not repo.path.is_dir() or not (repo.path / ".git").exists():
            raise ConfigError(f"Configured path is not a Git working tree: {repo.path}")
        return repo

    def _workspace(self, workspace_id: str) -> tuple[WorkspaceRecord, RepositoryConfig, Path]:
        record = self.state.load(workspace_id)
        repo = self._repo(record.repo_id)
        path = Path(record.path).resolve()
        root = self.config.server.workspace_root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError(f"Workspace path is outside workspace_root: {path}") from exc
        if not path.is_dir() or not (path / ".git").exists():
            raise WorkspaceError(f"Workspace is missing or invalid: {path}")
        branch = self._current_branch(path)
        if branch != record.branch:
            raise WorkspaceError(
                f"Workspace branch changed unexpectedly: registry={record.branch}, actual={branch}"
            )
        validate_branch(branch, repo)
        return record, repo, path

    def _current_branch(self, path: Path) -> str:
        return self.runner.run(["git", "branch", "--show-current"], cwd=path).stdout.strip()

    def _head_sha(self, path: Path) -> str:
        return self.runner.run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()

    def _status_porcelain(self, path: Path) -> str:
        return self.runner.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=path
        ).stdout

    def _assert_changed_paths_allowed(self, path: Path, repo: RepositoryConfig) -> list[str]:
        commands = (
            ["git", "diff", "--name-only", "-z", "--"],
            ["git", "diff", "--cached", "--name-only", "-z", "--"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        )
        changed: list[str] = []
        for command in commands:
            raw_bytes = self.runner.run_bytes(
                command,
                cwd=path,
                max_bytes=self.config.server.max_fingerprint_bytes,
            )
            raw = raw_bytes.decode("utf-8", errors="strict")
            for item in raw.split("\x00"):
                if item and item not in changed:
                    changed.append(item)
        for item in changed:
            assert_path_allowed(item, repo)
            candidate = path / item
            if candidate.is_symlink():
                raise SecurityError(f"Changed symlinks are not allowed: {item}")
            index_entry = self.runner.run(
                ["git", "ls-files", "-s", "--", item], cwd=path, check=False
            ).stdout.strip()
            head_entry = self.runner.run(
                ["git", "ls-tree", "HEAD", "--", item], cwd=path, check=False
            ).stdout.strip()
            modes = {
                entry.split(maxsplit=1)[0]
                for entry in (index_entry, head_entry)
                if entry and entry.split(maxsplit=1)
            }
            if modes.intersection({"120000", "160000"}):
                raise SecurityError(f"Symlink or submodule changes are not allowed: {item}")
        return changed

    def _untracked_paths(self, path: Path, repo: RepositoryConfig) -> list[str]:
        raw_bytes = self.runner.run_bytes(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=path,
            max_bytes=self.config.server.max_fingerprint_bytes,
        )
        raw = raw_bytes.decode("utf-8", errors="strict")
        paths: list[str] = []
        for item in raw.split("\x00"):
            if not item:
                continue
            paths.append(assert_path_allowed(item, repo))
        return paths

    def _bounded_text(self, text: str) -> tuple[str, bool]:
        limit = self.config.server.max_tool_output_chars
        if len(text) <= limit:
            return text, False
        half = max(1, limit // 2)
        omitted = len(text) - (half * 2)
        bounded = f"{text[:half]}\n\n... <{omitted} characters omitted> ...\n\n{text[-half:]}"
        return bounded, True

    def _ensure_clean(self, path: Path, *, context: str) -> None:
        if self._status_porcelain(path).strip():
            raise WorkspaceError(f"Working tree must be clean before {context}")

    def _remote_slug(self, repo_path: Path) -> str:
        result = self.runner.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=repo_path,
        )
        slug = result.stdout.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise CommandError(f"Unexpected GitHub repository name: {slug!r}")
        return slug

    def _fingerprint(self, path: Path) -> str:
        digest = hashlib.sha256()
        digest.update(self._head_sha(path).encode())
        diff = self.runner.run_bytes(
            ["git", "diff", "--binary", "HEAD", "--"],
            cwd=path,
            timeout=self.config.server.verification_timeout_seconds,
            max_bytes=self.config.server.max_fingerprint_bytes,
        )
        digest.update(diff)
        untracked_raw = self.runner.run_bytes(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=path,
            max_bytes=self.config.server.max_fingerprint_bytes,
        )
        total = len(diff) + len(untracked_raw)
        for raw_name in sorted(item for item in untracked_raw.split(b"\x00") if item):
            relative = raw_name.decode("utf-8", errors="strict")
            file_path = path / relative
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
                        if total > self.config.server.max_fingerprint_bytes:
                            raise WorkspaceError(
                                "Working-tree fingerprint exceeds configured max_fingerprint_bytes"
                            )
                        digest.update(chunk)
        return digest.hexdigest()

    def _change_metrics(self, path: Path, repo: RepositoryConfig) -> dict[str, Any]:
        """Return bounded change metrics used by status views and publish gates."""
        changed_paths = self._assert_changed_paths_allowed(path, repo)
        numstat = self.runner.run(
            ["git", "diff", "--numstat", "HEAD", "--"], cwd=path, check=False
        ).stdout
        added = 0
        deleted = 0
        binary_files = 0
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            if parts[0] == "-" or parts[1] == "-":
                binary_files += 1
                continue
            try:
                added += int(parts[0])
                deleted += int(parts[1])
            except ValueError:
                continue
        total_bytes = 0
        for relative in changed_paths:
            candidate = path / relative
            if candidate.is_file() and not candidate.is_symlink():
                total_bytes += candidate.stat().st_size
        changed_file_count = len(changed_paths)
        diff_line_count = added + deleted
        metrics: dict[str, Any] = {
            "changed_files": changed_file_count,
            "added_lines": added,
            "deleted_lines": deleted,
            "diff_lines": diff_line_count,
            "binary_files": binary_files,
            "total_current_bytes": total_bytes,
            "limits": {
                "max_changed_files": repo.max_changed_files,
                "max_diff_lines": repo.max_diff_lines,
                "max_total_changed_bytes": repo.max_total_changed_bytes,
            },
        }
        metrics["within_limits"] = (
            changed_file_count <= repo.max_changed_files
            and diff_line_count <= repo.max_diff_lines
            and total_bytes <= repo.max_total_changed_bytes
        )
        return metrics

    def _enforce_change_budget(self, path: Path, repo: RepositoryConfig) -> dict[str, Any]:
        metrics = self._change_metrics(path, repo)
        violations: list[str] = []
        if metrics["changed_files"] > repo.max_changed_files:
            violations.append(
                f"changed files {metrics['changed_files']} > {repo.max_changed_files}"
            )
        if metrics["diff_lines"] > repo.max_diff_lines:
            violations.append(f"diff lines {metrics['diff_lines']} > {repo.max_diff_lines}")
        if metrics["total_current_bytes"] > repo.max_total_changed_bytes:
            violations.append(
                "changed file bytes "
                f"{metrics['total_current_bytes']} > {repo.max_total_changed_bytes}"
            )
        if violations:
            raise WorkspaceError(
                "Change budget exceeded: " + "; ".join(violations) + ". "
                "Split the task or raise the explicit repository limits in config."
            )
        return metrics

    @staticmethod
    def _result_dict(result: CommandResult) -> dict[str, Any]:
        return {
            "argv": list(result.argv),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @staticmethod
    def _receipt_result_dict(result: CommandResult) -> dict[str, Any]:
        return {
            "argv": list(result.argv),
            "returncode": result.returncode,
            "stdout_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _load_json_object(result: CommandResult, *, context: str) -> dict[str, Any]:
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CommandError(f"{context} returned invalid or oversized JSON") from exc
        if not isinstance(payload, dict):
            raise CommandError(f"{context} returned a non-object JSON value")
        return cast(dict[str, Any], payload)

    @staticmethod
    def _trim_string(value: Any, limit: int) -> Any:
        if not isinstance(value, str) or len(value) <= limit:
            return value
        return f"{value[:limit]}\n... <{len(value) - limit} characters omitted>"

    def _trim_issue_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["body"] = self._trim_string(payload.get("body"), 50_000)
        comments = payload.get("comments")
        if isinstance(comments, list):
            payload["comment_count"] = len(comments)
            trimmed_comments: list[Any] = []
            for comment in comments[-20:]:
                if isinstance(comment, dict):
                    bounded = dict(comment)
                    bounded["body"] = self._trim_string(bounded.get("body"), 8_000)
                    trimmed_comments.append(bounded)
                else:
                    trimmed_comments.append(comment)
            payload["comments"] = trimmed_comments
            payload["comments_truncated"] = len(comments) > len(trimmed_comments)
        return payload

    def _trim_pr_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload["body"] = self._trim_string(payload.get("body"), 50_000)
        limits = {"files": 300, "commits": 100, "statusCheckRollup": 100, "reviews": 50}
        for key, limit in limits.items():
            value = payload.get(key)
            if isinstance(value, list) and len(value) > limit:
                payload[f"{key}_count"] = len(value)
                payload[key] = value[:limit]
                payload[f"{key}_truncated"] = True
        return payload

    def repo_list(self) -> dict[str, Any]:
        repositories = []
        for repo in self.config.repositories.values():
            repositories.append(
                {
                    "repo_id": repo.repo_id,
                    "display_name": repo.display_name or repo.repo_id,
                    "path": str(repo.path),
                    "remote": repo.remote,
                    "default_base": repo.default_base,
                    "allowed_base_branches": list(repo.allowed_base_branches),
                    "branch_prefix": repo.branch_prefix,
                    "default_verification_profile": repo.default_verification_profile,
                    "change_limits": {
                        "max_changed_files": repo.max_changed_files,
                        "max_diff_lines": repo.max_diff_lines,
                        "max_total_changed_bytes": repo.max_total_changed_bytes,
                    },
                    "pr_defaults": {
                        "labels": list(repo.pr_labels),
                        "reviewers": list(repo.pr_reviewers),
                        "no_maintainer_edit": repo.no_maintainer_edit,
                    },
                    "profiles": {
                        name: {
                            "description": profile.description,
                            "verification": profile.verification,
                            "commands": [list(command) for command in profile.commands],
                        }
                        for name, profile in repo.profiles.items()
                    },
                }
            )
        return {"repositories": repositories}

    def repo_status(self, repo_id: str) -> dict[str, Any]:
        repo = self._repo(repo_id)

        def operation() -> dict[str, Any]:
            git_status = self.runner.run(["git", "status", "--short", "--branch"], cwd=repo.path)
            remote = self.runner.run(["git", "remote", "-v"], cwd=repo.path)
            auth = self.runner.run(["gh", "auth", "status"], cwd=repo.path, check=False)
            return {
                "repo_id": repo_id,
                "path": str(repo.path),
                "git_status": git_status.combined,
                "remotes": remote.combined,
                "gh_authenticated": auth.returncode == 0,
                "gh_auth_status": auth.combined,
            }

        return self._audit_call("repo_status", {"repo_id": repo_id}, operation)

    def repo_context(self, repo_id: str) -> dict[str, Any]:
        """Return compact project metadata and instruction-file previews for planning."""
        repo = self._repo(repo_id)

        def operation() -> dict[str, Any]:
            package: dict[str, Any] | None = None
            package_path = repo.path / "package.json"
            if (
                package_path.is_file()
                and package_path.stat().st_size <= self.config.server.max_file_bytes
            ):
                try:
                    loaded = json.loads(package_path.read_text(encoding="utf-8"))
                    package = loaded if isinstance(loaded, dict) else None
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    package = None
            candidates = (
                "AGENTS.md",
                "CLAUDE.md",
                "CONTRIBUTING.md",
                "README.md",
                ".github/copilot-instructions.md",
                "docs/anatomy/README.md",
            )
            instructions: list[dict[str, Any]] = []
            for relative in candidates:
                try:
                    assert_path_allowed(relative, repo)
                except SecurityError:
                    continue
                path = repo.path / relative
                if not path.is_file() or path.is_symlink():
                    continue
                size = path.stat().st_size
                if size > self.config.server.max_file_bytes:
                    instructions.append({"path": relative, "size_bytes": size, "preview": None})
                    continue
                data = path.read_bytes()
                if b"\x00" in data:
                    continue
                preview = data.decode("utf-8", errors="replace")[:8_000]
                instructions.append(
                    {
                        "path": relative,
                        "size_bytes": size,
                        "preview": preview,
                        "preview_truncated": size > len(preview.encode("utf-8")),
                    }
                )
            root_files_raw = self.runner.run_bytes(
                ["git", "ls-files", "-z", "--", "*"],
                cwd=repo.path,
                max_bytes=min(self.config.server.max_fingerprint_bytes, 2_000_000),
            )
            root_files = []
            for raw in root_files_raw.split(b"\x00"):
                if not raw:
                    continue
                relative = raw.decode("utf-8", errors="strict")
                if "/" not in relative:
                    try:
                        root_files.append(assert_path_allowed(relative, repo))
                    except SecurityError:
                        continue
            scripts: dict[str, str] = {}
            package_manager = None
            engines: dict[str, Any] = {}
            if package:
                raw_scripts = package.get("scripts")
                if isinstance(raw_scripts, dict):
                    scripts = {str(k): str(v) for k, v in raw_scripts.items()}
                declared = package.get("packageManager")
                package_manager = str(declared) if declared is not None else None
                raw_engines = package.get("engines")
                if isinstance(raw_engines, dict):
                    engines = raw_engines
            return {
                "repo_id": repo_id,
                "display_name": repo.display_name or repo_id,
                "path": str(repo.path),
                "default_base": repo.default_base,
                "root_files": sorted(root_files),
                "package_manager": package_manager,
                "engines": engines,
                "scripts": scripts,
                "instruction_files": instructions,
                "profiles": sorted(repo.profiles),
                "default_verification_profile": repo.default_verification_profile,
            }

        return self._audit_call("repo_context", {"repo_id": repo_id}, operation)

    def repo_recent_commits(self, repo_id: str, limit: int = 20) -> dict[str, Any]:
        repo = self._repo(repo_id)
        limit = max(1, min(limit, 100))

        def operation() -> dict[str, Any]:
            output = self.runner.run(
                [
                    "git",
                    "log",
                    f"-{limit}",
                    "--date=iso-strict",
                    "--pretty=format:%H%x09%ad%x09%an%x09%s",
                ],
                cwd=repo.path,
            ).stdout
            commits = []
            for line in output.splitlines():
                sha, date, author, subject = [*line.split("\t", 3), "", "", "", ""][:4]
                commits.append({"sha": sha, "date": date, "author": author, "subject": subject})
            return {"repo_id": repo_id, "commits": commits}

        return self._audit_call(
            "repo_recent_commits", {"repo_id": repo_id, "limit": limit}, operation
        )

    def repo_issue_read(self, repo_id: str, issue_number: int) -> dict[str, Any]:
        repo = self._repo(repo_id)
        if issue_number <= 0:
            raise ValueError("issue_number must be positive")

        def operation() -> dict[str, Any]:
            slug = self._remote_slug(repo.path)
            result = self.runner.run(
                [
                    "gh",
                    "issue",
                    "view",
                    str(issue_number),
                    "--repo",
                    slug,
                    "--json",
                    "number,title,body,state,author,labels,assignees,url,comments",
                ],
                cwd=repo.path,
                output_limit=10_000_000,
            )
            payload = self._load_json_object(result, context="gh issue view")
            return self._trim_issue_payload(payload)

        return self._audit_call(
            "repo_issue_read", {"repo_id": repo_id, "issue_number": issue_number}, operation
        )

    def repo_pr_read(self, repo_id: str, pr_number: int) -> dict[str, Any]:
        repo = self._repo(repo_id)
        if pr_number <= 0:
            raise ValueError("pr_number must be positive")

        def operation() -> dict[str, Any]:
            slug = self._remote_slug(repo.path)
            result = self.runner.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    slug,
                    "--json",
                    "number,title,body,state,isDraft,author,baseRefName,headRefName,url,files,commits,statusCheckRollup,reviews",
                ],
                cwd=repo.path,
                output_limit=10_000_000,
            )
            payload = self._load_json_object(result, context="gh pr view")
            return self._trim_pr_payload(payload)

        return self._audit_call(
            "repo_pr_read", {"repo_id": repo_id, "pr_number": pr_number}, operation
        )

    def workspace_create(
        self, repo_id: str, task_slug: str, base: str | None = None
    ) -> dict[str, Any]:
        repo = self._repo(repo_id)
        creator = WorkspaceCreator(
            WorkspaceCreatorPorts(
                runner=self.runner,
                state=self.state,
                workspace_root=self.config.server.workspace_root,
                verification_timeout_seconds=self.config.server.verification_timeout_seconds,
            )
        )
        plan = creator.plan(repo, WorkspaceCreateCommand(repo_id, task_slug, base))

        def operation() -> dict[str, Any]:
            created = creator.execute(repo, plan)
            return {
                "workspace_id": created.workspace_id,
                "repo_id": created.repo_id,
                "path": str(created.path),
                "branch": created.branch,
                "base": created.base,
                "head_sha": created.head_sha,
                "next_step": "Inspect files, make changes, run a verification profile, then review the diff.",
            }

        return self._audit_call(
            "workspace_create",
            {
                "repo_id": repo_id,
                "base": plan.base,
                "branch": plan.branch,
                "workspace_id": plan.workspace_id,
            },
            operation,
        )

    def workspace_list(self) -> dict[str, Any]:
        records = []
        for record in self.state.list():
            path = Path(record.path)
            records.append(
                {
                    "workspace_id": record.workspace_id,
                    "repo_id": record.repo_id,
                    "path": record.path,
                    "branch": record.branch,
                    "base": record.base,
                    "created_at": record.created_at,
                    "exists": path.is_dir(),
                    "last_verification": (
                        {
                            "profile": record.last_verification.profile,
                            "completed_at": record.last_verification.completed_at,
                        }
                        if record.last_verification
                        else None
                    ),
                }
            )
        return {"workspaces": records}

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        record, repo, path = self._workspace(workspace_id)

        def operation() -> dict[str, Any]:
            changed_paths = self._assert_changed_paths_allowed(path, repo)
            status = self.runner.run(["git", "status", "--short", "--branch"], cwd=path)
            ahead = self.runner.run(
                ["git", "rev-list", "--count", f"{record.remote}/{record.base}..HEAD"], cwd=path
            ).stdout.strip()
            fingerprint = self._fingerprint(path)
            change_metrics = self._change_metrics(path, repo)
            return {
                "workspace_id": workspace_id,
                "repo_id": record.repo_id,
                "path": str(path),
                "branch": record.branch,
                "base": record.base,
                "head_sha": self._head_sha(path),
                "workspace_fingerprint": fingerprint,
                "ahead_of_base": int(ahead or "0"),
                "status": status.combined,
                "changed_paths": changed_paths,
                "change_metrics": change_metrics,
                "clean": not bool(self._status_porcelain(path).strip()),
                "last_verification": (
                    {
                        "profile": record.last_verification.profile,
                        "completed_at": record.last_verification.completed_at,
                        "fingerprint_matches": record.last_verification.fingerprint == fingerprint,
                    }
                    if record.last_verification
                    else None
                ),
            }

        return self._audit_call("workspace_status", {"workspace_id": workspace_id}, operation)

    def workspace_tree(self, workspace_id: str, max_entries: int = 2000) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)
        max_entries = max(1, min(max_entries, 10_000))

        def operation() -> dict[str, Any]:
            raw_bytes = self.runner.run_bytes(
                ["git", "ls-files", "-co", "--exclude-standard", "-z"],
                cwd=path,
                max_bytes=self.config.server.max_fingerprint_bytes,
            )
            raw = raw_bytes.decode("utf-8", errors="strict")
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
            return {
                "workspace_id": workspace_id,
                "entries": entries,
                "truncated": len(entries) >= max_entries,
            }

        return self._audit_call(
            "workspace_tree", {"workspace_id": workspace_id, "max_entries": max_entries}, operation
        )

    def workspace_read_file(
        self,
        workspace_id: str,
        relative_path: str,
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)

        # Pre-audit path validation and line clamping.
        _ = resolve_workspace_path(path, relative_path, repo)
        start_line = max(1, start_line)
        end_line = max(start_line, min(end_line, start_line + 2000))

        reader = WorkspaceFileReader(
            WorkspaceFileReadPorts(
                max_file_bytes=self.config.server.max_file_bytes,
                max_tool_output_chars=self.config.server.max_tool_output_chars,
            )
        )
        command = WorkspaceFileReadCommand(
            workspace_id=workspace_id,
            relative_path=relative_path,
            start_line=start_line,
            end_line=end_line,
        )

        def operation() -> dict[str, Any]:
            result = reader.execute(repo, path, command)
            return {
                "workspace_id": result.workspace_id,
                "path": result.path,
                "sha256": result.sha256,
                "size_bytes": result.size_bytes,
                "total_lines": result.total_lines,
                "start_line": result.start_line,
                "end_line": result.end_line,
                "content": result.content,
                "truncated": result.truncated,
            }

        return self._audit_call(
            "workspace_read_file",
            {"workspace_id": workspace_id, "path": relative_path},
            operation,
        )

    def workspace_read_files(
        self,
        workspace_id: str,
        relative_paths: list[str],
        start_line: int = 1,
        end_line: int = 500,
    ) -> dict[str, Any]:
        """Read a bounded batch of files to reduce MCP round trips."""
        if not relative_paths:
            raise ValueError("relative_paths must contain at least one path")
        if len(relative_paths) > self.config.server.max_batch_files:
            raise ValueError(
                f"relative_paths exceeds max_batch_files={self.config.server.max_batch_files}"
            )
        unique = list(dict.fromkeys(relative_paths))

        def operation() -> dict[str, Any]:
            files: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for relative_path in unique:
                try:
                    files.append(
                        self.workspace_read_file(
                            workspace_id, relative_path, start_line=start_line, end_line=end_line
                        )
                    )
                except (WorkspaceError, SecurityError, ValueError) as exc:
                    errors.append(
                        {
                            "path": relative_path,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
            return {
                "workspace_id": workspace_id,
                "files": files,
                "errors": errors,
                "requested": len(unique),
                "succeeded": len(files),
            }

        return self._audit_call(
            "workspace_read_files",
            {"workspace_id": workspace_id, "file_count": len(unique)},
            operation,
        )

    def workspace_search(
        self,
        workspace_id: str,
        query: str,
        path_glob: str | None = None,
        max_results: int = 200,
    ) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)
        if not query or "\x00" in query:
            raise ValueError("query must be non-empty")
        max_results = max(1, min(max_results, 2000))
        argv = ["git", "grep", "--untracked", "-n", "-I", "-F", "-e", query, "--"]
        if path_glob:
            # Git pathspecs are interpreted by Git, not a shell. Reject obvious traversal.
            if path_glob.startswith(("/", "-")) or ".." in Path(path_glob).parts:
                raise SecurityError("Unsafe path_glob")
            argv.append(path_glob)

        def operation() -> dict[str, Any]:
            result = self.runner.run(argv, cwd=path, check=False)
            if result.returncode not in (0, 1):
                raise CommandError(result.combined)
            matches = []
            for line in result.stdout.splitlines():
                file_name = line.split(":", 1)[0]
                try:
                    assert_path_allowed(file_name, repo)
                except SecurityError:
                    continue
                matches.append(line)
                if len(matches) >= max_results:
                    break
            return {
                "workspace_id": workspace_id,
                "query": query,
                "matches": matches,
                "truncated": len(matches) >= max_results,
            }

        return self._audit_call(
            "workspace_search",
            {"workspace_id": workspace_id, "path_glob": path_glob},
            operation,
        )

    def workspace_write_file(
        self,
        workspace_id: str,
        relative_path: str,
        content: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)

        # Pre-audit input validation — errors are NOT recorded in the audit log,
        # matching the original workspace_write_file audit boundary.
        _ = resolve_workspace_path(path, relative_path, repo)
        if "\x00" in content:
            raise SecurityError("NUL bytes are not allowed in text files")
        encoded = content.encode("utf-8")
        if len(encoded) > self.config.server.max_file_bytes:
            raise SecurityError("New file content exceeds max_file_bytes")
        if expected_sha256 != "<new>" and not _SHA256_RE.fullmatch(expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 or '<new>'")

        writer = WorkspaceFileWriter(
            WorkspaceFileWritePorts(
                state=self.state,
                runner=self.runner,
                max_file_bytes=self.config.server.max_file_bytes,
            )
        )
        command = WorkspaceFileWriteCommand(
            workspace_id=workspace_id,
            relative_path=relative_path,
            content=content,
            expected_sha256=expected_sha256,
        )

        def operation() -> dict[str, Any]:
            result = writer.execute(repo, path, command)
            return {
                "workspace_id": result.workspace_id,
                "path": result.path,
                "sha256": result.sha256,
                "size_bytes": result.size_bytes,
                "diff_stat": result.diff_stat,
            }

        return self._audit_call(
            "workspace_write_file",
            {"workspace_id": workspace_id, "path": relative_path, "size_bytes": len(encoded)},
            operation,
        )

    def workspace_replace_text(
        self,
        workspace_id: str,
        relative_path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        expected_occurrences: int = 1,
    ) -> dict[str, Any]:
        if not old_text:
            raise ValueError("old_text must be non-empty")
        if "\x00" in old_text or "\x00" in new_text:
            raise SecurityError("NUL bytes are not allowed in text replacements")
        if expected_occurrences <= 0 or expected_occurrences > 1000:
            raise ValueError("expected_occurrences must be between 1 and 1000")
        _, repo, path = self._workspace(workspace_id)
        file_path = resolve_workspace_path(path, relative_path, repo)
        if not _SHA256_RE.fullmatch(expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256")

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                if not file_path.is_file() or file_path.is_symlink():
                    raise WorkspaceError("Target must be an existing regular file")
                data = file_path.read_bytes()
                if b"\x00" in data:
                    raise SecurityError("Binary files are not supported by this tool")
                if len(data) > self.config.server.max_file_bytes:
                    raise SecurityError("File exceeds max_file_bytes")
                actual_sha = hashlib.sha256(data).hexdigest()
                if actual_sha != expected_sha256:
                    raise WorkspaceError(
                        f"File changed since it was read: expected {expected_sha256}, got {actual_sha}"
                    )
                text = data.decode("utf-8")
                count = text.count(old_text)
                if count != expected_occurrences:
                    raise WorkspaceError(
                        f"Expected {expected_occurrences} occurrences, found {count}; no changes applied"
                    )
                updated = text.replace(old_text, new_text, expected_occurrences)
                encoded = updated.encode("utf-8")
                if len(encoded) > self.config.server.max_file_bytes:
                    raise SecurityError("Updated content exceeds max_file_bytes")
                existing_mode = stat.S_IMODE(file_path.stat().st_mode)
                temporary = file_path.with_name(f".{file_path.name}.rf-{os.getpid()}")
                try:
                    temporary.write_bytes(encoded)
                    os.chmod(temporary, existing_mode)
                    os.replace(temporary, file_path)
                finally:
                    temporary.unlink(missing_ok=True)
                return {
                    "workspace_id": workspace_id,
                    "path": assert_path_allowed(relative_path, repo),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "replacements": expected_occurrences,
                    "diff_stat": self.runner.run(["git", "diff", "--stat", "--"], cwd=path).stdout,
                }

        return self._audit_call(
            "workspace_replace_text",
            {"workspace_id": workspace_id, "path": relative_path},
            operation,
        )

    def workspace_apply_patch(
        self,
        workspace_id: str,
        patch: str,
        expected_head_sha: str,
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)
        if not _GIT_OID_RE.fullmatch(expected_head_sha):
            raise ValueError("expected_head_sha must be a lowercase 40/64 hex Git object id")
        if not _SHA256_RE.fullmatch(expected_workspace_fingerprint):
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")
        changed_paths = validate_patch(
            patch, repo, max_chars=self.config.server.max_tool_output_chars * 4
        )

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                actual_head = self._head_sha(path)
                if actual_head != expected_head_sha:
                    raise WorkspaceError(
                        f"HEAD changed: expected {expected_head_sha}, got {actual_head}"
                    )
                actual_fingerprint = self._fingerprint(path)
                if actual_fingerprint != expected_workspace_fingerprint:
                    raise WorkspaceError(
                        "Workspace changed since it was inspected; refresh status before applying patch"
                    )
                self.runner.run(
                    ["git", "apply", "--check", "--whitespace=error-all", "-"],
                    cwd=path,
                    input_text=patch,
                )
                self.runner.run(
                    ["git", "apply", "--whitespace=fix", "-"], cwd=path, input_text=patch
                )
                try:
                    self._assert_changed_paths_allowed(path, repo)
                except Exception:
                    # Best-effort rollback: a patch that violates post-apply policy must not
                    # leave the workspace in a partially unsafe state.
                    self.runner.run(
                        ["git", "apply", "-R", "--whitespace=nowarn", "-"],
                        cwd=path,
                        input_text=patch,
                        check=False,
                    )
                    raise
                return {
                    "workspace_id": workspace_id,
                    "changed_paths": list(changed_paths),
                    "workspace_fingerprint": self._fingerprint(path),
                    "diff_stat": self.runner.run(["git", "diff", "--stat", "--"], cwd=path).stdout,
                }

        return self._audit_call(
            "workspace_apply_patch",
            {"workspace_id": workspace_id, "changed_paths": list(changed_paths)},
            operation,
        )

    def workspace_restore_paths(
        self,
        workspace_id: str,
        relative_paths: list[str],
        expected_workspace_fingerprint: str,
    ) -> dict[str, Any]:
        """Restore selected tracked files to HEAD or remove selected untracked regular files."""
        _, repo, path = self._workspace(workspace_id)
        if not relative_paths:
            raise ValueError("relative_paths must contain at least one path")
        if len(relative_paths) > self.config.server.max_batch_files:
            raise ValueError(
                f"relative_paths exceeds max_batch_files={self.config.server.max_batch_files}"
            )
        if not _SHA256_RE.fullmatch(expected_workspace_fingerprint):
            raise ValueError("expected_workspace_fingerprint must be a lowercase SHA-256")
        normalized = [assert_path_allowed(value, repo) for value in dict.fromkeys(relative_paths)]

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                actual = self._fingerprint(path)
                if actual != expected_workspace_fingerprint:
                    raise WorkspaceError(
                        "Workspace changed since it was inspected; refresh status before restoring"
                    )
                restored: list[str] = []
                removed_untracked: list[str] = []
                for relative in normalized:
                    candidate = resolve_workspace_path(path, relative, repo)
                    tracked = (
                        self.runner.run(
                            ["git", "ls-files", "--error-unmatch", "--", relative],
                            cwd=path,
                            check=False,
                        ).returncode
                        == 0
                    )
                    if tracked:
                        self.runner.run(
                            [
                                "git",
                                "restore",
                                "--source=HEAD",
                                "--staged",
                                "--worktree",
                                "--",
                                relative,
                            ],
                            cwd=path,
                        )
                        restored.append(relative)
                        continue
                    if candidate.exists():
                        if candidate.is_symlink() or not candidate.is_file():
                            raise SecurityError(
                                f"Only untracked regular files can be removed: {relative}"
                            )
                        candidate.unlink()
                        removed_untracked.append(relative)
                return {
                    "workspace_id": workspace_id,
                    "restored_tracked": restored,
                    "removed_untracked": removed_untracked,
                    "workspace_fingerprint": self._fingerprint(path),
                    "change_metrics": self._change_metrics(path, repo),
                }

        return self._audit_call(
            "workspace_restore_paths",
            {"workspace_id": workspace_id, "path_count": len(normalized)},
            operation,
        )

    def workspace_diff(self, workspace_id: str, staged: bool = False) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)

        def operation() -> dict[str, Any]:
            changed_paths = self._assert_changed_paths_allowed(path, repo)
            args = ["git", "diff", "--no-ext-diff", "--stat"]
            diff_args = ["git", "diff", "--no-ext-diff"]
            if staged:
                args.append("--cached")
                diff_args.append("--cached")
            stat = self.runner.run([*args, "--"], cwd=path).stdout
            diff_parts = [self.runner.run([*diff_args, "--"], cwd=path).stdout]
            untracked_paths: list[str] = []
            if not staged:
                untracked_paths = self._untracked_paths(path, repo)
                for relative_path in untracked_paths:
                    file_path = resolve_workspace_path(path, relative_path, repo)
                    if not file_path.is_file() or file_path.is_symlink():
                        continue
                    if file_path.stat().st_size > self.config.server.max_file_bytes:
                        diff_parts.append(
                            f"\nUntracked file omitted because it is too large: {relative_path}\n"
                        )
                        continue
                    if b"\x00" in file_path.read_bytes():
                        diff_parts.append(f"\nBinary untracked file omitted: {relative_path}\n")
                        continue
                    result = self.runner.run(
                        ["git", "diff", "--no-index", "--", "/dev/null", relative_path],
                        cwd=path,
                        check=False,
                    )
                    if result.returncode not in (0, 1):
                        raise CommandError(result.combined)
                    diff_parts.append(result.stdout)
            diff, truncated = self._bounded_text("\n".join(part for part in diff_parts if part))
            change_metrics = self._change_metrics(path, repo)
            return {
                "workspace_id": workspace_id,
                "staged": staged,
                "changed_paths": changed_paths,
                "change_metrics": change_metrics,
                "untracked_paths": untracked_paths,
                "stat": stat,
                "diff": diff,
                "truncated": truncated,
            }

        return self._audit_call(
            "workspace_diff", {"workspace_id": workspace_id, "staged": staged}, operation
        )

    def _profile(self, repo: RepositoryConfig, profile_name: str) -> ProfileConfig:
        try:
            return repo.profiles[profile_name]
        except KeyError as exc:
            raise ConfigError(
                f"Unknown profile {profile_name!r}. Available: {sorted(repo.profiles)}"
            ) from exc

    def workspace_run_profile(self, workspace_id: str, profile_name: str) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)
        profile = self._profile(repo, profile_name)

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                fresh_record = self.state.load(workspace_id)
                command_results: list[dict[str, Any]] = []
                receipt_results: list[dict[str, Any]] = []
                timeout = profile.timeout_seconds or self.config.server.verification_timeout_seconds
                for command in profile.commands:
                    result = self.runner.run(command, cwd=path, timeout=timeout)
                    command_results.append(self._result_dict(result))
                    receipt_results.append(self._receipt_result_dict(result))
                # Verification/build commands may generate files. Enforce repository policy
                # before recording a receipt so denied paths or symlinks cannot be blessed.
                self._assert_changed_paths_allowed(path, repo)
                change_metrics = self._enforce_change_budget(path, repo)
                fingerprint = self._fingerprint(path)
                if profile.verification:
                    fresh_record.last_verification = VerificationReceipt(
                        profile=profile.name,
                        fingerprint=fingerprint,
                        completed_at=utc_now(),
                        commands=receipt_results,
                    )
                    self.state.save(fresh_record)
                return {
                    "workspace_id": workspace_id,
                    "profile": profile.name,
                    "description": profile.description,
                    "verification": profile.verification,
                    "fingerprint": fingerprint,
                    "commands": command_results,
                    "change_metrics": change_metrics,
                    "satisfies_commit_gate": profile.verification,
                }

        return self._audit_call(
            "workspace_run_profile",
            {"workspace_id": workspace_id, "profile": profile_name},
            operation,
        )

    def workspace_verify(
        self, workspace_id: str, profile_name: str | None = None
    ) -> dict[str, Any]:
        """Run an explicit or repository-default verification profile."""
        record, repo, _ = self._workspace(workspace_id)
        selected = profile_name or repo.default_verification_profile
        if not selected:
            candidates = [name for name, profile in repo.profiles.items() if profile.verification]
            if len(candidates) == 1:
                selected = candidates[0]
            else:
                raise ConfigError(
                    "No default verification profile is configured. "
                    f"Available verification profiles: {sorted(candidates)}"
                )
        profile = self._profile(repo, selected)
        if not profile.verification:
            raise ConfigError(f"Profile {selected!r} is not a verification profile")
        result = self.workspace_run_profile(workspace_id, selected)
        result["used_default"] = profile_name is None
        result["repo_id"] = record.repo_id
        return result

    def workspace_commit(self, workspace_id: str, message: str) -> dict[str, Any]:
        _, repo, path = self._workspace(workspace_id)
        message = message.strip()
        if not message or len(message) > 1000 or "\x00" in message:
            raise ValueError("Commit message must contain 1-1000 characters")

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                fresh_record = self.state.load(workspace_id)
                self._assert_changed_paths_allowed(path, repo)
                change_metrics = self._enforce_change_budget(path, repo)
                if not self._status_porcelain(path).strip():
                    raise WorkspaceError("There are no changes to commit")
                if repo.require_verification_before_commit:
                    if not fresh_record.last_verification:
                        raise WorkspaceError(
                            "A successful verification profile is required before commit"
                        )
                    current = self._fingerprint(path)
                    if current != fresh_record.last_verification.fingerprint:
                        raise WorkspaceError(
                            "Working tree changed after verification; run a verification profile again"
                        )
                self.runner.run(["git", "add", "--all", "--"], cwd=path)
                staged = self.runner.run(
                    ["git", "diff", "--cached", "--name-only", "--"], cwd=path
                ).stdout.strip()
                if not staged:
                    raise WorkspaceError("No staged changes remain after git add")
                verification_profile = (
                    fresh_record.last_verification.profile
                    if fresh_record.last_verification
                    else None
                )
                verification_completed_at = (
                    fresh_record.last_verification.completed_at
                    if fresh_record.last_verification
                    else None
                )
                self.runner.run(["git", "commit", "-m", message], cwd=path)
                head_sha = self._head_sha(path)
                if repo.require_verification_before_commit:
                    fresh_record.metadata["verified_commit_sha"] = head_sha
                    fresh_record.metadata["verification_profile"] = verification_profile
                    fresh_record.metadata["verification_completed_at"] = verification_completed_at
                fresh_record.last_verification = None
                self.state.save(fresh_record)
                return {
                    "workspace_id": workspace_id,
                    "branch": fresh_record.branch,
                    "commit": self.runner.run(
                        ["git", "show", "-1", "--stat", "--oneline", "--decorate"], cwd=path
                    ).stdout,
                    "head_sha": head_sha,
                    "verified_profile": verification_profile,
                    "change_metrics": change_metrics,
                }

        return self._audit_call(
            "workspace_commit",
            {"workspace_id": workspace_id, "message_length": len(message)},
            operation,
        )

    def workspace_push(self, workspace_id: str) -> dict[str, Any]:
        record, repo, path = self._workspace(workspace_id)
        validate_branch(record.branch, repo)

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                fresh_record = self.state.load(workspace_id)
                self._assert_changed_paths_allowed(path, repo)
                self._ensure_clean(path, context="push")
                head_sha = self._head_sha(path)
                if repo.require_verification_before_commit:
                    verified_commit = fresh_record.metadata.get("verified_commit_sha")
                    if verified_commit != head_sha:
                        raise WorkspaceError(
                            "Current HEAD was not committed through the verified commit gate"
                        )
                result = self.runner.run(
                    [
                        "git",
                        "push",
                        "--set-upstream",
                        fresh_record.remote,
                        f"HEAD:refs/heads/{fresh_record.branch}",
                    ],
                    cwd=path,
                    timeout=self.config.server.verification_timeout_seconds,
                )
                fresh_record.metadata["last_pushed_sha"] = head_sha
                self.state.save(fresh_record)
                return {
                    "workspace_id": workspace_id,
                    "branch": fresh_record.branch,
                    "remote": fresh_record.remote,
                    "head_sha": head_sha,
                    "output": result.combined,
                }

        return self._audit_call(
            "workspace_push",
            {"workspace_id": workspace_id, "branch": record.branch, "remote": record.remote},
            operation,
        )

    def workspace_create_draft_pr(self, workspace_id: str, title: str, body: str) -> dict[str, Any]:
        record, repo, path = self._workspace(workspace_id)
        title = title.strip()
        if not title or len(title) > 256:
            raise ValueError("PR title must contain 1-256 characters")
        if len(body) > 96_000:
            raise ValueError("PR body is too large")

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                fresh_record = self.state.load(workspace_id)
                self._assert_changed_paths_allowed(path, repo)
                self._ensure_clean(path, context="creating a pull request")
                validate_branch(fresh_record.branch, repo)
                upstream = self.runner.run(
                    ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                    cwd=path,
                    check=False,
                )
                if upstream.returncode != 0:
                    raise WorkspaceError("Branch has no upstream; call workspace_push first")
                head_sha = self._head_sha(path)
                upstream_sha = self.runner.run(
                    ["git", "rev-parse", "@{u}"], cwd=path
                ).stdout.strip()
                if upstream_sha != head_sha:
                    raise WorkspaceError(
                        "Local branch is not synchronized with its upstream; call workspace_push first"
                    )
                if fresh_record.metadata.get("last_pushed_sha") != head_sha:
                    raise WorkspaceError(
                        "Workspace registry has no matching successful push for the current HEAD"
                    )
                ahead = int(
                    self.runner.run(
                        [
                            "git",
                            "rev-list",
                            "--count",
                            f"{fresh_record.remote}/{fresh_record.base}..HEAD",
                        ],
                        cwd=path,
                    ).stdout.strip()
                    or "0"
                )
                if ahead <= 0:
                    raise WorkspaceError("Branch has no commits ahead of the base branch")
                existing = self.runner.run(
                    [
                        "gh",
                        "pr",
                        "view",
                        fresh_record.branch,
                        "--json",
                        "number,url,isDraft,state",
                    ],
                    cwd=path,
                    check=False,
                )
                if existing.returncode == 0:
                    payload = self._load_json_object(existing, context="gh pr view")
                    payload["already_existed"] = True
                    fresh_record.metadata["pr_url"] = payload.get("url")
                    fresh_record.metadata["pr_number"] = payload.get("number")
                    self.state.save(fresh_record)
                    return payload

                verification_profile = fresh_record.metadata.get("verification_profile")
                verification_completed_at = fresh_record.metadata.get("verification_completed_at")
                footer = (
                    "\n\n<!-- repoforge -->\n"
                    "---\n"
                    "Created by **RepoForge** from an isolated local worktree.\n\n"
                    f"- Branch: `{fresh_record.branch}`\n"
                    f"- Head: `{head_sha}`\n"
                    f"- Verification: `{verification_profile or 'not recorded'}`"
                )
                if verification_completed_at:
                    footer += f" at `{verification_completed_at}`"
                final_body = body.rstrip() + footer + "\n"

                argv = [
                    "gh",
                    "pr",
                    "create",
                    "--draft",
                    "--base",
                    fresh_record.base,
                    "--head",
                    fresh_record.branch,
                    "--title",
                    title,
                    "--body-file",
                    "-",
                ]
                if repo.no_maintainer_edit:
                    argv.append("--no-maintainer-edit")
                for label in repo.pr_labels:
                    argv.extend(["--label", label])
                for reviewer in repo.pr_reviewers:
                    argv.extend(["--reviewer", reviewer])
                result = self.runner.run(
                    argv,
                    cwd=path,
                    input_text=final_body,
                    timeout=self.config.server.verification_timeout_seconds,
                )
                url = result.stdout.strip().splitlines()[-1]
                fresh_record.metadata["pr_url"] = url
                self.state.save(fresh_record)
                return {
                    "workspace_id": workspace_id,
                    "url": url,
                    "draft": True,
                    "branch": fresh_record.branch,
                    "base": fresh_record.base,
                    "labels": list(repo.pr_labels),
                    "reviewers": list(repo.pr_reviewers),
                    "already_existed": False,
                }

        return self._audit_call(
            "workspace_create_draft_pr",
            {"workspace_id": workspace_id, "branch": record.branch, "base": record.base},
            operation,
        )

    def workspace_update_draft_pr(
        self, workspace_id: str, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]:
        """Update the title and/or body of the workspace pull request without changing draft state."""
        record, _, path = self._workspace(workspace_id)
        if title is None and body is None:
            raise ValueError("At least one of title or body must be provided")
        if title is not None:
            title = title.strip()
            if not title or len(title) > 256:
                raise ValueError("PR title must contain 1-256 characters")
        if body is not None and len(body) > 100_000:
            raise ValueError("PR body is too large")

        def operation() -> dict[str, Any]:
            argv = ["gh", "pr", "edit", record.branch]
            input_text = None
            if title is not None:
                argv.extend(["--title", title])
            if body is not None:
                argv.extend(["--body-file", "-"])
                input_text = body
            self.runner.run(
                argv,
                cwd=path,
                input_text=input_text,
                timeout=self.config.server.verification_timeout_seconds,
            )
            result = self.runner.run(
                [
                    "gh",
                    "pr",
                    "view",
                    record.branch,
                    "--json",
                    "number,title,url,state,isDraft,body",
                ],
                cwd=path,
                output_limit=2_000_000,
            )
            return self._load_json_object(result, context="gh pr view")

        return self._audit_call(
            "workspace_update_draft_pr", {"workspace_id": workspace_id}, operation
        )

    def workspace_pr_status(self, workspace_id: str) -> dict[str, Any]:
        record, _, path = self._workspace(workspace_id)

        def operation() -> dict[str, Any]:
            result = self.runner.run(
                [
                    "gh",
                    "pr",
                    "view",
                    record.branch,
                    "--json",
                    "number,title,url,state,isDraft,mergeable,reviewDecision,statusCheckRollup",
                ],
                cwd=path,
                output_limit=10_000_000,
            )
            payload = self._load_json_object(result, context="gh pr view")
            return self._trim_pr_payload(payload)

        return self._audit_call("workspace_pr_status", {"workspace_id": workspace_id}, operation)

    def workspace_pr_checks(self, workspace_id: str, required_only: bool = False) -> dict[str, Any]:
        """Read compact pull-request check results with pass/fail/pending buckets."""
        record, _, path = self._workspace(workspace_id)

        def operation() -> dict[str, Any]:
            argv = [
                "gh",
                "pr",
                "checks",
                record.branch,
                "--json",
                "name,state,bucket,link,workflow,description,startedAt,completedAt",
            ]
            if required_only:
                argv.append("--required")
            result = self.runner.run(argv, cwd=path, check=False, output_limit=5_000_000)
            if result.returncode not in (0, 1, 8):
                raise CommandError(result.combined)
            try:
                payload = json.loads(result.stdout or "[]")
            except json.JSONDecodeError as exc:
                raise CommandError("gh pr checks returned invalid JSON") from exc
            if not isinstance(payload, list):
                raise CommandError("gh pr checks returned a non-list JSON value")
            buckets: dict[str, int] = {}
            for item in payload:
                if isinstance(item, dict):
                    bucket = str(item.get("bucket", "unknown"))
                    buckets[bucket] = buckets.get(bucket, 0) + 1
            return {
                "workspace_id": workspace_id,
                "branch": record.branch,
                "required_only": required_only,
                "checks": payload,
                "summary": buckets,
                "all_passed": bool(payload) and set(buckets).issubset({"pass", "skipping"}),
                "pending": buckets.get("pending", 0) > 0,
            }

        return self._audit_call(
            "workspace_pr_checks",
            {"workspace_id": workspace_id, "required_only": required_only},
            operation,
        )

    def workspace_remove(
        self, workspace_id: str, delete_local_branch: bool = False
    ) -> dict[str, Any]:
        record, repo, path = self._workspace(workspace_id)

        def operation() -> dict[str, Any]:
            with self.state.lock(workspace_id):
                self._ensure_clean(path, context="workspace removal")
                self.runner.run(
                    ["git", "worktree", "remove", str(path)],
                    cwd=repo.path,
                    timeout=self.config.server.verification_timeout_seconds,
                )
                branch_deleted = False
                if delete_local_branch:
                    self.runner.run(["git", "branch", "-D", record.branch], cwd=repo.path)
                    branch_deleted = True
                self.state.delete(workspace_id)
                return {
                    "workspace_id": workspace_id,
                    "removed": True,
                    "local_branch_deleted": branch_deleted,
                    "remote_branch_untouched": True,
                }

        return self._audit_call(
            "workspace_remove",
            {"workspace_id": workspace_id, "delete_local_branch": delete_local_branch},
            operation,
        )

    def doctor(self) -> dict[str, Any]:
        """Run actionable environment, authentication, repository, and profile checks."""
        checks: list[dict[str, Any]] = []

        def add(
            name: str,
            ok: bool,
            detail: str,
            *,
            severity: str = "error",
            remediation: str | None = None,
        ) -> None:
            item: dict[str, Any] = {
                "name": name,
                "ok": ok,
                "severity": severity,
                "detail": detail,
            }
            if remediation:
                item["remediation"] = remediation
            checks.append(item)

        add("config", True, str(self.config.source_path), severity="info")
        executable_paths: dict[str, str | None] = {}
        for executable in ("git", "gh"):
            found = shutil.which(executable, path=self.runner.environment().get("PATH"))
            executable_paths[executable] = found
            add(
                f"executable:{executable}",
                bool(found),
                found or "not found",
                remediation=(
                    "Install Git with Xcode Command Line Tools or Homebrew."
                    if executable == "git"
                    else "Install GitHub CLI with `brew install gh`."
                ),
            )
            if found:
                try:
                    version = self.runner.run([executable, "--version"], cwd=Path.home()).stdout
                    add(f"version:{executable}", True, version.splitlines()[0], severity="info")
                except Exception as exc:
                    add(f"version:{executable}", False, str(exc), severity="warning")

        if executable_paths.get("gh"):
            try:
                auth = self.runner.run(["gh", "auth", "status"], cwd=Path.home(), check=False)
                add(
                    "gh_auth",
                    auth.returncode == 0,
                    auth.combined,
                    remediation="Run `gh auth login`, then `gh auth setup-git`.",
                )
            except Exception as exc:
                add("gh_auth", False, str(exc), remediation="Run `gh auth login`.")

        for repo_id, repo in self.config.repositories.items():
            valid = repo.path.is_dir() and (repo.path / ".git").exists()
            add(
                f"repository:{repo_id}",
                valid,
                str(repo.path),
                remediation=f"Update repositories.{repo_id}.path in {self.config.source_path}.",
            )
            if not valid:
                continue
            try:
                self.runner.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo.path)
                add(f"repository_git:{repo_id}", True, "valid Git working tree")
            except Exception as exc:
                add(f"repository_git:{repo_id}", False, str(exc))
                continue

            current = self._current_branch(repo.path)
            add(f"repository_branch:{repo_id}", True, current or "detached HEAD", severity="info")
            dirty = bool(self._status_porcelain(repo.path).strip())
            add(
                f"repository_clean:{repo_id}",
                not dirty,
                "clean" if not dirty else "source clone has uncommitted changes",
                severity="warning",
                remediation="Commit/stash source-clone changes before creating new workspaces.",
            )
            remote_check = self.runner.run(
                ["git", "remote", "get-url", repo.remote], cwd=repo.path, check=False
            )
            add(
                f"repository_remote:{repo_id}",
                remote_check.returncode == 0,
                remote_check.combined,
                remediation=f"Configure Git remote {repo.remote!r}.",
            )
            base_check = self.runner.run(
                ["git", "rev-parse", "--verify", f"refs/remotes/{repo.remote}/{repo.default_base}"],
                cwd=repo.path,
                check=False,
            )
            add(
                f"repository_base:{repo_id}",
                base_check.returncode == 0,
                f"{repo.remote}/{repo.default_base}",
                severity="warning",
                remediation=f"Run `git fetch {repo.remote} {repo.default_base}`.",
            )

            package_path = repo.path / "package.json"
            if package_path.is_file():
                try:
                    package = json.loads(package_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    package = None
                if isinstance(package, dict):
                    package_manager = package.get("packageManager")
                    if isinstance(package_manager, str) and "@" in package_manager:
                        manager, expected_version = package_manager.split("@", 1)
                        found = shutil.which(manager, path=self.runner.environment().get("PATH"))
                        add(
                            f"package_manager:{repo_id}:{manager}",
                            bool(found),
                            found or "not found",
                            remediation=(
                                f"Enable/install {manager} {expected_version}; for Node projects try `corepack enable`."
                            ),
                        )
                        if found:
                            actual = self.runner.run(
                                [manager, "--version"], cwd=repo.path
                            ).stdout.strip()
                            add(
                                f"package_manager_version:{repo_id}:{manager}",
                                actual == expected_version,
                                f"expected {expected_version}, found {actual}",
                                severity="warning",
                                remediation=f"Use the version declared by packageManager: {package_manager}.",
                            )
                    engines = package.get("engines")
                    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
                        node = shutil.which("node", path=self.runner.environment().get("PATH"))
                        add(
                            f"runtime:{repo_id}:node",
                            bool(node),
                            node or "not found",
                            remediation=f"Install Node {engines['node']}.",
                        )
                        if node:
                            actual_node = (
                                self.runner.run(["node", "--version"], cwd=repo.path)
                                .stdout.strip()
                                .lstrip("v")
                            )
                            expected_node = str(engines["node"])
                            add(
                                f"runtime_version:{repo_id}:node",
                                actual_node == expected_node,
                                f"expected {expected_node}, found {actual_node}",
                                severity="warning",
                                remediation=f"Switch to Node {expected_node} using your version manager.",
                            )

            seen_executables: set[tuple[str, str]] = set()
            for profile in repo.profiles.values():
                for command in profile.commands:
                    executable = command[0]
                    key = (profile.name, executable)
                    if key in seen_executables:
                        continue
                    seen_executables.add(key)
                    found = shutil.which(executable, path=self.runner.environment().get("PATH"))
                    add(
                        f"profile_executable:{repo_id}:{profile.name}:{executable}",
                        bool(found),
                        found or "not found",
                        remediation=f"Install {executable} or update the configured profile command.",
                    )

        for name, root in (
            ("workspace_root_writable", self.config.server.workspace_root),
            ("state_root_writable", self.config.server.state_root),
        ):
            try:
                root.mkdir(parents=True, exist_ok=True)
                probe = root / f".write-test-{os.getpid()}"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                add(name, True, str(root))
            except OSError as exc:
                add(name, False, str(exc))

        errors = [item for item in checks if not item["ok"] and item["severity"] == "error"]
        warnings = [item for item in checks if not item["ok"] and item["severity"] == "warning"]
        return {
            "ok": not errors,
            "summary": {
                "passed": sum(1 for item in checks if item["ok"]),
                "errors": len(errors),
                "warnings": len(warnings),
                "total": len(checks),
            },
            "checks": checks,
            "audit_log": str(self.audit.path),
        }
