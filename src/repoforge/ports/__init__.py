"""Protocols only; concrete implementations live under adapters/."""

from .audit import AuditSink
from .capabilities import ExecutableLocator
from .clock import Clock
from .command import CommandExecutor, CommandResult
from .configuration import ConfigurationStore
from .filesystem import FileSystem
from .git import GitRepository
from .github import PullRequestGateway
from .ids import IdGenerator
from .locking import LockManager
from .operation_gate import GateState, OperationGate
from .process import ProcessInspector
from .repository_probe import RepositoryProbe
from .runtime_control import (
    RuntimeControlClient,
    RuntimeControlServer,
    RuntimeLauncher,
    RuntimeStore,
)
from .tunnel import TunnelClient, TunnelProfileStore
from .workspace_store import WorkspaceStore

__all__ = [
    "AuditSink",
    "Clock",
    "CommandExecutor",
    "CommandResult",
    "ConfigurationStore",
    "ExecutableLocator",
    "FileSystem",
    "GateState",
    "GitRepository",
    "IdGenerator",
    "LockManager",
    "OperationGate",
    "ProcessInspector",
    "PullRequestGateway",
    "RepositoryProbe",
    "RuntimeControlClient",
    "RuntimeControlServer",
    "RuntimeLauncher",
    "RuntimeStore",
    "TunnelClient",
    "TunnelProfileStore",
    "WorkspaceStore",
]
