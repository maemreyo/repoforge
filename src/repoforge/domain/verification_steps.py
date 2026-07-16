"""Typed verification steps and hygiene baseline policy contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

_STEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class VerificationStepKind(str, Enum):
    UNKNOWN = "unknown"
    HYGIENE = "hygiene"
    STATIC_ANALYSIS = "static_analysis"
    TYPECHECK = "typecheck"
    BUSINESS_TESTS = "business_tests"
    SECURITY = "security"
    CONTRACT = "contract"
    BUILD = "build"


class HygieneBaselinePolicy(str, Enum):
    STRICT_CLEAN = "strict_clean"
    NO_REGRESSION = "no_regression"


@dataclass(frozen=True, slots=True)
class VerificationStep:
    step_id: str
    kind: VerificationStepKind
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or _STEP_ID.fullmatch(self.step_id) is None:
            raise ValueError("verification step id is invalid")
        if not self.command or len(self.command) > 64:
            raise ValueError("verification step command must contain 1..64 arguments")
        for argument in self.command:
            if (
                not isinstance(argument, str)
                or not argument
                or len(argument) > 512
                or any(ord(character) < 32 for character in argument)
            ):
                raise ValueError("verification step command contains an invalid argument")

    def public(self) -> dict[str, object]:
        return {"id": self.step_id, "kind": self.kind.value}


def compile_legacy_steps(commands: tuple[tuple[str, ...], ...]) -> tuple[VerificationStep, ...]:
    return tuple(
        VerificationStep(f"step-{index}", VerificationStepKind.UNKNOWN, command)
        for index, command in enumerate(commands, start=1)
    )


@dataclass(frozen=True, slots=True)
class NoRegressionHygieneReceipt:
    base_sha: str
    workspace_fingerprint: str
    formatter_contract_hash: str
    environment_identity: str
    preexisting_count: int
    receipt_hash: str

    @classmethod
    def create(
        cls,
        *,
        base_sha: str,
        workspace_fingerprint: str,
        formatter_contract_hash: str,
        environment_identity: str,
        preexisting_count: int,
    ) -> NoRegressionHygieneReceipt:
        payload = {
            "base_sha": base_sha,
            "environment_identity": environment_identity,
            "formatter_contract_hash": formatter_contract_hash,
            "preexisting_count": preexisting_count,
            "workspace_fingerprint": workspace_fingerprint,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(
            base_sha=base_sha,
            workspace_fingerprint=workspace_fingerprint,
            formatter_contract_hash=formatter_contract_hash,
            environment_identity=environment_identity,
            preexisting_count=preexisting_count,
            receipt_hash=digest,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "base_sha": self.base_sha,
            "workspace_fingerprint": self.workspace_fingerprint,
            "formatter_contract_hash": self.formatter_contract_hash,
            "environment_identity": self.environment_identity,
            "preexisting_count": self.preexisting_count,
            "receipt_hash": self.receipt_hash,
        }


def no_regression_receipt(
    *,
    base_sha: str | None,
    workspace_fingerprint: str,
    formatter_contract_hash: str | None,
    environment_identity: str | None,
    preexisting_count: int,
    introduced_count: int,
    changed_path_finding_count: int,
    output_truncated: bool,
) -> NoRegressionHygieneReceipt | None:
    """Return evidence only when hygiene debt is pre-existing and changed paths are clean."""

    if (
        base_sha is None
        or formatter_contract_hash is None
        or environment_identity is None
        or introduced_count
        or changed_path_finding_count
        or output_truncated
    ):
        return None
    return NoRegressionHygieneReceipt.create(
        base_sha=base_sha,
        workspace_fingerprint=workspace_fingerprint,
        formatter_contract_hash=formatter_contract_hash,
        environment_identity=environment_identity,
        preexisting_count=preexisting_count,
    )
