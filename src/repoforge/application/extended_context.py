"""Capability extensions kept outside the landed application context contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from ..domain.errors import ConfigError
from ..ports.approval_store import ApprovalPayloadStore, ApprovalStore
from ..ports.external_mutation_ledger import ExternalMutationLedger
from ..ports.filesystem_transaction import (
    FileTransactionFactory as ReceiptFileTransactionFactory,
)
from ..ports.issue_mutation import IssueMutationGateway
from .context import ApplicationContext
from .idempotency import IdempotencyEffectBoundary, execute_idempotent

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ExtendedApplicationContext(ApplicationContext):
    """Branch capabilities layered over the landed, provider-neutral context."""

    issue_mutations: IssueMutationGateway | None = None
    external_mutations: ExternalMutationLedger | None = None
    approvals: ApprovalStore | None = None
    approval_payloads: ApprovalPayloadStore | None = None
    receipt_file_transactions: ReceiptFileTransactionFactory | None = None

    def approval_stores(self) -> tuple[ApprovalStore, ApprovalPayloadStore]:
        return approval_stores(self)

    def external_mutation_ledger(self) -> ExternalMutationLedger:
        return external_mutation_ledger(self)

    def issue_mutation_gateway(self) -> IssueMutationGateway:
        return issue_mutation_gateway(self)

    def idempotent(
        self,
        action: str,
        key: str | None,
        request: Any,
        operation: Callable[[], T],
        *,
        details: dict[str, Any] | None = None,
        serialize: Callable[[T], Any] | None = None,
        deserialize: Callable[[Any], T] | None = None,
        effect_boundary: IdempotencyEffectBoundary | None = None,
        reconcile_uncertain: Callable[[], T | None] | None = None,
    ) -> T:
        return execute_idempotent(
            self,
            action,
            key,
            request,
            operation,
            details=details,
            serialize=serialize,
            deserialize=deserialize,
            effect_boundary=effect_boundary,
            reconcile_uncertain=reconcile_uncertain,
        )


def approval_stores(ctx: ApplicationContext) -> tuple[ApprovalStore, ApprovalPayloadStore]:
    approvals = getattr(ctx, "approvals", None)
    payloads = getattr(ctx, "approval_payloads", None)
    if approvals is None or payloads is None:
        raise ConfigError("Shared approval stores are unavailable")
    return cast(ApprovalStore, approvals), cast(ApprovalPayloadStore, payloads)


def external_mutation_ledger(ctx: ApplicationContext) -> ExternalMutationLedger:
    ledger = getattr(ctx, "external_mutations", None)
    if ledger is None:
        raise ConfigError("External mutation ledger is unavailable")
    return cast(ExternalMutationLedger, ledger)


def issue_mutation_gateway(ctx: ApplicationContext) -> IssueMutationGateway:
    gateway = getattr(ctx, "issue_mutations", None)
    if gateway is None:
        raise ConfigError("GitHub issue mutation gateway is unavailable")
    return cast(IssueMutationGateway, gateway)


def receipt_file_transaction_factory(
    ctx: ApplicationContext,
) -> ReceiptFileTransactionFactory:
    factory = getattr(ctx, "receipt_file_transactions", None)
    if factory is None:
        raise ConfigError("Receipt-aware file transaction factory is unavailable")
    return cast(ReceiptFileTransactionFactory, factory)
