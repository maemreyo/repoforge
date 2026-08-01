"""Constrained ``gh api graphql`` batch transport for ticket-graph reads.

One aliased query carries many issue selections; ``gh`` exits non-zero
whenever GraphQL reports any error, even when other aliases succeeded, so the
command runs with ``check=False`` and the caller attributes per-alias errors.
Transport-level failures (truncated, invalid, or error-free non-zero output)
raise ``CommandError``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import ServerConfig
from ...domain.errors import CommandError
from ...domain.tickets import (
    GITHUB_API_VERSION,
    CapabilityReadStat,
    GraphEvidenceCapability,
    TicketGraphReadStats,
)
from ...ports.command import CommandExecutor

_API_VERSION = GITHUB_API_VERSION
#: Bounded ceiling for one batched GraphQL response. Graph reads are cached and
#: infrequent but carry many issue bodies, so they may exceed the generic tool
#: output cap; the executor still enforces this explicit bound.
GRAPH_OUTPUT_LIMIT = 50_000_000


@dataclass(slots=True)
class Stats:
    """Mutable provider-traffic accumulator for one graph observation.

    Every counter records a subprocess launch against the GitHub provider.
    A launch is not claimed to be exactly one HTTPS request: higher-level
    operations such as ``gh project item-list`` may perform more than one
    network request, and the transport is not instrumented directly. The
    public contract names the measured units (``provider_processes``,
    ``captured_stdout_bytes``, ``provider_process_duration_ms``) instead.
    """

    processes: int = 0
    bytes: int = 0
    duration_ms: float = 0.0
    per_capability: dict[GraphEvidenceCapability, list[float]] = field(default_factory=dict)

    def record(
        self,
        capabilities: tuple[GraphEvidenceCapability, ...],
        *,
        processes: int,
        response_bytes: int,
        duration_ms: float,
    ) -> None:
        self.processes += processes
        self.bytes += response_bytes
        self.duration_ms += duration_ms
        for capability in capabilities:
            bucket = self.per_capability.setdefault(capability, [0, 0, 0])
            bucket[0] += processes
            bucket[1] += response_bytes
            bucket[2] += duration_ms

    def snapshot(self) -> TicketGraphReadStats:
        per_capability = tuple(
            CapabilityReadStat(
                capability=capability,
                provider_processes=int(values[0]),
                captured_stdout_bytes=int(values[1]),
                provider_process_duration_ms=round(values[2], 3),
            )
            for capability, values in sorted(
                self.per_capability.items(), key=lambda item: item[0].value
            )
        )
        return TicketGraphReadStats(
            source="live_full",
            provider_processes=self.processes,
            captured_stdout_bytes=self.bytes,
            provider_process_duration_ms=round(self.duration_ms, 3),
            per_capability=per_capability,
        )


def run_graphql(
    executor: CommandExecutor,
    server: ServerConfig,
    cwd: Path,
    query: str,
    stats: Stats,
    *,
    capabilities: tuple[GraphEvidenceCapability, ...],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one ``gh api graphql`` batch and return ``(data-payload, errors)``.

    A non-zero exit is tolerated only when the errors carry per-alias paths;
    any other non-zero exit (auth failure, network error, oversized output) is
    a transport failure and raises ``CommandError``.
    """
    started = time.monotonic()
    result = executor.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-H",
            f"X-GitHub-Api-Version: {_API_VERSION}",
        ],
        cwd=cwd,
        check=False,
        timeout=server.default_command_timeout_seconds,
        output_limit=GRAPH_OUTPUT_LIMIT,
    )
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    response_bytes = len(result.stdout.encode("utf-8"))
    stats.record(
        capabilities,
        processes=1,
        response_bytes=response_bytes,
        duration_ms=duration_ms,
    )
    if result.stdout_truncated:
        raise CommandError("GitHub GraphQL batch returned oversized JSON")
    try:
        payload: Any = json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise CommandError("GitHub GraphQL batch returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CommandError("GitHub GraphQL batch returned a non-object JSON value")
    errors = payload.get("errors")
    if not isinstance(errors, list):
        errors = []
    alias_count = query.count("repository(owner:")
    if result.returncode != 0 and not any(
        isinstance(error, dict)
        and isinstance(error.get("path"), list)
        and error["path"]
        and isinstance(error["path"][0], str)
        and error["path"][0].startswith("r")
        and error["path"][0][1:].isdigit()
        and int(error["path"][0][1:]) < alias_count
        for error in errors
    ):
        raise CommandError(f"GitHub GraphQL batch failed (exit {result.returncode})")
    return payload, errors
