"""Typed three-layer latency and bounded payload-budget evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LatencyLayer(str, Enum):
    ENGINE = "engine"
    CONNECTOR = "connector"
    CLIENT_ROUND_TRIP = "client_round_trip"


class LatencyStatus(str, Enum):
    OBSERVED = "observed"
    UNOBSERVED = "unobserved"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ToolPayloadClass(str, Enum):
    COMPACT = "compact"
    STANDARD = "standard"
    EVIDENCE = "evidence"


PAYLOAD_BUDGET_BYTES: dict[ToolPayloadClass, int] = {
    ToolPayloadClass.COMPACT: 16_384,
    ToolPayloadClass.STANDARD: 65_536,
    ToolPayloadClass.EVIDENCE: 131_072,
}

DURATION_BUCKETS_MS: tuple[float, ...] = (
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1_000.0,
    2_500.0,
    5_000.0,
    15_000.0,
    60_000.0,
)
PAYLOAD_BUCKETS_BYTES: tuple[int, ...] = (
    1_024,
    4_096,
    16_384,
    65_536,
    131_072,
    262_144,
)

_EVIDENCE_TOOL_MARKERS = (
    "_read",
    "_search",
    "_tree",
    "_diff",
    "_history",
    "_evidence",
    "_context",
    "_logs",
    "_checks",
)
_COMPACT_TOOLS = frozenset(
    {
        "repo_list",
        "repo_status",
        "workspace_list",
        "workspace_status",
        "workspace_commit",
        "workspace_push",
        "workspace_pr",
        "workspace_mutate",
        "workspace_verify",
        "workspace_format_changed",
        "operation",
        "operation_status",
        "operation_list",
        "operation_cancel",
        "config_inspect",
        "repo_policy",
        "repo_policy_apply",
    }
)


def classify_tool_payload(tool_name: str) -> ToolPayloadClass:
    if tool_name in _COMPACT_TOOLS or tool_name.startswith("operation_"):
        return ToolPayloadClass.COMPACT
    if any(marker in tool_name for marker in _EVIDENCE_TOOL_MARKERS):
        return ToolPayloadClass.EVIDENCE
    return ToolPayloadClass.STANDARD


def payload_budget(tool_class: ToolPayloadClass) -> int:
    return PAYLOAD_BUDGET_BYTES[tool_class]


@dataclass(frozen=True, slots=True)
class LatencyObservation:
    layer: LatencyLayer
    status: LatencyStatus
    duration_ms: float | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is LatencyStatus.OBSERVED:
            if self.duration_ms is None or self.duration_ms < 0:
                raise ValueError("Observed latency requires a non-negative duration")
        elif self.duration_ms is not None:
            raise ValueError("Only observed latency may carry duration_ms")
        if self.error_code is not None and not 1 <= len(self.error_code) <= 128:
            raise ValueError("Latency error_code must be bounded")

    @classmethod
    def observed(cls, layer: LatencyLayer, duration_ms: float) -> LatencyObservation:
        return cls(layer, LatencyStatus.OBSERVED, round(max(0.0, float(duration_ms)), 3))

    @classmethod
    def unobserved(cls, layer: LatencyLayer) -> LatencyObservation:
        return cls(layer, LatencyStatus.UNOBSERVED)

    def as_dict(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class PayloadMetrics:
    structured_bytes: int
    text_bytes: int
    emitted_bytes: int
    budget_bytes: int
    within_budget: bool
    legacy_text_duplication: bool

    def __post_init__(self) -> None:
        for value in (
            self.structured_bytes,
            self.text_bytes,
            self.emitted_bytes,
            self.budget_bytes,
        ):
            if value < 0:
                raise ValueError("Payload byte metrics cannot be negative")
        if self.emitted_bytes < self.structured_bytes + self.text_bytes:
            raise ValueError("Emitted payload bytes cannot be smaller than its measured parts")
        if self.within_budget != (self.emitted_bytes <= self.budget_bytes):
            raise ValueError("Payload budget decision does not match measured bytes")

    def as_dict(self) -> dict[str, object]:
        return {
            "structured_bytes": self.structured_bytes,
            "text_bytes": self.text_bytes,
            "emitted_bytes": self.emitted_bytes,
            "budget_bytes": self.budget_bytes,
            "within_budget": self.within_budget,
            "legacy_text_duplication": self.legacy_text_duplication,
        }


@dataclass(frozen=True, slots=True)
class LatencyTrace:
    trace_id: str
    tool_name: str
    tool_class: ToolPayloadClass
    client_name: str
    client_version: str
    engine: LatencyObservation
    connector: LatencyObservation
    client_round_trip: LatencyObservation
    payload: PayloadMetrics

    def __post_init__(self) -> None:
        if not 1 <= len(self.trace_id) <= 160:
            raise ValueError("Latency trace_id must be bounded")
        if not 1 <= len(self.tool_name) <= 160:
            raise ValueError("Latency tool_name must be bounded")
        if self.engine.layer is not LatencyLayer.ENGINE:
            raise ValueError("Engine observation has the wrong layer")
        if self.connector.layer is not LatencyLayer.CONNECTOR:
            raise ValueError("Connector observation has the wrong layer")
        if self.client_round_trip.layer is not LatencyLayer.CLIENT_ROUND_TRIP:
            raise ValueError("Client observation has the wrong layer")

    def as_dict(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "tool_name": self.tool_name,
            "tool_class": self.tool_class.value,
            "client_name": self.client_name[:160],
            "client_version": self.client_version[:80],
            "engine": self.engine.as_dict(),
            "connector": self.connector.as_dict(),
            "client_round_trip": self.client_round_trip.as_dict(),
            "payload": self.payload.as_dict(),
        }


def histogram_bucket(value: float, bounds: tuple[float | int, ...]) -> str:
    for bound in bounds:
        if value <= bound:
            return f"<={bound:g}"
    return f">{bounds[-1]:g}"


def histogram_template(bounds: tuple[float | int, ...]) -> dict[str, int]:
    return {**{f"<={bound:g}": 0 for bound in bounds}, f">{bounds[-1]:g}": 0}


def histogram_percentile(
    histogram: dict[str, int],
    bounds: tuple[float | int, ...],
    *,
    percentile: float,
) -> float:
    total = sum(max(0, int(value)) for value in histogram.values())
    if total <= 0:
        return 0.0
    rank = max(1, int((total * percentile) + 0.999999))
    cumulative = 0
    for bound in bounds:
        cumulative += max(0, int(histogram.get(f"<={bound:g}", 0)))
        if cumulative >= rank:
            return float(bound)
    return float(bounds[-1])
