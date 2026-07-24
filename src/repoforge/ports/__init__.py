"""Protocols only; concrete implementations live under adapters/."""

from .approval_store import ApprovalPayloadStore, ApprovalStore
from .audit import AuditSink
from .background_tasks import BackgroundTaskRunner
from .capabilities import ExecutableLocator
from .clock import Clock
from .code_intelligence import CodeIntelligenceProvider
from .command import CommandExecutor, CommandResult
from .configuration import ConfigurationStore
from .effect_receipt_store import EffectReceiptStore
from .execution_environment import (
    ApprovedExecution,
    ArtifactResult,
    EnvironmentInspection,
    ExecutionEnvironmentPort,
    ExecutionReceipt,
    ExecutionRequest,
    PreparedEnvironmentSession,
)
from .execution_plan_store import ExecutionPlanAcceptanceStore, ExecutionPlanStore
from .execution_receipt_store import ExecutionReceiptStore
from .failure_evidence_store import FailureEvidencePage, FailureEvidenceStore
from .failure_output_artifact_store import FailureOutputArtifact, FailureOutputArtifactStore
from .file_transactions import FileTransaction, FileTransactionFactory
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
from .github_capabilities import GitHubCapabilityProbe
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
from .iteration_cache import IterationCache
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
from .process_reaper import ProcessReaper, ReapOutcome
from .provider_registry import ProviderRegistry
from .repository_discovery import DiscoveryRequest, RepositoryDiscovery
from .repository_probe import RepositoryProbe
from .runtime_activation_store import RuntimeActivationStore
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
from .worker_binding_store import WorkerBindingStore
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
    "EffectReceiptStore",
    "EnvironmentInspection",
    "EnvironmentPreflight",
    "ExecutableLocator",
    "ExecutionEnvironmentPort",
    "ExecutionPlanAcceptanceStore",
    "ExecutionPlanStore",
    "ExecutionReceipt",
    "ExecutionReceiptStore",
    "ExecutionRequest",
    "FailureEvidencePage",
    "FailureEvidenceStore",
    "FailureOutputArtifact",
    "FailureOutputArtifactStore",
    "FileSystem",
    "FileTransaction",
    "FileTransactionFactory",
    "GateState",
    "GitBaseReferences",
    "GitHubActionsJob",
    "GitHubActionsStep",
    "GitHubCapabilityProbe",
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
    "IterationCache",
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
    "PreparedEnvironmentSession",
    "ProcessInspector",
    "ProcessReaper",
    "ProviderRegistry",
    "PullRequestGateway",
    "ReapOutcome",
    "RepositoryDiscovery",
    "RepositoryProbe",
    "ResolvedRepositoryRef",
    "RuntimeActivationStore",
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
    "WorkerBindingStore",
    "WorkflowRecordingPage",
    "WorkflowRecordingStore",
    "WorkflowReplayAdapter",
    "WorkflowReplayDecision",
    "WorkflowReplayObservation",
    "WorkflowRetentionReport",
    "WorkspaceStore",
]
