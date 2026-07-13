from dataclasses import dataclass
from pathlib import Path
from ..context import ApplicationContext
from ...domain.errors import SecurityError


@dataclass(frozen=True, slots=True)
class WorkspaceSearchCommand:
    workspace_id: str
    query: str
    path_glob: str | None = None
    max_results: int = 200


@dataclass(frozen=True, slots=True)
class WorkspaceSearchResult:
    workspace_id: str
    query: str
    matches: list[str]
    truncated: bool


class WorkspaceSearcher:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx

    def execute(self, c: WorkspaceSearchCommand) -> WorkspaceSearchResult:
        _, repo, path = self.ctx.workspace(c.workspace_id)
        if not c.query or "\x00" in c.query:
            raise ValueError("query must be non-empty")
        if c.path_glob and (
            c.path_glob.startswith(("/", "-")) or ".." in Path(c.path_glob).parts
        ):
            raise SecurityError("Unsafe path_glob")
        limit = max(1, min(c.max_results, 2000))

        def op() -> WorkspaceSearchResult:
            matches, truncated = self.ctx.git.search(
                path, repo, c.query, c.path_glob, limit
            )
            return WorkspaceSearchResult(c.workspace_id, c.query, matches, truncated)

        return self.ctx.audited(
            "workspace_search",
            {"workspace_id": c.workspace_id, "path_glob": c.path_glob},
            op,
        )
