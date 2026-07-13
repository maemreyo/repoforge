from __future__ import annotations

from dataclasses import dataclass

from ..context import ApplicationContext


@dataclass(frozen=True, slots=True)
class RepositoryListCommand:
    pass


@dataclass(frozen=True, slots=True)
class RepositoryListResult:
    repositories: list[dict[str, object]]


class RepositoryLister:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, command: RepositoryListCommand) -> RepositoryListResult:
        repositories: list[dict[str, object]] = []
        for repo in self.ctx.config.repositories.values():
            entry: dict[str, object] = {
                "repo_id": repo.repo_id,
                "display_name": repo.display_name or repo.repo_id,
                "path": str(repo.path),
                "remote": repo.remote,
                "default_base": repo.default_base,
                "allowed_base_branches": list(repo.allowed_base_branches),
                "branch_prefix": repo.branch_prefix,
                "read_only": repo.read_only,
                "publish_enabled": repo.publish_enabled,
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
                        "description": p.description,
                        "verification": p.verification,
                        "commands": [list(c) for c in p.commands],
                        "working_directory": p.working_directory,
                    }
                    for name, p in repo.profiles.items()
                },
            }
            repositories.append(entry)
        return RepositoryListResult(repositories)
