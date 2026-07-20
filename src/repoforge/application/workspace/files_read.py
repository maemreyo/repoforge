from dataclasses import dataclass
from typing import Any

from ...domain.errors import SecurityError, WorkspaceError
from ..context import ApplicationContext
from .file_read import WorkspaceFileReadCommand, WorkspaceFileReader


@dataclass(frozen=True, slots=True)
class WorkspaceFilesReadCommand:
    workspace_id: str
    relative_paths: list[str]
    start_line: int = 1
    end_line: int = 500


@dataclass(frozen=True, slots=True)
class WorkspaceFilesReadResult:
    workspace_id: str
    files: list[dict[str, Any]]
    errors: list[dict[str, str]]
    requested: int
    succeeded: int


class WorkspaceFilesReader:
    def __init__(self, ctx: ApplicationContext):
        self.ctx = ctx
        self.reader = WorkspaceFileReader(ctx)

    def execute(self, c: WorkspaceFilesReadCommand) -> WorkspaceFilesReadResult:
        if not c.relative_paths:
            raise WorkspaceError("relative_paths must contain at least one path")
        if len(c.relative_paths) > self.ctx.config.server.max_batch_files:
            raise WorkspaceError(
                f"relative_paths exceeds max_batch_files={self.ctx.config.server.max_batch_files}"
            )
        unique = list(dict.fromkeys(c.relative_paths))

        def op() -> WorkspaceFilesReadResult:
            files = []
            errors = []
            for path in unique:
                try:
                    r = self.reader.execute(
                        WorkspaceFileReadCommand(c.workspace_id, path, c.start_line, c.end_line)
                    )
                    files.append(
                        {
                            "workspace_id": r.workspace_id,
                            "path": r.path,
                            "sha256": r.sha256,
                            "size_bytes": r.size_bytes,
                            "total_lines": r.total_lines,
                            "start_line": r.start_line,
                            "end_line": r.end_line,
                            "content": r.content,
                            "truncated": r.truncated,
                        }
                    )
                except (WorkspaceError, SecurityError, ValueError) as exc:
                    errors.append(
                        {
                            "path": path,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
            # Batching is the efficient pattern the read_file-repetition nudge exists to
            # steer callers toward, so using it here always clears this workspace's
            # single-file-read tracking -- both any drift the internal per-file reads
            # above just caused, and any progress accumulated by prior single reads.
            if self.ctx.nudge_tracker is not None:
                self.ctx.nudge_tracker.reset_file_reads(c.workspace_id)
            return WorkspaceFilesReadResult(c.workspace_id, files, errors, len(unique), len(files))

        return self.ctx.audited(
            "workspace_read_files",
            {"workspace_id": c.workspace_id, "file_count": len(unique)},
            op,
        )
