from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ...domain.errors import SecurityError
from ...domain.policy import assert_path_allowed
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RepositoryContextCommand:
    repo_id: str


@dataclass(frozen=True, slots=True)
class RepositoryContextResult:
    repo_id: str
    display_name: str
    path: str
    default_base: str
    root_files: list[str]
    package_manager: str | None
    engines: dict[str, Any]
    scripts: dict[str, str]
    instruction_files: list[dict[str, Any]]
    profiles: list[str]
    default_verification_profile: str | None


class RepositoryContextReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryContextCommand) -> RepositoryContextResult:
        repo = self.ctx.repo(c.repo_id)

        def op() -> RepositoryContextResult:
            package = None
            package_path = repo.path / "package.json"
            if (
                package_path.is_file()
                and package_path.stat().st_size <= self.ctx.config.server.max_file_bytes
            ):
                try:
                    value = json.loads(package_path.read_text(encoding="utf-8"))
                    package = value if isinstance(value, dict) else None
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
            instructions = []
            for relative in (
                "AGENTS.md",
                "CLAUDE.md",
                "CONTRIBUTING.md",
                "README.md",
                ".github/copilot-instructions.md",
                "docs/anatomy/README.md",
            ):
                try:
                    assert_path_allowed(relative, repo)
                except SecurityError:
                    continue
                path = repo.path / relative
                if not path.is_file() or path.is_symlink():
                    continue
                size = path.stat().st_size
                if size > self.ctx.config.server.max_file_bytes:
                    instructions.append({"path": relative, "size_bytes": size, "preview": None})
                    continue
                data = path.read_bytes()
                if b"\x00" in data:
                    continue
                preview = data.decode("utf-8", errors="replace")[:8000]
                instructions.append(
                    {
                        "path": relative,
                        "size_bytes": size,
                        "preview": preview,
                        "preview_truncated": size > len(preview.encode("utf-8")),
                    }
                )
            scripts = {}
            manager = None
            engines = {}
            if package:
                raw = package.get("scripts")
                scripts = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
                declared = package.get("packageManager")
                manager = str(declared) if declared is not None else None
                raw_engines = package.get("engines")
                engines = raw_engines if isinstance(raw_engines, dict) else {}
            return RepositoryContextResult(
                c.repo_id,
                repo.display_name or c.repo_id,
                str(repo.path),
                repo.default_base,
                self.ctx.git.root_files(repo.path, repo),
                manager,
                engines,
                scripts,
                instructions,
                sorted(repo.profiles),
                repo.default_verification_profile,
            )

        return self.ctx.audited("repo_context", {"repo_id": c.repo_id}, op)
