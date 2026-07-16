"""Deterministic registry and runtime validators for Forge v2 tool contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel

from .common import StrictModel, ToolResponse
from .v2 import MODEL_PAIRS


@dataclass(frozen=True, slots=True)
class ToolContractSpec:
    """One public tool's authoritative input and output models."""

    name: str
    input_model: type[StrictModel]
    output_model: type[ToolResponse]

    def validate_input(self, payload: Mapping[str, object]) -> BaseModel:
        return self.input_model.model_validate(payload)

    def validate_output(self, payload: Mapping[str, object]) -> BaseModel:
        return self.output_model.model_validate(payload)


V2_TOOL_NAMES: tuple[str, ...] = tuple(name for name, _, _ in MODEL_PAIRS)
V2_TOOL_SPECS: dict[str, ToolContractSpec] = {
    name: ToolContractSpec(name, input_model, output_model)
    for name, input_model, output_model in MODEL_PAIRS
}

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
            "output": spec.output_model.model_json_schema(mode="validation"),
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
