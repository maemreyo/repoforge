"""Bounded read-only repository fact collection; discovered commands are never executed."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.policy import slugify
from ...domain.repository_detection import ManifestFact, RemoteFact, RepositoryFacts
from ...ports.command import CommandExecutor


class LocalRepositoryProbe:
    MAX_TRACKED_OUTPUT = 32 * 1024 * 1024
    MAX_FILES_INSPECTED = 100_000
    LARGE_FILE_BYTES = 10 * 1024 * 1024

    def __init__(self, commands: CommandExecutor):
        self._commands = commands

    def _git(
        self, root: Path, argv: list[str], *, check: bool = False, output_limit: int = 4_000_000
    ) -> str:
        result = self._commands.run(
            ["git", *argv], cwd=root, check=check, output_limit=output_limit
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _make_targets(path: Path) -> tuple[str, ...]:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
            return ()
        targets: set[str] = set()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line or line[0].isspace() or line.startswith("#"):
                continue
            head, sep, _ = line.partition(":")
            if not sep or "=" in head or "%" in head:
                continue
            targets.update(
                item for item in head.split() if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item)
            )
        return tuple(sorted(targets))

    @staticmethod
    def _manifest_kind(path: Path) -> tuple[str, str | None] | None:
        name = path.name
        return {
            "package.json": ("javascript", None),
            "pyproject.toml": ("python", None),
            "Cargo.toml": ("rust", "cargo"),
            "go.mod": ("go", "go"),
            "pom.xml": ("java", "maven"),
            "build.gradle": ("java", "gradle"),
            "build.gradle.kts": ("java", "gradle"),
        }.get(name)

    def inspect(self, path: Path, *, repo_id: str | None = None) -> RepositoryFacts:
        requested = path.expanduser().resolve()
        if not requested.is_dir():
            raise ConfigError(f"Repository path does not exist: {requested}")
        root_raw = self._git(requested, ["rev-parse", "--show-toplevel"], check=True)
        root = Path(root_raw).resolve()
        common_dir_raw = self._git(root, ["rev-parse", "--git-common-dir"], check=True)
        common_dir = (
            (root / common_dir_raw).resolve()
            if not Path(common_dir_raw).is_absolute()
            else Path(common_dir_raw).resolve()
        )
        current = self._git(root, ["branch", "--show-current"])
        detached = not bool(current)
        remote_lines = self._git(root, ["remote", "-v"]).splitlines()
        remote_map: dict[str, dict[str, str]] = {}
        for line in remote_lines:
            parts = line.split()
            if len(parts) >= 3 and parts[2] in {"(fetch)", "(push)"}:
                remote_map.setdefault(parts[0], {})[parts[2][1:-1]] = parts[1]
        remotes = tuple(
            RemoteFact(name, values.get("fetch"), values.get("push"))
            for name, values in sorted(remote_map.items())
        )
        github_remote = any(
            "github.com" in (value or "").lower()
            for remote in remotes
            for value in (remote.fetch_url, remote.push_url)
        )
        github_authenticated: bool | None = None
        if github_remote:
            try:
                github_authenticated = (
                    self._commands.run(
                        ["gh", "auth", "status"],
                        cwd=root,
                        check=False,
                        timeout=10,
                        output_limit=20_000,
                    ).returncode
                    == 0
                )
            except Exception:
                github_authenticated = False
        candidates: list[str] = []
        for remote in remotes:
            head = self._git(root, ["symbolic-ref", "--short", f"refs/remotes/{remote.name}/HEAD"])
            if head.startswith(remote.name + "/"):
                candidates.append(head.split("/", 1)[1])
        for value in (current, "main", "master"):
            if value and value not in candidates:
                if value in {"main", "master"}:
                    exists = self._git(root, ["show-ref", "--verify", f"refs/heads/{value}"])
                    if not exists:
                        continue
                candidates.append(value)
        tracked_raw = self._commands.run_bytes(
            ["git", "ls-files", "-z"], cwd=root, max_bytes=self.MAX_TRACKED_OUTPUT
        )
        tracked = [
            item.decode("utf-8", errors="strict") for item in tracked_raw.split(b"\0") if item
        ][: self.MAX_FILES_INSPECTED]
        manifests: list[ManifestFact] = []
        lockfiles: list[str] = []
        toolchains: list[str] = []
        scripts: set[str] = set()
        workspace_packages: set[str] = set()
        instructions: list[str] = []
        ci_files: list[str] = []
        policy_files: list[str] = []
        symlinks = large = binary = total_bytes = 0
        manifest_names = {
            "package.json",
            "pyproject.toml",
            "Cargo.toml",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
        }
        lock_names = {
            "pnpm-lock.yaml",
            "package-lock.json",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            "uv.lock",
            "poetry.lock",
            "Pipfile.lock",
            "Cargo.lock",
            "go.sum",
        }
        instruction_names = {
            "AGENTS.md",
            "CLAUDE.md",
            "CONTRIBUTING.md",
            "README.md",
            ".github/copilot-instructions.md",
            "docs/anatomy/README.md",
        }
        for relative in tracked:
            candidate = root / relative
            name = candidate.name
            depth = len(Path(relative).parts)
            if name in manifest_names and depth <= 5:
                kind = self._manifest_kind(candidate)
                if kind:
                    manager = kind[1]
                    manifest_scripts: tuple[str, ...] = ()
                    if name == "package.json":
                        package = self._json(candidate)
                        if package:
                            declared = package.get("packageManager")
                            if isinstance(declared, str):
                                toolchains.append(declared)
                                manager = declared.split("@", 1)[0]
                            raw_scripts = package.get("scripts")
                            if isinstance(raw_scripts, dict):
                                manifest_scripts = tuple(sorted(str(key) for key in raw_scripts))
                                scripts.update(manifest_scripts)
                            workspaces = package.get("workspaces")
                            if isinstance(workspaces, list):
                                workspace_packages.update(
                                    str(item) for item in workspaces if isinstance(item, str)
                                )
                            elif isinstance(workspaces, dict) and isinstance(
                                workspaces.get("packages"), list
                            ):
                                workspace_packages.update(
                                    str(item)
                                    for item in workspaces["packages"]
                                    if isinstance(item, str)
                                )
                    manifests.append(
                        ManifestFact(relative, kind[0], manager, depth == 1, manifest_scripts)
                    )
            if name in lock_names:
                lockfiles.append(relative)
            if relative in instruction_names:
                instructions.append(relative)
            if relative in {
                "repoforge.toml",
                ".repoforge.toml",
                ".repoforge/policy.toml",
                ".repoforge/config.toml",
            }:
                policy_files.append(relative)
            if (
                relative.startswith(".github/workflows/")
                or relative.startswith(".gitlab-ci")
                or name in {"Jenkinsfile", ".circleci"}
            ):
                ci_files.append(relative)
            try:
                if candidate.is_symlink():
                    symlinks += 1
                    continue
                if not candidate.is_file():
                    continue
                size = candidate.stat().st_size
                total_bytes += size
                if size >= self.LARGE_FILE_BYTES:
                    large += 1
                if size <= 8192:
                    try:
                        if b"\0" in candidate.read_bytes()[:8192]:
                            binary += 1
                    except OSError:
                        pass
            except OSError:
                continue
        make_targets = self._make_targets(root / "Makefile")
        submodules = (
            tuple(
                sorted(
                    re.findall(
                        r"^\s*path\s*=\s*(.+)$",
                        (root / ".gitmodules").read_text(encoding="utf-8", errors="ignore"),
                        re.MULTILINE,
                    )
                )
            )
            if (root / ".gitmodules").is_file()
            else ()
        )
        lfs_tracked = False
        attrs = root / ".gitattributes"
        if attrs.is_file() and attrs.stat().st_size <= 2_000_000:
            lfs_tracked = "filter=lfs" in attrs.read_text(encoding="utf-8", errors="ignore")
        shallow = self._git(root, ["rev-parse", "--is-shallow-repository"]) == "true"
        worktree_raw = self._git(root, ["worktree", "list", "--porcelain"])
        worktrees = tuple(
            sorted(
                line.removeprefix("worktree ")
                for line in worktree_raw.splitlines()
                if line.startswith("worktree ")
            )
        )
        warnings: list[str] = []
        if len(tracked) >= self.MAX_FILES_INSPECTED:
            warnings.append("Tracked-file scan reached its configured bound")
        return RepositoryFacts(
            root=root,
            common_dir=common_dir,
            repo_id=repo_id or slugify(root.name),
            display_name=root.name,
            current_branch=current or None,
            default_branch_candidates=tuple(candidates),
            remotes=remotes,
            manifests=tuple(sorted(manifests, key=lambda item: item.path)),
            lockfiles=tuple(sorted(lockfiles)),
            toolchain_declarations=tuple(sorted(set(toolchains))),
            scripts=tuple(sorted(scripts)),
            make_targets=make_targets,
            instruction_files=tuple(sorted(instructions)),
            ci_files=tuple(sorted(ci_files)),
            workspace_packages=tuple(sorted(workspace_packages)),
            submodules=submodules,
            lfs_tracked=lfs_tracked,
            shallow=shallow,
            detached=detached,
            symlink_count=symlinks,
            large_file_count=large,
            binary_file_count=binary,
            tracked_file_count=len(tracked),
            total_tracked_bytes=total_bytes,
            existing_worktrees=worktrees,
            policy_files=tuple(sorted(policy_files)),
            github_authenticated=github_authenticated,
            scan_truncated=len(tracked) >= self.MAX_FILES_INSPECTED,
            warnings=tuple(warnings),
        )
