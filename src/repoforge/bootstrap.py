"""Sole production composition root for RepoForge concrete adapters."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .adapters.activation.release_store import RuntimeReleaseStore
    from .application.activation.dev_runtime import DevRuntimeService
    from .application.activation.handoff import GenerationHandoffReconciler
    from .application.activation.upgrade import UpgradeService
    from .ports.activation import (
        ReleaseObserver,
        ReleaseProcessInspector,
        SupervisorKickstarter,
    )
    from .ports.process_supervisor import ProcessSupervisorRegistrar

from .adapters.audit import JsonlAuditSink as JsonlAuditSink
from .adapters.audit.query import prune_audit_log as prune_audit_log
from .adapters.audit.query import read_audit_event_page as read_audit_event_page
from .adapters.audit.query import read_audit_events as read_audit_events
from .adapters.audit.query import read_audit_events_since as read_audit_events_since
from .adapters.audit.query import (
    summarize_command_source_stats as summarize_command_source_stats,
)
from .adapters.audit.query import summarize_operation_metrics as summarize_operation_metrics
from .adapters.background import SystemSleeper, ThreadBackgroundTaskRunner
from .adapters.capabilities import SystemExecutableLocator
from .adapters.code_intelligence import (
    FallbackCodeIntelligenceProvider,
    SyntaxCodeIntelligenceProvider,
    TreeSitterCodeIntelligenceProvider,
)
from .adapters.configuration import ConfigGenerationStore
from .adapters.execution.native import NativeReviewedAdapter
from .adapters.filesystem import JournaledFileTransactionFactory, LocalFileSystem
from .adapters.filesystem.receipt_transaction_factory import (
    ReceiptJournaledFileTransactionFactory,
)
from .adapters.git import (
    GitAmbientAuthConflictReader,
    GitCliRepository,
    GitCommitIdentityGateway,
    GitNestedResourceDiscovery,
    GitTransportRouter,
    SshCommandAliasDiscovery,
)
from .adapters.git.endpoint_identity import ReviewedSshEndpointAuthority
from .adapters.git.remote_identity import (
    ConstrainedGitRemoteParser,
    ConstrainedSshConfigAliasResolver,
    EffectiveUserPaths,
)
from .adapters.git.ssh_key_identity import FileSshKeyIdentity
from .adapters.github import (
    CommandGitHubCapabilityPreflight,
    CommandGitHubCapabilityProbe,
    CommandGitHubTicketGraphGateway,
    GhCliGateway,
    GhCliGitHubApiIdentityVerifier,
    GhCliNamedAccountDiscovery,
    GhCliStoredAccountTokenSource,
    GitHubApiAuthProvider,
    UnavailableGitHubAppInstallationTokenIssuer,
)
from .adapters.github.repository_observation import GhCliRepositoryObserver
from .adapters.github.ssh_principal import GitHubSshPrincipalVerifier
from .adapters.github.ticket_project import GhTicketProjectGateway
from .adapters.hygiene import CommandHygieneGateway
from .adapters.locking import FcntlLockManager as FcntlLockManager
from .adapters.observability import JsonMetricsSink
from .adapters.onboarding_environment import SystemOnboardingEnvironment
from .adapters.persistence import (
    FileFailureOutputArtifactStore,
    JsonApprovalPayloadStore,
    JsonApprovalStore,
    JsonEffectReceiptStore,
    JsonExecutionPlanAcceptanceStore,
    JsonExecutionPlanStore,
    JsonExecutionReceiptStore,
    JsonExecutionWorkerBindingStore,
    JsonExternalMutationLedger,
    JsonFailureEvidenceStore,
    JsonGitHubReadCache,
    JsonHygieneBaselineCache,
    JsonIdempotencyStore,
    JsonIssueGraphProposalStore,
    JsonIssueGraphPublicationStore,
    JsonIterationCache,
    JsonOnboardingStore,
    JsonOperationIdentityStore,
    JsonOperationResultStore,
    JsonOperationStore,
    JsonOperationWorkQueue,
    JsonPrCheckWatchStore,
    JsonRepositoryBindingStore,
    JsonRuntimeActivationStore,
    JsonTaskStore,
    JsonWorkerBindingStore,
    JsonWorkflowRecordingStore,
    SqliteLeaseStore,
)
from .adapters.persistence import JsonWorkspaceStore as JsonWorkspaceStore
from .adapters.provider.config_registry import ConfigProviderRegistry
from .adapters.publication import PublicationAdapter
from .adapters.publication_identity import (
    DurableBindingPublicationRepositoryResolver,
    PinnedPublicationAuthorizationGateway,
)
from .adapters.repository import LocalRepositoryProbe
from .adapters.repository.discovery import LocalRepositoryDiscovery
from .adapters.runtime import (
    InProcessOperationGate,
    JsonRuntimeStore,
    JsonTunnelProfileStore,
    SubprocessExecutionWorker,
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
    read_runtime_log_page as read_runtime_log_page,
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
from .adapters.subprocess import OsProcessReaper as OsProcessReaper
from .adapters.subprocess import SubprocessCommandExecutor as SubprocessCommandExecutor
from .adapters.system import SystemClock as SystemClock
from .adapters.system import UuidGenerator
from .application.approvals import PendingPolicyChangeStore
from .application.auth_migration import AuthMigrationService, RepositoryEndpointSnapshot
from .application.auth_ux import AuthUxService, ObserveRepository
from .application.configuration.source import parse_source
from .application.context import ApplicationContext
from .application.execution.coordinator import ExecutionCoordinator
from .application.extended_context import ExtendedApplicationContext
from .application.fingerprint_cache import FingerprintCache
from .application.nudges import AdoptionNudgeTracker
from .application.onboarding.activation import ConfigurationActivator
from .application.onboarding.candidate import smoke_candidate
from .application.onboarding.coordinator import OnboardingCoordinator
from .application.onboarding.discover import OnboardingDiscoveryService
from .application.onboarding.planner import OnboardingPlanner
from .application.onboarding.preflight import OnboardingPreflightService
from .application.operations import (
    OperationManager,
    recover_operation_work,
    recover_operations,
)
from .application.operations.identity import OperationIdentityManager
from .application.outcome_reconciliation import (
    OutcomeReceiptReconciler,
    RuntimeActivationReconciler,
)
from .application.publication import PublicationCoordinator
from .application.repository_admin.proposals import RepositoryProposalService
from .application.repository_identity_runtime import RepositoryIdentityRuntime
from .application.runtime.activation import GenerationActivator
from .application.runtime.activation_journal import RuntimeActivationJournal
from .application.runtime.execution_worker_reconciler import ExecutionWorkerReconciler
from .application.runtime.supervisor import RuntimeSupervisor
from .application.runtime.worker_lifecycle import WorkerLifecycleStore
from .application.runtime.worker_registrar import WorkerRegistrar
from .application.tasks import TaskCapsuleService
from .application.workflow import (
    RecordedCategoryReplayAdapter,
    WorkflowRecorder,
    WorkflowReplayEngine,
)
from .application.workspace.pr_watch import PrCheckWatchCoordinator
from .application.workspace.publication import CoordinatedWorkspacePublicationService
from .application.workspace.publication_request_factory import (
    ScopedWorkspacePublicationRequestFactory,
)
from .config import (
    DEFAULT_STATE_ROOT,
    AppConfig,
    AuthProfileConfig,
    RepositoryConfig,
    ServerConfig,
    load_config,
)
from .contracts.registry import validate_generated_contract_identity
from .domain.activation import AGENT_SECRET_FILE_ENV_VAR, AGENT_SECRET_KEY
from .domain.auth_profile import AuthProfileSelector, RequestedActorClass
from .domain.errors import ConfigError, ErrorCode, RepoForgeError
from .domain.github_api_identity import GitHubAppInstallationSpec, StoredGhAccountSpec
from .domain.operation_task import OperationTask
from .domain.repository_auth_broker import (
    AuthBrokerRequest,
    EphemeralSecret,
    ProcessAuthContext,
    RepositoryAuthBroker,
)
from .domain.repository_identity import AuthTargetKind, RepositoryProvider
from .domain.repository_identity_resolution import (
    RepositoryIdentityObservation,
    role_accepts_actor_class,
)
from .domain.runtime import RuntimeRecord, TunnelProfile
from .ports import (
    ApprovalPayloadStore,
    ApprovalStore,
    AuditSink,
    BackgroundTaskRunner,
    Clock,
    CodeIntelligenceProvider,
    CommandExecutor,
    CommitIdentityGateway,
    ConfigurationStore,
    EffectReceiptStore,
    ExecutableLocator,
    ExecutionEnvironmentPort,
    ExecutionPlanAcceptanceStore,
    ExecutionPlanStore,
    ExecutionReceiptStore,
    FailureEvidenceStore,
    FailureOutputArtifactStore,
    FileSystem,
    FileTransactionFactory,
    GitHubCapabilityPreflightGateway,
    GitHubCapabilityProbe,
    GitHubReadCache,
    GitRepository,
    GitTransportGateway,
    HygieneBaselineCache,
    HygieneGateway,
    IdempotencyStore,
    IdGenerator,
    IterationCache,
    LockManager,
    MetricsSink,
    NestedLeaseProvider,
    NestedResourceDiscovery,
    NestedTargetResolver,
    OnboardingEnvironment,
    OnboardingStore,
    OperationGate,
    OperationIdentityStore,
    OperationResultStore,
    OperationStore,
    OperationWorkQueue,
    PrCheckWatchStore,
    ProcessInspector,
    ProcessReaper,
    ProviderRegistry,
    PullRequestGateway,
    RepositoryBindingStore,
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
    WorkerBindingStore,
    WorkflowRecordingStore,
    WorkspacePublicationService,
    WorkspaceStore,
)
from .ports.admission_epoch import AdmissionEpochStore
from .ports.auth_discovery import NamedAccountDiscovery, SshAliasDiscovery
from .ports.auth_inspection import RepositoryObservationTarget
from .ports.external_mutation_ledger import ExternalMutationLedger
from .ports.filesystem_transaction import (
    FileTransactionFactory as ReceiptFileTransactionFactory,
)
from .ports.issue_graph_proposal_store import IssueGraphProposalStore
from .ports.issue_graph_publication_store import IssueGraphPublicationStore
from .ports.issue_mutation import IssueMutationGateway
from .ports.runtime_transition_coordinator import RuntimeTransitionCoordinator


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
    commit_identities: CommitIdentityGateway | None = None
    github: PullRequestGateway | None = None
    ticket_graphs: TicketGraphGateway | None = None
    ticket_projects: TicketProjectGateway | None = None
    github_capabilities: GitHubCapabilityProbe | None = None
    github_capability_preflight: GitHubCapabilityPreflightGateway | None = field(
        default=None,
        kw_only=True,
    )
    nested_resource_discovery: NestedResourceDiscovery | None = field(
        default=None,
        kw_only=True,
    )
    nested_target_resolver: NestedTargetResolver | None = field(
        default=None,
        kw_only=True,
    )
    nested_lease_provider: NestedLeaseProvider | None = field(
        default=None,
        kw_only=True,
    )
    executables: ExecutableLocator | None = None
    metrics: MetricsSink | None = None
    idempotency: IdempotencyStore | None = None
    operations: OperationStore | None = None
    operation_work_queue: OperationWorkQueue | None = None
    operation_identities: OperationIdentityStore | None = None
    operation_results: OperationResultStore | None = None
    github_read_cache: GitHubReadCache | None = None
    hygiene: HygieneGateway | None = None
    hygiene_cache: HygieneBaselineCache | None = None
    pr_check_watches: PrCheckWatchStore | None = None
    background_tasks: BackgroundTaskRunner | None = None
    sleeper: Sleeper | None = None
    workflow_recordings: WorkflowRecordingStore | None = None
    provider_registry: ProviderRegistry | None = None
    repository_bindings: RepositoryBindingStore | None = None
    repository_identity_runtime: RepositoryIdentityRuntime | None = field(
        default=None,
        kw_only=True,
    )
    git_transport_router: GitTransportGateway | None = field(
        default=None,
        kw_only=True,
    )
    code_intelligence: CodeIntelligenceProvider | None = None
    approvals: ApprovalStore | None = None
    approval_payloads: ApprovalPayloadStore | None = None
    issue_mutations: IssueMutationGateway | None = None
    external_mutations: ExternalMutationLedger | None = None
    receipt_file_transactions: ReceiptFileTransactionFactory | None = None
    execution_plans: ExecutionPlanStore | None = None
    execution_plan_acceptances: ExecutionPlanAcceptanceStore | None = None
    execution_receipts: ExecutionReceiptStore | None = None
    effect_receipts: EffectReceiptStore | None = None
    iteration_cache: IterationCache | None = None
    failure_evidence: FailureEvidenceStore | None = None
    failure_output_artifacts: FailureOutputArtifactStore | None = None
    worker_bindings: WorkerBindingStore | None = None
    reaper: ProcessReaper | None = None
    issue_graph_proposals: IssueGraphProposalStore | None = None
    issue_graph_publications: IssueGraphPublicationStore | None = None
    publications: WorkspacePublicationService | None = None


@dataclass(frozen=True, slots=True)
class Application:
    context: ApplicationContext
    operations: OperationManager
    pr_check_watches: PrCheckWatchCoordinator
    workflow_recorder: WorkflowRecorder
    workflow_replay: WorkflowReplayEngine
    background_tasks: BackgroundTaskRunner
    issue_graph_proposals: IssueGraphProposalStore
    issue_graph_publications: IssueGraphPublicationStore


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
            validate_contract_artifacts=validate_generated_contract_identity,
            activation_journal=build_runtime_activation_journal(
                root,
                locks=locks,
            ),
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


def build_auth_ux_service(
    ctx: ApplicationContext,
    *,
    observe: ObserveRepository,
) -> AuthUxService:
    """Compose the identity facade from whatever this context actually has.

    Each per-surface inspector is passed through exactly as composed: a context without one
    yields ``unavailable`` for that surface, which is the whole point -- there is no ambient
    fallback path that could quietly answer with the active account instead.
    """

    bindings = ctx.repository_bindings or JsonRepositoryBindingStore(
        ctx.config.server.state_root, build_lock_manager(ctx.config.server.state_root)
    )
    identities: OperationIdentityManager | None = None
    if ctx.operation_store is not None and ctx.operation_identities is not None:
        identities = OperationIdentityManager(
            operations=ctx.operation_store,
            identities=ctx.operation_identities,
        )
    return AuthUxService(
        config=ctx.config,
        bindings=bindings,
        observe=observe,
        identities=identities,
        clock=ctx.clock,
        api=ctx.auth_api_inspector,
        transport=ctx.auth_transport_inspector,
        commits=ctx.auth_commit_inspector,
        publication=ctx.auth_publication_inspector,
    )


@dataclass(frozen=True, slots=True)
class AuthCommandDependencies:
    """Everything one `rf auth` invocation needs, composed in one place."""

    service: AuthUxService
    migration: AuthMigrationService
    accounts: NamedAccountDiscovery
    ssh: SshAliasDiscovery


def _auth_observation_error(
    code: ErrorCode,
    message: str,
    *,
    next_action: str,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No repository identity was observed, bound, or used for a write.",),
        safe_next_action=next_action,
    )


def _observation_profile(
    *,
    config: AppConfig,
    observer: GhCliRepositoryObserver,
    repository: RepositoryConfig,
    selector: AuthProfileSelector,
    config_revision: str,
    observed_at: str,
) -> AuthProfileConfig:
    if not config.auth_profiles:
        raise _auth_observation_error(
            ErrorCode.INPUT_REQUIRED,
            "This configuration has no reviewed repository auth profiles.",
            next_action="Run `rf auth migrate inspect <repo-id>` before resolving an identity.",
        )

    def eligible(configured: AuthProfileConfig) -> bool:
        try:
            target = observer.target(
                repository,
                expected_provider_host=configured.api_identity.host,
                reviewed_ssh_endpoint=configured.transport.ssh_endpoint,
            )
        except RepoForgeError as exc:
            if exc.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH:
                return False
            raise
        provisional = RepositoryIdentityObservation(
            provider=target.provider,
            provider_host=target.provider_host,
            repository_id=configured.api_identity.repository_id,
            canonical_name=target.canonical_name,
            exists=True,
            observed_at=observed_at,
            config_revision=config_revision,
        )
        return (
            configured.eligibility.enabled
            and role_accepts_actor_class(selector.role, configured.profile.actor_class)
            and configured.eligibility.matches(provisional)
        )

    if not selector.automatic:
        configured = config.auth_profiles.get(selector.auth_profile)
        if configured is None:
            raise _auth_observation_error(
                ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                f"No reviewed auth profile is declared with id {selector.auth_profile!r}.",
                next_action="Run `rf auth profile list` and select one listed profile.",
            )
        if not eligible(configured):
            raise _auth_observation_error(
                ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                "The selected auth profile is not eligible for this repository and actor role.",
                next_action="Select an enabled profile whose repository boundary and actor role match.",
            )
        return configured

    matches = tuple(
        configured for configured in config.auth_profiles.values() if eligible(configured)
    )
    if len(matches) != 1:
        reason = (
            "No auth profile is eligible"
            if not matches
            else "More than one auth profile is eligible"
        )
        raise _auth_observation_error(
            ErrorCode.INPUT_REQUIRED,
            f"{reason} for this repository and actor role.",
            next_action="Pass `--auth-profile <profile-id>` to select one reviewed profile explicitly.",
        )
    return matches[0]


def _named_account_context(
    commands: SubprocessCommandExecutor,
    *,
    cwd: Path,
    profile_id: str,
    host: str,
    login: str,
    target_id: str,
) -> tuple[EphemeralSecret, ProcessAuthContext]:
    inherited = commands.environment()
    environment = {
        key: inherited[key]
        for key in ("HOME", "PATH", "LANG", "LC_ALL")
        if key in inherited and isinstance(inherited[key], str)
    }
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    secret = commands.run_secret_text(
        ["gh", "auth", "token", "--hostname", host, "--user", login],
        cwd=cwd,
        environment=environment,
        secrets=(),
        max_bytes=100_000,
    )
    token = secret.reveal()
    reference_digest = hashlib.sha256(
        f"{host}\0{login}\0{profile_id}\0{target_id}".encode()
    ).hexdigest()
    return secret, ProcessAuthContext(
        profile_id=profile_id,
        material_id=f"repository-observation-{reference_digest[:24]}",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id=target_id,
        environment=(("GH_TOKEN", token),),
        _secret_values=(token,),
    )


def _production_config_revision(config: AppConfig, config_generation: int) -> str:
    """Return the accepted resolved-config digest without requiring the source file to exist."""

    try:
        payload = config.source_path.read_bytes()
    except OSError:
        safe_config = {
            "source_path": str(config.source_path),
            "config_generation": config_generation,
            "profiles": [
                {
                    "profile_id": profile_id,
                    "profile_revision": configured.profile.revision,
                    "credential_reference": configured.api_identity.reference_id,
                    "repository_id": configured.api_identity.repository_id,
                    "transport_fingerprint": configured.transport.credential_fingerprint,
                }
                for profile_id, configured in sorted(config.auth_profiles.items())
            ],
        }
        payload = json.dumps(safe_config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _runtime_observation_profile(
    *,
    config: AppConfig,
    bindings: RepositoryBindingStore,
    observer: GhCliRepositoryObserver,
    repository: RepositoryConfig,
    selector: AuthProfileSelector,
    config_revision: str,
    observed_at: str,
) -> AuthProfileConfig:
    """Select the exact profile used to prove stable identity before broker admission."""

    def target_for(configured: AuthProfileConfig) -> RepositoryObservationTarget:
        return observer.target(
            repository,
            expected_provider_host=configured.api_identity.host,
            reviewed_ssh_endpoint=configured.transport.ssh_endpoint,
        )

    def eligible(configured: AuthProfileConfig) -> bool:
        try:
            target = target_for(configured)
        except RepoForgeError as exc:
            if exc.code is ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH:
                return False
            raise
        provisional = RepositoryIdentityObservation(
            provider=target.provider,
            provider_host=target.provider_host,
            repository_id=configured.api_identity.repository_id,
            canonical_name=target.canonical_name,
            exists=True,
            observed_at=observed_at,
            config_revision=config_revision,
        )
        return (
            configured.eligibility.enabled
            and role_accepts_actor_class(selector.role, configured.profile.actor_class)
            and configured.eligibility.matches(provisional)
        )

    if not selector.automatic:
        configured = config.auth_profiles.get(selector.auth_profile)
        if configured is None:
            raise _auth_observation_error(
                ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                f"No reviewed auth profile is declared with id {selector.auth_profile!r}.",
                next_action="Select one profile reported by `rf auth profile list`.",
            )
        if not eligible(configured):
            raise _auth_observation_error(
                ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                "The selected auth profile is not eligible for this repository and actor role.",
                next_action="Select an enabled profile whose boundary and actor role match.",
            )
        target = target_for(configured)
        existing = bindings.read(target.provider_host, configured.api_identity.repository_id)
        if existing is not None:
            bound_profile = (
                existing.value.agent_profile_id
                if selector.actor_class is RequestedActorClass.AGENT
                else existing.value.human_profile_id
            )
            if existing.value.config_revision != config_revision:
                raise _auth_observation_error(
                    ErrorCode.CONFIG_STALE,
                    "The durable repository identity binding belongs to another configuration revision.",
                    next_action="Reconcile the binding with `rf auth bind` before retrying.",
                )
            if bound_profile != configured.profile.profile_id:
                raise _auth_observation_error(
                    ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                    "The selected profile conflicts with the exact durable binding for this actor role.",
                    next_action="Use the profile recorded in the durable repository binding.",
                )
        return configured

    bound_matches: list[AuthProfileConfig] = []
    for envelope in bindings.list_bindings(max_records=500).records:
        binding = envelope.value
        profile_id = (
            binding.agent_profile_id
            if selector.actor_class is RequestedActorClass.AGENT
            else binding.human_profile_id
        )
        configured = config.auth_profiles.get(profile_id or "")
        if (
            configured is None
            or configured.api_identity.repository_id != binding.repository_id
            or not eligible(configured)
        ):
            continue
        target = target_for(configured)
        if (
            binding.provider_host != target.provider_host
            or binding.canonical_name.lower() != target.canonical_name.lower()
            or binding.config_revision != config_revision
        ):
            continue
        bound_matches.append(configured)
    if len(bound_matches) == 1:
        return bound_matches[0]
    if len(bound_matches) > 1:
        raise _auth_observation_error(
            ErrorCode.STATE_CORRUPT,
            "More than one durable binding claims this repository and actor role.",
            next_action="Inspect and repair the repository identity binding store.",
        )

    eligible_profiles = tuple(
        configured for configured in config.auth_profiles.values() if eligible(configured)
    )
    if len(eligible_profiles) != 1:
        reason = (
            "No auth profile is eligible"
            if not eligible_profiles
            else "More than one auth profile is eligible"
        )
        raise _auth_observation_error(
            ErrorCode.INPUT_REQUIRED,
            f"{reason} for this repository and actor role.",
            next_action="Pass an explicit reviewed auth profile before retrying.",
        )
    return eligible_profiles[0]


def build_auth_command_dependencies(
    store: ConfigurationStore,
    *,
    config: AppConfig,
    config_revision: str,
    cwd: Path | None = None,
) -> AuthCommandDependencies:
    """Compose the identity facade and discovery adapters for the operator CLI.

    `rf auth` runs outside the managed runtime, so no per-surface inspector and no durable
    operation identity store is composed here. That is deliberate: those surfaces report
    `unavailable` rather than answering from whatever account happens to be active.
    """

    clock = system_clock()
    commands = SubprocessCommandExecutor(config.server)
    working_directory = cwd if cwd is not None else Path.cwd()
    remote_parser = ConstrainedGitRemoteParser()
    ssh_discovery = SshCommandAliasDiscovery(commands, cwd=working_directory)
    observer = GhCliRepositoryObserver(
        commands,
        clock=clock,
        remote_parser=remote_parser,
    )
    effective_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve()
    key_identity = FileSshKeyIdentity(
        commands,
        material_root=(config.server.state_root / "ssh-material").expanduser().resolve(),
    )
    endpoint_authority = ReviewedSshEndpointAuthority(
        remote_parser=remote_parser,
        aliases=ConstrainedSshConfigAliasResolver(
            paths=EffectiveUserPaths(
                home=effective_home,
                ssh_config=effective_home / ".ssh" / "config",
            )
        ),
        keys=key_identity,
        principals=GitHubSshPrincipalVerifier(
            commands,
            materials=key_identity,
            cwd=working_directory,
        ),
        clock=clock,
    )

    def repository(repo_id: str) -> RepositoryConfig:
        repository = config.repositories.get(repo_id)
        if repository is None:
            raise ConfigError(f"Unknown repository id: {repo_id}")
        return repository

    def observe_selected(
        repo_id: str, selector: AuthProfileSelector
    ) -> RepositoryIdentityObservation:
        selected_repository = repository(repo_id)
        configured = _observation_profile(
            config=config,
            observer=observer,
            repository=selected_repository,
            selector=selector,
            config_revision=config_revision,
            observed_at=clock.now_iso(),
        )
        spec = configured.api_identity
        if not isinstance(spec, StoredGhAccountSpec):
            raise _auth_observation_error(
                ErrorCode.INPUT_REQUIRED,
                "GitHub App observation requires the managed repository identity runtime.",
                next_action="Run this operation through the managed RepoForge runtime.",
            )
        secret, context = _named_account_context(
            commands,
            cwd=working_directory,
            profile_id=configured.profile.profile_id,
            host=spec.host,
            login=spec.login,
            target_id=spec.repository_id,
        )
        try:
            observation = observer.observe(
                selected_repository,
                expected_provider_host=spec.host,
                config_revision=config_revision,
                context=context,
                reviewed_ssh_endpoint=configured.transport.ssh_endpoint,
            )
        finally:
            secret.release()
        if observation.repository_id != spec.repository_id:
            raise _auth_observation_error(
                ErrorCode.GITHUB_API_REPOSITORY_MISMATCH,
                "The selected profile token observed a different stable repository identity.",
                next_action="Review the profile repository_id and the checkout remote before retrying.",
            )
        return observation

    def raw_migration_remote(selected_repository: RepositoryConfig) -> str:
        inherited = commands.environment()
        environment = {
            key: inherited[key]
            for key in ("PATH", "LANG", "LC_ALL")
            if key in inherited and isinstance(inherited[key], str)
        }
        environment.update(
            {
                "HOME": str(working_directory / ".repoforge-empty-home"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        result = commands.run_isolated(
            [
                "git",
                "config",
                "--local",
                "--get",
                f"remote.{selected_repository.remote}.url",
            ],
            cwd=selected_repository.path,
            environment=environment,
            secrets=(),
            output_limit=16_384,
        )
        raw = result.stdout.strip()
        if not raw:
            raise _auth_observation_error(
                ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
                "The configured repository remote is missing.",
                next_action="Review the repository remote before migrating identity.",
            )
        return raw

    def local_migration_observation(
        selected_repository: RepositoryConfig,
        raw_remote: str,
        provider_host: str,
    ) -> RepositoryIdentityObservation:
        parsed = remote_parser.parse(raw_remote)
        return RepositoryIdentityObservation(
            provider=RepositoryProvider.GITHUB,
            provider_host=provider_host,
            repository_id="0",
            canonical_name=f"{provider_host}/{parsed.owner}/{parsed.repository}",
            exists=False,
            observed_at=clock.now_iso(),
            config_revision=config_revision,
        )

    def observe_migration(repo_id: str, login: str | None) -> RepositoryIdentityObservation:
        del login
        selected_repository = repository(repo_id)
        raw_remote = raw_migration_remote(selected_repository)
        return local_migration_observation(selected_repository, raw_remote, "github.com")

    def resolve_migration_endpoint(
        repo_id: str,
        *,
        provider_host: str,
        login: str,
        expected_actor_id: str,
    ) -> RepositoryEndpointSnapshot:
        selected_repository = repository(repo_id)
        raw_remote = raw_migration_remote(selected_repository)
        try:
            remote, endpoint = endpoint_authority.resolve(
                raw_remote,
                provider_host=provider_host,
                expected_login=login,
                expected_actor_id=expected_actor_id,
            )
        except RepoForgeError as exc:
            remote = remote_parser.parse(raw_remote)
            return RepositoryEndpointSnapshot(
                observation=local_migration_observation(
                    selected_repository,
                    raw_remote,
                    provider_host,
                ),
                remote=remote,
                ssh_endpoint=None,
                blocking_code=exc.code,
                blocking_detail=str(exc),
            )
        secret, context = _named_account_context(
            commands,
            cwd=working_directory,
            profile_id=login,
            host=provider_host,
            login=login,
            target_id=repo_id,
        )
        try:
            observation = observer.observe(
                selected_repository,
                expected_provider_host=provider_host,
                config_revision=config_revision,
                context=context,
                reviewed_ssh_endpoint=endpoint,
            )
        finally:
            secret.release()
        return RepositoryEndpointSnapshot(
            observation=observation,
            remote=remote,
            ssh_endpoint=endpoint,
        )

    accounts = GhCliNamedAccountDiscovery(commands, cwd=working_directory)
    ssh = ssh_discovery
    return AuthCommandDependencies(
        service=AuthUxService(
            config=config,
            bindings=JsonRepositoryBindingStore(
                config.server.state_root, build_lock_manager(config.server.state_root)
            ),
            observe=observe_selected,
            clock=clock,
        ),
        migration=AuthMigrationService(
            store=store,
            clock=clock,
            ids=id_generator(),
            accounts=accounts,
            ssh=ssh,
            ambient=GitAmbientAuthConflictReader(commands),
            observe=observe_migration,
            resolve_endpoint=resolve_migration_endpoint,
        ),
        accounts=accounts,
        ssh=ssh,
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


def build_runtime_activation_store(
    state_root: Path | None = None, *, locks: LockManager | None = None
) -> JsonRuntimeActivationStore:
    root = (state_root or default_state_root()).expanduser().resolve()
    return JsonRuntimeActivationStore(root, locks or build_lock_manager(root))


def build_runtime_activation_journal(
    state_root: Path | None = None,
    *,
    locks: LockManager | None = None,
    ids: IdGenerator | None = None,
    clock: Clock | None = None,
) -> RuntimeActivationJournal:
    root = (state_root or default_state_root()).expanduser().resolve()
    selected_locks = locks or build_lock_manager(root)
    return RuntimeActivationJournal(
        operations=JsonOperationStore(root, selected_locks),
        receipts=build_runtime_activation_store(root, locks=selected_locks),
        ids=ids or id_generator(),
        clock=clock or system_clock(),
    )


def build_tunnel_profile_store(path: Path) -> TunnelProfileStore:
    return JsonTunnelProfileStore(path, LocalFileSystem())


def build_runtime_control_client(path: Path) -> RuntimeControlClient:
    return UnixRuntimeControlClient(path)


def build_runtime_control_server(path: Path) -> RuntimeControlServer:
    return UnixRuntimeControlServer(path)


def build_runtime_launcher() -> RuntimeLauncher:
    return SubprocessRuntimeLauncher()


def build_release_store(
    root: Path | None = None, *, manage_path_launcher: bool = False
) -> RuntimeReleaseStore:
    from .adapters.activation.release_store import RuntimeReleaseStore as _Store
    from .domain.user_paths import resolve_release_root

    # The PATH launcher lives outside the release root, so the store only gets to manage
    # it when the caller explicitly opts in (never for a temporary --release-root).
    return _Store(
        resolve_release_root(root),
        path_launcher=_Store.default_path_launcher() if manage_path_launcher else None,
    )


def build_execution_worker_binding_store(state_root: Path) -> JsonExecutionWorkerBindingStore:
    """The durable execution-worker binding collection for one state root."""
    from .adapters.persistence import JsonExecutionWorkerBindingStore as _Store

    return _Store(state_root, build_lock_manager(state_root))


def build_lease_shadow_store(state_root: Path) -> SqliteLeaseStore:
    """The shadow lease registry mirroring JSON bindings for parity checking.

    Shadow-only: no production safety gate reads from SQLite. A write failure here
    must never fail the authoritative JSON registration, so callers treat it as
    best-effort parity evidence.
    """
    return SqliteLeaseStore(state_root / "runtime-leases-shadow.db")


def build_admission_epoch_store(state_root: Path) -> AdmissionEpochStore:
    """The durable worker-admission epoch shared by registrar and restarter (P1-3)."""
    from .adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore

    return JsonAdmissionEpochStore(state_root)


def build_worker_registrar(state_root: Path) -> WorkerRegistrar:
    """The pre-spawn worker registrar: authoritative JSON lease + shadow mirror.

    The JSON ProcessLease store is authoritative for the intent -> running lease
    lifecycle (F-001); the SQLite shadow mirrors it for parity checking. The
    durable admission epoch (P1-3) is shared with the restarter so a spawn cannot
    begin once the restarter has fenced admission.
    """
    from .adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from .adapters.persistence.json_process_lease_adapter import JsonProcessLeaseAdapter

    return WorkerRegistrar(
        leases=JsonProcessLeaseAdapter(state_root, build_lock_manager(state_root)),
        ids=id_generator(),
        clock=system_clock(),
        shadow=build_lease_shadow_store(state_root),
        epochs=JsonAdmissionEpochStore(state_root),
        # P1-3: the same lock manager the restarter receives, so the registrar's
        # OPEN check + intent create and the restarter's fence -> stop are
        # mutually exclusive across processes.
        locks=build_lock_manager(state_root),
    )


def build_worker_lifecycle_store(state_root: Path) -> WorkerLifecycleStore:
    """The shared worker-lifecycle outcome store (single authority, F-010).

    Normal termination and startup reconciliation both apply reaping outcomes
    through this one service so the canonical ProcessLease, the binding
    projection, and the SQLite shadow can never diverge.
    """
    from .adapters.persistence.json_process_lease_adapter import JsonProcessLeaseAdapter
    from .application.runtime.worker_lifecycle import WorkerLifecycleStore

    bindings = build_execution_worker_binding_store(state_root)
    leases = JsonProcessLeaseAdapter(state_root, build_lock_manager(state_root))
    return WorkerLifecycleStore(
        bindings=bindings,
        leases=leases,
        shadow=build_lease_shadow_store(state_root),
        now_iso=system_clock().now_iso,
    )


def build_execution_worker_reconciler(state_root: Path) -> ExecutionWorkerReconciler:
    """Reclaim orphaned execution workers of departed releases before a new start."""
    from .adapters.persistence.json_process_lease_adapter import JsonProcessLeaseAdapter
    from .adapters.runtime.state_store import process_identity
    from .adapters.subprocess.os_process_reaper import OsProcessReaper
    from .adapters.subprocess.process_tree import (
        group_has_live_member,
        read_command_line,
        read_identity,
    )
    from .application.runtime.execution_worker_reconciler import ExecutionWorkerReconciler
    from .application.runtime.worker_lifecycle import WorkerLifecycleStore

    bindings = build_execution_worker_binding_store(state_root)
    leases = JsonProcessLeaseAdapter(state_root, build_lock_manager(state_root))
    return ExecutionWorkerReconciler(
        bindings=bindings,
        reaper=OsProcessReaper(),
        owner_identity_reader=process_identity,
        command_line_reader=read_command_line,
        identity_reader=read_identity,
        process_group_gone=lambda pgid: group_has_live_member(pgid) is False,
        leases=leases,
        now_iso=system_clock().now_iso,
        lifecycle=WorkerLifecycleStore(
            bindings=bindings,
            leases=leases,
            shadow=build_lease_shadow_store(state_root),
            now_iso=system_clock().now_iso,
        ),
    )


def build_execution_worker_report_section(state_root: Path) -> dict[str, object]:
    """Execution-worker evidence for `rf doctor` / `rf runtime ls`, read-only."""
    from .adapters.persistence.json_process_lease_adapter import JsonProcessLeaseAdapter
    from .adapters.runtime.state_store import process_identity
    from .adapters.subprocess.process_tree import (
        group_has_live_member,
        read_command_line,
        read_identity,
    )
    from .application.runtime.execution_worker_report import build_execution_worker_report

    return build_execution_worker_report(
        bindings=build_execution_worker_binding_store(state_root),
        lock_root=state_root / "locks",
        owner_identity_reader=process_identity,
        command_line_reader=read_command_line,
        identity_reader=read_identity,
        process_group_gone=lambda pgid: group_has_live_member(pgid) is False,
        leases=JsonProcessLeaseAdapter(state_root, build_lock_manager(state_root)),
    ).as_dict()


def build_upgrade_service(
    *,
    release_root: Path | None,
    supervisor_socket: Path,
    runtime_record_path: Path,
    config_path: Path,
    correlation_id: str,
    extra_env: dict[str, str] | None = None,
    kickstarter: SupervisorKickstarter | None = None,
    manage_path_launcher: bool = False,
    inspect_release_processes: bool = False,
) -> UpgradeService:
    from .adapters.activation.build import (
        GitWorktreeInspector,
        RuntimeRecordReleaseObserver,
        SubprocessReleaseSmokeTester,
        SupervisorHealthProbe,
        SupervisorRestarter,
        UvVenvReleaseInstaller,
        UvWheelBuilder,
    )
    from .adapters.activation.launcher import ReleaseAwareRuntimeLauncher
    from .adapters.background import SystemSleeper
    from .application.activation.upgrade import UpgradeService as _Service

    control_client = build_runtime_control_client(supervisor_socket)
    runtime_store = build_runtime_store(runtime_record_path)
    sleeper = SystemSleeper()
    store = build_release_store(release_root, manage_path_launcher=manage_path_launcher)
    return _Service(
        store=store,
        inspector=GitWorktreeInspector(),
        builder=UvWheelBuilder(),
        installer=UvVenvReleaseInstaller(),
        smoke=SubprocessReleaseSmokeTester(),
        restarter=SupervisorRestarter(
            control=control_client,
            runtime=runtime_store,
            # NOT build_runtime_launcher(): that spawns with the *calling* CLI's
            # sys.executable, which is the old release, so the candidate would never be
            # adopted. The shim resolves through `current`.
            launcher=ReleaseAwareRuntimeLauncher(store.bin_launcher()),
            config_path=config_path,
            correlation_id=correlation_id,
            extra_env=extra_env,
            sleeper=sleeper,
            kickstarter=kickstarter,
            # The supervisor records worker bindings under the config state root (the
            # same root the runtime record lives in); the restarter must read THAT store.
            worker_reconciler=build_execution_worker_reconciler(runtime_record_path.parent),
            # P1-3: the same durable admission epoch the registrar consults, so the
            # restarter's fence is visible to every spawn path.
            admission_epochs=build_admission_epoch_store(runtime_record_path.parent),
            # P1-3: the SAME lock manager the registrar receives (same state root),
            # so the restarter's fence -> final observation -> stop -> reopen window
            # and the registrar's OPEN check + intent create are mutually exclusive.
            locks=build_lock_manager(runtime_record_path.parent),
        ),
        observer=RuntimeRecordReleaseObserver(
            runtime=runtime_store, releases_root=store.root / "releases"
        ),
        clock=system_clock(),
        health_probe=SupervisorHealthProbe(control_client, correlation_id=correlation_id),
        sleeper=sleeper,
        locks=build_lock_manager(store.root / "runtime"),
        # F-009: the effect-owning runtime-transition coordinator, so the activation
        # ledger is the recovery authority wired into every upgrade path.
        transitions=build_runtime_transition_coordinator(store.root / "runtime"),
        # Retention decides from pointers and recency, which says nothing about what is
        # running, so prune consults the process table before deleting a release tree.
        release_processes=(
            build_release_process_inspector(release_root=release_root)
            if inspect_release_processes
            else None
        ),
    )


def build_runtime_transition_coordinator(state_root: Path) -> RuntimeTransitionCoordinator:
    """The effect-owning runtime-transition coordinator (F-009)."""
    from .adapters.persistence.json_runtime_transition_adapter import (
        JsonRuntimeTransitionAdapter,
    )
    from .application.runtime.runtime_transition_coordinator import (
        RuntimeTransitionCoordinator as _Coordinator,
    )

    return _Coordinator(
        transitions=JsonRuntimeTransitionAdapter(state_root, build_lock_manager(state_root)),
        ids=id_generator(),
        clock=system_clock(),
    )


def build_release_process_inspector(*, release_root: Path | None) -> ReleaseProcessInspector:
    """Inspect which live processes execute from this release root."""
    from .adapters.activation.release_processes import SystemReleaseProcessInspector

    return SystemReleaseProcessInspector(build_release_store(release_root).root / "releases")


def build_release_observer(
    *, release_root: Path | None, runtime_record_path: Path
) -> ReleaseObserver:
    from .adapters.activation.build import RuntimeRecordReleaseObserver

    store = build_release_store(release_root)
    return RuntimeRecordReleaseObserver(
        runtime=build_runtime_store(runtime_record_path), releases_root=store.root / "releases"
    )


def build_dev_runtime_service(
    *, base_config: Path, base_state_root: Path | None = None
) -> DevRuntimeService:
    from .adapters.activation.dev_config import TomlDevConfigProvisioner
    from .application.activation.dev_runtime import DevRuntimeService as _Service

    return _Service(
        launcher=build_runtime_launcher(),
        provisioner=TomlDevConfigProvisioner(),
        runtime_store_factory=build_runtime_store,
        base_config=base_config,
        base_state_root=base_state_root or default_state_root(),
    )


def build_generation_handoff_reconciler(
    *, state_root: Path, locks: LockManager
) -> GenerationHandoffReconciler:
    from .adapters.persistence.json_worker_binding_store import JsonWorkerBindingStore
    from .adapters.subprocess.os_process_reaper import OsProcessReaper
    from .application.activation.handoff import GenerationHandoffReconciler as _Reconciler

    return _Reconciler(
        bindings=JsonWorkerBindingStore(state_root, locks),
        reaper=OsProcessReaper(),
    )


def build_supervisor_kickstarter(
    *,
    launcher_path: Path,
    config_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    inherited_env: dict[str, str],
    agents_dir: Path,
    label: str,
) -> SupervisorKickstarter:
    """The launchd job as a kickstarter, so activation keeps OS ownership."""
    from .adapters.activation.launchd import LaunchAgentSpec, LaunchdRegistrar

    return LaunchdRegistrar(
        spec=LaunchAgentSpec(
            label=label,
            launcher_path=launcher_path,
            config_path=config_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            inherited_env=inherited_env,
        ),
        agents_dir=agents_dir,
    )


def build_supervisor_launch_agent_registrar(
    *,
    launcher_path: Path,
    config_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    inherited_env: dict[str, str],
    agents_dir: Path,
    label: str,
) -> ProcessSupervisorRegistrar:
    from .adapters.activation.launchd import LaunchAgentSpec, LaunchdRegistrar

    spec = LaunchAgentSpec(
        label=label,
        launcher_path=launcher_path,
        config_path=config_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        inherited_env=inherited_env,
    )
    return LaunchdRegistrar(spec=spec, agents_dir=agents_dir)


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


def build_effect_receipt_store(
    state_root: Path,
    locks: LockManager | None = None,
) -> EffectReceiptStore:
    return JsonEffectReceiptStore(state_root, locks or build_lock_manager(state_root))


def _background_operation_liveness(
    task: OperationTask,
    *,
    locks: LockManager,
    processes: ProcessInspector,
) -> bool | None:
    """Return direct worker liveness when the operation owns a workspace lock."""

    if (
        task.kind
        not in {"workspace_run_profile", "workspace_run_adhoc", "workspace_run_diagnostic"}
        or task.workspace_id is None
    ):
        return None

    owner_pid: int | None = None
    lock_path = locks.path_for(task.workspace_id)
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
        candidate = raw.get("pid") if isinstance(raw, dict) else None
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            owner_pid = candidate
    except (OSError, json.JSONDecodeError):
        pass

    try:
        with locks.lock(
            task.workspace_id,
            timeout_seconds=0,
            metadata={"purpose": "startup_operation_liveness_probe"},
        ):
            pass
    except RepoForgeError as exc:
        if exc.code is ErrorCode.LOCK_TIMEOUT:
            return True
        raise

    if owner_pid is None:
        return None
    return processes.identity(owner_pid) is not None


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


def _active_runtime_for_reconciliation(path: Path) -> RuntimeRecord | None:
    """Read the runtime record for startup reconciliation, tolerating one it cannot use.

    Startup reads this record to decide whether a pending activation receipt actually
    landed. That is a sweep, not a caller asking for this exact record, so it follows
    the same rule as every other durable store swept here: one unusable record must not
    stop the process from starting. `JsonRuntimeStore.read` stays strict for direct
    reads -- `rf runtime status` has to keep reporting a bad record rather than quietly
    calling it absent.

    Without this, an upgrade that makes the record unreadable is unrecoverable in place:
    `rf` runs the active release's code, so the tool an operator would reach for runs
    the same decoder that is rejecting the record, and every release carrying the defect
    is equally unusable. That is what happened on 2026-07-29.

    `None` is the conservative answer, not a guess: the reconciler already handles it as
    "no runtime to compare against", which can only downgrade a receipt to a manually
    resolvable outcome. It never lets one be recorded as activated.
    """
    try:
        return build_runtime_store(path).read()
    except ConfigError:
        return None


def build_application(
    config: AppConfig,
    *,
    overrides: AdapterOverrides | None = None,
    config_generation: int = 0,
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
    execution = ExecutionCoordinator(execution_environment)
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
    commit_identities = o.commit_identities or GitCommitIdentityGateway(command)
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
    github_capabilities = o.github_capabilities or CommandGitHubCapabilityProbe(
        command, config.server
    )
    github_capability_preflight = o.github_capability_preflight or CommandGitHubCapabilityPreflight(
        command, config.server
    )
    nested_resource_discovery = (
        o.nested_resource_discovery
        if o.nested_resource_discovery is not None
        else GitNestedResourceDiscovery(command)
    )
    ids = o.ids or UuidGenerator()
    executables = o.executables or SystemExecutableLocator()
    provider_registry = o.provider_registry or ConfigProviderRegistry(config.providers, executables)
    repository_bindings = o.repository_bindings or JsonRepositoryBindingStore(
        config.server.state_root, locks
    )
    config_revision = _production_config_revision(config, config_generation)
    policy_revision = hashlib.sha256(
        f"{config_revision}\0repository-auth-policy-v1".encode()
    ).hexdigest()
    identity_cwd = config.source_path.expanduser().resolve().parent
    credential_commands = cast(SubprocessCommandExecutor, command)
    stored_accounts = tuple(
        configured.api_identity
        for configured in config.auth_profiles.values()
        if isinstance(configured.api_identity, StoredGhAccountSpec)
    )
    app_installations = tuple(
        configured.api_identity
        for configured in config.auth_profiles.values()
        if isinstance(configured.api_identity, GitHubAppInstallationSpec)
    )
    auth_provider = GitHubApiAuthProvider(
        stored_accounts=stored_accounts,
        app_installations=app_installations,
        stored_source=GhCliStoredAccountTokenSource(
            credential_commands,
            cwd=identity_cwd,
            clock=clock,
        ),
        app_issuer=UnavailableGitHubAppInstallationTokenIssuer(),
        verifier=GhCliGitHubApiIdentityVerifier(command, cwd=identity_cwd),
        capability_preflight=github_capability_preflight,
        cwd=identity_cwd,
        config_revision=config_revision,
        policy_revision=policy_revision,
    )
    auth_broker = RepositoryAuthBroker(auth_provider)
    production_remote_parser = ConstrainedGitRemoteParser()
    effective_home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve()
    production_key_identity = FileSshKeyIdentity(
        command,
        material_root=(config.server.state_root / "ssh-material").expanduser().resolve(),
    )
    production_endpoint_authority = ReviewedSshEndpointAuthority(
        remote_parser=production_remote_parser,
        aliases=ConstrainedSshConfigAliasResolver(
            paths=EffectiveUserPaths(
                home=effective_home,
                ssh_config=effective_home / ".ssh" / "config",
            )
        ),
        keys=production_key_identity,
        principals=GitHubSshPrincipalVerifier(
            command,
            materials=production_key_identity,
            cwd=identity_cwd,
        ),
        clock=clock,
    )
    repository_observer = GhCliRepositoryObserver(
        command,
        clock=clock,
        remote_parser=production_remote_parser,
    )

    def observe_repository_identity(
        repo_id: str,
        selector: AuthProfileSelector,
    ) -> RepositoryIdentityObservation:
        repository = config.repositories.get(repo_id)
        if repository is None:
            raise ConfigError(f"Unknown repository id: {repo_id}")
        configured = _runtime_observation_profile(
            config=config,
            bindings=repository_bindings,
            observer=repository_observer,
            repository=repository,
            selector=selector,
            config_revision=config_revision,
            observed_at=clock.now_iso(),
        )
        with auth_broker.session(
            AuthBrokerRequest(
                profile=configured.profile,
                target_kind=AuthTargetKind.REPOSITORY,
                target_id=configured.api_identity.repository_id,
                required_capability_ids=(),
                allowed_environment_keys=("GH_TOKEN",),
                now=clock.now_iso(),
            )
        ) as session:
            auth_context = session.process_context(command.environment())
            observation = repository_observer.observe(
                repository,
                expected_provider_host=configured.api_identity.host,
                config_revision=config_revision,
                context=auth_context,
                reviewed_ssh_endpoint=configured.transport.ssh_endpoint,
            )
        if (
            observation.provider_host != configured.api_identity.host
            or observation.repository_id != configured.api_identity.repository_id
        ):
            raise _auth_observation_error(
                ErrorCode.GITHUB_API_REPOSITORY_MISMATCH,
                "The admitted profile observed a different stable repository identity.",
                next_action="Review the profile repository_id and checkout remote before retrying.",
            )
        return observation

    git_transport_router = o.git_transport_router or GitTransportRouter(
        command,
        endpoint_revalidator=production_endpoint_authority,
        identity_materials=production_key_identity,
    )
    repository_identity_runtime = o.repository_identity_runtime or RepositoryIdentityRuntime(
        config=config,
        bindings=repository_bindings,
        broker=auth_broker,
        observe=observe_repository_identity,
        clock=clock,
    )
    code_intelligence = o.code_intelligence or FallbackCodeIntelligenceProvider(
        primary=TreeSitterCodeIntelligenceProvider(),
        fallback=SyntaxCodeIntelligenceProvider(),
    )
    metrics = o.metrics or JsonMetricsSink(config.server.state_root, locks, clock)
    idempotency = o.idempotency or JsonIdempotencyStore(config.server.state_root)
    execution_plans = o.execution_plans or JsonExecutionPlanStore(config.server.state_root, locks)
    execution_plan_acceptances = o.execution_plan_acceptances or JsonExecutionPlanAcceptanceStore(
        config.server.state_root, locks
    )
    execution_receipts = o.execution_receipts or JsonExecutionReceiptStore(
        config.server.state_root, locks
    )
    effect_receipts = o.effect_receipts or JsonEffectReceiptStore(config.server.state_root, locks)
    iteration_cache = o.iteration_cache or JsonIterationCache(config.server.state_root, locks)
    failure_evidence = o.failure_evidence or JsonFailureEvidenceStore(
        config.server.state_root, locks
    )
    failure_output_artifacts = o.failure_output_artifacts or FileFailureOutputArtifactStore(
        config.server.state_root
    )
    operation_store = o.operations or JsonOperationStore(config.server.state_root, locks)
    operation_identities = o.operation_identities or JsonOperationIdentityStore(
        config.server.state_root,
        locks,
    )
    operation_work_queue = o.operation_work_queue or JsonOperationWorkQueue(
        config.server.state_root,
        locks,
    )
    worker_bindings = o.worker_bindings or JsonWorkerBindingStore(config.server.state_root, locks)
    reaper = o.reaper or OsProcessReaper()
    operation_result_store = o.operation_results or JsonOperationResultStore(
        config.server.state_root,
        locks,
        max_result_bytes=config.server.max_tool_output_chars,
    )
    github_read_cache = o.github_read_cache or JsonGitHubReadCache(config.server.state_root, locks)
    hygiene = o.hygiene or CommandHygieneGateway(execution)
    hygiene_cache = o.hygiene_cache or JsonHygieneBaselineCache(config.server.state_root, locks)
    pr_check_watch_store = o.pr_check_watches or JsonPrCheckWatchStore(
        config.server.state_root,
        locks,
    )
    workflow_recording_store = o.workflow_recordings or JsonWorkflowRecordingStore(
        config.server.state_root,
        locks,
    )
    issue_graph_proposals = o.issue_graph_proposals or JsonIssueGraphProposalStore(
        config.server.state_root,
        locks,
    )
    issue_graph_publications = o.issue_graph_publications or JsonIssueGraphPublicationStore(
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
        commit_identities=commit_identities,
        filesystem=filesystem,
        file_transactions=file_transactions,
        store=store,
        locks=locks,
        gate=gate,
        audit=audit,
        clock=clock,
        ids=ids,
        executables=executables,
        execution=execution,
        provider_registry=provider_registry,
        repository_bindings=repository_bindings,
        repository_identity_runtime=repository_identity_runtime,
        git_transport_router=git_transport_router,
        code_intelligence=code_intelligence,
        metrics=metrics,
        idempotency=idempotency,
        operation_store=operation_store,
        operation_work_queue=operation_work_queue,
        operation_identities=operation_identities,
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
        github_capabilities=github_capabilities,
        github_capability_preflight=github_capability_preflight,
        nested_resource_discovery=nested_resource_discovery,
        nested_target_resolver=o.nested_target_resolver,
        nested_lease_provider=o.nested_lease_provider,
        execution_plans=execution_plans,
        execution_plan_acceptances=execution_plan_acceptances,
        execution_receipts=execution_receipts,
        effect_receipts=effect_receipts,
        iteration_cache=iteration_cache,
        failure_evidence=failure_evidence,
        failure_output_artifacts=failure_output_artifacts,
        worker_bindings=worker_bindings,
        reaper=reaper,
        publications=o.publications,
        config_generation=config_generation,
    )
    if context.publications is None:
        publication_gateway = PublicationAdapter(
            commands=command,
            repositories=DurableBindingPublicationRepositoryResolver(repository_bindings),
            authorization=PinnedPublicationAuthorizationGateway(),
            capability_preflight=github_capability_preflight,
            transport=git_transport_router,
            github=default_github,
            clock=clock.now_iso,
        )
        publication_coordinator = PublicationCoordinator(
            context,
            gateway=publication_gateway,
            identities=OperationIdentityManager(
                operations=operation_store,
                identities=operation_identities,
            ),
        )
        publication_requests = ScopedWorkspacePublicationRequestFactory(
            config=config,
            runtime=repository_identity_runtime,
            commands=command,
            clock=clock,
            ids=ids,
            config_revision=config_revision,
            policy_revision=policy_revision,
        )
        context = replace(
            context,
            publications=CoordinatedWorkspacePublicationService(
                publication_coordinator,
                publication_requests,
            ),
        )
    operations = OperationManager(context)
    processes = build_process_inspector()
    runtime_activation_store = build_runtime_activation_store(
        config.server.state_root,
        locks=locks,
    )
    runtime_activation_journal = RuntimeActivationJournal(
        operations=operation_store,
        receipts=runtime_activation_store,
        ids=ids,
        clock=clock,
    )
    RuntimeActivationReconciler(
        journal=runtime_activation_journal,
        receipts=runtime_activation_store,
        operations=operation_store,
    ).reconcile(
        active_runtime=_active_runtime_for_reconciliation(
            config.server.state_root / "managed-runtime-v3.json"
        )
    )
    OutcomeReceiptReconciler(context).reconcile(
        stale_after_seconds=config.server.idempotency_stale_seconds,
        resumable_actions=frozenset({"issue_graph_publication"}),
    )
    # Durable work is reconciled first: its sidecar carries the exact evidence
    # (lease, ownership, whether a child was ever spawned) needed to tell work that
    # can be requeued from work whose outcome is ambiguous. Only afterwards does the
    # generic sweep judge whatever is still marked running, so a claim that crashed
    # before spawning is requeued rather than orphaned on a guess.
    recover_operation_work(
        operations,
        operation_work_queue,
        now=clock.now_iso(),
        expected_config_generation=config_generation or None,
        worker_bindings=worker_bindings,
        reaper=reaper,
    )
    recover_operations(
        operations,
        now=clock.now_iso(),
        running_stale_seconds=config.server.idempotency_stale_seconds,
        resumable_kinds=frozenset(
            {
                "pr_check_watch",
                "runtime_activation",
                "issue_graph_publication",
            }
        ),
        running_liveness=lambda task: _background_operation_liveness(
            task,
            locks=locks,
            processes=processes,
        ),
        worker_bindings=worker_bindings,
        reaper=reaper,
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
        issue_graph_proposals,
        issue_graph_publications,
    )


def _load_dotenv_if_present(path: Path) -> None:
    """Fill in unset environment variables from a simple ``KEY=VALUE`` .env file.

    Never overrides a variable the environment already provides -- an explicit
    shell export always wins over a stored default. Deliberately minimal (no
    python-dotenv dependency): the managed runtime is often launched without
    an inherited shell environment (a different terminal, cron, a supervisor
    restart), so a secret like CONTROL_PLANE_API_KEY that only lives in a
    project .env file would otherwise never reach the process, and startup
    fails with ConfigError every time no matter how many times it's retried.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


