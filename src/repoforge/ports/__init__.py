"""Protocols only; concrete implementations live under adapters/."""

from .audit import AuditSink
from .capabilities import ExecutableLocator
from .clock import Clock
from .command import CommandExecutor, CommandResult
from .configuration import ConfigurationStore
from .filesystem import FileSystem
from .git import GitRepository
from .github import PullRequestGateway
from .idempotency import IdempotencyStore
from .ids import IdGenerator
from .locking import LockManager
from .metrics import MetricsSink
from .onboarding_environment import EnvironmentPreflight, OnboardingEnvironment
from .onboarding_store import OnboardingStore
from .operation_gate import GateState, OperationGate
from .operator_io import OperatorIO
from .process import ProcessInspector
from .repository_discovery import DiscoveryRequest, RepositoryDiscovery
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
    "DiscoveryRequest",
    "EnvironmentPreflight",
    "ExecutableLocator",
    "FileSystem",
    "GateState",
    "GitRepository",
    "IdGenerator",
    "IdempotencyStore",
    "LockManager",
    "MetricsSink",
    "OnboardingEnvironment",
    "OnboardingStore",
    "OperationGate",
    "OperatorIO",
    "ProcessInspector",
    "PullRequestGateway",
    "RepositoryDiscovery",
    "RepositoryProbe",
    "RuntimeControlClient",
    "RuntimeControlServer",
    "RuntimeLauncher",
    "RuntimeStore",
    "TunnelClient",
    "TunnelProfileStore",
    "WorkspaceStore",
]
