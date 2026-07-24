"""Answer "which commit is the live runtime actually serving?" -- truthfully.

The desired release (what ``current`` points at) and the observed release (what the
live process is really running) are reported as *separate* facts, because they can
legitimately diverge: after a symlink switch whose restart has not happened or has
failed, `current` is the candidate while the running process is still the old
release. Reporting only the symlink would state a wish as a fact, so when the two
disagree this status fails closed and says an activation has not converged.

This is a pure read: it never rebuilds, spawns, or restarts anything, so it answers
even when the connector channel is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...ports.activation import ObservedRuntime, ReleaseStore


@dataclass(frozen=True, slots=True)
class RuntimeIdentityInputs:
    """The live-side facts the CLI supplies alongside on-disk release state."""

    launcher_version: str | None = None
    running_tool_surface_hash: str | None = None
    process_start_identity: str | None = None
    active_generation: int | None = None


def build_version_status(
    store: ReleaseStore,
    inputs: RuntimeIdentityInputs,
    observed: ObservedRuntime | None = None,
) -> dict[str, object]:
    """Return the ``rf version status`` payload."""
    desired_sha = store.current_sha()
    desired_manifest = store.read_manifest(desired_sha) if desired_sha else None
    observed_sha = observed.running_release_sha if observed is not None else None
    observed_manifest = store.read_manifest(observed_sha) if observed_sha else None
    phase = observed.phase if observed is not None else "unknown"
    running = phase not in {"stopped", "unknown"}

    # Convergence is only claimable when a live process was observed serving the
    # release the symlink desires.
    converged = observed_sha is not None and observed_sha == desired_sha
    receipts = store.list_receipts()
    latest = receipts[0] if receipts else None

    # Rediscovery is a property of the activation that installed the running release,
    # not a live hash comparison: once the new runtime is up the hashes match again,
    # which would wrongly clear the flag for clients still holding the old surface.
    rediscovery_required = bool(latest.rediscovery_required) if latest is not None else False
    if not converged and desired_manifest is not None and observed_manifest is not None:
        rediscovery_required = (
            desired_manifest.tool_surface_hash != observed_manifest.tool_surface_hash
        )

    return {
        "launcher_version": inputs.launcher_version,
        # Desired: what the release symlink points at.
        "desired_commit": desired_sha,
        "desired_release_path": str(store.release_path(desired_sha)) if desired_sha else None,
        "desired_tool_surface_hash": desired_manifest.tool_surface_hash
        if desired_manifest
        else None,
        # Observed: what the live process is actually running.
        "observed_commit": observed_sha,
        "observed_release_path": str(store.release_path(observed_sha)) if observed_sha else None,
        "observed_executable": observed.executable if observed is not None else None,
        "observed_phase": phase,
        "observed_pid": observed.pid if observed is not None else None,
        # `active_commit` is only truthful when the two agree.
        "active_commit": desired_sha if converged else None,
        "activation_converged": converged,
        "previous_commit": store.previous_sha(),
        "source_worktree": desired_manifest.source_worktree if desired_manifest else None,
        "build_fingerprint": desired_manifest.build_fingerprint if desired_manifest else None,
        "package_version": desired_manifest.package_version if desired_manifest else None,
        "tool_surface_hash": desired_manifest.tool_surface_hash if desired_manifest else None,
        "running_tool_surface_hash": inputs.running_tool_surface_hash,
        "process_start_identity": inputs.process_start_identity,
        "active_generation": inputs.active_generation,
        "activation_receipt": latest.receipt_id if latest is not None else None,
        "activation_receipt_outcome": latest.outcome.value if latest is not None else None,
        "client_rediscovery_required": rediscovery_required,
        "safe_next_action": _safe_next_action(
            desired_sha=desired_sha,
            observed_sha=observed_sha,
            converged=converged,
            running=running,
            rediscovery_required=rediscovery_required,
        ),
    }


def build_version_list(store: ReleaseStore) -> dict[str, object]:
    """Return the ``rf version list`` payload: every installed release, newest first."""
    current = store.current_sha()
    previous = store.previous_sha()
    releases = [
        {
            "commit_sha": manifest.commit_sha,
            "package_version": manifest.package_version,
            "build_fingerprint": manifest.build_fingerprint,
            "tool_surface_hash": manifest.tool_surface_hash,
            "built_at": manifest.built_at,
            "source_worktree": manifest.source_worktree,
            "current": manifest.commit_sha == current,
            "previous": manifest.commit_sha == previous,
        }
        for manifest in store.list_releases()
    ]
    return {
        "current": current,
        "previous": previous,
        "releases": releases,
    }


def _safe_next_action(
    *,
    desired_sha: str | None,
    observed_sha: str | None,
    converged: bool,
    running: bool,
    rediscovery_required: bool,
) -> str:
    if desired_sha is None:
        return "No release is active. Run `rf upgrade --from-worktree . --activate`."
    if not running:
        return (
            f"`current` points at {desired_sha} but no runtime is running. "
            "Run `rf start --background` to serve it."
        )
    if not converged:
        # Fail closed: never claim identity is current while the two disagree.
        return (
            f"ACTIVATION NOT CONVERGED: `current` points at {desired_sha} but the live "
            f"runtime is serving {observed_sha or 'an unidentified release'}. Run "
            "`rf runtime restart` to adopt it, or `rf upgrade rollback` to return."
        )
    if rediscovery_required:
        return (
            "The activated release changed the tool surface. Reconnect the connector "
            "so it rediscovers tools."
        )
    return "Runtime identity is current."