def _agent_secret_from_file() -> dict[str, str]:
    """Read the durable agent credential when the supervisor shim pointed us at one.

    The launchd shim deliberately does NOT source the file -- sourcing would execute its
    contents as shell -- so it passes only the path and the credential is opened here, on
    the process that needs it. Every security invariant (regular file, owner, mode 0600,
    exactly one allowlisted key, non-empty value) is re-proven on this start, on the same
    file descriptor the bytes are read from. A file that was safe when
    ``rf runtime install-agent`` ran but has since been chmodded or replaced by a symlink
    is refused here, at the trust boundary, however many months later that boot happens.

    An explicit ``CONTROL_PLANE_API_KEY`` in the environment wins, so this only runs when
    the process has no inherited credential -- which is always the case under launchd.
    """
    from .adapters.activation.release_store import inspect_agent_secret

    raw_path = os.environ.get(AGENT_SECRET_FILE_ENV_VAR, "")
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    status, secret = inspect_agent_secret(path)
    if not status.usable:
        raise ConfigError(
            f"AGENT_SECRET_UNUSABLE: {AGENT_SECRET_FILE_ENV_VAR} points at {path}, which "
            "cannot be trusted (" + "; ".join(status.problems()) + "); re-run "
            "`rf runtime install-agent --persist-api-key` to restore it."
        )
    return {AGENT_SECRET_KEY: secret}


