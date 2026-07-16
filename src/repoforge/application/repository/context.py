from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ...config import RepositoryConfig
from ...domain.diagnostic_packs import detect_ecosystems, ecosystem_diagnostic_packs
from ...domain.diagnostics import DiagnosticProfileConfig, DiagnosticSelectorConfig
from ...domain.errors import SecurityError
from ...domain.policy import assert_path_allowed
from ..context import ApplicationContext, repository_policy_snapshot
from ..profile_drift import ProfileDriftAssessor


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
    diagnostics: list[dict[str, Any]]
    diagnostic_pack_suggestions: list[dict[str, Any]]
    profile_drift: dict[str, Any]


def _selector_schema(selector: DiagnosticSelectorConfig) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "name": selector.name,
        "kind": selector.kind.value,
        "max_values": selector.max_values,
        "expansion": selector.expansion,
    }
    if selector.values:
        schema["values"] = list(selector.values)
    if selector.char_classes:
        schema["char_classes"] = list(selector.char_classes)
        schema["max_length"] = selector.max_length
    if selector.prefix is not None:
        schema["prefix"] = selector.prefix
    if selector.suffix is not None:
        schema["suffix"] = selector.suffix
    if selector.separator is not None:
        schema["separator"] = selector.separator
    if selector.allow_leading_dash:
        schema["allow_leading_dash"] = True
    return schema


def _diagnostic_schema(diagnostic: DiagnosticProfileConfig) -> dict[str, Any]:
    return {
        "diagnostic_id": diagnostic.diagnostic_id,
        "summary": diagnostic.summary,
        "mutability": diagnostic.mutability.value,
        "selectors": [
            _selector_schema(selector)
            for selector in diagnostic.selectors
            if selector.kind.value != "none"
        ],
    }


def _pack_suggestion_payload(root_files: list[str]) -> list[dict[str, Any]]:
    ecosystems = detect_ecosystems(tuple(root_files))
    return [
        {
            "ecosystem": pack.ecosystem,
            "diagnostic_id": pack.diagnostic_id,
            "summary": pack.summary,
            "config_snippet": pack.config_snippet,
        }
        for pack in ecosystem_diagnostic_packs(ecosystems)
    ]


class RepositoryContextReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: RepositoryContextCommand) -> RepositoryContextResult:
        repo = self.ctx.repo(c.repo_id)
        return self.ctx.audited(
            "repo_context", {"repo_id": c.repo_id}, lambda: self._build(c, repo)
        )

    def compute(self, c: RepositoryContextCommand) -> RepositoryContextResult:
        """Pure application logic with no audit event, for embedding in a larger audited bundle."""
        repo = self.ctx.repo(c.repo_id)
        return self._build(c, repo)

    def _build(
        self, c: RepositoryContextCommand, repo: RepositoryConfig
    ) -> RepositoryContextResult:
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
        root_files = self.ctx.git.root_files(repo.path, repo)
        diagnostics = [
            _diagnostic_schema(diagnostic) for _, diagnostic in sorted(repo.diagnostics.items())
        ]
        pack_suggestions = [] if diagnostics else _pack_suggestion_payload(root_files)
        config_identity = hashlib.sha256(self.ctx.config.source_path.read_bytes()).hexdigest()
        policy_hash = repository_policy_snapshot(repo).get("sha256")
        if not isinstance(policy_hash, str):
            raise RuntimeError("repository policy hash is unavailable")
        profile_drift = ProfileDriftAssessor().assess(
            repo,
            head_sha=self.ctx.git.head_sha(repo.path).lower(),
            config_identity=config_identity,
            policy_hash=policy_hash,
            source_dirty=bool(self.ctx.git.status_porcelain(repo.path)),
        )
        return RepositoryContextResult(
            c.repo_id,
            repo.display_name or c.repo_id,
            str(repo.path),
            repo.default_base,
            root_files,
            manager,
            engines,
            scripts,
            instructions,
            sorted(repo.profiles),
            repo.default_verification_profile,
            diagnostics,
            pack_suggestions,
            profile_drift.as_dict(),
        )
