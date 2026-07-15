import hashlib
from dataclasses import dataclass
from typing import Any

from ...domain.errors import SecurityError, WorkspaceError
from ...domain.policy import normalize_relative_path
from ...domain.verification import get_profile
from ...domain.workspace import VerificationReceipt
from ...ports.command import CommandResult
from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileCommand:
    workspace_id: str
    profile_name: str


@dataclass(frozen=True, slots=True)
class WorkspaceRunProfileResult:
    workspace_id: str
    profile: str
    description: str
    verification: bool
    fingerprint: str
    commands: list[dict[str, Any]]
    change_metrics: dict[str, Any]
    satisfies_commit_gate: bool
    working_directory: str | None = None


class WorkspaceProfileRunner:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    @staticmethod
    def public(r: CommandResult) -> dict[str, Any]:
        return {
            "argv": list(r.argv),
            "returncode": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }

    @staticmethod
    def receipt(r: CommandResult) -> dict[str, Any]:
        return {
            "argv": list(r.argv),
            "returncode": r.returncode,
            "stdout_sha256": hashlib.sha256(r.stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(r.stderr.encode()).hexdigest(),
        }

    def execute(self, c: WorkspaceRunProfileCommand) -> WorkspaceRunProfileResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        profile = get_profile(repo, c.profile_name)
        command_cwd = path
        if profile.working_directory:
            relative = normalize_relative_path(profile.working_directory)
            unresolved = path / relative
            if unresolved.is_symlink():
                raise SecurityError("Profile working_directory cannot be a symlink")
            command_cwd = unresolved.resolve(strict=False)
            try:
                command_cwd.relative_to(path.resolve(strict=True))
            except ValueError as exc:
                raise SecurityError("Profile working_directory escapes workspace") from exc
            if not command_cwd.is_dir():
                raise WorkspaceError(
                    f"Profile working_directory does not exist: {profile.working_directory}"
                )

        def op() -> WorkspaceRunProfileResult:
            with self.ctx.locks.lock(c.workspace_id):
                fresh = self.ctx.store.load(c.workspace_id)
                timeout = (
                    profile.timeout_seconds or self.ctx.config.server.verification_timeout_seconds
                )
                results = [
                    (
                        self.ctx.execution_environment.execute(
                            command,
                            cwd=command_cwd,
                            timeout=timeout,
                        ).result
                        if self.ctx.execution_environment is not None
                        else self.ctx.commands.run(command, cwd=command_cwd, timeout=timeout)
                    )
                    for command in profile.commands
                ]
                self.ctx.git.changed_paths(path, repo)
                metrics = self.ctx.git.enforce_change_budget(path, repo)
                fp = self.ctx.git.fingerprint(path)
                if profile.verification:
                    environment_identity_hash = (
                        self.ctx.execution_environment.identity(cwd=path).identity_hash
                        if self.ctx.execution_environment is not None
                        else None
                    )
                    fresh.last_verification = VerificationReceipt(
                        profile.name,
                        fp,
                        self.ctx.clock.now_iso(),
                        [self.receipt(r) for r in results],
                        environment_identity_hash,
                    )
                    self.ctx.store.save(fresh)
                return WorkspaceRunProfileResult(
                    c.workspace_id,
                    profile.name,
                    profile.description,
                    profile.verification,
                    fp,
                    [self.public(r) for r in results],
                    metrics,
                    profile.verification,
                    profile.working_directory,
                )

        return self.ctx.audited(
            "workspace_run_profile",
            {"workspace_id": c.workspace_id, "profile": c.profile_name},
            op,
        )