def _claim_child_lease_if_spawned(configs: ConfigurationStore, generation: int) -> int | None:
    """Child-side handshake for a worker spawned with a pre-spawn lease (F-001).

    Returns an exit code when the worker must not start working (superseded or
    lease conflict), None to continue. Runs only when the parent passed the
    lease id via env; a directly-launched worker has no lease to claim.
    """
    from .adapters.persistence.json_process_lease_adapter import JsonProcessLeaseAdapter
    from .adapters.runtime.execution_worker import (
        LEASE_ID_ENV,
        STATE_ROOT_ENV,
        SUPERVISOR_IDENTITY_ENV,
        SUPERVISOR_PID_ENV,
    )
    from .adapters.runtime.state_store import process_identity
    from .adapters.subprocess.process_tree import read_identity
    from .application.runtime.child_lease_claim import claim_child_lease

    lease_id = os.environ.get(LEASE_ID_ENV, "")
    if not lease_id:
        return None
    try:
        supervisor_pid = int(os.environ.get(SUPERVISOR_PID_ENV, "0") or 0)
    except ValueError:
        supervisor_pid = 0
    supervisor_identity = os.environ.get(SUPERVISOR_IDENTITY_ENV, "")
    # The parent passes the exact state root it registered the lease under; fall
    # back to the config-derived root only when no parent supplied one.
    root = Path(os.environ.get(STATE_ROOT_ENV, "") or configs.root)
    own_identity = process_identity(os.getpid())
    proc_identity = read_identity(os.getpid())
    claim = claim_child_lease(
        leases=JsonProcessLeaseAdapter(root, build_lock_manager(root)),
        registrar=build_worker_registrar(root),
        bindings=build_execution_worker_binding_store(root),
        lease_id=lease_id,
        supervisor_pid=supervisor_pid,
        supervisor_process_identity=supervisor_identity,
        generation=generation,
        release_sha=os.environ.get("REPOFORGE_RUNNING_RELEASE_SHA"),
        identity=own_identity,
        start_token=proc_identity.start_token if proc_identity is not None else None,
        now=system_clock().now_iso(),
        supervisor_identity_reader=process_identity,
    )
    if claim.exit_code is not None:
        return claim.exit_code
    return None


