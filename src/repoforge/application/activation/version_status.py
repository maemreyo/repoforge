"""Project release + runtime state into an answer to "which commit is running?".

This is a pure read: it never rebuilds, spawns, or introspects a live process. Every
field is a projection of persisted release manifests and the runtime record, so
``rf version status`` answers even when the connector channel is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...ports.activation import ReleaseStore


@dataclass(frozen=True, slots=True)
class RuntimeIdentityInputs:
    """The live-side facts the CLI supplies alongside on-disk release state."""

    launcher_version: str | None = None
    running_tool_surface_hash: str | None = None
    process_start_identity: str | None = None
    active_generation: int | None = None


def build_version_status(store: ReleaseStore, inputs: RuntimeIdentityInputs) -> dict[str, object]:
    """Return the ``rf version status`` payload."""
    active_sha = store.current_sha()
    manifest = store.read_manifest(active_sha) if active_sha else None
    receipts = store.list_receipts()
    latest_receipt = receipts[0].receipt_id if receipts else None
    installed_surface = manifest.tool_surface_hash if manifest else None
    running_surface = inputs.running_tool_surface_hash
    rediscovery_required = (
        running_surface is not None
        and installed_surface is not None
        and running_surface != installed_surface
    )
    return {
        "launcher_version": inputs.launcher_version,
        "active_commit": active_sha,
        "active_release_path": str(store.release_path(active_sha)) if active_sha else None,
        "previous_commit": store.previous_sha(),
        "source_worktree": manifest.source_worktree if manifest else None,
        "build_fingerprint": manifest.build_fingerprint if manifest else None,
        "package_version": manifest.package_version if manifest else None,
        "tool_surface_hash": installed_surface,
        "running_tool_surface_hash": running_surface,
        "process_start_identity": inputs.process_start_identity,
        "active_generation": inputs.active_generation,
        "activation_receipt": latest_receipt,
        "client_rediscovery_required": rediscovery_required,
        "safe_next_action": _safe_next_action(active_sha, rediscovery_required),
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


def _safe_next_action(active_sha: str | None, rediscovery_required: bool) -> str:
    if active_sha is None:
        return "No release is active. Run `rf upgrade --from-worktree . --activate`."
    if rediscovery_required:
        return (
            "The installed tool surface differs from the running one. Reconnect the "
            "connector to rediscover tools, or restart the runtime."
        )
    return "Runtime identity is current."
