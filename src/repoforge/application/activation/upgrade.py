"""The standalone upgrade pipeline: worktree -> built wheel -> release -> activation.

Runs in the launcher/CLI context, never as a runtime tool call -- this is the trust
boundary that lets it mutate the install tree while the live runtime may not. It does
not require the runtime being replaced to be healthy: the build, install, and smoke
steps all run against the candidate itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ...domain.activation import (
    ActivationOutcome,
    ActivationReceipt,
    ActivationStage,
    ReleaseManifest,
    worker_reclamation_summary,
)
from ...domain.errors import ConfigError
from ...ports import (
    ReleaseBuilder,
    ReleaseInstaller,
    ReleaseObserver,
    ReleaseProcessInspector,
    ReleaseSmokeTester,
    ReleaseStore,
    RuntimeHealthProbe,
    RuntimeRestarter,
    WorktreeInspector,
)
from ...ports.clock import Clock
from ...ports.locking import LockManager
from ...ports.sleeper import Sleeper
from .selection import resolve_release

DEFAULT_KEEP_RELEASES = 5
DEFAULT_HEALTH_WINDOW_SECONDS = 30.0
DEFAULT_HEALTH_INTERVAL_SECONDS = 5.0
# A single failed sample is a blip; rollback needs sustained failure.
DEFAULT_HEALTH_FAILURE_THRESHOLD = 3
_RECEIPT_ID_ATTEMPTS = 8
RECONCILE_COMMAND = "rf upgrade reconcile"
#: Temp-dir prefix used by the wheel builder for the wheel that survives build()
#: until this service installs (or skips) it; cleanup removes the wheel's own dir,
#: never a caller-provided path.
_WHEEL_TMP_PREFIX = "repoforge-upgrade-wheel-"
#: The ONLY failure dispositions `reconcile --repair rollback` may authorize: the
#: release's own contract identity diverged, so the runtime is deterministically
#: fail-closed. Anything else (tunnel, worker, handoff, transient) needs its own
#: investigation -- repair is destructive, so it never acts on a guess (#420).
_REPAIR_ALLOWLISTED_ERRORS = frozenset(
    {"CONTRACT_ARTIFACT_MISMATCH", "RELEASE_CONTRACT_IDENTITY_MISMATCH"}
)


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
    # Releases retention wanted to remove but a live process is still executing from.
    retained_for_live_process: tuple[str, ...] = ()
    detail: str = ""
    observed_sha: str | None = None
    converged: bool = False
    stage: str = ""
    path_launcher_status: str = ""
    path_launcher_detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        rollback = (
            f"rf upgrade rollback {self.activation_receipt}"
            # A reconciled activation ends with an ordinary activation receipt, so it is
            # reversible in exactly the same way.
            if self.activation_receipt and self.status in {"activated", "reconciled"}
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
            "retained_for_live_process": list(self.retained_for_live_process),
            "path_launcher_status": self.path_launcher_status,
            "path_launcher_detail": self.path_launcher_detail,
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
        # The supervisor's own startup health budget is ~30s (tunnel init, doctor,
        # repository self-check), so convergence must outlast it or a valid candidate
        # would be rolled back before it ever became healthy.
        converge_attempts: int = 120,
        converge_interval_seconds: float = 0.5,
        health_failure_threshold: int = DEFAULT_HEALTH_FAILURE_THRESHOLD,
        locks: LockManager | None = None,
        lock_timeout_seconds: float = 900.0,
        release_processes: ReleaseProcessInspector | None = None,
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
        self._locks = locks
        self._lock_timeout = lock_timeout_seconds
        self._release_processes = release_processes

    @contextmanager
    def _activation_lock(self) -> Iterator[None]:
        """Serialize build/install/swap/restart/receipt/prune across processes.

        Without this, two concurrent upgrades can interleave their `current`/`previous`
        swaps and receipts and leave the layout inconsistent.
        """
        if self._locks is None:
            yield
            return
        with self._locks.lock(
            "runtime-activation",
            timeout_seconds=self._lock_timeout,
            metadata={"operation": "runtime-activation"},
        ):
            yield

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
        with self._activation_lock():
            return self._upgrade_locked(
                worktree,
                activate=activate,
                keep_releases=keep_releases,
                watch=watch,
                health_window_seconds=health_window_seconds,
                health_interval_seconds=health_interval_seconds,
            )

    def _upgrade_locked(
        self,
        worktree: Path,
        *,
        activate: bool,
        keep_releases: int,
        watch: bool,
        health_window_seconds: float,
        health_interval_seconds: float,
    ) -> UpgradeResult:
        self._require_no_unreconciled_activation(action="starting another activation")

        state = self._inspector.inspect(worktree)
        if not state.clean:
            raise ConfigError(
                "WORKTREE_DIRTY: refusing to build an ambiguous release from an "
                f"uncommitted worktree ({state.dirty_detail or 'uncommitted changes'})"
            )
        commit_sha = state.head_sha

        artifact = self._builder.build(worktree, commit_sha=commit_sha)
        destination = self._store.release_path(commit_sha)
        try:
            # Releases are immutable: only install when this commit is not already
            # present with identical bits (reserve_release raises on a fingerprint
            # conflict).
            fresh = self._store.reserve_release(
                commit_sha, build_fingerprint=artifact.build_fingerprint
            )
            if fresh:
                self._installer.install(artifact.wheel_path, destination)

            smoke = self._smoke.smoke(destination)
        finally:
            # Covers both the consumed wheel and the already-installed (never installed) path.
            wheel = artifact.wheel_path
            wheel.unlink(missing_ok=True)
            if wheel.parent.name.startswith(_WHEEL_TMP_PREFIX):
                with suppress(OSError):
                    wheel.parent.rmdir()
        # A contract mismatch is a distinct, typed failure: the candidate runs but was
        # built from a stale worktree, so it would crash-loop as soon as it serves.
        if smoke.contract_mismatched_fields:
            raise ConfigError(
                "RELEASE_CONTRACT_IDENTITY_MISMATCH: candidate "
                f"{commit_sha} packaged contract identity differs from its in-process "
                f"registry ({', '.join(smoke.contract_mismatched_fields)}); offending "
                f"artifacts: {', '.join(smoke.contract_artifact_paths) or 'unknown'}; "
                f"packaged identity {smoke.contract_packaged_identity or 'unknown'}, "
                f"computed identity {smoke.contract_computed_identity or 'unknown'}. "
                f"{smoke.detail}"
            )
        if not smoke.ok:
            raise ConfigError(f"SMOKE_FAILED: candidate {commit_sha} did not pass: {smoke.detail}")

        if fresh:
            manifest = ReleaseManifest(
                commit_sha=commit_sha,
                package_version=artifact.package_version,
                build_fingerprint=artifact.build_fingerprint,
                tool_surface_hash=smoke.tool_surface_hash,
                contract_identity=smoke.contract_identity,
                source_worktree=str(worktree),
                source_digest=artifact.source_digest,
                built_at=self._clock.now_iso(),
                branch=state.branch,
                subject=state.subject,
            )
            self._store.write_manifest(manifest)
        else:
            # The release was already installed. Its manifest is immutable, so keep the
            # recorded one rather than restamping built_at/source_worktree; only verify
            # that what we just smoke-tested still matches it.
            existing = self._store.read_manifest(commit_sha)
            if existing is None:
                raise ConfigError(f"RELEASE_MANIFEST_MISSING: {commit_sha}")
            if existing.tool_surface_hash != smoke.tool_surface_hash:
                raise ConfigError(
                    f"RELEASE_SURFACE_DRIFT: installed {commit_sha} records tool surface "
                    f"{existing.tool_surface_hash} but the release now reports "
                    f"{smoke.tool_surface_hash}"
                )
            # Both non-empty is the only comparable case: an empty manifest proof means
            # the release predates #367, and an empty live probe means the smoke tester
            # does not probe (a fake) -- neither proves drift.
            if (
                existing.contract_identity
                and smoke.contract_identity
                and existing.contract_identity != smoke.contract_identity
            ):
                raise ConfigError(
                    "RELEASE_CONTRACT_IDENTITY_MISMATCH: installed "
                    f"{commit_sha} records contract identity {existing.contract_identity} "
                    f"but the release now reports {smoke.contract_identity}; the release "
                    "directory has drifted from its reviewed manifest"
                )
            manifest = existing

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

    # -- reconciliation of an unterminalized activation ----------------------

    def reconcile(self, *, repair: str | None = None) -> UpgradeResult:
        """Terminalize an activation that reached its target but wrote no receipt.

        An activation can die between the symlink swap and its receipt -- the process is
        replacing its own runtime, so it can be killed at exactly that point -- while the
        runtime still converges onto the target by following the switched symlink. The
        deployment is then correct and healthy, but the journal is non-terminal, which
        blocks every later activation. Rollback is not the answer to that state: it would
        move a healthy runtime OFF the release it is correctly serving. This is the
        forward path, and it terminalizes only what it can prove.

        ``repair="rollback"`` is the explicit recovery for the opposite state (#367): a
        release that failed closed with a typed non-retryable failure and cannot serve.
        It is never the default -- reconciliation stays forward-only -- because moving
        ``current`` is a high-impact mutation that must be asked for.
        """
        with self._activation_lock():
            in_flight = self._store.read_in_flight_activation()
            if in_flight is None:
                current = self._store.current_sha()
                return UpgradeResult(
                    status="nothing_to_reconcile",
                    candidate_sha=current or "",
                    build_fingerprint="",
                    tool_surface_hash="",
                    active_sha=current,
                    detail="No activation is in flight; there is nothing to reconcile.",
                )
            reconciled = self._reconcile_converged(in_flight)
            if reconciled is not None:
                return reconciled
            if repair == "rollback":
                return self._repair_rollback(in_flight)
            raise ConfigError(
                "ACTIVATION_NOT_RECONCILABLE: "
                f"{self._unreconciled_detail(in_flight)}. An activation may only be "
                "terminalized as activated when the live runtime is observed serving its "
                "target and health-verified, so fix the runtime and re-run this command, "
                "or run `rf upgrade reconcile --repair rollback` when the runtime failed "
                "closed with a typed non-retryable failure, or run `rf upgrade rollback` "
                f"to return to {self._store.previous_sha()} and record that outcome "
                "instead."
            )

    def _repair_rollback(self, in_flight: dict[str, object]) -> UpgradeResult:
        """Terminalize a fail-closed activation by rolling back to the previous release.

        Every step is verified before the symlink moves: the journal must describe the
        live state (`current` is the in-flight target), the runtime must provably have
        failed closed with a typed non-retryable error, `previous` must still be the
        journal's origin, and that release must be installed and pass the contract
        re-verification -- only then does the ordinary rollback path run.
        """
        to_sha = in_flight.get("to_sha")
        from_sha = in_flight.get("from_sha")
        if not isinstance(to_sha, str) or not isinstance(from_sha, str):
            raise ConfigError(
                "ACTIVATION_NOT_REPAIRABLE: the journal has no identifiable target or "
                "origin to repair"
            )
        if self._store.current_sha() != to_sha:
            raise ConfigError(
                "ACTIVATION_NOT_REPAIRABLE: `current` is "
                f"{self._store.current_sha()}, not the in-flight target {to_sha}; the "
                "journal does not describe the live state"
            )
        observed = self._observer.observe()
        if (
            observed.phase != "fail_closed"
            or observed.last_error_code not in _REPAIR_ALLOWLISTED_ERRORS
            or observed.fail_closed_since is None
        ):
            raise ConfigError(
                "ACTIVATION_NOT_REPAIRABLE: the runtime is not provably fail-closed on "
                f"a deterministic contract failure (phase={observed.phase}, "
                f"error={observed.last_error_code or 'none'}, "
                f"fail_closed_since={observed.fail_closed_since or 'none'}). Repair "
                "rollback only applies to a release whose packaged contract identity "
                "diverged from its registry; a healthy runtime should be reconciled "
                "forward, and any other failure needs its own investigation before "
                "`current` moves."
            )
        if observed.running_release_sha != to_sha:
            raise ConfigError(
                "ACTIVATION_NOT_REPAIRABLE: the live runtime is serving "
                f"{observed.running_release_sha}, not the journal target {to_sha}; the "
                "failure this journal describes does not match what is running, so "
                "repair would move the wrong pointer"
            )
        if self._store.previous_sha() != from_sha:
            raise ConfigError(
                "ACTIVATION_NOT_REPAIRABLE: `previous` is "
                f"{self._store.previous_sha()}, not the journal's origin {from_sha}"
            )
        previous_manifest = self._store.read_manifest(from_sha)
        if previous_manifest is None or not self._store.release_path(from_sha).is_dir():
            raise ConfigError(
                f"ACTIVATION_NOT_REPAIRABLE: the previous release {from_sha} is not "
                "installed and usable; rebuild it with `rf upgrade` before repairing"
            )
        self._verify_installed_contract(previous_manifest)
        rolled_back = self._rollback_locked(receipt_id=None, expected_current=to_sha)
        return replace(rolled_back, detail=f"Repair rollback: {rolled_back.detail}")

    def _require_no_unreconciled_activation(self, *, action: str) -> None:
        """Fail closed on an unterminalized activation -- unless it demonstrably won.

        Re-running blindly would overwrite `previous` and delete the only evidence of the
        last-known-good release, so "just re-run it" is not safe advice. But refusing
        outright was a dead end: the guard rejected a re-run while the only other advice
        on offer was a rollback that would demote a healthy runtime. So the journal is
        reconciled first when the runtime is provably serving its target, and only a state
        that cannot be proven is refused -- with the command that resolves it.
        """
        in_flight = self._store.read_in_flight_activation()
        if in_flight is None:
            return
        if self._reconcile_converged(in_flight) is not None:
            return
        raise ConfigError(
            "ACTIVATION_RECONCILIATION_REQUIRED: "
            f"{self._unreconciled_detail(in_flight)}. Run `{RECONCILE_COMMAND}` to "
            f"terminalize it, or `rf upgrade rollback` to return to "
            f"{self._store.previous_sha()}, before {action}."
        )

    def _unreconciled_detail(self, in_flight: dict[str, object]) -> str:
        return (
            f"an earlier activation reached stage {in_flight.get('stage')!r} targeting "
            f"{in_flight.get('to_sha')} without writing a terminal receipt, and the live "
            f"runtime is not verifiably serving it (`current` points at "
            f"{self._store.current_sha()})"
        )

    def _reconcile_converged(self, in_flight: dict[str, object]) -> UpgradeResult | None:
        """Write the terminal receipt for a journal whose target is live and healthy.

        Returns ``None`` -- changing nothing -- when that cannot be proven, so every
        caller can treat a ``None`` as "this state still needs a human decision".
        """
        to_sha = in_flight.get("to_sha")
        from_sha = in_flight.get("from_sha")
        if not isinstance(to_sha, str) or to_sha != self._store.current_sha():
            return None
        manifest = self._store.read_manifest(to_sha)
        if manifest is None:
            return None
        converged, observed_sha, verify_detail = self._verify_serving(to_sha, attempts=1)
        if not converged:
            return None
        previous_manifest = (
            self._store.read_manifest(from_sha) if isinstance(from_sha, str) else None
        )
        rediscovery_required = (
            previous_manifest is not None
            and previous_manifest.tool_surface_hash != manifest.tool_surface_hash
        )
        detail = f"Reconciled an unterminalized activation: {verify_detail}"
        receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=from_sha if isinstance(from_sha, str) else None,
            to_sha=to_sha,
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
        # Keep the id the journal already published, so the receipt an operator was told
        # to look for is the receipt that exists. An unusable or taken id falls back to a
        # freshly allocated one rather than failing the reconciliation.
        journal_receipt_id = in_flight.get("receipt_id")
        if isinstance(journal_receipt_id, str):
            with suppress(ValueError):
                receipt = replace(receipt, receipt_id=journal_receipt_id)
        receipt = self._write_receipt(receipt)
        self._store.end_activation()
        return UpgradeResult(
            status="reconciled",
            candidate_sha=to_sha,
            build_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            previous_sha=from_sha if isinstance(from_sha, str) else None,
            active_sha=to_sha,
            activation_receipt=receipt.receipt_id,
            rediscovery_required=rediscovery_required,
            observed_sha=observed_sha,
            converged=True,
            stage=ActivationStage.HEALTH_VERIFIED.value,
            detail=detail,
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
        rolled_back = self._rollback_locked(receipt_id=activated.activation_receipt)
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

        # Read-only handoff preflight BEFORE any mutation (#424): if the worker
        # registry cannot support the reclamation (truncated, unreadable, possibly
        # alive unproven worker, survived kill), the replacement cannot start -- so
        # abort with `current` unmoved and the healthy runtime untouched.
        preflight_ok, preflight_detail, _ = self._restarter.preflight_reclaim(previous_sha)
        if not preflight_ok:
            raise ConfigError(f"ACTIVATION_PREFLIGHT_FAILED: {preflight_detail}")

        # Journal the attempt BEFORE any side effect. A receipt is only written at a
        # terminal outcome, so without this a crash between the swap and the receipt
        # would leave `current` moved with no record that an activation ever started.
        journal_receipt_id = self._store.allocate_receipt_id(date_stamp=self._date_stamp())
        self._store.begin_activation(
            receipt_id=journal_receipt_id, from_sha=previous_sha, to_sha=commit_sha
        )

        # Both shims must exist BEFORE the first restart -- on a fresh release root there
        # is nothing to launch otherwise. The internal shim is what the manual restarter
        # execs; the supervisor shim is what an OS process manager execs.
        self._store.write_internal_launcher_shim()
        self._store.write_supervisor_shim()
        self._store.swap_current(commit_sha)
        stage = ActivationStage.SYMLINK_SWITCHED
        self._store.record_activation_stage(stage.value)

        restart = self._restarter.restart(departing_release=previous_sha)
        if restart.ok:
            stage = ActivationStage.RUNTIME_RESTARTED
            self._store.record_activation_stage(stage.value)
        converged, observed_sha, verify_detail = self._verify_serving(commit_sha)
        if converged:
            stage = ActivationStage.HEALTH_VERIFIED
            self._store.record_activation_stage(stage.value)

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

        detail = f"Activated and verified: {verify_detail}"
        # The receipt carries a bounded reclamation summary that references an immutable
        # evidence artifact written first (#424): at incident scale (92 workers) the full
        # evidence exceeds the receipt's 4 KiB cap, and a receipt without its artifact is
        # not truthful -- so an artifact write failure fails the activation closed.
        worker_reclamation = None
        if restart.reclamation is not None:
            try:
                artifact_id, digest = self._store.write_worker_reclamation_artifact(
                    restart.reclamation
                )
                worker_reclamation = worker_reclamation_summary(
                    restart.reclamation, artifact_id=artifact_id, digest=digest
                )
            except Exception as exc:
                return self._fail_and_rollback(
                    manifest,
                    previous_sha=previous_sha,
                    previous_manifest=previous_manifest,
                    rediscovery_required=rediscovery_required,
                    stage=stage,
                    observed_sha=observed_sha,
                    detail=(
                        "Activation failed: the worker reclamation evidence could not "
                        f"be persisted ({exc})"
                    ),
                )
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
            worker_reclamation=worker_reclamation,
        )
        # Commit the durable truth FIRST. Everything after this point is convenience:
        # the runtime is already live and verified, so an auxiliary failure must never be
        # able to erase the fact that the activation happened.
        receipt = self._write_receipt(receipt)
        self._store.end_activation()
        path_status, path_detail = self._install_path_launcher_best_effort()
        pruned, retained_by_process = self._prune_without_evicting_live_code(keep_releases)
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
            retained_for_live_process=retained_by_process,
            observed_sha=observed_sha,
            converged=True,
            stage=ActivationStage.HEALTH_VERIFIED.value,
            path_launcher_status=path_status,
            path_launcher_detail=path_detail,
            detail=detail,
        )

    def _prune_without_evicting_live_code(
        self, keep_releases: int
    ) -> tuple[list[str], tuple[str, ...]]:
        """Prune retention-eligible releases, except any a live process is executing from.

        Retention decides from `current`, `previous`, and recency, none of which says
        anything about what is *running*. Deleting a release out from under a live process
        leaves it executing code that no longer exists on disk: it cannot be restarted, its
        identity cannot be resolved back to a release, and it can still write shared durable
        state from a version the installation no longer has.

        Best-effort by design: this runs after the activation is already verified, so an
        unreadable process table degrades to "prune nothing" rather than failing a
        successful activation. Never to "prune everything".
        """
        if self._release_processes is None:
            return self._store.prune(keep=keep_releases), ()
        try:
            held = frozenset(
                process.commit_sha
                for process in self._release_processes.list_processes()
                if process.release_installed
            )
        except (OSError, ConfigError):
            # The process table could not be read, so nothing can be proven unused.
            return [], ()
        candidates = set(self._store.retention_candidates(keep=keep_releases))
        pruned = self._store.prune(keep=keep_releases, protect=held)
        return pruned, tuple(sorted(candidates & held))

    def _install_path_launcher_best_effort(self) -> tuple[str, str]:
        """Provision the PATH launcher, reporting failure instead of raising.

        A PATH shim is developer convenience. The runtime is already activated and
        verified by the time this runs, so a blocked or unwritable ``~/.local/bin/rf``
        must be surfaced as a warning -- never allowed to fail the command and leave the
        operator believing a live activation did not happen.
        """
        try:
            installed = self._store.install_path_launcher()
        except (ConfigError, OSError) as exc:
            return "failed", str(exc)
        if installed is None:
            return "skipped", "This release store does not manage a PATH launcher."
        return "installed", str(installed)

    def _verify_serving(
        self, expected_sha: str, *, attempts: int | None = None
    ) -> tuple[bool, str | None, str]:
        """Poll until the live runtime is observed serving ``expected_sha`` and healthy.

        ``attempts`` bounds the poll for callers that are inspecting an existing state
        rather than waiting for a restart: reconciliation must answer now, not spend the
        full convergence budget waiting for a runtime nobody just replaced.
        """
        observed_sha: str | None = None
        detail = "runtime never reported a release"
        for attempt in range(max(1, attempts) if attempts is not None else self._converge_attempts):
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
            # A fail-closed/failed runtime that still names the release in its record is
            # NOT serving it: convergence is observed serving AND healthy, and a typed
            # terminal failure is never reconciled forward (#367).
            if observation.phase in {"failed", "fail_closed"}:
                detail = f"runtime is {observation.phase}, not serving {expected_sha}"
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
        failed_receipt = self._write_receipt(failed_receipt)
        self._store.end_activation()
        if previous_sha is None:
            # Nothing to restore: there was no prior release. Leave `current` at the
            # candidate but report failure loudly -- never a soft success.
            raise ConfigError(
                f"ACTIVATION_FAILED: {detail}. No previous release exists to roll back to; "
                f"receipt {failed_receipt.receipt_id}."
            )
        # The unwind must not be able to fail quietly. Two things can go wrong and both
        # used to be reported as a successful rollback: `_rollback_locked` can raise before
        # it touches anything (a pruned or unreadable target), and it can complete as
        # `rollback_failed` when the restored release never converges -- in which case
        # `active_sha` is None and the old message read "Rolled back to None".
        try:
            rolled_back = self._rollback_locked(
                receipt_id=failed_receipt.receipt_id, expected_current=manifest.commit_sha
            )
        except ConfigError as exc:
            raise ConfigError(
                f"ACTIVATION_FAILED_ROLLBACK_FAILED: {detail}. The rollback to "
                f"{previous_sha} could not be attempted ({exc}), so `current` still points "
                f"at {self._store.current_sha()} (failed receipt "
                f"{failed_receipt.receipt_id}). Repair or reinstall {previous_sha} and run "
                f"`rf upgrade rollback`, or `rf version switch` to a release that is "
                "installed; `rf version status` reports what the runtime is serving now."
            ) from exc
        if rolled_back.status != "rolled_back":
            raise ConfigError(
                f"ACTIVATION_FAILED_ROLLBACK_FAILED: {detail}. The rollback to "
                f"{previous_sha} did not converge either ({rolled_back.detail}), so no "
                f"release is verified as serving (failed receipt "
                f"{failed_receipt.receipt_id}, rollback receipt "
                f"{rolled_back.activation_receipt}). Inspect `rf version status` and "
                "`rf runtime status`, then `rf runtime restart` to adopt "
                f"{previous_sha}, or `rf version switch` to a known-good release."
            )
        raise ConfigError(
            f"ACTIVATION_FAILED_ROLLED_BACK: {detail}. Rolled back to {rolled_back.active_sha} "
            f"(failed receipt {failed_receipt.receipt_id}, "
            f"rollback receipt {rolled_back.activation_receipt})."
        )

    def switch(
        self,
        selector: str,
        *,
        keep_releases: int = DEFAULT_KEEP_RELEASES,
    ) -> UpgradeResult:
        """Activate an ALREADY-INSTALLED release, named by branch or short sha.

        Switching between branches previously meant re-running the whole build pipeline
        against a worktree parked at the right commit, even though the release was already
        installed and immutable. This activates one directly, through the same
        journal -> swap -> restart -> observe -> receipt path as a fresh activation (and
        the same automatic rollback when it does not converge), because a switch is an
        activation: nothing about it may be less verified than one.
        """
        with self._activation_lock():
            self._require_no_unreconciled_activation(action="switching")
            manifest = resolve_release(self._store, selector)
            if not self._store.release_path(manifest.commit_sha).is_dir():
                raise ConfigError(
                    f"RELEASE_NOT_INSTALLED: {manifest.commit_sha} has a manifest but its "
                    "release directory is gone; rebuild it with `rf upgrade`"
                )
            if manifest.commit_sha == self._store.current_sha():
                # Not an error: the operator asked for a state that already holds. Doing a
                # real activation anyway would restart a healthy runtime for nothing.
                return UpgradeResult(
                    status="already_active",
                    candidate_sha=manifest.commit_sha,
                    build_fingerprint=manifest.build_fingerprint,
                    tool_surface_hash=manifest.tool_surface_hash,
                    active_sha=manifest.commit_sha,
                    converged=True,
                    stage=ActivationStage.HEALTH_VERIFIED.value,
                    detail=(
                        f"{manifest.label} ({manifest.commit_sha[:12]}) is already the "
                        "active release; nothing to switch."
                    ),
                )
            self._verify_installed_contract(manifest)
            return self._activate(manifest, keep_releases=keep_releases)

    def _verify_installed_contract(self, manifest: ReleaseManifest) -> None:
        """Re-run the candidate probe against an installed release before switching to it.

        A switch is an activation: nothing about it may be less verified than a fresh
        one. The release is re-smoked so a directory that drifted after install -- or
        whose packaged identity no longer matches its registry -- fails closed here
        rather than crash-looping after the swap.
        """
        smoke = self._smoke.smoke(self._store.release_path(manifest.commit_sha))
        if smoke.contract_mismatched_fields:
            raise ConfigError(
                "RELEASE_CONTRACT_IDENTITY_MISMATCH: "
                f"{manifest.commit_sha} packaged contract identity differs from its "
                f"in-process registry ({', '.join(smoke.contract_mismatched_fields)}); "
                f"offending artifacts: {', '.join(smoke.contract_artifact_paths) or 'unknown'}"
            )
        if not smoke.ok:
            raise ConfigError(
                f"SMOKE_FAILED: release {manifest.commit_sha} did not pass re-verification: "
                f"{smoke.detail}"
            )
        if (
            manifest.contract_identity
            and smoke.contract_identity
            and manifest.contract_identity != smoke.contract_identity
        ):
            raise ConfigError(
                "RELEASE_CONTRACT_IDENTITY_MISMATCH: "
                f"{manifest.commit_sha} records contract identity "
                f"{manifest.contract_identity} but the release now reports "
                f"{smoke.contract_identity}; the release directory has drifted from its "
                "reviewed manifest"
            )

    def rollback(
        self,
        receipt_id: str | None = None,
        *,
        expected_current: str | None = None,
        force: bool = False,
        force_unverified: bool = False,
    ) -> UpgradeResult:
        """Public rollback: takes the activation lock, then delegates."""
        with self._activation_lock():
            return self._rollback_locked(
                receipt_id,
                expected_current=expected_current,
                force=force,
                force_unverified=force_unverified,
            )

    def _rollback_locked(
        self,
        receipt_id: str | None = None,
        *,
        expected_current: str | None = None,
        force: bool = False,
        force_unverified: bool = False,
    ) -> UpgradeResult:
        """Roll back, targeting the release named by ``receipt_id`` when given.

        A receipted rollback is reproducible: the target is the receipt's ``from_sha``,
        and it is refused unless `current` still matches what that receipt activated
        (so repeating the command cannot toggle releases back and forth).

        Everything is validated *before* the symlink is touched, and the rollback only
        terminalizes as ``rolled_back`` when the live runtime is observed serving the
        target and health-verified -- otherwise it is a ``rollback_failed``.
        """
        current = self._store.current_sha()
        cause_receipt_id: str | None = None
        target: str

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
            if not force and current != guard:
                raise ConfigError(
                    f"ROLLBACK_STATE_MISMATCH: receipt {receipt_id} activated {guard} but "
                    f"current is {current}; re-run with --force to override"
                )
        else:
            previous = self._store.previous_sha()
            if previous is None:
                raise ConfigError("NO_PREVIOUS_RELEASE: nothing to roll back to")
            target = previous

        # Validate the target *before* mutating `current`: a receipt naming a release
        # that has been pruned or corrupted must not move the symlink first.
        manifest = self._store.read_manifest(target)
        if manifest is None:
            raise ConfigError(
                f"ROLLBACK_TARGET_UNUSABLE: {target} has no manifest; refusing to switch "
                "`current` to a release that cannot be verified"
            )
        # Re-smoke the target like a switch would: a rollback moving `current` onto a
        # release whose bits no longer match its reviewed manifest trades one broken
        # runtime for another (#420). `--force-unverified` is the explicit escape
        # hatch -- it skips only the pre-swap probe, never the post-restart health
        # verification the receipt still requires.
        if not force_unverified:
            self._verify_installed_contract(manifest)

        # Read-only handoff preflight BEFORE any mutation (#424): refuse the rollback
        # while `current` still serves rather than after it has been stopped.
        preflight_ok, preflight_detail, _ = self._restarter.preflight_reclaim(current)
        if not preflight_ok:
            raise ConfigError(f"ROLLBACK_PREFLIGHT_FAILED: {preflight_detail}")

        # Journal the rollback attempt BEFORE any pointer mutation, exactly like an
        # activation (F-006). A crash between the swap and the receipt previously
        # left `current` moved with no record that a rollback was in flight -- and
        # `reconcile()` only understands the activation journal -- so the deployment
        # became unrecoverable without guessing. The journal is written first and
        # cleared only when the terminal receipt exists, so every failure after the
        # swap leaves a trace reconciliation can act on. When a journal is already
        # in flight (repair-rollback of a fail-closed activation), that journal is
        # the trace and must not be overwritten.
        if self._store.read_in_flight_activation() is None:
            journal_receipt_id = self._store.allocate_receipt_id(date_stamp=self._date_stamp())
            self._store.begin_activation(
                receipt_id=journal_receipt_id, from_sha=current, to_sha=target
            )

        self._store.swap_current(target)
        restart = self._restarter.restart(departing_release=current)
        converged, observed_sha, verify_detail = self._verify_serving(target)
        stage = (
            ActivationStage.HEALTH_VERIFIED
            if converged
            else ActivationStage.RUNTIME_RESTARTED
            if restart.ok
            else ActivationStage.SYMLINK_SWITCHED
        )
        succeeded = restart.ok and converged
        detail = (
            f"Rolled back to {target}: {verify_detail}"
            if succeeded
            else (
                f"Rollback to {target} did NOT converge: "
                f"{restart.detail if not restart.ok else verify_detail}"
            )
        )
        # Same bounded-evidence contract as activation: full reclamation evidence goes
        # to an immutable artifact; the receipt carries only the summary + reference.
        worker_reclamation = None
        if restart.reclamation is not None:
            try:
                artifact_id, digest = self._store.write_worker_reclamation_artifact(
                    restart.reclamation
                )
                worker_reclamation = worker_reclamation_summary(
                    restart.reclamation, artifact_id=artifact_id, digest=digest
                )
            except Exception as exc:
                raise ConfigError(
                    "ROLLBACK_EVIDENCE_FAILED: the worker reclamation evidence could "
                    f"not be persisted ({exc}); no receipt was written, so the "
                    "activation journal remains for reconciliation"
                ) from exc
        receipt = ActivationReceipt(
            receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
            from_sha=current,
            to_sha=target,
            from_fingerprint=None,
            to_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            rediscovery_required=True,
            outcome=(
                ActivationOutcome.ROLLED_BACK if succeeded else ActivationOutcome.ROLLBACK_FAILED
            ),
            activated_at=self._clock.now_iso(),
            detail=detail,
            stage=stage,
            observed_sha=observed_sha,
            converged=converged,
            cause_receipt_id=cause_receipt_id,
            worker_reclamation=worker_reclamation,
        )
        receipt = self._write_receipt(receipt)
        self._store.end_activation()
        return UpgradeResult(
            status="rolled_back" if succeeded else "rollback_failed",
            candidate_sha=target,
            build_fingerprint=manifest.build_fingerprint,
            tool_surface_hash=manifest.tool_surface_hash,
            previous_sha=current,
            active_sha=target if succeeded else None,
            activation_receipt=receipt.receipt_id,
            rediscovery_required=True,
            observed_sha=observed_sha,
            converged=converged,
            stage=stage.value,
            detail=detail,
        )

    def _write_receipt(self, receipt: ActivationReceipt) -> ActivationReceipt:
        """Persist a receipt, re-allocating its id if another writer took it first."""
        from dataclasses import replace as _replace

        for _ in range(_RECEIPT_ID_ATTEMPTS):
            try:
                self._store.write_receipt(receipt)
                return receipt
            except ConfigError as exc:
                if "RECEIPT_EXISTS" not in str(exc):
                    raise
                receipt = _replace(
                    receipt,
                    receipt_id=self._store.allocate_receipt_id(date_stamp=self._date_stamp()),
                )
        raise ConfigError(
            f"RECEIPT_ID_EXHAUSTED: could not allocate a free receipt id after "
            f"{_RECEIPT_ID_ATTEMPTS} attempts"
        )

    def _date_stamp(self) -> str:
        return self._clock.now_iso()[:10].replace("-", "")