def run_execution_worker(config_path: Path, *, generation: int) -> int:
    """Run the generation-bound durable execution loop in its own process."""
    import signal
    import threading

    from .adapters.runtime.execution_worker import LEASE_ID_ENV, STATE_ROOT_ENV
    from .application.activation.handoff import (
        GenerationHandoffReconciler,
        OwnerIdentity,
        reconcile_before_admit,
    )
    from .application.operations.work_executor import VerificationWorkHandlers
    from .application.operations.work_loop import OperationWorkLoop
    from .application.workspace.run_adhoc import WorkspaceAdhocRunner
    from .application.workspace.run_diagnostic import WorkspaceDiagnosticRunner
    from .application.workspace.run_profile import WorkspaceProfileRunner

    if generation <= 0:
        raise ConfigError("Execution worker generation must be positive")
    config_path = config_path.expanduser().resolve()
    configs = build_configuration_store(config_path)
    claim_exit = _claim_child_lease_if_spawned(configs, generation)
    if claim_exit is not None:
        return claim_exit
    resolved_path = configs.resolved_path(generation)
    config = load_config(resolved_path)
    application = build_application(config, config_generation=generation)
    ctx = application.context
    worker_id = os.environ.get(LEASE_ID_ENV, "")
    worker_binding_store = (
        build_execution_worker_binding_store(
            Path(os.environ.get(STATE_ROOT_ENV, "") or configs.root)
        )
        if worker_id
        else None
    )

    def publish_worker_heartbeat(
        state: str,
        operation_id: str | None,
        recovery_completed: bool,
    ) -> None:
        if worker_binding_store is None:
            return
        updated = worker_binding_store.update_heartbeat(
            worker_id,
            heartbeat_at=ctx.clock.now_iso(),
            loop_state=state,
            current_operation_id=operation_id,
            recovery_completed=recovery_completed,
        )
        if updated is None:
            raise ConfigError("Execution worker durable binding disappeared during heartbeat")

    if ctx.worker_bindings is not None and ctx.reaper is not None:
        server_pid = os.getpid()
        admit = reconcile_before_admit(
            reconciler=GenerationHandoffReconciler(bindings=ctx.worker_bindings, reaper=ctx.reaper),
            current_owner=OwnerIdentity(
                server_pid=server_pid,
                server_start_token=ctx.reaper.read_start_token(server_pid),
                generation=generation,
            ),
            audit=ctx.audit,
        )
        if admit.exit_code is not None:
            return admit.exit_code
    handlers = VerificationWorkHandlers(
        WorkspaceProfileRunner(application.context),
        WorkspaceAdhocRunner(application.context),
        WorkspaceDiagnosticRunner(application.context),
    )
    loop = OperationWorkLoop(
        application.context,
        application.operations,
        handlers,
        owner_id=worker_id or None,
        worker_heartbeat=publish_worker_heartbeat,
    )
    stop = threading.Event()
    previous_handlers: dict[signal.Signals, object] = {}

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()
        loop.request_stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signal.Signals(signum)] = signal.signal(signum, request_stop)
    try:
        loop.run_until_stopped(stop)
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


