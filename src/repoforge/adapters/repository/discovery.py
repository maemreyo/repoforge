"""Bounded, symlink-safe, Git-aware local repository discovery."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ...domain.errors import ConfigError
from ...domain.onboarding import DiscoveryIdentity
from ...ports.command import CommandExecutor
from ...ports.repository_discovery import DiscoveryRequest

_PRUNE_NAMES = {".git", "node_modules", ".venv", "venv", "vendor", ".cache"}


class LocalRepositoryDiscovery:
    def __init__(self, command: CommandExecutor):
        self._command = command

    def _git(self, cwd: Path, *args: str) -> tuple[int, str]:
        try:
            result = self._command.run(
                ["git", *args], cwd=cwd, check=False, timeout=10, output_limit=100_000
            )
        except Exception:
            return 1, ""
        return result.returncode, result.stdout.strip()

    @staticmethod
    def _resolve_git_path(root: Path, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (root / path).resolve()

    def _identity(self, candidate: Path) -> DiscoveryIdentity:
        code, bare_value = self._git(candidate, "rev-parse", "--is-bare-repository")
        if code != 0:
            return DiscoveryIdentity(
                str(candidate), str(candidate), "", True, False, "invalid_git_repository"
            )
        bare = bare_value == "true"
        if bare:
            return DiscoveryIdentity(str(candidate), str(candidate), str(candidate), True, True)
        top_code, top = self._git(candidate, "rev-parse", "--show-toplevel")
        common_code, common = self._git(candidate, "rev-parse", "--git-common-dir")
        dir_code, git_dir = self._git(candidate, "rev-parse", "--git-dir")
        if top_code or common_code or dir_code or not top or not common or not git_dir:
            return DiscoveryIdentity(
                str(candidate), str(candidate), "", True, False, "invalid_git_repository"
            )
        worktree = Path(top).resolve()
        common_path = self._resolve_git_path(worktree, common)
        git_path = self._resolve_git_path(worktree, git_dir)
        return DiscoveryIdentity(
            path=str(worktree),
            worktree_root=str(worktree),
            git_common_dir=str(common_path),
            primary=git_path == common_path,
            bare=False,
        )

    @staticmethod
    def _matches(path: Path, roots: tuple[Path, ...], patterns: tuple[str, ...]) -> bool:
        for root in roots:
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if any(
                fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(f"/{relative}", pattern)
                for pattern in patterns
            ):
                return True
        return False

    @staticmethod
    def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def discover(self, request: DiscoveryRequest) -> tuple[DiscoveryIdentity, ...]:
        roots = tuple(root.expanduser().resolve() for root in request.roots)
        managed = tuple(root.expanduser().resolve() for root in request.managed_workspace_roots)
        identities: dict[tuple[str, str], DiscoveryIdentity] = {}
        for root in roots:
            if not root.exists():
                raise ConfigError(f"DISCOVERY_ROOT_NOT_FOUND: {root}")
            if not root.is_dir():
                raise ConfigError(f"DISCOVERY_ROOT_NOT_FOUND: not a directory: {root}")
            stack: list[tuple[Path, int]] = [(root, 0)]
            while stack:
                current, depth = stack.pop()
                if current.is_symlink() or self._inside(current, managed):
                    continue
                if self._matches(current, roots, request.exclude):
                    continue
                marker = current / ".git"
                bare_marker = (current / "HEAD").is_file() and (current / "objects").is_dir()
                if marker.exists() or marker.is_symlink() or bare_marker:
                    identity = self._identity(current)
                    candidate_path = Path(identity.path)
                    if not request.include or self._matches(candidate_path, roots, request.include):
                        identities[(identity.worktree_root, identity.git_common_dir)] = identity
                if depth >= request.max_depth:
                    continue
                try:
                    entries = list(os.scandir(current))
                except PermissionError:
                    identities[(str(current), "")] = DiscoveryIdentity(
                        str(current), str(current), "", True, False, "unreadable_path"
                    )
                    continue
                except OSError:
                    continue
                for entry in reversed(sorted(entries, key=lambda item: item.name)):
                    if entry.name in _PRUNE_NAMES or entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
                    except OSError:
                        continue
        return tuple(sorted(identities.values(), key=lambda item: (item.path, item.git_common_dir)))
