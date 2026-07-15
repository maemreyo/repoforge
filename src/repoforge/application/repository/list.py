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
        details: dict[str, object] = {}

        def op() -> RepositoryListResult:
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
                    "resource_budget": {
                        "max_cpu_seconds_per_operation": repo.resource_budget.max_cpu_seconds_per_operation,
                        "max_memory_bytes": repo.resource_budget.max_memory_bytes,
                        "max_disk_bytes": repo.resource_budget.max_disk_bytes,
                        "max_subprocesses": repo.resource_budget.max_subprocesses,
                        "max_concurrent_operations": repo.resource_budget.max_concurrent_operations,
                        "max_queued_operations": repo.resource_budget.max_queued_operations,
                        "max_network_bytes": repo.resource_budget.max_network_bytes,
                        "max_output_bytes": repo.resource_budget.max_output_bytes,
                        "task_ttl_seconds": repo.resource_budget.task_ttl_seconds,
                        "max_cache_bytes": repo.resource_budget.max_cache_bytes,
                        "max_index_bytes": repo.resource_budget.max_index_bytes,
                        "max_provider_requests": repo.resource_budget.max_provider_requests,
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
                    "diagnostics": {
                        name: {
                            "summary": diagnostic.summary,
                            "selector_kind": diagnostic.selector.kind.value,
                            "mutability": diagnostic.mutability.value,
                            "parser": diagnostic.parser.value,
                            "network_policy": diagnostic.network_policy.value,
                            "timeout_seconds": diagnostic.timeout_seconds,
                            "output_limit": diagnostic.output_limit,
                            "artifact_paths": list(diagnostic.artifact_paths),
                        }
                        for name, diagnostic in repo.diagnostics.items()
                    },
                }
                repositories.append(entry)
            details["repo_count"] = len(repositories)
            return RepositoryListResult(repositories)

        return self.ctx.audited("repo_list", details, op)
