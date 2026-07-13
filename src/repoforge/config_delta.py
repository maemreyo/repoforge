"""Compatibility facade for the typed, field-aware capability delta engine."""

from .domain.config_generation import (
    CapabilityChange,
    CapabilityDelta,
    CapabilityDeltaKind,
    classify_capability_delta,
)

__all__ = [
    "CapabilityChange",
    "CapabilityDelta",
    "CapabilityDeltaKind",
    "classify_capability_delta",
]
