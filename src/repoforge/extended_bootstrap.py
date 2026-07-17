"""Extended composition root layered over the landed production bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

from .adapters.audit import JsonlAuditSink
from .adapters.background import SystemSleeper, ThreadBackgroundTaskRunner
from .adapters.capabilities import SystemExecutableLocator
from .adapters.code_intelligence import SyntaxCodeIntelligenceProvider
from .adapters.execution.native import NativeReviewedAdapter
from .adapters.filesystem import JournaledFileTransactionFactory, LocalFileSystem
from .adapters.filesystem.receipt_transaction_factory import (
    ReceiptJournaledFileTransactionFactory,
)
from .adapters.git import GitCliRepository
from .adapters.github import CommandGitHubTicketGraphGateway, GhCliGateway
from .adapters.github.ticket_project import GhTicketProjectGateway
from .adapters.hygiene import CommandHygieneGateway
from .adapters.locking import FcntlLockManager
from .adapters.observability import JsonMetricsSink
from .adapters.persistence import (
    JsonApprovalPayloadStore,
    JsonApprovalStore,
    JsonExternalMutationLedger,
    JsonGitHubReadCache,
    JsonHygieneBaselineCache,
    JsonIdempotencyStore,
    JsonOperationResultStore,
    JsonOperationStore,
    JsonPrCheckWatchStore,
    JsonWorkflowRecordingStore,
    JsonWorkspaceStore,
)
from .adapters.provider.config_registry import ConfigProviderRegistry
from .adapters.runtime import InProcessOperationGate
from .adapters.subprocess import SubprocessCommandExecutor
from .adapters.system import SystemClock, UuidGenerator
from .application.extended_context import ExtendedApplicationContext
from .application.fingerprint_cache import FingerprintCache
from .application.nudges import AdoptionNudgeTracker
from .application.operations import OperationManager, recover_operations
from .application.workflow import (
    RecordedCategoryReplayAdapter,
    WorkflowRecorder,
    WorkflowReplayEngine,
)
from .application.workspace.pr_watch import PrCheckWatchCoordinator
from .bootstrap import AdapterOverrides as BaseAdapterOverrides
from .bootstrap import Application
from .config import AppConfig
from .ports.approval_store import ApprovalPayloadStore, ApprovalStore
from .ports.external_mutation_ledger import ExternalMutationLedger
from .ports.filesystem_transaction import (
    FileTransactionFactory as ReceiptFileTransactionFactory,
)
from .ports.issue_mutation import IssueMutationGateway


@dataclass(frozen=True, slots=True)
class AdapterOverrides(BaseAdapterOverrides):
    """Additional adapters required by governed issue and receipt mutations."""

    approvals: ApprovalStore | None = None
    approval_payloads: ApprovalPayloadStore | None = None
    issue_mutations: IssueMutationGateway | None = None
    external_mutations: ExternalMutationLedger | None = None
    receipt_file_transactions: ReceiptFileTransactionFactory | None = None


def build_application(
    config: AppConfig, *, overrides: AdapterOverrides | None = None
) -> Application:
    """Build the landed application plus receipt and issue-mutation capabilities."""

    o = overrides or AdapterOverrides()
    config.server.workspace_root.mkdir(parents=True, exist_ok=True)
    config.server.state_root.mkdir(parents=True, exist_ok=True)
    clock = o.clock or SystemClock()
    command = o.command or SubprocessCommandExecutor(config.server)
    execution_environment = o.execution_environment or NativeReviewedAdapter(
        command,
        max_artifact_bytes=config.server.max_file_bytes,
    )
    store = o.store or JsonWorkspaceStore(config.server.state_root)
    locks = o.locks or FcntlLockManager(config.server.state_root / "locks")
    gate = o.gate or InProcessOperationGate()
    audit = o.audit or JsonlAuditSink(
        config.server.state_root,
        clock,
        max_bytes=config.server.audit_max_bytes,
        backup_count=config.server.audit_backup_count,
    )
    filesystem = o.filesystem or LocalFileSystem()
    file_transactions = o.file_transactions or JournaledFileTransactionFactory()
    git = o.git or GitCliRepository(command, config.server)
    default_github = GhCliGateway(command, config.server)
    github = o.github or default_github
    issue_mutations = o.issue_mutations or default_github
    external_mutations = o.external_mutations or JsonExternalMutationLedger(
        config.server.state_root,
        locks,
    )
    approvals = o.approvals or JsonApprovalStore(config.server.state_root, locks)
    approval_payloads = o.approval_payloads or JsonApprovalPayloadStore(
        config.server.state_root,
        locks,
    )
    receipt_file_transactions = (
        o.receipt_file_transactions or ReceiptJournaledFileTransactionFactory()
    )
    ticket_graphs = o.ticket_graphs or CommandGitHubTicketGraphGateway(command, config.server)
    ticket_projects = o.ticket_projects or GhTicketProjectGateway(command, config.server)
    ids = o.ids or UuidGenerator()
    executables = o.executables or SystemExecutableLocator()
    provider_registry = o.provider_registry or ConfigProviderRegistry(config.providers, executables)
    code_intelligence = o.code_intelligence or SyntaxCodeIntelligenceProvider()
    metrics = o.metrics or JsonMetricsSink(config.server.state_root, locks, clock)
    idempotency = o.idempotency or JsonIdempotencyStore(config.server.state_root)
    operation_store = o.operations or JsonOperationStore(config.server.state_root, locks)
    operation_result_store = o.operation_results or JsonOperationResultStore(
        config.server.state_root,
        locks,
        max_result_bytes=config.server.max_tool_output_chars,
    )
    github_read_cache = o.github_read_cache or JsonGitHubReadCache(config.server.state_root, locks)
    hygiene = o.hygiene or CommandHygieneGateway(command)
    hygiene_cache = o.hygiene_cache or JsonHygieneBaselineCache(config.server.state_root, locks)
    pr_check_watch_store = o.pr_check_watches or JsonPrCheckWatchStore(
        config.server.state_root,
        locks,
    )
    workflow_recording_store = o.workflow_recordings or JsonWorkflowRecordingStore(
        config.server.state_root,
        locks,
    )
    background_tasks = o.background_tasks or ThreadBackgroundTaskRunner()
    sleeper = o.sleeper or SystemSleeper()
    context = ExtendedApplicationContext(
        config=config,
        fingerprint_cache=FingerprintCache(),
        nudge_tracker=AdoptionNudgeTracker(),
        commands=command,
        git=git,
        github=github,
        filesystem=filesystem,
        file_transactions=file_transactions,
        store=store,
        locks=locks,
        gate=gate,
        audit=audit,
        clock=clock,
        ids=ids,
        executables=executables,
        execution_environment=execution_environment,
        provider_registry=provider_registry,
        code_intelligence=code_intelligence,
        metrics=metrics,
        idempotency=idempotency,
        operation_store=operation_store,
        operation_result_store=operation_result_store,
        github_read_cache=github_read_cache,
        hygiene=hygiene,
        hygiene_cache=hygiene_cache,
        ticket_graphs=ticket_graphs,
        ticket_projects=ticket_projects,
        issue_mutations=issue_mutations,
        external_mutations=external_mutations,
        approvals=approvals,
        approval_payloads=approval_payloads,
        receipt_file_transactions=receipt_file_transactions,
    )
    operations = OperationManager(context)
    recover_operations(
        operations,
        now=clock.now_iso(),
        resumable_kinds=frozenset({"pr_check_watch"}),
    )
    pr_check_watches = PrCheckWatchCoordinator(
        context,
        operations,
        pr_check_watch_store,
        background_tasks,
        sleeper,
    )
    pr_check_watches.resume_active()
    workflow_recorder = WorkflowRecorder(context, workflow_recording_store)
    workflow_replay = WorkflowReplayEngine(RecordedCategoryReplayAdapter())
    return Application(
        context,
        operations,
        pr_check_watches,
        workflow_recorder,
        workflow_replay,
        background_tasks,
    )
