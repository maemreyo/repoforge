"""Sole production composition root for RepoForge concrete adapters."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .adapters.audit import JsonlAuditSink as JsonlAuditSink
from .adapters.audit.query import prune_audit_log as prune_audit_log
from .adapters.audit.query import read_audit_events as read_audit_events
from .adapters.audit.query import (
    summarize_command_source_stats as summarize_command_source_stats,
)
from .adapters.audit.query import summarize_operation_metrics as summarize_operation_metrics
from .adapters.background import SystemSleeper, ThreadBackgroundTaskRunner
from .adapters.capabilities import SystemExecutableLocator
from .adapters.code_intelligence import SyntaxCodeIntelligenceProvider
from .adapters.configuration import ConfigGenerationStore
from .adapters.execution.native import NativeReviewedAdapter
from .adapters.filesystem import JournaledFileTransactionFactory, LocalFileSystem
from .adapters.git import GitCliRepository
from .adapters.github import (
    CommandGitHubCapabilityProbe,
    CommandGitHubTicketGraphGateway,
    GhCliGateway,
)
from .adapters.github.ticket_project import GhTicketProjectGateway
from .adapters.hygiene import CommandHygieneGateway
from .adapters.locking import FcntlLockManager as FcntlLockManager
from .adapters.observability import JsonMetricsSink
from .adapters.onboarding_environment import SystemOnboardingEnvironment
from .adapters.persistence import (
    JsonApprovalPayloadStore,
    JsonApprovalStore,
    JsonExecutionPlanAcceptanceStore,
    JsonExecutionPlanStore,
    JsonExecutionReceiptStore,
    JsonFailureEvidenceStore,
    JsonGitHubReadCache,
    JsonHygieneBaselineCache,
    JsonIdempotencyStore,
    JsonIterationCache,
    JsonOnboardingStore,
    JsonOperationResultStore,
    JsonOperationStore,
    JsonPrCheckWatchStore,
    JsonTaskStore,
    JsonWorkflowRecordingStore,
)
from .adapters.persistence import JsonWorkspaceStore as JsonWorkspaceStore
from .adapters.provider.config_registry import ConfigProviderRegistry
from .adapters.repository import LocalRepositoryProbe
from .adapters.repository.discovery import LocalRepositoryDiscovery
from .adapters.runtime import (
    InProcessOperationGate,
    JsonRuntimeStore,
    JsonTunnelProfileStore,
    SubprocessRuntimeLauncher,
    SystemProcessInspector,
    TunnelCliClient,
    UnixRuntimeControlClient,
    UnixRuntimeControlServer,
)
from .adapters.runtime.local_runtime import (
    ManagedRuntime as ManagedRuntime,
)
from .adapters.runtime.local_runtime import (
    RuntimeState as RuntimeState,
)
from .adapters.runtime.local_runtime import (
    clear_runtime_state as clear_runtime_state,
)
from .adapters.runtime.local_runtime import (
    managed_start_claim as managed_start_claim,
)
from .adapters.runtime.local_runtime import (
    read_managed_runtime as read_managed_runtime,
)
from .adapters.runtime.local_runtime import (
    read_runtime_log as read_runtime_log,
)
from .adapters.runtime.local_runtime import (
    read_runtime_state as read_runtime_state,
)
from .adapters.runtime.local_runtime import (
    runtime_log_files as runtime_log_files,
)
from .adapters.runtime.local_runtime import (
    stop_managed_runtime as stop_managed_runtime,
)
from .adapters.runtime.local_runtime import (
    write_managed_runtime as write_managed_runtime,
)
from .adapters.runtime.local_runtime import (
    write_runtime_state as write_runtime_state,
)
from .adapters.subprocess import SubprocessCommandExecutor as SubprocessCommandExecutor
from .adapters.system import SystemClock as SystemClock
from .adapters.system import UuidGenerator
from .application.approvals import PendingPolicyChangeStore
from .application.configuration.source import parse_source
from .application.context import ApplicationContext
from .application.fingerprint_cache import FingerprintCache
from .application.nudges import AdoptionNudgeTracker
from .application.onboarding.activation import ConfigurationActivator
from .application.onboarding.candidate import smoke_candidate
from .application.onboarding.coordinator import OnboardingCoordinator
from .application.onboarding.discover import OnboardingDiscoveryService
from .application.onboarding.planner import OnboardingPlanner
from .application.onboarding.preflight import OnboardingPreflightService
from .application.operations import OperationManager, recover_operations
from .application.repository_admin.proposals import RepositoryProposalService
from .application.runtime.activation import GenerationActivator
from .application.runtime.supervisor import RuntimeSupervisor
from .application.tasks import TaskCapsuleService
from .application.workflow import (
    RecordedCategoryReplayAdapter,
    WorkflowRecorder,
    WorkflowReplayEngine,
)
from .application.workspace.pr_watch import PrCheckWatchCoordinator
from .config import DEFAULT_STATE_ROOT, AppConfig, ServerConfig, load_config
from .domain.errors import ConfigError
from .domain.runtime import TunnelProfile
from .ports import (
    ApprovalPayloadStore,
    ApprovalStore,
    AuditSink,
    BackgroundTaskRunner,
    Clock,
    CodeIntelligenceProvider,
    CommandExecutor,
    ConfigurationStore,
    ExecutableLocator,
    ExecutionEnvironmentPort,
    ExecutionPlanAcceptanceStore,
    ExecutionPlanStore,
    ExecutionReceiptStore,
    FailureEvidenceStore,
    FileSystem,
    FileTransactionFactory,
    GitHubCapabilityProbe,
    GitHubReadCache,
    GitRepository,
    HygieneBaselineCache,
    HygieneGateway,
    IdempotencyStore,
    IdGenerator,
    IterationCache,
    LockManager,
    MetricsSink,
    OnboardingEnvironment,
    OnboardingStore,
    OperationGate,
    OperationResultStore,
    OperationStore,
    PrCheckWatchStore,
    ProcessInspector,
    ProviderRegistry,
    PullRequestGateway,
    RepositoryDiscovery,
    RepositoryProbe,
    RuntimeControlClient,
    RuntimeControlServer,
    RuntimeLauncher,
    RuntimeStore,
    Sleeper,
    TaskStore,
    TicketGraphGateway,
    TicketProjectGateway,
    TunnelClient,
    TunnelProfileStore,
    WorkflowRecordingStore,
    WorkspaceStore,
)


@dataclass(frozen=True, slots=True)
class AdapterOverrides:
    command: CommandExecutor | None = None
    execution_environment: ExecutionEnvironmentPort | None = None
    store: WorkspaceStore | None = None
    locks: LockManager | None = None
    gate: OperationGate | None = None
    audit: AuditSink | None = None
    clock: Clock | None = None
    ids: IdGenerator | None = None
    filesystem: FileSystem | None = None
    file_transactions: FileTransactionFactory | None = None
    git: GitRepository | None = None
    github: PullRequestGateway | None = None
    ticket_graphs: TicketGraphGateway | None = None
    ticket_projects: TicketProjectGateway | None = None
    github_capabilities: GitHubCapabilityProbe | None = None
    executables: ExecutableLocator | None = None
    metrics: MetricsSink | None = None
    idempotency: IdempotencyStore | None = None
    operations: OperationStore | None = None
    operation_results: OperationResultStore | None = None
    github_read_cache: GitHubReadCache | None = None
    hygiene: HygieneGateway | None = None
    hygiene_cache: HygieneBaselineCache | None = None
    pr_check_watches: PrCheckWatchStore | None = None
    background_tasks: BackgroundTaskRunner | None = None
    sleeper: Sleeper | None = None
    workflow_recordings: WorkflowRecordingStore | None = None
    provider_registry: ProviderRegistry | None = None
    code_intelligence: CodeIntelligenceProvider | None = None
    execution_plans: ExecutionPlanStore | None = None
    execution_plan_acceptances: ExecutionPlanAcceptanceStore | None = None
    execution_receipts: ExecutionReceiptStore | None = None
    iteration_cache: IterationCache | None = None
    failure_evidence: FailureEvidenceStore | None = None


@dataclass(frozen=True, slots=True)
class Application:
    context: ApplicationContext
    operations: OperationManager
    pr_check_watches: PrCheckWatchCoordinator
    workflow_recorder: WorkflowRecorder
    workflow_replay: WorkflowReplayEngine
    background_tasks: BackgroundTaskRunner


def default_state_root() -> Path:
    return Path(DEFAULT_STATE_ROOT).expanduser().resolve()


def system_clock() -> Clock:
    return SystemClock()


def id_generator() -> IdGenerator:
    return UuidGenerator()


def build_lock_manager(state_root: Path | None = None) -> LockManager:
    root = (state_root or default_state_root()).expanduser().resolve()
    return FcntlLockManager(root / "locks")


def build_configuration_store(
    config_path: Path, *, state_root: Path | None = None, locks: LockManager | None = None
) -> ConfigurationStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    return ConfigGenerationStore(config_path, root, locks or build_lock_manager(root))


def build_repository_probe(state_root: Path | None = None) -> RepositoryProbe:
    root = (state_root or default_state_root()).expanduser().resolve()
    server = ServerConfig(root / "probe-workspaces", root)
    return LocalRepositoryProbe(SubprocessCommandExecutor(server))


def build_onboarding_store(
    state_root: Path | None = None, *, locks: LockManager | None = None
) -> OnboardingStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    return JsonOnboardingStore(root, locks or build_lock_manager(root))


def build_repository_discovery(state_root: Path | None = None) -> RepositoryDiscovery:
    root = (state_root or default_state_root()).expanduser().resolve()
    server = ServerConfig(root / "discovery-workspaces", root)
    return LocalRepositoryDiscovery(SubprocessCommandExecutor(server))


def build_onboarding_environment() -> OnboardingEnvironment:
    return SystemOnboardingEnvironment()


def build_onboarding_coordinator(config_path: Path) -> OnboardingCoordinator:
    config_path = config_path.expanduser().resolve()
    root = default_state_root()
    locks = build_lock_manager(root)
    configs = build_configuration_store(config_path, state_root=root, locks=locks)
    runtime_path = configs.root / "managed-runtime-v3.json"
    runtime = build_runtime_store(runtime_path)
    activator = ConfigurationActivator(
        configs=configs,
        runtime=runtime,
        activator=GenerationActivator(
            configs=configs,
            runtime=runtime,
            mcp_control=build_runtime_control_client(configs.root / "mcp.sock"),
            supervisor_control=build_runtime_control_client(configs.root / "supervisor.sock"),
            launcher=build_runtime_launcher(),
            ids=id_generator(),
            clock=system_clock(),
            config_path=config_path,
        ),
    )
    return OnboardingCoordinator(
        sessions=build_onboarding_store(root, locks=locks),
        discovery=OnboardingDiscoveryService(build_repository_discovery(root)),
        preflight=OnboardingPreflightService(build_onboarding_environment()),
        planner=OnboardingPlanner(RepositoryProposalService(build_repository_probe(root))),
        configs=configs,
        clock=system_clock(),
        ids=id_generator(),
        smoke=lambda resolved, repo_ids: smoke_candidate(resolved, repo_ids, state_root=root),
        activate=lambda generation, mode, wait, rollback: activator.activate(
            generation, mode=mode, wait=wait, rollback_on_failure=rollback
        ),
    )


def build_operation_gate() -> OperationGate:
    return InProcessOperationGate()


def build_approval_store(
    state_root: Path | None = None, *, locks: LockManager | None = None
) -> ApprovalStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    return JsonApprovalStore(root, locks or build_lock_manager(root))


def build_approval_payload_store(
    state_root: Path | None = None, *, locks: LockManager | None = None
) -> ApprovalPayloadStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    return JsonApprovalPayloadStore(root, locks or build_lock_manager(root))


def build_pending_policy_change_store(
    state_root: Path | None = None, *, locks: LockManager | None = None
) -> PendingPolicyChangeStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    selected_locks = locks or build_lock_manager(root)
    return PendingPolicyChangeStore(
        approvals=build_approval_store(root, locks=selected_locks),
        payloads=build_approval_payload_store(root, locks=selected_locks),
        legacy_root=root / "pending-policy-changes",
    )


def build_task_store(
    state_root: Path | None = None, *, locks: LockManager | None = None
) -> TaskStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    return JsonTaskStore(root, locks or build_lock_manager(root))


def build_task_service(
    state_root: Path | None = None,
    *,
    locks: LockManager | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> TaskCapsuleService:
    root = (state_root or default_state_root()).expanduser().resolve()
    selected_locks = locks or build_lock_manager(root)
    return TaskCapsuleService(
        store=build_task_store(root, locks=selected_locks),
        clock=clock or system_clock(),
        ids=ids or id_generator(),
    )


def build_runtime_store(path: Path) -> RuntimeStore:
    return JsonRuntimeStore(path)


def build_tunnel_profile_store(path: Path) -> TunnelProfileStore:
    return JsonTunnelProfileStore(path, LocalFileSystem())


def build_runtime_control_client(path: Path) -> RuntimeControlClient:
    return UnixRuntimeControlClient(path)


def build_runtime_control_server(path: Path) -> RuntimeControlServer:
    return UnixRuntimeControlServer(path)


def build_runtime_launcher() -> RuntimeLauncher:
    return SubprocessRuntimeLauncher()


def build_process_inspector() -> ProcessInspector:
    return SystemProcessInspector()


def build_tunnel_client(
    executable: str,
    *,
    log_max_bytes: int = 5_000_000,
    log_backup_count: int = 3,
) -> TunnelClient:
    return TunnelCliClient(
        executable,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )


def build_metrics_sink(
    state_root: Path,
    locks: LockManager | None = None,
    clock: Clock | None = None,
) -> MetricsSink:
    return JsonMetricsSink(state_root, locks or build_lock_manager(state_root), clock)


def build_idempotency_store(state_root: Path) -> IdempotencyStore:
    return JsonIdempotencyStore(state_root)


def build_operation_store(
    state_root: Path,
    locks: LockManager | None = None,
) -> OperationStore:
    return JsonOperationStore(state_root, locks or build_lock_manager(state_root))


def build_operation_result_store(
    state_root: Path,
    locks: LockManager | None = None,
) -> OperationResultStore:
    return JsonOperationResultStore(state_root, locks or build_lock_manager(state_root))


def build_github_read_cache(
    state_root: Path,
    locks: LockManager | None = None,
) -> GitHubReadCache:
    return JsonGitHubReadCache(state_root, locks or build_lock_manager(state_root))


def build_workflow_recording_store(
    state_root: Path,
    locks: LockManager | None = None,
) -> WorkflowRecordingStore:
    return JsonWorkflowRecordingStore(state_root, locks or build_lock_manager(state_root))


def write_private_file(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    ConfigGenerationStore._atomic_write(path, data, mode=mode)


def build_application(
    config: AppConfig, *, overrides: AdapterOverrides | None = None
) -> Application:
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
    github = o.github or GhCliGateway(command, config.server)
    ticket_graphs = o.ticket_graphs or CommandGitHubTicketGraphGateway(command, config.server)
    ticket_projects = o.ticket_projects or GhTicketProjectGateway(command, config.server)
    github_capabilities = o.github_capabilities or CommandGitHubCapabilityProbe(
        command, config.server
    )
    ids = o.ids or UuidGenerator()
    executables = o.executables or SystemExecutableLocator()
    provider_registry = o.provider_registry or ConfigProviderRegistry(config.providers, executables)
    code_intelligence = o.code_intelligence or SyntaxCodeIntelligenceProvider()
    metrics = o.metrics or JsonMetricsSink(config.server.state_root, locks, clock)
    idempotency = o.idempotency or JsonIdempotencyStore(config.server.state_root)
    execution_plans = o.execution_plans or JsonExecutionPlanStore(config.server.state_root, locks)
    execution_plan_acceptances = o.execution_plan_acceptances or JsonExecutionPlanAcceptanceStore(
        config.server.state_root, locks
    )
    execution_receipts = o.execution_receipts or JsonExecutionReceiptStore(
        config.server.state_root, locks
    )
    iteration_cache = o.iteration_cache or JsonIterationCache(config.server.state_root, locks)
    failure_evidence = o.failure_evidence or JsonFailureEvidenceStore(
        config.server.state_root, locks
    )
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
    context = ApplicationContext(
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
        github_capabilities=github_capabilities,
        execution_plans=execution_plans,
        execution_plan_acceptances=execution_plan_acceptances,
        execution_receipts=execution_receipts,
        iteration_cache=iteration_cache,
        failure_evidence=failure_evidence,
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


def run_runtime_worker(config_path: Path) -> int:
    """Construct and run the long-lived supervisor for one reviewed configuration."""
    from .interfaces.mcp.server import tool_surface_hash

    config_path = config_path.expanduser().resolve()
    configs = build_configuration_store(config_path)
    target = configs.activation_target() or configs.active()
    if target is None:
        raise ConfigError("No staged or active configuration generation; run `rf runtime start`")
    try:
        source = parse_source(config_path.read_text(encoding="utf-8"))
        tunnel_id = source.tunnel_id
        profile_name = source.profile
        if tunnel_id is None:
            raise ConfigError(
                "Managed runtime requires a tunnel ID; this accepted configuration is local-only. "
                f"Run `rf --config {config_path} serve` or rerun setup with --tunnel-id."
            )
    except (ValueError, OSError):
        tunnel_id = os.environ.get("REPOFORGE_TUNNEL_ID", "")
        profile_name = os.environ.get("REPOFORGE_TUNNEL_PROFILE", "repoforge")
        if not tunnel_id:
            raise ConfigError(
                "Legacy configuration requires REPOFORGE_TUNNEL_ID or "
                "`rf runtime start --tunnel-id ID`"
            ) from None
    tunnel_executable = shutil.which("tunnel-client")
    if tunnel_executable is None:
        raise ConfigError("tunnel-client is not in PATH")
    runtime_config = load_config(configs.resolved_path(target.generation))
    tunnel = build_tunnel_client(
        tunnel_executable,
        log_max_bytes=runtime_config.server.runtime_log_max_bytes,
        log_backup_count=runtime_config.server.runtime_log_backup_count,
    )
    tunnel_version = tunnel.executable_version()
    if not tunnel_version:
        raise ConfigError("Cannot determine tunnel-client version")
    tunnel_id_fingerprint = hashlib.sha256(tunnel_id.encode()).hexdigest()
    mcp_argv = (sys.executable, "-m", "repoforge", "--config", str(config_path), "serve")
    profile = TunnelProfile(
        tunnel_id_fingerprint,
        profile_name,
        tunnel_executable,
        tunnel_version,
        mcp_argv,
    )
    inherited_keys = (
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "SSH_AUTH_SOCK",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "CONTROL_PLANE_API_KEY",
    )
    environment = {key: os.environ[key] for key in inherited_keys if key in os.environ}
    environment["REPOFORGE_TUNNEL_ID"] = tunnel_id
    environment["REPOFORGE_TUNNEL_PROFILE"] = profile_name
    if not environment.get("CONTROL_PLANE_API_KEY"):
        raise ConfigError("CONTROL_PLANE_API_KEY is required for managed runtime startup")
    root = configs.root
    supervisor = RuntimeSupervisor(
        store=build_runtime_store(root / "managed-runtime-v3.json"),
        configs=configs,
        locks=build_lock_manager(),
        control=build_runtime_control_server(root / "supervisor.sock"),
        mcp_control=build_runtime_control_client(root / "mcp.sock"),
        tunnel=tunnel,
        profile_store=build_tunnel_profile_store(root / "tunnel-profile-v3.json"),
        clock=system_clock(),
        ids=id_generator(),
        processes=build_process_inspector(),
        mcp_runtime_path=root / "runtime.json",
        log_path=root / "managed-runtime.log",
    )
    return supervisor.run(
        generation=target.generation,
        profile=profile,
        tool_surface_hash=tool_surface_hash(),
        environment=environment,
    )
