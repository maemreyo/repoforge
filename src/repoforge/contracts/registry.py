"""Deterministic registry and runtime validators for Forge v2 tool contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, TypeAdapter

from ..domain.errors import ConfigError
from . import generated_contract_identity
from .common import StrictModel, ToolFailure, ToolResponse
from .v2 import MODEL_PAIRS


@dataclass(frozen=True, slots=True)
class ToolContractSpec:
    """One public tool's authoritative input and output models."""

    name: str
    input_model: type[StrictModel]
    output_model: type[ToolResponse]
    _output_adapter: TypeAdapter[Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_output_adapter",
            TypeAdapter(self.output_model | ToolFailure),
        )

    def validate_input(self, payload: Mapping[str, object]) -> BaseModel:
        return self.input_model.model_validate(payload)

    def output_schema(self) -> dict[str, Any]:
        return self._output_adapter.json_schema(mode="validation")

    def validate_success_output(self, payload: Mapping[str, object]) -> ToolResponse:
        return self.output_model.model_validate(payload)

    def validate_failure_output(self, payload: Mapping[str, object]) -> ToolFailure:
        return ToolFailure.model_validate(payload)

    def validate_output(self, payload: Mapping[str, object]) -> BaseModel:
        return cast(BaseModel, self._output_adapter.validate_python(payload))


V2_TOOL_NAMES: tuple[str, ...] = tuple(name for name, _, _ in MODEL_PAIRS)
V2_TOOL_SPECS: dict[str, ToolContractSpec] = {
    name: ToolContractSpec(name, input_model, output_model)
    for name, input_model, output_model in MODEL_PAIRS
}


@dataclass(frozen=True, slots=True)
class ContractSchemaDigests:
    """Separate deterministic identities for public request and response schemas."""

    input_digest: str
    output_digest: str
    tool_count: int


if len(V2_TOOL_SPECS) != len(MODEL_PAIRS):
    raise RuntimeError("Forge v2 tool names must be unique")


def validate_tool_input(name: str, payload: Mapping[str, object]) -> BaseModel:
    """Validate one tool request against the public discovery contract."""

    try:
        spec = V2_TOOL_SPECS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Forge v2 tool: {name}") from exc
    return spec.validate_input(payload)


def validate_tool_output(name: str, payload: Mapping[str, object]) -> BaseModel:
    """Validate one tool result against the public discovery contract."""

    try:
        spec = V2_TOOL_SPECS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Forge v2 tool: {name}") from exc
    return spec.validate_output(payload)


def render_v2_schema_bundle() -> dict[str, object]:
    """Render the byte-stable discovery contract generated from Pydantic models."""

    tools: dict[str, object] = {}
    for name in sorted(V2_TOOL_SPECS):
        spec = V2_TOOL_SPECS[name]
        tools[name] = {
            "input": spec.input_model.model_json_schema(mode="validation"),
            "output": spec.output_schema(),
        }
    return {
        "contract_version": 2,
        "tool_count": len(V2_TOOL_SPECS),
        "evolution": {
            "outputs_closed": True,
            "tolerant_reader_required": True,
            "additive_output_fields_require_contract_bump": False,
        },
        "tools": tools,
    }


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def contract_schema_digests() -> ContractSchemaDigests:
    """Hash inputs and outputs independently so recovery can name the changed side."""

    inputs = {
        name: V2_TOOL_SPECS[name].input_model.model_json_schema(mode="validation")
        for name in sorted(V2_TOOL_SPECS)
    }
    outputs = {name: V2_TOOL_SPECS[name].output_schema() for name in sorted(V2_TOOL_SPECS)}
    return ContractSchemaDigests(
        input_digest=_canonical_digest(inputs),
        output_digest=_canonical_digest(outputs),
        tool_count=len(V2_TOOL_SPECS),
    )


def render_contract_identity_artifact() -> dict[str, object]:
    """Render the compact generated identity shipped inside source and wheel artifacts."""

    digests = contract_schema_digests()
    return {
        "contract_version": 2,
        "tool_count": digests.tool_count,
        "input_contract_digest": digests.input_digest,
        "output_contract_digest": digests.output_digest,
        "tool_schema_bundle_digest": _canonical_digest(render_v2_schema_bundle()),
    }


def validate_generated_contract_identity() -> None:
    """Fail before runtime effects when packaged generated identity is stale or tampered."""

    if render_contract_identity_artifact() != generated_contract_identity.CONTRACT_IDENTITY:
        raise ConfigError(
            "CONTRACT_ARTIFACT_MISMATCH: packaged contract identity differs from the in-process registry"
        )


def validate_generated_contract_artifact(path: Path) -> None:
    """Fail closed when a reviewed generated schema differs from the live registry.

    Errors deliberately identify the artifact by logical role rather than by host path.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            "CONTRACT_ARTIFACT_INVALID: generated tool schema artifact cannot be read"
        ) from exc
    if raw != render_v2_schema_bundle():
        raise ConfigError(
            "CONTRACT_ARTIFACT_MISMATCH: generated tool schemas differ from the in-process registry"
        )
