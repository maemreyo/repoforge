"""Reusable deterministic harness adapters for application and crash tests."""

from .auth_fakes import DeterministicAuthMaterialProvider
from .fakes import (
    CleanupTracker,
    FailureInjector,
    FixedClock,
    InMemoryLockManager,
    InMemoryOperationGate,
    InMemoryWorkspaceStore,
    ResourceSnapshot,
    ScriptedCommandExecutor,
    SequenceIdGenerator,
)

__all__ = [
    "CleanupTracker",
    "DeterministicAuthMaterialProvider",
    "FailureInjector",
    "FixedClock",
    "InMemoryLockManager",
    "InMemoryOperationGate",
    "InMemoryWorkspaceStore",
    "ResourceSnapshot",
    "ScriptedCommandExecutor",
    "SequenceIdGenerator",
]
