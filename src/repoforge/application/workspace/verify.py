from dataclasses import dataclass
from typing import Any
from ..context import ApplicationContext
from ...domain.verification import select_verification_profile
from .run_profile import WorkspaceRunProfileCommand, WorkspaceProfileRunner


@dataclass(frozen=True, slots=True)
class WorkspaceVerifyCommand:
    workspace_id: str
    profile_name: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceVerifyResult:
    payload: dict[str, Any]


class WorkspaceVerifier:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx
        self.runner = WorkspaceProfileRunner(ctx)

    def execute(self, c: WorkspaceVerifyCommand) -> WorkspaceVerifyResult:
        record, repo, _ = self.ctx.workspace(c.workspace_id)
        profile, used_default = select_verification_profile(repo, c.profile_name)
        r = self.runner.execute(
            WorkspaceRunProfileCommand(c.workspace_id, profile.name)
        )
        return WorkspaceVerifyResult(
            {
                "workspace_id": r.workspace_id,
                "profile": r.profile,
                "description": r.description,
                "verification": r.verification,
                "fingerprint": r.fingerprint,
                "commands": r.commands,
                "change_metrics": r.change_metrics,
                "satisfies_commit_gate": r.satisfies_commit_gate,
                "used_default": used_default,
                "repo_id": record.repo_id,
            }
        )
