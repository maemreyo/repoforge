"""The standalone upgrade pipeline: worktree -> built wheel -> release -> activation.

Runs in the launcher/CLI context, never as a runtime tool call -- this is the trust
boundary that lets it mutate the install tree while the live runtime may not. It does
not require the runtime being replaced to be healthy: the build, install, and smoke
steps all run against the candidate itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...domain.activation import ActivationOutcome, ActivationReceipt, ReleaseManifest
from ...domain.errors import ConfigError
from ...ports import (
    ReleaseBuilder,
    ReleaseInstaller,
    ReleaseSmokeTester,
    ReleaseStore,
    RuntimeHealthProbe,
    SupervisorReloader,
    WorktreeInspector,
)
from ...ports.clock import Clock
from ...ports.sleeper import Sleeper

DEFAULT_KEEP_RELEASES = 5
DEFAULT_HEALTH_WINDOW_SECONDS = 30.0
DEFAULT_HEALTH_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    status: str
    candidate_sha: str
    build_fingerprint: str
    tool_surface_hash: str
    previous_sha: str | None = None
    active_sha: str | None = None
    activation_receipt: str | None = None
    rediscovery_required: bool = False
    pruned: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        rollback = (
            f"rf upgrade rollback {self.activation_receipt}"
            if self.activation_receipt and self.status == "activated"
            else None
        )
        return {
            "status": self.status,
            "candidate_sha": self.candidate_sha,
            "previous_sha": self.previous_sha,
            "active_sha": self.active_sha,
            "build_fingerprint": self.build_fingerprint,
            "tool_surface_hash": self.tool_surface_hash,
            "activation_receipt": self.activation_receipt,
            "client_rediscovery_required": self.rediscovery_required,
            "pruned_releases": list(self.pruned),
            "rollback_command": rollback,
            "detail": self.detail,
        }


class UpgradeService:
    """Orchestrate build/smoke/install/activate/rollback over injected boundaries."""

    def __init__(
        self,
        *,
        store: ReleaseStore,
        inspector: WorktreeInspector,
        builder: ReleaseBuilder,
        installer: ReleaseInstaller,
        smoke: ReleaseSmokeTester,
        reloader: SupervisorReloader,
        clock: Clock,
        health_probe: RuntimeHealthProbe | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._store = store
        self._inspector = inspector
        self._builder = builder
        self._installer = installer
        self._smoke = smoke
        self._reloader = reloader
        self._clock = clock
        self._health_probe = health_probe
        self._sleeper = sleeper

    def upgrade(
        self,
        worktree: Path,
        *,
        activate: bool,
        keep_releases: int = DEFAULT_KEEP_RELEASES,
        watch: bool = False,
        health_window_seconds: float = DEFAULT_HEALTH_WINDOW_SECONDS,
        health_interval_seconds: float = DEFAULT_HEALTH_INTERVAL_SECONDS,
    ) -> UpgradeResult:
        state = self._inspector.inspect(worktree)
        if not state.clean:
            raise ConfigError(
                "WORKTREE_DIRTY: refusing to build an ambiguous release from an "
                f"uncommitted worktree ({state.dirty_detail or 'uncommitted changes'})"
            )
        commit_sha = state.head_sha

        artifact = self._builder.build(worktree)
        destination = self._store.release_path(commit_sha)
        self._installer.install(artifact.wheel_path, destination)

        smoke = self._smoke.smoke(destination)
        if not smoke.ok:
            raise ConfigError(f"SMOKE_FAILED: candidate {commit_sha} did not pass: {smoke.detail}")

        manifest = ReleaseManifest(
            commit_sha=commit_sha,
            package_version=artifact.package_version,
            build_fingerprint=artifact.build_fingerprint,
            tool_surface_hash=smoke.tool_surface_hash,
            source_worktree=str(worktree),
            built_at=self._clock.now_iso(),
        )
        self._store.write_manifest(manifest)

        if not activate:
            return UpgradeResult(
                status="staged",
                candidate_sha=commit_sha,
                build_fingerprint=artifact.build_fingerprint,
                tool_surface_hash=smoke.tool_surface_hash,
                previous_sha=self._store.current_sha(),
                detail="Release built, smoke-tested, and installed. Not activated.",
            )
        activated = self._activate(manifest, keep_releases=keep_releases)
        if not watch:
            return activated
        return self._watch_or_rollback(
            activated,
            window_seconds=health_window_seconds,
            interval_seconds=health_interval_seconds,
        )

    def _watch_or_rollback(
        self, activated: UpgradeResult, *, window_seconds: float, interval_seconds: float
    ) -> UpgradeResult:
        """Watch health for a window; auto-rollback to the warm ``previous`` on failure.

        Health passing at activation time is not proof of health minutes later, so a
        candidate that crash-loops or degrades inside the window is rolled back to the
        release that ``previous`` still points at (kept warm -- prune never removes it).
        """
        if self._health_probe is None:
            return replace(
                activated,
                detail=activated.detail + " Health window skipped (no probe configured).",
            )
        detail = self._sample_window(
            window_seconds=window_seconds, interval_seconds=interval_seconds
        )
        if detail is None:
            return replace(
                activated,
                detail=f"{activated.detail} Healthy through the {window_seconds:g}s window.",
            )
        rolled_back = self.rollback(receipt_id=activated.activation_receipt)
        return replace(
            rolled_back,
            detail=(
                f"Auto-rollback: candidate {activated.candidate_sha} unhealthy within the "
                f"{window_seconds:g}s window ({detail}). {rolled_back.detail}"
            ),
        )

    def _sample_window(self, *, window_seconds: float, interval_seconds: float) -> str | None:
        """Return a failure detail if health degrades in the window, else ``None``."""
        assert self._health_probe is not None
        interval = max(0.1, interval_seconds)
        iterations = max(1, int(window_seconds // interval))
        for _ in range(iterations):
            if self._sleeper is not None:
                self._sleeper.sleep(interval)
            sample = self._health_probe.sample()
            if not sample.healthy:
                return sample.detail
        return None

    def _activate(self, manifest: ReleaseManifest, *, keep_releases: int) -> UpgradeResult:
        commit_sha = manifest.commit_sha
        previous_sha = self._store.current_sha()
        previous_manifest = (
            self._store.read_manifest(previous_sha) if previous_sha is not None else None
        )
        rediscovery_required = (
            previous_manifest is not None
            and previous_manifest.tool_surface_hash != manifest.tool_surface_hash
        )

        self._store.swap_current(commit_sha)
        self._store.write_launcher_shim()
        reloaded = self._reloader.reload()
        outcome = ActivationOutcome.ACTIVATED
        detail = (
            "Activated; supervisor reloaded."
            if reloaded
            else "Activated; supervisor not running (will adopt on next start)."
        )

        receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=previous_sha,
            to_sha=commit_sha,
            from_fingerprint=previous_manifest.build_fingerprint if previous_manifest else None,
            to_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            rediscovery_required=rediscovery_required,
            outcome=outcome,
            activated_at=self._clock.now_iso(),
            detail=detail,
        )
        self._store.write_receipt(receipt)
        pruned = self._store.prune(keep=keep_releases)
        return UpgradeResult(
            status="activated",
            candidate_sha=commit_sha,
            build_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            previous_sha=previous_sha,
            active_sha=commit_sha,
            activation_receipt=receipt.receipt_id,
            rediscovery_required=rediscovery_required,
            pruned=tuple(pruned),
            detail=detail,
        )

    def rollback(self, receipt_id: str | None = None) -> UpgradeResult:
        target = self._store.rollback()
        reloaded = self._reloader.reload()
        manifest = self._store.read_manifest(target)
        if manifest is None:
            raise ConfigError(f"ROLLBACK_MANIFEST_MISSING: {target}")
        detail = f"Rolled back to {target}."
        if receipt_id is not None:
            detail = f"Rolled back to {target} (from receipt {receipt_id})."
        receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=self._store.previous_sha(),
            to_sha=target,
            from_fingerprint=None,
            to_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            rediscovery_required=True,
            outcome=ActivationOutcome.ROLLED_BACK,
            activated_at=self._clock.now_iso(),
            detail=detail,
        )
        self._store.write_receipt(receipt)
        return UpgradeResult(
            status="rolled_back",
            candidate_sha=target,
            build_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            active_sha=target,
            activation_receipt=receipt.receipt_id,
            rediscovery_required=True,
            detail=detail + (" Supervisor reloaded." if reloaded else ""),
        )

    def _date_stamp(self) -> str:
        return self._clock.now_iso()[:10].replace("-", "")
