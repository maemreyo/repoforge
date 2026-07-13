"""Sole production composition root for RepoForge concrete adapters."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .adapters.audit import JsonlAuditSink as JsonlAuditSink
from .adapters.capabilities import SystemExecutableLocator
from .adapters.configuration import ConfigGenerationStore
from .adapters.filesystem import LocalFileSystem
from .adapters.git import GitCliRepository
from .adapters.github import GhCliGateway
from .adapters.locking import FcntlLockManager as FcntlLockManager
from .adapters.observability import JsonMetricsSink
from .adapters.persistence import JsonIdempotencyStore
from .adapters.persistence import JsonWorkspaceStore as JsonWorkspaceStore
from .adapters.repository import LocalRepositoryProbe
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
from .application.configuration.source import parse_source
from .application.context import ApplicationContext
from .application.runtime.supervisor import RuntimeSupervisor
from .config import DEFAULT_STATE_ROOT, AppConfig, ServerConfig, load_config
from .domain.errors import ConfigError
from .domain.runtime import TunnelProfile
from .ports import (
    AuditSink,
    Clock,
    CommandExecutor,
    ConfigurationStore,
    ExecutableLocator,
    FileSystem,
    GitRepository,
    IdempotencyStore,
    IdGenerator,
    LockManager,
    MetricsSink,
    OperationGate,
    ProcessInspector,
    PullRequestGateway,
    RepositoryProbe,
    RuntimeControlClient,
    RuntimeControlServer,
    RuntimeLauncher,
    RuntimeStore,
    TunnelClient,
    TunnelProfileStore,
    WorkspaceStore,
)


@dataclass(frozen=True, slots=True)
class AdapterOverrides:
    command: CommandExecutor | None = None
    store: WorkspaceStore | None = None
    locks: LockManager | None = None
    gate: OperationGate | None = None
    audit: AuditSink | None = None
    clock: Clock | None = None
    ids: IdGenerator | None = None
    filesystem: FileSystem | None = None
    git: GitRepository | None = None
    github: PullRequestGateway | None = None
    executables: ExecutableLocator | None = None
    metrics: MetricsSink | None = None
    idempotency: IdempotencyStore | None = None


@dataclass(frozen=True, slots=True)
class Application:
    context: ApplicationContext


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


def build_operation_gate() -> OperationGate:
    return InProcessOperationGate()


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


def build_metrics_sink(state_root: Path, locks: LockManager | None = None) -> MetricsSink:
    return JsonMetricsSink(state_root, locks or build_lock_manager(state_root))


def build_idempotency_store(state_root: Path) -> IdempotencyStore:
    return JsonIdempotencyStore(state_root)


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
    git = o.git or GitCliRepository(command, config.server)
    github = o.github or GhCliGateway(command, config.server)
    ids = o.ids or UuidGenerator()
    executables = o.executables or SystemExecutableLocator()
    metrics = o.metrics or JsonMetricsSink(config.server.state_root, locks)
    idempotency = o.idempotency or JsonIdempotencyStore(config.server.state_root)
    return Application(
        ApplicationContext(
            config,
            command,
            git,
            github,
            filesystem,
            store,
            locks,
            gate,
            audit,
            clock,
            ids,
            executables,
            metrics,
            idempotency,
        )
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
