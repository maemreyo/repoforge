"""Composition root: the only module that constructs concrete adapters."""

from __future__ import annotations
from dataclasses import dataclass
from .adapters.audit import JsonlAuditSink
from .adapters.capabilities import SystemExecutableLocator
from .adapters.filesystem import LocalFileSystem
from .adapters.git import GitCliRepository
from .adapters.github import GhCliGateway
from .adapters.persistence import JsonWorkspaceStore
from .adapters.subprocess import SubprocessCommandExecutor
from .adapters.system import SystemClock, UuidGenerator
from .application.context import ApplicationContext
from .config import AppConfig
from .ports import (
    AuditSink,
    Clock,
    CommandExecutor,
    ExecutableLocator,
    FileSystem,
    GitRepository,
    IdGenerator,
    PullRequestGateway,
    WorkspaceStore,
)


@dataclass(frozen=True, slots=True)
class AdapterOverrides:
    command: CommandExecutor | None = None
    store: WorkspaceStore | None = None
    audit: AuditSink | None = None
    clock: Clock | None = None
    ids: IdGenerator | None = None
    filesystem: FileSystem | None = None
    git: GitRepository | None = None
    github: PullRequestGateway | None = None
    executables: ExecutableLocator | None = None


@dataclass(frozen=True, slots=True)
class Application:
    context: ApplicationContext


def build_application(
    config: AppConfig, *, overrides: AdapterOverrides | None = None
) -> Application:
    o = overrides or AdapterOverrides()
    config.server.workspace_root.mkdir(parents=True, exist_ok=True)
    config.server.state_root.mkdir(parents=True, exist_ok=True)
    clock = o.clock or SystemClock()
    command = o.command or SubprocessCommandExecutor(config.server)
    store = o.store or JsonWorkspaceStore(config.server.state_root)
    audit = o.audit or JsonlAuditSink(config.server.state_root, clock)
    filesystem = o.filesystem or LocalFileSystem()
    git = o.git or GitCliRepository(command, config.server)
    github = o.github or GhCliGateway(command, config.server)
    ids = o.ids or UuidGenerator()
    executables = o.executables or SystemExecutableLocator()
    return Application(
        ApplicationContext(
            config,
            command,
            git,
            github,
            filesystem,
            store,
            audit,
            clock,
            ids,
            executables,
        )
    )
