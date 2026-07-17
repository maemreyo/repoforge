"""Pure runtime identity, skew, and client-rediscovery health classification."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    package_version: str | None
    executable: str | None
    install_origin: str | None

    def as_dict(self, *, prefix: str) -> dict[str, str | None]:
        return {
            f"{prefix}_package_version": self.package_version,
            f"{prefix}_executable": self.executable,
            f"{prefix}_install_origin": self.install_origin,
        }


@dataclass(frozen=True, slots=True)
class RuntimeHealthSnapshot:
    running: RuntimeIdentity
    current: RuntimeIdentity
    running_tool_surface_hash: str | None
    current_tool_surface_hash: str | None
    accepted_generation: int | None
    active_generation: int | None
    phase: str
    package_version_skew: bool | None
    generation_activation_required: bool
    restart_required: bool
    hot_reload_available: bool
    client_rediscovery_recommended: bool
    rediscovery_reason: str | None
    unknown_fields: tuple[str, ...]
    safe_next_action: str

    def as_dict(self) -> dict[str, object]:
        return {
            **self.running.as_dict(prefix="running"),
            **self.current.as_dict(prefix="current"),
            "running_tool_surface_hash": self.running_tool_surface_hash,
            "current_tool_surface_hash": self.current_tool_surface_hash,
            "package_version_skew": self.package_version_skew,
            "generation_activation_required": self.generation_activation_required,
            "restart_required": self.restart_required,
            "hot_reload_available": self.hot_reload_available,
            "client_rediscovery_recommended": self.client_rediscovery_recommended,
            "rediscovery_reason": self.rediscovery_reason,
            "unknown_fields": list(self.unknown_fields),
            "safe_next_action": self.safe_next_action,
        }


def _known_difference(left: str | None, right: str | None) -> bool | None:
    if left is None or right is None:
        return None
    return left != right


def assess_runtime_health(
    *,
    running: RuntimeIdentity,
    current: RuntimeIdentity,
    running_tool_surface_hash: str | None,
    current_tool_surface_hash: str | None,
    accepted_generation: int | None,
    active_generation: int | None,
    phase: str,
) -> RuntimeHealthSnapshot:
    """Classify exact identity skew without timestamps or vendor-name heuristics."""

    package_skew = _known_difference(running.package_version, current.package_version)
    surface_changed = _known_difference(running_tool_surface_hash, current_tool_surface_hash)
    generation_required = (
        accepted_generation is not None and active_generation != accepted_generation
    )
    runtime_alive = phase in {"healthy", "degraded", "reloading", "draining"}
    hot_reload_available = runtime_alive and package_skew is not True
    restart_required = package_skew is True or (generation_required and not hot_reload_available)
    rediscovery = surface_changed is True

    unknown = tuple(
        sorted(
            key
            for key, value in {
                "running_package_version": running.package_version,
                "running_executable": running.executable,
                "running_install_origin": running.install_origin,
                "current_executable": current.executable,
                "current_install_origin": current.install_origin,
                "running_tool_surface_hash": running_tool_surface_hash,
            }.items()
            if value is None
        )
    )

    if package_skew is True:
        version = current.package_version or "the current reviewed version"
        action = (
            f"Reinstall RepoForge {version}, restart the managed runtime, then reconnect the MCP "
            "client so it rediscovers the current tool surface."
        )
    elif phase == "stopped":
        action = "Run `rf runtime start` or inspect logs."
    elif generation_required and hot_reload_available:
        action = "Run `rf runtime reload` to activate the accepted configuration generation."
    elif generation_required:
        action = "Run `rf runtime restart` to activate the accepted configuration generation."
    elif rediscovery:
        action = (
            "Reconnect or recreate the MCP connector so the client rediscovers the changed tool "
            "surface; the serving process cannot invalidate a client-side manifest cache."
        )
    elif phase in {"failed", "fail_closed"}:
        action = "Run `rf runtime reload` after correcting the reported failure."
    elif phase == "healthy":
        action = "Runtime is healthy."
    else:
        action = "Run `rf runtime start` or inspect logs."

    return RuntimeHealthSnapshot(
        running=running,
        current=current,
        running_tool_surface_hash=running_tool_surface_hash,
        current_tool_surface_hash=current_tool_surface_hash,
        accepted_generation=accepted_generation,
        active_generation=active_generation,
        phase=phase,
        package_version_skew=package_skew,
        generation_activation_required=generation_required,
        restart_required=restart_required,
        hot_reload_available=hot_reload_available,
        client_rediscovery_recommended=rediscovery,
        rediscovery_reason="tool_surface_changed" if rediscovery else None,
        unknown_fields=unknown,
        safe_next_action=action,
    )
