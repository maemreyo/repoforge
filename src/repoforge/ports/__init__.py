"""Protocols only; concrete implementations live under adapters/."""

from .audit import AuditSink
from .background_tasks import BackgroundTaskRunner
from .capabilities import ExecutableLocator
from .clock import Clock
from .command import CommandExecutor, CommandResult
from .configuration import ConfigurationStore
from .filesystem import FileSystem
from .git import (
    GitBaseReferences,
    GitMergePreview,
    GitMergeResult,
    GitRepository,
    GitSnapshotBlob,
    ResolvedRepositoryRef,
)
from .github import (
    GitHubActionsJob,
    GitHubActionsStep,
    GitHubCheckAnnotation,
    GitHubCheckRun,
    GitHubJobLog,
    PullRequestGateway,
)
from .idempotency import IdempotencyStore
from .ids import IdGenerator
from .locking import LockManager
from .metrics import MetricsSink
from .onboarding_environment import EnvironmentPreflight, OnboardingEnvironment
from .onboarding_store import OnboardingStore
from .operation_gate import GateState, OperationGate
from .operation_store import OperationRecordPage, OperationStore
from .operator_io import OperatorIO
from .pr_check_watch_store import PrCheckWatchPage, PrCheckWatchStore
from .process import ProcessInspector
from .repository_discovery import DiscoveryRequest, RepositoryDiscovery
from .repository_probe import RepositoryProbe
from .runtime_control import (
    RuntimeControlClient,
    RuntimeControlServer,
    RuntimeLauncher,
    RuntimeStore,
)
from .sleeper import Sleeper
from .tunnel import TunnelClient, TunnelProfileStore
from .workspace_store import WorkspaceStore

__all__ = [
    "AuditSink",
    "BackgroundTaskRunner",
    "Clock",
    "CommandExecutor",
    "CommandResult",
    "ConfigurationStore",
    "DiscoveryRequest",
    "EnvironmentPreflight",
    "ExecutableLocator",
    "FileSystem",
    "GateState",
    "GitBaseReferences",
    "GitHubActionsJob",
    "GitHubActionsStep",
    "GitHubCheckAnnotation",
    "GitHubCheckRun",
    "GitHubJobLog",
    "GitMergePreview",
    "GitMergeResult",
    "GitRepository",
    "GitSnapshotBlob",
    "IdGenerator",
    "IdempotencyStore",
    "LockManager",
    "MetricsSink",
    "OnboardingEnvironment",
    "OnboardingStore",
    "OperationGate",
    "OperationRecordPage",
    "OperationStore",
    "OperatorIO",
    "PrCheckWatchPage",
    "PrCheckWatchStore",
    "ProcessInspector",
    "PullRequestGateway",
    "RepositoryDiscovery",
    "RepositoryProbe",
    "ResolvedRepositoryRef",
    "RuntimeControlClient",
    "RuntimeControlServer",
    "RuntimeLauncher",
    "RuntimeStore",
    "Sleeper",
    "TunnelClient",
    "TunnelProfileStore",
    "WorkspaceStore",
]
