"""Stable application ports; no concrete adapter imports."""

from .audit import AuditSink
from .clock import Clock
from .capabilities import ExecutableLocator
from .command import CommandExecutor, CommandResult
from .filesystem import FileSystem
from .git import GitRepository
from .github import PullRequestGateway
from .ids import IdGenerator
from .workspace_store import WorkspaceStore

__all__ = [
    "AuditSink",
    "Clock",
    "CommandExecutor",
    "CommandResult",
    "ExecutableLocator",
    "FileSystem",
    "GitRepository",
    "IdGenerator",
    "PullRequestGateway",
    "WorkspaceStore",
]
