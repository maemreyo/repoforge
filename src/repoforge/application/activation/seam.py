"""Near-zero-downtime serve-child swap at the tunnel-client boundary (#269).

MCP tools are served over stdio, but the client does not hold that pipe -- the
*tunnel-client* does, and it is the stable connection holder. So a release upgrade
can replace the serve child underneath the tunnel without dropping the client: this
coordinator performs a two-child overlap cutover.

    start candidate child  ->  health-gate it  (old child still serving)
        |  unhealthy -> abort, retire candidate, old child untouched (no downtime)
        v  healthy
    drain old child  ->  reconcile worker bindings (#270)  ->  retire old child

Client rediscovery is signalled only when the tool-surface hash actually changed;
an unchanged surface is a transparent swap. The design is fail-safe: the old child
is retired only after the candidate is proven healthy, so a bad candidate never
takes the runtime down.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigError
from ...domain.runtime import ChildProcess, ControlCommand, ControlRequest, TunnelProfile
from ...ports.ids import IdGenerator
from ...ports.runtime_control import RuntimeControlClient
from ...ports.sleeper import Sleeper
from ...ports.tunnel import TunnelClient
from .handoff import GenerationHandoffReconciler, HandoffReport, OwnerIdentity


@dataclass(frozen=True, slots=True)
class SwapResult:
    status: str  # "swapped" | "aborted"
    rediscovery_required: bool
    detail: str
    candidate_pid: int | None = None
    handoff: HandoffReport | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_pid": self.candidate_pid,
            "client_rediscovery_required": self.rediscovery_required,
            "handoff": self.handoff.as_dict() if self.handoff is not None else None,
            "detail": self.detail,
        }


class TunnelSeamSwapCoordinator:
    """Swap the serve child of the active generation for the candidate's, live."""

    def __init__(
        self,
        *,
        tunnel: TunnelClient,
        reconciler: GenerationHandoffReconciler,
        sleeper: Sleeper,
        control: RuntimeControlClient | None = None,
        ids: IdGenerator | None = None,
        health_attempts: int = 20,
        health_interval_seconds: float = 0.5,
        drain_timeout_seconds: float = 30.0,
        grace_seconds: float = 5.0,
    ) -> None:
        self._tunnel = tunnel
        self._reconciler = reconciler
        self._sleeper = sleeper
        self._control = control
        self._ids = ids
        self._health_attempts = max(1, health_attempts)
        self._health_interval = max(0.0, health_interval_seconds)
        self._drain_timeout = drain_timeout_seconds
        self._grace = grace_seconds

    def swap(
        self,
        *,
        old_child: ChildProcess,
        candidate_profile: TunnelProfile,
        env: dict[str, str],
        log_path: Path,
        current_owner: OwnerIdentity,
        old_surface_hash: str,
        new_surface_hash: str,
        is_resumable: Callable[[str], bool] | None = None,
    ) -> SwapResult:
        candidate = self._tunnel.start(candidate_profile, env=env, log_path=log_path)

        healthy, detail = self._await_healthy(candidate)
        if not healthy:
            # Fail-safe: retire the candidate, leave the old child serving. No downtime.
            self._tunnel.terminate(candidate, grace_seconds=self._grace)
            return SwapResult(
                status="aborted",
                rediscovery_required=False,
                detail=f"Candidate serve child unhealthy; swap aborted, old child retained ({detail}).",
                candidate_pid=candidate.pid,
            )

        self._drain(old_child)
        report = self._reconciler.reconcile(current_owner=current_owner, is_resumable=is_resumable)
        self._tunnel.terminate(old_child, grace_seconds=self._grace)

        rediscovery = old_surface_hash != new_surface_hash
        return SwapResult(
            status="swapped",
            rediscovery_required=rediscovery,
            detail=(
                "Swapped serve child; old generation drained and bindings reconciled."
                + (" Tool surface changed: client rediscovery required." if rediscovery else "")
            ),
            candidate_pid=candidate.pid,
            handoff=report,
        )

    def _await_healthy(self, candidate: ChildProcess) -> tuple[bool, str]:
        for attempt in range(self._health_attempts):
            if not self._tunnel.is_alive(candidate):
                return False, "candidate exited before becoming healthy"
            checks = self._tunnel.health(candidate, timeout_seconds=2.0)
            if checks and all(check.ok for check in checks):
                return True, f"healthy after {attempt + 1} probe(s)"
            if self._health_interval:
                self._sleeper.sleep(self._health_interval)
        return False, f"not healthy within {self._health_attempts} probes"

    def _drain(self, old_child: ChildProcess) -> None:
        if self._control is None or self._ids is None:
            return
        try:
            self._control.request(
                ControlRequest(
                    1,
                    ControlCommand.DRAIN,
                    self._ids.new_hex(24),
                    payload=(("timeout_seconds", self._drain_timeout),),
                ),
                timeout_seconds=self._drain_timeout + 5.0,
            )
        except ConfigError:
            # Best-effort: a drain that cannot be requested still proceeds to a
            # reconcile + terminate, which is the safe fallback the old restart path took.
            return
