"""Request-scoped publication of authoritative mutation outcome evidence."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from ..domain.execution_receipt import EffectReceipt


@dataclass(frozen=True, slots=True)
class OutcomeReference:
    operation_id: str
    receipt_id: str
    state: str
    result_reference: str | None
    effect_boundary_crossed: bool
    pre_identity: dict[str, str]
    post_identity: dict[str, str]

    @classmethod
    def from_receipt(cls, receipt: EffectReceipt) -> OutcomeReference:
        return cls(
            operation_id=receipt.operation_id,
            receipt_id=receipt.receipt_id,
            state=receipt.state.value,
            result_reference=receipt.result_reference,
            effect_boundary_crossed=receipt.effect_boundary_crossed,
            pre_identity=dict(receipt.pre_identity),
            post_identity=dict(receipt.post_identity),
        )

    def payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "receipt_id": self.receipt_id,
            "state": self.state,
            "result_reference": self.result_reference,
            "effect_boundary_crossed": self.effect_boundary_crossed,
            "pre_identity": self.pre_identity,
            "post_identity": self.post_identity,
        }


_CURRENT_OUTCOME: ContextVar[OutcomeReference | None] = ContextVar(
    "repoforge_current_outcome",
    default=None,
)


def begin_outcome_capture() -> Token[OutcomeReference | None]:
    return _CURRENT_OUTCOME.set(None)


def publish_outcome(receipt: EffectReceipt) -> OutcomeReference:
    reference = OutcomeReference.from_receipt(receipt)
    _CURRENT_OUTCOME.set(reference)
    return reference


def current_outcome() -> OutcomeReference | None:
    return _CURRENT_OUTCOME.get()


def reset_outcome_capture(token: Token[OutcomeReference | None]) -> None:
    _CURRENT_OUTCOME.reset(token)
