"""Provider-neutral models for journaled workspace filesystem transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


class TransactionError(RuntimeError):
    """Base class for filesystem transaction failures."""


class TransactionValidationError(TransactionError):
    """The complete plan cannot be applied to the current tree."""


class TransactionRecoveryError(TransactionError):
    """A primary failure was followed by an incomplete rollback."""


class SimulatedTransactionCrash(TransactionError):
    """Fault-injection signal that deliberately skips in-process rollback."""


@dataclass(frozen=True, slots=True)
class WriteFile:
    path: str
    data: bytes
    preserve_mode: bool = True


@dataclass(frozen=True, slots=True)
class CreateFile:
    path: str
    data: bytes
    mode: int = 0o644


@dataclass(frozen=True, slots=True)
class DeleteFile:
    path: str


@dataclass(frozen=True, slots=True)
class MoveFile:
    source: str
    destination: str


TransactionAction: TypeAlias = WriteFile | CreateFile | DeleteFile | MoveFile


@dataclass(frozen=True, slots=True)
class TransactionPlan:
    actions: tuple[TransactionAction, ...]


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    transaction_id: str
    changed_paths: tuple[str, ...]
    committed: bool = True


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    rolled_back: int
    finalized: int
