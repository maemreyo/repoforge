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

from ...domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
)
from ...domain.errors import ConfigError
from ...ports import (
    ReleaseBuilder,
    ReleaseInstaller,
    ReleaseObserver,
    ReleaseSmokeTester,
    ReleaseStore,
    RuntimeHealthProbe,
    RuntimeRestarter,
    WorktreeInspector,
)
from ...ports.clock import Clock
from ...ports.sleeper import Sleeper

DEFAULT_KEEP_RELEASES = 5
DEFAULT_HEALTH_WINDOW_SECONDS = 30.0
DEFAULT_HEALTH_INTERVAL_SECONDS = 5.0
# A single failed sample is a blip; rollback needs sustained failure.
DEFAULT_HEALTH_FAILURE_THRESHOLD = 3


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
    observed_sha: str | None = None
    converged: bool = False
    stage: str = ""

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
            "observed_sha": self.observed_sha,
            "converged": self.converged,
            "stage": self.stage,
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
        restarter: RuntimeRestarter,
        observer: ReleaseObserver,
        clock: Clock,
        health_probe: RuntimeHealthProbe | None = None,
        sleeper: Sleeper | None = None,
        converge_attempts: int = 20,
        converge_interval_seconds: float = 0.5,
        health_failure_threshold: int = DEFAULT_HEALTH_FAILURE_THRESHOLD,
    ) -> None:
        self._store = store
        self._inspector = inspector
        self._builder = builder
        self._installer = installer
        self._smoke = smoke
        self._restarter = restarter
        self._observer = observer
        self._clock = clock
        self._health_probe = health_probe
        self._sleeper = sleeper
        self._converge_attempts = max(1, converge_attempts)
        self._converge_interval = max(0.0, converge_interval_seconds)
        self._failure_threshold = max(1, health_failure_threshold)

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
        # Releases are immutable: only install when this commit is not already present
        # with identical bits (reserve_release raises on a fingerprint conflict).
        if self._store.reserve_release(commit_sha, build_fingerprint=artifact.build_fingerprint):
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
        """Return a failure detail if health degrades *persistently*, else ``None``.

        A single unhealthy sample is a transient blip -- a slow probe, a momentarily
        busy socket -- and must not trigger a rollback. Only ``failure_threshold``
        consecutive failures do; any healthy sample resets the streak.
        """
        assert self._health_probe is not None
        interval = max(0.1, interval_seconds)
        iterations = max(1, int(window_seconds // interval))
        streak = 0
        last_detail = ""
        for _ in range(iterations):
            if self._sleeper is not None:
                self._sleeper.sleep(interval)
            sample = self._health_probe.sample()
            if sample.healthy:
                streak = 0
                continue
            streak += 1
            last_detail = sample.detail
            if streak >= self._failure_threshold:
                return (
                    f"{streak} consecutive unhealthy samples "
                    f"(threshold {self._failure_threshold}): {last_detail}"
                )
        return None

    def _activate(self, manifest: ReleaseManifest, *, keep_releases: int) -> UpgradeResult:
        """Switch `current`, replace the runtime, and prove the candidate is serving.

        Staged and fail-closed: nothing terminalizes as ``activated`` until the live
        runtime has been *observed* running the candidate release and health-verified.
        Any failure after the symlink switch rolls back to `previous`, restarts, and
        writes a FAILED receipt linked to the rollback receipt.
        """
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
        stage = ActivationStage.SYMLINK_SWITCHED

        restart = self._restarter.restart()
        if restart.ok:
            stage = ActivationStage.RUNTIME_RESTARTED
        converged, observed_sha, verify_detail = self._verify_serving(commit_sha)
        if converged:
            stage = ActivationStage.HEALTH_VERIFIED

        if not (restart.ok and converged):
            return self._fail_and_rollback(
                manifest,
                previous_sha=previous_sha,
                previous_manifest=previous_manifest,
                rediscovery_required=rediscovery_required,
                stage=stage,
                observed_sha=observed_sha,
                detail=(
                    f"Activation failed: {restart.detail if not restart.ok else verify_detail}"
                ),
            )

        # Only a converged, health-verified activation may publish the stable shim
        # and prune: both are irreversible relative to the release we just proved.
        self._store.write_launcher_shim()
        detail = f"Activated and verified: {verify_detail}"
        receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=previous_sha,
            to_sha=commit_sha,
            from_fingerprint=previous_manifest.build_fingerprint if previous_manifest else None,
            to_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            rediscovery_required=rediscovery_required,
            outcome=ActivationOutcome.ACTIVATED,
            activated_at=self._clock.now_iso(),
            detail=detail,
            stage=ActivationStage.HEALTH_VERIFIED,
            observed_sha=observed_sha,
            converged=True,
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
            observed_sha=observed_sha,
            converged=True,
            stage=ActivationStage.HEALTH_VERIFIED.value,
            detail=detail,
        )

    def _verify_serving(self, expected_sha: str) -> tuple[bool, str | None, str]:
        """Poll until the live runtime is observed serving ``expected_sha`` and healthy."""
        observed_sha: str | None = None
        detail = "runtime never reported a release"
        for attempt in range(self._converge_attempts):
            if attempt and self._sleeper is not None:
                self._sleeper.sleep(self._converge_interval)
            observation = self._observer.observe()
            observed_sha = observation.running_release_sha
            if observed_sha is None:
                detail = (
                    "runtime did not publish a release identity "
                    f"(phase={observation.phase}, executable={observation.executable})"
                )
                continue
            if observed_sha != expected_sha:
                detail = f"runtime is serving {observed_sha}, expected {expected_sha}"
                continue
            if self._health_probe is None:
                return True, observed_sha, f"observed {observed_sha} (health probe not configured)"
            sample = self._health_probe.sample()
            if sample.healthy:
                return True, observed_sha, f"observed {observed_sha} healthy: {sample.detail}"
            detail = f"observed {observed_sha} but unhealthy: {sample.detail}"
        return False, observed_sha, detail

    def _fail_and_rollback(
        self,
        manifest: ReleaseManifest,
        *,
        previous_sha: str | None,
        previous_manifest: ReleaseManifest | None,
        rediscovery_required: bool,
        stage: ActivationStage,
        observed_sha: str | None,
        detail: str,
    ) -> UpgradeResult:
        """Record a FAILED activation and restore `previous`, linking both receipts."""
        failed_receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=previous_sha,
            to_sha=manifest.commit_sha,
            from_fingerprint=previous_manifest.build_fingerprint if previous_manifest else None,
            to_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            rediscovery_required=rediscovery_required,
            outcome=ActivationOutcome.FAILED,
            activated_at=self._clock.now_iso(),
            detail=detail,
            stage=stage,
            observed_sha=observed_sha,
            converged=False,
        )
        self._store.write_receipt(failed_receipt)
        if previous_sha is None:
            # Nothing to restore: there was no prior release. Leave `current` at the
            # candidate but report failure loudly -- never a soft success.
            raise ConfigError(
                f"ACTIVATION_FAILED: {detail}. No previous release exists to roll back to; "
                f"receipt {failed_receipt.receipt_id}."
            )
        rolled_back = self.rollback(
            receipt_id=failed_receipt.receipt_id, expected_current=manifest.commit_sha
        )
        raise ConfigError(
            f"ACTIVATION_FAILED_ROLLED_BACK: {detail}. Rolled back to {rolled_back.active_sha} "
            f"(failed receipt {failed_receipt.receipt_id}, "
            f"rollback receipt {rolled_back.activation_receipt})."
        )

    def rollback(
        self,
        receipt_id: str | None = None,
        *,
        expected_current: str | None = None,
        force: bool = False,
    ) -> UpgradeResult:
        """Roll back, targeting the release named by ``receipt_id`` when given.

        A receipted rollback is reproducible: the target is the receipt's ``from_sha``,
        and it is refused unless `current` still matches what that receipt activated
        (so repeating the command cannot toggle releases back and forth).
        """
        current = self._store.current_sha()
        cause_receipt_id: str | None = None
        if receipt_id is not None:
            cause = self._store.read_receipt(receipt_id)
            if cause is None:
                raise ConfigError(f"RECEIPT_NOT_FOUND: {receipt_id}")
            if cause.from_sha is None:
                raise ConfigError(
                    f"RECEIPT_NOT_REVERSIBLE: {receipt_id} activated the first release; "
                    "there is no earlier release to return to"
                )
            cause_receipt_id = cause.receipt_id
            target = cause.from_sha
            guard = expected_current if expected_current is not None else cause.to_sha
            if not force and current is not None and current != guard:
                raise ConfigError(
                    f"ROLLBACK_STATE_MISMATCH: receipt {receipt_id} activated {guard} but "
                    f"current is {current}; re-run with force to override"
                )
            self._store.swap_current(target)
        else:
            target = self._store.rollback()

        manifest = self._store.read_manifest(target)
        if manifest is None:
            raise ConfigError(f"ROLLBACK_MANIFEST_MISSING: {target}")
        restart = self._restarter.restart()
        converged, observed_sha, verify_detail = self._verify_serving(target)
        detail = f"Rolled back to {target}: {verify_detail}"
        receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=current,
            to_sha=target,
            from_fingerprint=None,
            to_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            rediscovery_required=True,
            outcome=ActivationOutcome.ROLLED_BACK,
            activated_at=self._clock.now_iso(),
            detail=detail,
            stage=(
                ActivationStage.HEALTH_VERIFIED
                if converged
                else ActivationStage.RUNTIME_RESTARTED
                if restart.ok
                else ActivationStage.SYMLINK_SWITCHED
            ),
            observed_sha=observed_sha,
            converged=converged,
            cause_receipt_id=cause_receipt_id,
        )
        self._store.write_receipt(receipt)
        return UpgradeResult(
            status="rolled_back",
            candidate_sha=target,
            build_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            previous_sha=current,
            active_sha=target,
            activation_receipt=receipt.receipt_id,
            rediscovery_required=True,
            observed_sha=observed_sha,
            converged=converged,
            stage=receipt.stage.value,
            detail=detail,
        )

    def _date_stamp(self) -> str:
        return self._clock.now_iso()[:10].replace("-", "")