def run_runtime_worker(
    config_path: Path,
    *,
    connector_identity: str = "forge_v2",
) -> int:
    """Construct and run the long-lived supervisor for the Forge v2 identity."""
    from .interfaces.mcp.server import FORGE_V2_IDENTITY, tool_surface_hash

    if connector_identity != FORGE_V2_IDENTITY:
        raise ConfigError("Managed runtime supports only the forge_v2 connector identity")
    config_path = config_path.expanduser().resolve()
    for dotenv_candidate in (config_path.parent / ".env", Path.cwd() / ".env"):
        _load_dotenv_if_present(dotenv_candidate)
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
    mcp_argv = (
        sys.executable,
        "-m",
        "repoforge",
        "--config",
        str(config_path),
        "serve",
        "--connector-identity",
        connector_identity,
    )
    profile = TunnelProfile(
        tunnel_id_fingerprint,
        profile_name,
        tunnel_executable,
        tunnel_version,
        mcp_argv,
        source.mcp_connection_max_ttl_seconds,
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
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "REPOFORGE_RUNNING_RELEASE_SHA",
        AGENT_SECRET_KEY,
    )
    environment = {key: os.environ[key] for key in inherited_keys if key in os.environ}
    environment["REPOFORGE_TUNNEL_ID"] = tunnel_id
    environment["REPOFORGE_TUNNEL_PROFILE"] = profile_name
    if not environment.get(AGENT_SECRET_KEY):
        environment.update(_agent_secret_from_file())
    if not environment.get(AGENT_SECRET_KEY):
        raise ConfigError(f"{AGENT_SECRET_KEY} is required for managed runtime startup")
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
        execution_worker=SubprocessExecutionWorker(
            config_path,
            bindings=build_execution_worker_binding_store(root),
            registrar=build_worker_registrar(root),
            lifecycle=build_worker_lifecycle_store(root),
            state_root=root,
        ),
        execution_worker_log_path=root / "execution-worker.log",
        preflight=validate_generated_contract_identity,
        worker_reconciler=build_execution_worker_reconciler(root),
    )
    return supervisor.run(
        generation=target.generation,
        profile=profile,
        tool_surface_hash=tool_surface_hash(),
        environment=environment,
    )
