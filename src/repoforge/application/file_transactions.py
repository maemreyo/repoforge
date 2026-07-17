"""Compatibility boundary for baseline and receipt-aware transaction factories."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from ..domain.errors import ConfigError
from ..ports.filesystem_transaction import FaultInjector, FileTransaction
from .context import ApplicationContext
from .extended_context import receipt_file_transaction_factory


def open_file_transaction(
    ctx: ApplicationContext,
    workspace_root: Path,
    *,
    fault_injector: FaultInjector | None = None,
) -> FileTransaction:
    """Open an extended engine without widening the landed baseline port."""

    factory = ctx.file_transactions
    if factory is None:
        raise ConfigError("Journaled file transaction factory is unavailable")
    creator = getattr(factory, "create", None)
    if callable(factory):
        candidate = factory(workspace_root, fault_injector=fault_injector)
    elif callable(creator):
        candidate = creator(workspace_root)
    else:
        raise ConfigError("Journaled file transaction factory is invalid")
    if callable(getattr(candidate, "load_commit_receipt", None)):
        return cast(FileTransaction, candidate)

    receipt_factory = receipt_file_transaction_factory(ctx)
    checkpoint = fault_injector
    if checkpoint is None:
        candidate_checkpoint = getattr(candidate, "_checkpoint", None)
        checkpoint = candidate_checkpoint if callable(candidate_checkpoint) else None
    return receipt_factory(workspace_root, fault_injector=checkpoint)
