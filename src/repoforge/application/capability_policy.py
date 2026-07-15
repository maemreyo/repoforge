"""Capability-aware extension emission policy and deterministic fallbacks."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.client_capabilities import ClientCapabilities, ClientFeature

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_SUMMARY = 2_000
_MAX_LABEL = 256
_MAX_OPTIONS = 32

_FALLBACKS: dict[ClientFeature, str] = {
    ClientFeature.APPS_UI: "structured",
    ClientFeature.ELICITATION_FORM: "input_required",
    ClientFeature.ELICITATION_URL: "input_required",
    ClientFeature.TASKS: "repoforge_operation",
    ClientFeature.PROGRESS_NOTIFICATIONS: "polling",
    ClientFeature.CANCELLATION_NOTIFICATIONS: "operation_cancel",
    ClientFeature.TOOL_SEARCH: "static",
    ClientFeature.DEFERRED_DISCOVERY: "static",
    ClientFeature.RESOURCE_SUBSCRIPTIONS: "polling",
}


def _bounded_text(value: str, field: str, *, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 and character not in "\n\t\r" for character in value)
    ):
        raise ValueError(f"{field} is invalid or exceeds {limit} characters")
    return value


def _safe_id(value: str, field: str) -> str:
    normalized = _bounded_text(value, field, limit=128)
    if _SAFE_ID.fullmatch(normalized) is None:
        raise ValueError(f"{field} has an invalid format")
    return normalized


@dataclass(frozen=True, slots=True)
class SafeAction:
    action_id: str
    label: str
    required: bool = True

    def __post_init__(self) -> None:
        _safe_id(self.action_id, "action_id")
        _bounded_text(self.label, "label", limit=_MAX_LABEL)
        if not isinstance(self.required, bool):
            raise ValueError("required must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class ExtensionDecision:
    feature: ClientFeature
    allowed: bool
    fallback: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "feature": self.feature.value,
            "allowed": self.allowed,
            "fallback": self.fallback,
            "reason": self.reason,
        }


class CapabilityPolicy:
    """Decide extension emission from negotiated support, never from authority."""

    def __init__(self, capabilities: ClientCapabilities):
        self.capabilities = capabilities

    def may_emit(self, feature: ClientFeature) -> bool:
        return self.capabilities.supports(feature)

    def decision(self, feature: ClientFeature) -> ExtensionDecision:
        support = self.capabilities.feature(feature)
        return ExtensionDecision(
            feature=feature,
            allowed=support.supported,
            fallback=_FALLBACKS[feature],
            reason=support.reason,
        )

    def present_app_or_fallback(
        self,
        *,
        summary: str,
        actions: tuple[SafeAction, ...],
    ) -> dict[str, object]:
        safe_summary = _bounded_text(summary, "summary", limit=_MAX_SUMMARY)
        if not actions:
            raise ValueError("actions must contain at least one safe action")
        rendered_actions = [action.as_dict() for action in actions]
        if self.may_emit(ClientFeature.APPS_UI):
            return {
                "delivery": "mcp_app",
                "summary": safe_summary,
                "actions": rendered_actions,
            }
        return {
            "delivery": "structured",
            "fallback_for": ClientFeature.APPS_UI.value,
            "summary": safe_summary,
            "actions": rendered_actions,
        }

    def input_required(
        self,
        *,
        decision_id: str,
        prompt: str,
        allowed_options: tuple[str, ...],
    ) -> dict[str, object]:
        safe_decision_id = _safe_id(decision_id, "decision_id")
        safe_prompt = _bounded_text(prompt, "prompt", limit=_MAX_SUMMARY)
        if not allowed_options or len(allowed_options) > _MAX_OPTIONS:
            raise ValueError(f"allowed_options must contain between 1 and {_MAX_OPTIONS} values")
        normalized: list[str] = []
        seen: set[str] = set()
        for option in allowed_options:
            safe_option = _bounded_text(option, "allowed option", limit=_MAX_LABEL)
            if safe_option not in seen:
                normalized.append(safe_option)
                seen.add(safe_option)
        return {
            "status": "INPUT_REQUIRED",
            "fallback_for": "elicitation",
            "decision_id": safe_decision_id,
            "prompt": safe_prompt,
            "allowed_options": normalized,
        }

    def deliver_task(self, operation_id: str, *, cancel_supported: bool) -> dict[str, object]:
        safe_operation_id = _safe_id(operation_id, "operation_id")
        if not isinstance(cancel_supported, bool):
            raise ValueError("cancel_supported must be a boolean")
        if self.may_emit(ClientFeature.TASKS):
            return {
                "delivery": "mcp_task",
                "operation_id": safe_operation_id,
                "cancel_supported": cancel_supported,
            }
        fallback: dict[str, object] = {
            "delivery": "repoforge_operation",
            "fallback_for": ClientFeature.TASKS.value,
            "operation_id": safe_operation_id,
            "status_tool": "operation_status",
        }
        if cancel_supported:
            fallback["cancel_tool"] = "operation_cancel"
        return fallback

    def discover_tools(self, static_tools: tuple[str, ...]) -> dict[str, object]:
        if not static_tools:
            raise ValueError("static_tools must contain the complete safe tool surface")
        normalized = [_safe_id(tool, "tool name") for tool in static_tools]
        if len(set(normalized)) != len(normalized):
            raise ValueError("static_tools must not contain duplicates")
        if self.may_emit(ClientFeature.TOOL_SEARCH):
            return {
                "delivery": "tool_search",
                "tools": [],
                "complete": False,
            }
        return {
            "delivery": "static",
            "fallback_for": ClientFeature.TOOL_SEARCH.value,
            "tools": normalized,
            "complete": True,
        }

    def deliver_progress(self, operation_id: str) -> dict[str, object]:
        safe_operation_id = _safe_id(operation_id, "operation_id")
        if self.may_emit(ClientFeature.PROGRESS_NOTIFICATIONS):
            return {
                "delivery": "notification",
                "operation_id": safe_operation_id,
            }
        return {
            "delivery": "polling",
            "fallback_for": ClientFeature.PROGRESS_NOTIFICATIONS.value,
            "operation_id": safe_operation_id,
            "status_tool": "operation_status",
        }
