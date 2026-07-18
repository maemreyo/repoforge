from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.redaction import redact_text
from ...domain.workspace_removal import order_candidates
from ..context import ApplicationContext
from ..workspace.removal_safety import MAX_NUDGE_CANDIDATES, compute_removal_candidates

#: Per-workspace file-count bound for the doctor disk-usage walk, so a
#: pathologically large worktree cannot make `doctor` itself slow.
_MAX_DISK_USAGE_FILES = 20_000


@dataclass(frozen=True, slots=True)
class DoctorCommand:
    pass


@dataclass(frozen=True, slots=True)
class DoctorResult:
    ok: bool
    summary: dict[str, int]
    checks: list[dict[str, Any]]
    audit_log: str
    workspaces: dict[str, Any]


def _directory_size(path: Path, *, max_files: int = _MAX_DISK_USAGE_FILES) -> tuple[int, bool]:
    total = 0
    count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            count += 1
            if count > max_files:
                return total, True
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total, False


class Doctor:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: DoctorCommand) -> DoctorResult:
        checks = []
        environment = self.ctx.commands.environment()
        secrets = tuple(value for value in (environment.get("CONTROL_PLANE_API_KEY", ""),) if value)

        def add(
            name: str,
            ok: bool,
            detail: str,
            *,
            severity: str = "error",
            remediation: str | None = None,
        ) -> None:
            item = {
                "name": name,
                "ok": ok,
                "severity": severity,
                "detail": redact_text(str(detail), secrets=secrets),
            }
            if remediation:
                item["remediation"] = redact_text(remediation, secrets=secrets)
            checks.append(item)

        add("config", True, str(self.ctx.config.source_path), severity="info")
        paths = {}
        search_path = environment.get("PATH")
        remediation_by_executable = {
            "git": "Install Git with Xcode Command Line Tools or Homebrew.",
            "gh": "Install GitHub CLI with `brew install gh`.",
            "tunnel-client": "Install and authenticate the supported tunnel client before runtime start.",
        }
        for executable in ("git", "gh", "tunnel-client"):
            found = self.ctx.executables.which(executable, path=search_path)
            paths[executable] = found
            add(
                f"executable:{executable}",
                bool(found),
                found or "not found",
                severity="warning" if executable == "tunnel-client" else "error",
                remediation=remediation_by_executable[executable],
            )
            if found:
                try:
                    add(
                        f"version:{executable}",
                        True,
                        self.ctx.commands.run(
                            [executable, "--version"], cwd=Path.home()
                        ).stdout.splitlines()[0],
                        severity="info",
                    )
                except Exception as exc:
                    add(f"version:{executable}", False, str(exc), severity="warning")
        if paths.get("gh"):
            try:
                ok, detail = self.ctx.github.auth_status(Path.home())
                add(
                    "gh_auth",
                    ok,
                    detail,
                    remediation="Run `gh auth login`, then `gh auth setup-git`.",
                )
            except Exception as exc:
                add("gh_auth", False, str(exc), remediation="Run `gh auth login`.")
        for repo_id, repo in self.ctx.config.repositories.items():
            valid = repo.path.is_dir() and (repo.path / ".git").exists()
            add(
                f"repository:{repo_id}",
                valid,
                str(repo.path),
                remediation=f"Update repositories.{repo_id}.path in {self.ctx.config.source_path}.",
            )
            if not valid:
                continue
            try:
                if not self.ctx.git.is_worktree(repo.path):
                    raise RuntimeError("not a Git working tree")
                add(f"repository_git:{repo_id}", True, "valid Git working tree")
            except Exception as exc:
                add(f"repository_git:{repo_id}", False, str(exc))
                continue
            if paths.get("gh") and self.ctx.github_capabilities is not None:
                try:
                    report = self.ctx.github_capabilities.probe(repo.path, repo.ticket_graph)
                except Exception as exc:
                    add(f"github_capabilities:{repo_id}", False, str(exc), severity="warning")
                else:
                    for result in report.results:
                        if result.state.value == "unavailable":
                            add(
                                f"github_capability:{repo_id}:{result.capability.value}",
                                False,
                                result.detail,
                                severity="warning",
                                remediation=result.remediation,
                            )
                        else:
                            add(
                                f"github_capability:{repo_id}:{result.capability.value}",
                                True,
                                result.detail,
                                severity="info",
                            )
            current = self.ctx.git.current_branch(repo.path)
            add(
                f"repository_branch:{repo_id}",
                True,
                current or "detached HEAD",
                severity="info",
            )
            dirty = bool(self.ctx.git.status_porcelain(repo.path).strip())
            add(
                f"repository_clean:{repo_id}",
                not dirty,
                "clean" if not dirty else "source clone has uncommitted changes",
                severity="warning",
                remediation="Commit/stash source-clone changes before creating new workspaces.",
            )
            remote = self.ctx.git.remote_url(repo.path, repo.remote)
            add(
                f"repository_remote:{repo_id}",
                remote.returncode == 0,
                remote.combined,
                remediation=f"Configure Git remote {repo.remote!r}.",
            )
            base = self.ctx.git.verify_base(repo.path, repo.remote, repo.default_base)
            add(
                f"repository_base:{repo_id}",
                base.returncode == 0,
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
                    manager_value = package.get("packageManager")
                    if isinstance(manager_value, str) and "@" in manager_value:
                        manager, expected = manager_value.split("@", 1)
                        found = self.ctx.executables.which(manager, path=search_path)
                        add(
                            f"package_manager:{repo_id}:{manager}",
                            bool(found),
                            found or "not found",
                            remediation=f"Enable/install {manager} {expected}; for Node projects try `corepack enable`.",
                        )
                        if found:
                            actual = self.ctx.commands.run(
                                [manager, "--version"], cwd=repo.path
                            ).stdout.strip()
                            add(
                                f"package_manager_version:{repo_id}:{manager}",
                                actual == expected,
                                f"expected {expected}, found {actual}",
                                severity="warning",
                                remediation=f"Use the version declared by packageManager: {manager_value}.",
                            )
                    engines = package.get("engines")
                    if isinstance(engines, dict) and isinstance(engines.get("node"), str):
                        node = self.ctx.executables.which("node", path=search_path)
                        add(
                            f"runtime:{repo_id}:node",
                            bool(node),
                            node or "not found",
                            remediation=f"Install Node {engines['node']}.",
                        )
            seen = set()
            for profile in repo.profiles.values():
                for command in profile.commands:
                    key = (profile.name, command[0])
                    if key in seen:
                        continue
                    seen.add(key)
                    found = self.ctx.executables.which(command[0], path=search_path)
                    add(
                        f"profile_executable:{repo_id}:{profile.name}:{command[0]}",
                        bool(found),
                        found or "not found",
                        remediation=f"Install {command[0]} or update the configured profile command.",
                    )
            for diagnostic in repo.diagnostics.values():
                executable = diagnostic.argv_template[0]
                found = self.ctx.executables.which(executable, path=search_path)
                add(
                    f"diagnostic_executable:{repo_id}:{diagnostic.diagnostic_id}:{executable}",
                    bool(found),
                    found or "not found",
                    remediation=(
                        f"Install {executable} or update the reviewed diagnostic profile."
                    ),
                )
        for name, path in (
            ("workspace_root_writable", self.ctx.config.server.workspace_root),
            ("state_root_writable", self.ctx.config.server.state_root),
        ):
            try:
                self.ctx.filesystem.mkdir(path)
                probe = path / f".write-test-{os.getpid()}"
                self.ctx.filesystem.write_bytes_atomic(probe, b"ok", preserve_mode=False)
                self.ctx.filesystem.unlink(probe)
                add(name, True, str(path))
            except OSError as exc:
                add(name, False, str(exc))
        errors = [x for x in checks if not x["ok"] and x["severity"] == "error"]
        warnings = [x for x in checks if not x["ok"] and x["severity"] == "warning"]

        records = self.ctx.store.list()
        disk_usage_bytes = 0
        disk_usage_truncated = False
        existing_on_disk = 0
        for record in records:
            path = Path(record.path)
            if not path.is_dir():
                continue
            existing_on_disk += 1
            size, truncated = _directory_size(path)
            disk_usage_bytes += size
            disk_usage_truncated = disk_usage_truncated or truncated
        removable = order_candidates(tuple(compute_removal_candidates(self.ctx, records)))[
            :MAX_NUDGE_CANDIDATES
        ]
        workspaces_section = {
            "count": len(records),
            "existing_on_disk": existing_on_disk,
            "disk_usage_bytes": disk_usage_bytes,
            "disk_usage_bytes_truncated": disk_usage_truncated,
            "removable_candidates": [
                {
                    "workspace_id": item.workspace_id,
                    "age_seconds": item.age_seconds,
                    "pr_state": item.pr_state,
                }
                for item in removable
            ],
        }

        return DoctorResult(
            not errors,
            {
                "passed": sum(1 for x in checks if x["ok"]),
                "errors": len(errors),
                "warnings": len(warnings),
                "total": len(checks),
            },
            checks,
            str(self.ctx.audit.path),
            workspaces_section,
        )
