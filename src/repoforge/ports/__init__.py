"""Protocols only; concrete implementations live under adapters/."""

from .approval_store import ApprovalPayloadStore, ApprovalStore
from .audit import AuditSink
from .background_tasks import BackgroundTaskRunner
from .capabilities import ExecutableLocator
from .clock import Clock
from .code_intelligence import CodeIntelligenceProvider
from .command import CommandExecutor, CommandResult
from .configuration import ConfigurationStore
from .execution_environment import (
    ApprovedExecution,
    ArtifactResult,
    ExecutionEnvironmentPort,
    ExecutionReceipt,
)
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
from .github_read_cache import GitHubReadCache
from .hygiene import (
    HygieneBaselineCache,
    HygieneCacheKey,
    HygieneFormatReceipt,
    HygieneGateway,
    HygieneInspection,
)
from .idempotency import IdempotencyStore
from .ids import IdGenerator
from .locking import LockManager
from .metrics import MetricsSink
from .onboarding_environment import EnvironmentPreflight, OnboardingEnvironment
from .onboarding_store import OnboardingStore
from .operation_gate import GateState, OperationGate
from .operation_result_store import OperationResultStore
from .operation_store import OperationRecordPage, OperationStore
from .operator_io import OperatorIO
from .pr_check_watch_store import PrCheckWatchPage, PrCheckWatchStore
from .process import ProcessInspector
from .provider_registry import ProviderRegistry
from .repository_discovery import DiscoveryRequest, RepositoryDiscovery
from .repository_probe import RepositoryProbe
from .runtime_control import (
    RuntimeControlClient,
    RuntimeControlServer,
    RuntimeLauncher,
    RuntimeStore,
)
from .sleeper import Sleeper
from .state_repository import StateRepository
from .task_store import TaskStore
from .ticket_graph import TicketGraphGateway
from .ticket_project import TicketProjectGateway
from .tunnel import TunnelClient, TunnelProfileStore
from .workflow_recording_store import (
    WorkflowRecordingPage,
    WorkflowRecordingStore,
    WorkflowRetentionReport,
)
from .workflow_replay import (
    WorkflowReplayAdapter,
    WorkflowReplayDecision,
    WorkflowReplayObservation,
)
from .workspace_store import WorkspaceStore

__all__ = [
    "ApprovalPayloadStore",
    "ApprovalStore",
    "ApprovedExecution",
    "ArtifactResult",
    "AuditSink",
    "BackgroundTaskRunner",
    "Clock",
    "CodeIntelligenceProvider",
    "CommandExecutor",
    "CommandResult",
    "ConfigurationStore",
    "DiscoveryRequest",
    "EnvironmentPreflight",
    "ExecutableLocator",
    "ExecutionEnvironmentPort",
    "ExecutionReceipt",
    "FileSystem",
    "GateState",
    "GitBaseReferences",
    "GitHubActionsJob",
    "GitHubActionsStep",
    "GitHubCheckAnnotation",
    "GitHubCheckRun",
    "GitHubJobLog",
    "GitHubReadCache",
    "GitMergePreview",
    "GitMergeResult",
    "GitRepository",
    "GitSnapshotBlob",
    "HygieneBaselineCache",
    "HygieneCacheKey",
    "HygieneFormatReceipt",
    "HygieneGateway",
    "HygieneInspection",
    "IdGenerator",
    "IdempotencyStore",
    "LockManager",
    "MetricsSink",
    "OnboardingEnvironment",
    "OnboardingStore",
    "OperationGate",
    "OperationRecordPage",
    "OperationResultStore",
    "OperationStore",
    "OperatorIO",
    "PrCheckWatchPage",
    "PrCheckWatchStore",
    "ProcessInspector",
    "ProviderRegistry",
    "PullRequestGateway",
    "RepositoryDiscovery",
    "RepositoryProbe",
    "ResolvedRepositoryRef",
    "RuntimeControlClient",
    "RuntimeControlServer",
    "RuntimeLauncher",
    "RuntimeStore",
    "Sleeper",
    "StateRepository",
    "TaskStore",
    "TicketGraphGateway",
    "TicketProjectGateway",
    "TunnelClient",
    "TunnelProfileStore",
    "WorkflowRecordingPage",
    "WorkflowRecordingStore",
    "WorkflowReplayAdapter",
    "WorkflowReplayDecision",
    "WorkflowReplayObservation",
    "WorkflowRetentionReport",
    "WorkspaceStore",
]
