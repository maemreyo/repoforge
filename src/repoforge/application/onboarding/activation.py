"""Onboarding-facing activation façade preserving generation activator semantics."""

from __future__ import annotations

from dataclasses import asdict

from ...domain.config_generation import ConfigGeneration
from ...domain.runtime import RuntimePhase
from ...ports.configuration import ConfigurationStore
from ...ports.runtime_control import RuntimeStore
from ..runtime.activation import GenerationActivator


class ConfigurationActivator:
    def __init__(
        self, *, configs: ConfigurationStore, runtime: RuntimeStore, activator: GenerationActivator
    ):
        self._configs = configs
        self._runtime = runtime
        self._activator = activator

    def activate(
        self, generation: ConfigGeneration, *, mode: str, wait: bool, rollback_on_failure: bool
    ) -> dict[str, object]:
        if mode not in {"auto", "always", "never"}:
            raise ValueError(f"Unsupported activation mode: {mode}")
        if not wait and rollback_on_failure:
            raise ValueError("--no-wait requires --no-rollback-on-failure")
        running = self._runtime.read()
        managed = running is not None and running.phase not in {
            RuntimePhase.STOPPED,
            RuntimePhase.FAILED,
        }
        if mode == "never" or (mode == "auto" and not managed):
            active = self._configs.active()
            return {
                "status": "restart_required" if active else "stopped",
                "config_generation": generation.generation,
                "active_generation": active.generation if active else None,
                "restart_required": active is None or active.generation != generation.generation,
                "safe_next_action": f"Run `rf --config {self._configs.source_path} runtime start` to activate generation {generation.generation}.",
            }
        return asdict(
            self._activator.activate(
                generation,
                extra_env={},
                wait_for_health=wait,
                rollback_on_failure=rollback_on_failure,
            )
        )
