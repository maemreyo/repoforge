"""Lean MCP payload rendering with explicit legacy duplication compatibility."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mcp.types import ContentBlock, TextContent

from ...domain.latency import (
    PayloadMetrics,
    ToolPayloadClass,
    classify_tool_payload,
    payload_budget,
)

_MAX_SUMMARY_BYTES = 500


@dataclass(frozen=True, slots=True)
class RenderedToolPayload:
    content: list[ContentBlock]
    structured: dict[str, Any]
    tool_class: ToolPayloadClass
    metrics: PayloadMetrics


def _bounded_summary(value: str) -> str:
    normalized = " ".join(value.split()) or "Completed successfully."
    encoded = normalized.encode("utf-8")
    if len(encoded) <= _MAX_SUMMARY_BYTES:
        return normalized
    truncated = encoded[: _MAX_SUMMARY_BYTES - 3]
    while True:
        try:
            return truncated.decode("utf-8") + "..."
        except UnicodeDecodeError:
            truncated = truncated[:-1]


def human_summary(tool_name: str, structured: dict[str, Any]) -> str:
    summary = structured.get("summary")
    if isinstance(summary, str) and summary.strip():
        return _bounded_summary(summary)
    status = structured.get("status")
    if isinstance(status, str) and status.strip():
        return _bounded_summary(f"{tool_name}: {status}.")
    return _bounded_summary(f"RepoForge completed {tool_name}.")


def render_tool_payload(
    tool_name: str,
    structured: dict[str, Any],
    *,
    legacy_text_result_duplication: bool,
) -> RenderedToolPayload:
    compact_json = json.dumps(
        structured,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    text = compact_json if legacy_text_result_duplication else human_summary(tool_name, structured)
    structured_bytes = len(compact_json.encode("utf-8"))
    text_bytes = len(text.encode("utf-8"))
    emitted_bytes = structured_bytes + text_bytes
    tool_class = classify_tool_payload(tool_name)
    budget = payload_budget(tool_class)
    return RenderedToolPayload(
        content=[TextContent(type="text", text=text)],
        structured=structured,
        tool_class=tool_class,
        metrics=PayloadMetrics(
            structured_bytes=structured_bytes,
            text_bytes=text_bytes,
            emitted_bytes=emitted_bytes,
            budget_bytes=budget,
            within_budget=emitted_bytes <= budget,
            legacy_text_duplication=legacy_text_result_duplication,
        ),
    )


__all__ = ["RenderedToolPayload", "human_summary", "render_tool_payload"]
