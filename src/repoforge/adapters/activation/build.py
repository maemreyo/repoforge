"""Concrete build/install/smoke/inspect adapters for the upgrade pipeline.

These are thin shells over ``git`` and ``uv`` that run in the launcher/CLI context.
The orchestration that uses them (``UpgradeService``) is unit-tested with fakes; these
adapters carry only the subprocess wiring.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from ...application.runtime.execution_worker_reconciler import (
    ExecutionWorkerReclamationReport,
    ExecutionWorkerReconciler,
)
from ...contracts.registry import contract_identity_digest
from ...domain.errors import ConfigError
from ...domain.runtime import ControlCommand, ControlRequest, RuntimePhase, RuntimeRecord
from ...ports.activation import (
    BuildArtifact,
    HealthSample,
    ObservedRuntime,
    RestartOutcome,
    SmokeResult,
    SupervisorKickstarter,
    WorktreeState,
)
from ...ports.admission_epoch import AdmissionEpochStore
from ...ports.locking import LockManager
from ...ports.runtime_control import RuntimeControlClient, RuntimeLauncher, RuntimeStore
from ...ports.sleeper import Sleeper
from ...ports.worker_registrar import WORKER_ADMISSION_LOCK

_VENV = "venv"


@dataclass(frozen=True, slots=True)
class _ContractProbe:
    """Structured packaged-vs-computed contract identity from the candidate release."""

    packaged: dict[str, object]
    computed: dict[str, object]
    mismatched_fields: tuple[str, ...]
    artifact_paths: tuple[str, ...]
    agreement: bool


def _run(
    argv: list[str], *, cwd: Path | None = None, timeout: float = 600.0
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigError(f"COMMAND_FAILED: {' '.join(argv)}: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr


class GitWorktreeInspector:
    """Report the worktree's HEAD commit and whether it is clean."""

    def inspect(self, worktree: Path) -> WorktreeState:
        code, out, err = _run(["git", "-C", str(worktree), "rev-parse", "HEAD"], timeout=30.0)
        if code != 0:
            raise ConfigError(f"GIT_HEAD_UNKNOWN: {err.strip() or out.strip()}")
        head = out.strip().lower()
        code, out, err = _run(["git", "-C", str(worktree), "status", "--porcelain"], timeout=30.0)
        if code != 0:
            raise ConfigError(f"GIT_STATUS_FAILED: {err.strip() or out.strip()}")
        dirty = out.strip()
        return WorktreeState(
            head_sha=head,
            clean=not dirty,
            dirty_detail=_first_lines(dirty, 5),
            branch=self._branch(worktree),
            subject=self._subject(worktree),
        )

    @staticmethod
    def _branch(worktree: Path) -> str:
        """The checked-out branch, or "" on a detached HEAD.

        Best-effort by design: this is a label for humans reading a release list, so a
        detached HEAD or an unexpected git failure must degrade to "unknown" rather than
        refuse to build a release that is otherwise perfectly identified by its sha.
        """
        code, out, _ = _run(
            ["git", "-C", str(worktree), "symbolic-ref", "--quiet", "--short", "HEAD"],
            timeout=30.0,
        )
        return out.strip()[:255] if code == 0 else ""

    @staticmethod
    def _subject(worktree: Path) -> str:
        """The HEAD commit subject, so a listing shows what a release actually contains."""
        code, out, _ = _run(
            ["git", "-C", str(worktree), "log", "-1", "--no-color", "--pretty=%s"],
            timeout=30.0,
        )
        return out.strip()[:255] if code == 0 else ""


class UvWheelBuilder:
    """Build exactly one wheel from the immutable snapshot of a commit.

    The worktree only selects the commit; ``uv build`` runs against a detached
    materialization of that commit's tree, never the mutable working directory. A
    concurrent edit after the clean check can otherwise ship a wheel carrying the
    commit's sha but not the commit's bytes -- a release whose directory claims SHA A
    while the wheel holds the bytes of a modified tree (F-012).
    """

    def build(self, worktree: Path, *, commit_sha: str) -> BuildArtifact:
        source_digest = _commit_tree_sha(worktree, commit_sha)
        snapshot = _materialize_snapshot(worktree, commit_sha)
        out_dir: Path | None = None
        try:
            out_dir = Path(tempfile.mkdtemp(prefix="repoforge-upgrade-build-"))
            code, out, err = _run(
                ["uv", "build", "--wheel", "--out-dir", str(out_dir)], cwd=snapshot
            )
            if code != 0:
                raise ConfigError(
                    f"BUILD_FAILED: uv build exited {code}: {err.strip() or out.strip()}"
                )
            wheels = sorted(out_dir.glob("*.whl"))
            if len(wheels) != 1:
                raise ConfigError(f"BUILD_AMBIGUOUS: expected one wheel, found {len(wheels)}")
            wheel = wheels[0]
            # The caller installs this wheel after build() returns, so it must not
            # live in a directory this method removes.
            kept_wheel = _persist_wheel(wheel)
            return BuildArtifact(
                wheel_path=kept_wheel,
                build_fingerprint=_sha256_file(kept_wheel),
                package_version=_version_from_wheel(kept_wheel.name),
                source_digest=source_digest,
            )
        finally:
            _remove_tree(snapshot)
            if out_dir is not None:
                _remove_tree(out_dir)


class UvVenvReleaseInstaller:
    """Install a wheel into a self-contained per-release virtual environment."""

    def install(self, wheel: Path, destination: Path) -> None:
        """Build into a staging directory, then atomically rename it into place.

        Installing straight into ``releases/<sha>`` means a crashed ``uv venv``/``uv pip
        install`` leaves a partial directory with no manifest, which the next attempt can
        only report as unclaimable. Staging keeps the release directory all-or-nothing.
        """
        staging = destination.with_name(f".staging-{destination.name}-{os.getpid()}")
        _remove_tree(staging)
        try:
            self._install_into(wheel, staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
        finally:
            _remove_tree(staging)

    def _install_into(self, wheel: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        venv = destination / _VENV
        # `--relocatable` is required, not cosmetic: a venv's console scripts normally
        # hard-code the absolute interpreter path, so staging the install and renaming it
        # into releases/<sha> would leave `venv/bin/rf` exec-ing the deleted staging path.
        # A live sandbox activation failed exactly this way ("cannot execute: No such file
        # or directory") until the venv was made relocatable.
        code, out, err = _run(["uv", "venv", "--relocatable", str(venv)], timeout=120.0)
        if code != 0:
            raise ConfigError(f"VENV_FAILED: uv venv exited {code}: {err.strip() or out.strip()}")
        code, out, err = _run(
            ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)]
        )
        if code != 0:
            raise ConfigError(
                f"INSTALL_FAILED: uv pip install exited {code}: {err.strip() or out.strip()}"
            )


class SubprocessReleaseSmokeTester:
    """Smoke-test a candidate release by running the release itself.

    Checks, in order: the ``rf`` entry point runs (CLI/parser bootstrap), the runtime
    worker and supervisor modules import, config loading is importable, the MCP tool
    surface can be computed, and the packaged contract identity agrees with the
    in-process registry (#367). Everything runs under the candidate's own interpreter,
    so a release that cannot start -- or one built from a stale worktree -- is rejected
    before it can become ``current``.
    """

    def smoke(self, release_path: Path) -> SmokeResult:
        python = release_path / _VENV / "bin" / "python"
        if not python.is_file():
            return SmokeResult(
                ok=False, tool_surface_hash="", detail=f"missing interpreter {python}"
            )
        entry_point = release_path / _VENV / "bin" / "rf"
        if not entry_point.is_file():
            return SmokeResult(
                ok=False, tool_surface_hash="", detail=f"missing `rf` entry point {entry_point}"
            )
        code, out, err = _run([str(entry_point), "--version"], timeout=60.0)
        if code != 0:
            return SmokeResult(
                ok=False,
                tool_surface_hash="",
                detail=f"`rf --version` exited {code}: {err.strip() or out.strip() or 'no output'}",
            )
        code, out, err = _run(
            [
                str(python),
                "-c",
                # Importing these proves the runtime worker, supervisor construction path
                # and config loader are all loadable in the candidate.
                "import repoforge.config, repoforge.bootstrap, "
                "repoforge.interfaces.runtime.worker, "
                "repoforge.application.runtime.supervisor",
            ],
            timeout=60.0,
        )
        if code != 0:
            return SmokeResult(
                ok=False,
                tool_surface_hash="",
                detail=f"runtime imports failed: {err.strip() or 'no output'}",
            )
        code, out, err = _run(
            [
                str(python),
                "-c",
                "from repoforge.interfaces.mcp.server import tool_surface_hash; "
                "print(tool_surface_hash())",
            ],
            timeout=60.0,
        )
        surface = out.strip()
        if code != 0 or not surface:
            return SmokeResult(
                ok=False,
                tool_surface_hash="",
                detail=f"tool-surface probe failed: {err.strip() or 'no output'}",
            )
        probe = self._contract_probe(python)
        if probe is None:
            return SmokeResult(
                ok=False,
                tool_surface_hash=surface,
                detail="contract-identity probe failed: the release did not report a "
                "structured contract identity",
            )
        if not probe.agreement:
            detail_parts = [
                f"{field}: packaged {probe.packaged.get(field)!r} vs "
                f"computed {probe.computed.get(field)!r}"
                for field in probe.mismatched_fields
            ]
            return SmokeResult(
                ok=False,
                tool_surface_hash=surface,
                detail="RELEASE_CONTRACT_IDENTITY_MISMATCH: packaged contract identity "
                f"differs from the in-process registry ({'; '.join(detail_parts)}); "
                f"offending artifacts: {', '.join(probe.artifact_paths) or 'unknown'}",
                contract_mismatched_fields=probe.mismatched_fields,
                contract_artifact_paths=probe.artifact_paths,
                contract_packaged_identity=contract_identity_digest(probe.packaged),
                contract_computed_identity=contract_identity_digest(probe.computed),
            )
        return SmokeResult(
            ok=True,
            tool_surface_hash=surface,
            detail="entry point, runtime imports, tool surface, and contract identity verified",
            contract_identity=contract_identity_digest(probe.computed),
        )

    @staticmethod
    def _contract_probe(python: Path) -> _ContractProbe | None:
        """Run the structured contract probe inside the candidate, or ``None`` on failure."""

        code, out, _err = _run(
            [
                str(python),
                "-c",
                "import json; "
                "from repoforge.contracts.registry import release_contract_probe; "
                "print(json.dumps(release_contract_probe(), sort_keys=True))",
            ],
            timeout=60.0,
        )
        if code != 0:
            return None
        try:
            parsed = json.loads(out.strip())
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        packaged = parsed.get("packaged")
        computed = parsed.get("computed")
        raw_mismatched = parsed.get("mismatched_fields")
        raw_paths = parsed.get("artifact_paths")
        agreement = parsed.get("agreement")
        if (
            not isinstance(packaged, dict)
            or not isinstance(computed, dict)
            or not isinstance(raw_mismatched, list)
            or not isinstance(raw_paths, list)
            or not isinstance(agreement, bool)
        ):
            return None
        mismatched = tuple(str(field) for field in raw_mismatched)
        paths = tuple(str(path) for path in raw_paths)
        return _ContractProbe(
            packaged=packaged,
            computed=computed,
            mismatched_fields=mismatched,
            artifact_paths=paths,
            agreement=agreement,
        )


class SupervisorRestarter:
    """Replace the live supervisor so it re-execs through the ``current`` symlink.

    A new release is new code, so it cannot be adopted by an in-process reload -- and
    the supervisor's control protocol deliberately supports only PING/STATUS/HEALTH/
    SHUTDOWN. Two subtleties this must respect:

    * A graceful shutdown does **not** delete the runtime record: the supervisor writes
      a ``STOPPED`` record with ``pid=None``. Waiting for the record to disappear would
      therefore misread a clean stop as a stop failure, so we wait for the *old process*
      to be gone by identity instead.
    * When the supervisor is registered with launchd, spawning our own detached process
      would take it out of the OS manager's ownership. If a kickstarter is available we
      restart the registered job instead -- but only after draining the incumbent by
      identity, because the process holding the runtime lock is not always the one
      launchd owns, and a kickstart that races it starts a second live supervisor.
    """

    def __init__(
        self,
        *,
        control: RuntimeControlClient,
        runtime: RuntimeStore,
        launcher: RuntimeLauncher,
        config_path: Path,
        correlation_id: str,
        extra_env: dict[str, str] | None = None,
        sleeper: Sleeper | None = None,
        kickstarter: SupervisorKickstarter | None = None,
        worker_reconciler: ExecutionWorkerReconciler | None = None,
        admission_epochs: AdmissionEpochStore | None = None,
        locks: LockManager | None = None,
        admission_timeout_seconds: float = 5.0,
        stop_timeout_seconds: float = 20.0,
        start_timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self._control = control
        self._runtime = runtime
        self._launcher = launcher
        self._config_path = config_path
        self._correlation_id = correlation_id
        self._extra_env = dict(extra_env or {})
        self._sleeper = sleeper
        self._kickstarter = kickstarter
        self._worker_reconciler = worker_reconciler
        self._admission_epochs = admission_epochs
        self._locks = locks
        self._admission_timeout_seconds = max(0.0, admission_timeout_seconds)
        self._stop_timeout = stop_timeout_seconds
        self._start_timeout = start_timeout_seconds
        self._poll = max(0.01, poll_interval_seconds)

    def _reclaim_departing(
        self, departing_release: str | None
    ) -> tuple[bool, str, dict[str, object] | None]:
        """Reclaim the departing release's execution workers; block on uncertainty.

        Returns ``(ok, detail, evidence)``. ``ok=False`` means the replacement
        supervisor must not start: a worker survived SIGKILL, a worker's identity
        could not be proven while it may still be running, the registry scan was
        truncated, or a registry record could not be read -- each recreates the
        2026-08-01 deadlock if we proceed anyway.
        """
        if self._worker_reconciler is None:
            return True, "", None
        report = self._worker_reconciler.reconcile(
            departing_releases=(
                frozenset({departing_release}) if departing_release else frozenset()
            )
        )
        return self._reclaim_gate(report, departing_release, report.as_dict())

    def preflight_reclaim(
        self, departing_release: str | None
    ) -> tuple[bool, str, dict[str, object] | None]:
        """Read-only handoff preflight: can this handoff proceed, without stopping?

        Answers the same blocker questions as ``_reclaim_departing`` with no side
        effect, so a caller can decide *before* taking a healthy runtime down (#424):
        a registry that is truncated, unreadable, or hiding a possibly-alive worker
        means the replacement cannot start, and stopping the incumbent first would
        turn a refused upgrade into an outage.
        """
        if self._worker_reconciler is None:
            return True, "", None
        report = self._worker_reconciler.reconcile(
            departing_releases=(
                frozenset({departing_release}) if departing_release else frozenset()
            ),
            read_only=True,
        )
        return self._reclaim_gate(report, departing_release, report.as_dict())

    def _reclaim_gate(
        self,
        report: ExecutionWorkerReclamationReport,
        departing_release: str | None,
        evidence: dict[str, object],
    ) -> tuple[bool, str, dict[str, object] | None]:
        """Apply the fail-closed reclamation gates to one reconciliation report."""
        if report.survived_kill > 0:
            return (
                False,
                "STALE_EXECUTION_WORKER_RECLAMATION_FAILED: a worker of the departing "
                f"release {departing_release or 'unknown'} survived SIGKILL; refusing "
                "to start a replacement that would contend with its locks",
                evidence,
            )
        if report.possibly_alive_unproven > 0:
            return (
                False,
                "STALE_EXECUTION_WORKER_IDENTITY_UNPROVEN: "
                f"{report.possibly_alive_unproven} worker(s) of the departing release "
                f"{departing_release or 'unknown'} could not be proven dead and may "
                "still hold shared locks; refusing to start a replacement on unproven "
                "identity",
                evidence,
            )
        if report.process_lease_incomplete > 0:
            return (
                False,
                "PROCESS_LEASE_INCOMPLETE: "
                f"{report.process_lease_incomplete} process lease(s) are REGISTERED "
                "with no pid (pre-spawn crash window); refusing to start a replacement "
                "on incomplete lease evidence",
                evidence,
            )
        if report.process_lease_binding_divergence > 0:
            return (
                False,
                "PROCESS_LEASE_BINDING_DIVERGENCE: "
                f"{report.process_lease_binding_divergence} process lease(s) diverge "
                "from the execution-worker binding registry (missing binding, pid "
                "mismatch, or status split-brain); refusing to start a replacement",
                evidence,
            )
        if report.persistence_failures > 0:
            return (
                False,
                "PROCESS_LEASE_PERSISTENCE_FAILURE: "
                f"{report.persistence_failures} lifecycle outcome(s) "
                f"({report.persistence_failure_ids or 'unknown workers'}) could not "
                "be persisted durably; the registry may still show an active worker "
                "that was reaped -- refusing to start a replacement until the repair "
                "path reconciles the durable state",
                evidence,
            )
        if not report.evidence_complete:
            if report.unreadable_record_ids:
                return (
                    False,
                    "EXECUTION_WORKER_REGISTRY_UNREADABLE_RECORDS: "
                    f"{len(report.unreadable_record_ids)} worker registry record(s) "
                    "could not be read; an unreadable record may describe a live "
                    "orphan holding shared locks; refusing to start a replacement on "
                    "incomplete evidence",
                    evidence,
                )
            return (
                False,
                "EXECUTION_WORKER_REGISTRY_SCAN_INCOMPLETE: the worker registry scan "
                "was truncated, so an orphan may be invisible; refusing to start a "
                "replacement on incomplete evidence",
                evidence,
            )
        return True, "", evidence

    def _admission_fence(
        self,
    ) -> tuple[bool, str]:
        """Close the durable worker-admission epoch so no NEW spawn can start.

        Returns ``(ok, detail)``. This is the durable half of P1-3: once closed,
        ``WorkerRegistrar.create_intent`` refuses new intents with a typed error,
        so a spawn can never appear between this final observation and the stop
        of the incumbent. A fence that cannot be closed fails closed -- the
        incumbent is not stopped on unprovable admission state.
        """
        if self._admission_epochs is None:
            return True, ""
        try:
            self._admission_epochs.close()
        except Exception as exc:
            return (
                False,
                f"WORKER_ADMISSION_FENCE_FAILED: could not close worker admission "
                f"before stopping the incumbent: {exc}",
            )
        return True, ""

    def _reopen_admission(self) -> None:
        """Open the next admission epoch so the replacement can spawn workers.

        A reopen that cannot be durably written is a fail-closed error, never a
        silent no-op: leaving admission stuck in CLOSING would refuse every future
        worker spawn while the restarter reports success (P1-3). The caller turns
        this into a typed failure outcome instead of pretending the handoff
        completed.
        """
        if self._admission_epochs is None:
            return
        try:
            self._admission_epochs.open_next()
        except Exception as exc:
            raise ConfigError(
                f"WORKER_ADMISSION_REOPEN_FAILED: could not reopen worker admission "
                f"after the handoff: {exc}"
            ) from exc

    def restart(self, *, departing_release: str | None = None) -> RestartOutcome:
        before = self._runtime.read()
        old_pid = before.pid if before else None
        old_identity = before.process_identity if before else None

        # Phase 1 -- read-only preflight BEFORE any stop or swap: a handoff that cannot
        # proceed (truncated or unreadable registry, possibly-alive unproven worker,
        # survived kill) must not take a healthy runtime down first (#424).
        preflight_ok, preflight_detail, preflight_evidence = self.preflight_reclaim(
            departing_release
        )
        if not preflight_ok:
            return RestartOutcome(
                ok=False,
                detail=preflight_detail,
                reclamation=preflight_evidence,
            )
        plan_digest = str(preflight_evidence["registry_digest"]) if preflight_evidence else None

        # P1-3: the fence -> final observation -> stop -> reopen window is ONE atomic
        # section under the shared worker-admission lock. The registrar holds the same
        # lock across its OPEN check + intent create, so a spawn can never interleave
        # between this fence and the stop of the incumbent. The lock is released BEFORE
        # the launcher starts the replacement: the new supervisor spawns its own worker
        # at boot through the same registrar, and holding the fence across that launch
        # would block the replacement's first spawn (bounded LOCK_TIMEOUT) instead of
        # admitting it. A wedged holder surfaces a typed fail-closed outcome.
        try:
            return self._restart_fenced(
                old_pid=old_pid,
                old_identity=old_identity,
                departing_release=departing_release,
                plan_digest=plan_digest,
            )
        except ConfigError as exc:
            if "LOCK_TIMEOUT" in str(exc):
                return RestartOutcome(
                    ok=False,
                    detail=(
                        "WORKER_ADMISSION_LOCK_TIMEOUT: could not acquire the worker-"
                        f"admission lock ({exc}); a spawn may be in flight, so the "
                        "incumbent is not stopped"
                    ),
                )
            if "WORKER_ADMISSION_REOPEN_FAILED" in str(exc):
                return RestartOutcome(
                    ok=False,
                    detail=(
                        "WORKER_ADMISSION_REOPEN_FAILED: the runtime was stopped but "
                        f"worker admission could not be reopened ({exc}); the "
                        "replacement cannot spawn workers until admission is open"
                    ),
                )
            raise

    def _restart_fenced(
        self,
        *,
        old_pid: int | None,
        old_identity: str | None,
        departing_release: str | None,
        plan_digest: str | None,
    ) -> RestartOutcome:
        """The stop/start sequence, held under the shared worker-admission lock."""
        with self._admission_lock():
            fence_ok, fence_detail = self._admission_fence()
            if not fence_ok:
                return RestartOutcome(ok=False, detail=fence_detail)

            # F-004: the plan is fenced to the registry snapshot it was read from. A
            # lease that appeared or transitioned since the preflight could become a
            # blocker once the incumbent is stopped, so before ANY stop the registry is
            # re-scanned and a changed digest refuses the handoff -- the healthy runtime
            # stays up and the caller replans instead of stopping on stale evidence.
            verify_ok, verify_detail, verify_evidence = self.preflight_reclaim(departing_release)
            if not verify_ok:
                self._reopen_admission()
                return RestartOutcome(
                    ok=False,
                    detail=(
                        "handoff registry changed since the preflight plan and cannot "
                        f"proceed ({verify_detail}); replan before stopping the incumbent"
                    ),
                    reclamation=verify_evidence,
                )
            if (
                plan_digest is not None
                and verify_evidence is not None
                and str(verify_evidence.get("registry_digest")) != plan_digest
            ):
                self._reopen_admission()
                return RestartOutcome(
                    ok=False,
                    detail=(
                        "handoff registry changed since the preflight plan (registry "
                        "digest changed); refusing to stop the incumbent on stale "
                        "evidence -- replan before stopping"
                    ),
                    reclamation=verify_evidence,
                )

            # Prefer the OS process manager so an upgrade never orphans the supervisor
            # from launchd. `kickstart -k` stops and restarts the registered job in one
            # step.
            if self._kickstarter is not None and self._kickstarter.available():
                # Drain the incumbent FIRST, by identity. `kickstart -k` only replaces
                # the process launchd itself owns, so a supervisor started outside
                # launchd -- a manual `rf start`, or a leftover from an earlier release
                # -- survives it and keeps holding `runtime-single-instance`. The
                # incoming process then dies on that lock and an otherwise fine
                # activation is reported as a failure. Observed on a real activation:
                # two supervisors alive at once, one per release.
                stopped, stop_detail = self._stop(old_pid=old_pid, old_identity=old_identity)
                if not stopped:
                    self._reopen_admission()
                    return RestartOutcome(
                        ok=False,
                        detail=(
                            "could not stop the outgoing supervisor, so an OS-managed "
                            f"replacement would race it for the runtime lock: {stop_detail}"
                        ),
                    )
                reclaim_ok, reclaim_detail, reclamation = self._reclaim_departing(departing_release)
                if not reclaim_ok:
                    self._reopen_admission()
                    return RestartOutcome(
                        ok=False,
                        detail=reclaim_detail,
                        reclamation=reclamation,
                    )
                outcome = self._kickstarter.kickstart()
                if not outcome.ok:
                    self._reopen_admission()
                    return outcome
                # A STOPPED record from the old process is still on disk, so "a record
                # exists" proves nothing: require a live record that is not the old
                # process.
                if not self._await(
                    lambda: self._live_record(exclude_pid=old_pid) is not None,
                    self._start_timeout,
                ):
                    self._reopen_admission()
                    return RestartOutcome(
                        ok=False,
                        detail="OS-managed supervisor did not publish a live record after kickstart",
                    )
                record = self._live_record(exclude_pid=old_pid)
                # The replacement is live; reopen admission BEFORE releasing the lock so
                # the replacement's first worker spawn is admitted, not refused.
                self._reopen_admission()
                return RestartOutcome(
                    ok=True,
                    detail="restarted via the OS process manager",
                    pid=record.pid if record else None,
                    reclamation=reclamation,
                )

            # Phase 2 -- drain the incumbent, then reclaim on fresh evidence and start.
            stopped, stop_detail = self._stop(old_pid=old_pid, old_identity=old_identity)
            if not stopped:
                self._reopen_admission()
                return RestartOutcome(ok=False, detail=f"could not stop the runtime: {stop_detail}")
            reclaim_ok, reclaim_detail, reclamation = self._reclaim_departing(departing_release)
            if not reclaim_ok:
                self._reopen_admission()
                return RestartOutcome(
                    ok=False,
                    detail=reclaim_detail,
                    reclamation=reclamation,
                )
            try:
                pid = self._launcher.start(
                    self._config_path, foreground=False, extra_env=self._extra_env
                )
            except (ConfigError, OSError) as exc:
                self._reopen_admission()
                return RestartOutcome(ok=False, detail=f"launcher failed to start: {exc}")
            # A STOPPED record from the old process is still on disk, so "a record
            # exists" proves nothing: require a live record that is not the old process.
            if not self._await(
                lambda: self._live_record(exclude_pid=old_pid) is not None,
                self._start_timeout,
            ):
                self._reopen_admission()
                return RestartOutcome(
                    ok=False, detail="runtime did not publish a live record after restart", pid=pid
                )
            # The replacement is live; reopen admission BEFORE releasing the lock so the
            # replacement's first worker spawn is admitted, not refused.
            self._reopen_admission()
            return RestartOutcome(
                ok=True, detail=f"restarted (pid {pid})", pid=pid, reclamation=reclamation
            )

    def _admission_lock(self) -> AbstractContextManager[None]:
        """The shared worker-admission fence (P1-3), bounded.

        Both the registrar and the restarter acquire the same lock name on the
        same lock root, so the registrar's OPEN-check + intent-create and this
        fence -> final observation -> stop -> reopen window are mutually
        exclusive across processes. When no lock manager is wired, admission is
        unlocked -- the caller opted out of cross-process fencing.
        """
        if self._locks is None:
            return contextlib.nullcontext()
        return self._locks.lock(
            WORKER_ADMISSION_LOCK,
            timeout_seconds=self._admission_timeout_seconds,
            metadata={"owner": "supervisor-restarter"},
        )

    def _live_record(self, *, exclude_pid: int | None) -> RuntimeRecord | None:
        """Return the record only when it describes a live process that is not the old one."""
        record = self._runtime.read()
        if record is None or record.pid is None or record.process_identity is None:
            return None
        if record.phase in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
            return None
        if exclude_pid is not None and record.pid == exclude_pid:
            return None
        return record

    def _stop(self, *, old_pid: int | None, old_identity: str | None) -> tuple[bool, str]:
        if old_pid is None:
            return True, "no runtime was running"

        def old_process_gone() -> bool:
            current = self._runtime.read()
            if current is None:
                return True
            if current.phase in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
                return True
            # A different pid/identity means our target already exited and something
            # else owns the record.
            return current.pid != old_pid or current.process_identity != old_identity

        # A refused/unreachable shutdown is not fatal: the force-stop path below is
        # the identity-guarded fallback.
        with contextlib.suppress(ConfigError):
            self._control.request(
                ControlRequest(1, ControlCommand.SHUTDOWN, self._correlation_id),
                timeout_seconds=10.0,
            )
        if self._await(old_process_gone, self._stop_timeout):
            return True, "shutdown acknowledged"
        record = self._runtime.read()
        if record is not None and self._launcher.force_stop(record, grace_seconds=5.0):
            return True, "identity-guarded force stop"
        if old_process_gone():
            return True, "old process is gone"
        return False, "runtime still present after shutdown and force stop"

    def _await(self, predicate: Callable[[], bool], timeout_seconds: float) -> bool:
        attempts = max(1, int(timeout_seconds / self._poll))
        for _ in range(attempts):
            if predicate():
                return True
            if self._sleeper is not None:
                self._sleeper.sleep(self._poll)
        return predicate()


class RuntimeRecordReleaseObserver:
    """Derive the release the live runtime is actually serving from its own record.

    The running supervisor publishes ``executable`` (its own ``sys.executable``). When
    it was launched through the stable shim, that path lives inside
    ``<release-root>/releases/<sha>/``, so the serving release is an *observation* of
    the live process rather than a restatement of what ``current`` points at.
    """

    def __init__(self, *, runtime: RuntimeStore, releases_root: Path) -> None:
        self._runtime = runtime
        self._releases_root = releases_root

    def observe(self) -> ObservedRuntime:
        record = self._runtime.read()
        if record is None:
            return ObservedRuntime(running_release_sha=None, phase="stopped")
        # A STOPPED/FAILED record keeps `executable` but has no live process, so deriving
        # a serving release from it would claim a runtime that is not running.
        live = record.pid is not None and record.phase not in {
            RuntimePhase.STOPPED,
            RuntimePhase.FAILED,
        }
        # Source of truth is the identity the process itself published. `executable` is
        # NOT usable for this: a relocatable venv's python is a symlink to a shared
        # uv-managed interpreter, so resolving it escapes the release directory entirely.
        return ObservedRuntime(
            running_release_sha=record.running_release_sha if live else None,
            phase=record.phase.value,
            pid=record.pid,
            executable=record.executable,
            tool_surface_hash=record.tool_surface_hash or None,
            last_error_code=record.last_error_code,
            fail_closed_since=record.fail_closed_since,
        )

    def release_of_executable(self, executable: str | None) -> str | None:
        """Supporting evidence only -- never the identity source.

        Deliberately does NOT resolve symlinks: a relocatable venv's ``bin/python`` points
        at a shared uv-managed interpreter, so resolving would leave the release tree.
        Kept for diagnostics where a legacy record has no published identity.
        """
        if not executable:
            return None
        try:
            relative = Path(executable).relative_to(self._releases_root)
        except ValueError:
            return None
        return relative.parts[0] if relative.parts else None


class SupervisorHealthProbe:
    """Sample runtime health via the supervisor control socket HEALTH command."""

    def __init__(self, client: RuntimeControlClient, *, correlation_id: str) -> None:
        self._client = client
        self._correlation_id = correlation_id

    def sample(self) -> HealthSample:
        try:
            response = self._client.request(
                ControlRequest(1, ControlCommand.HEALTH, self._correlation_id),
                timeout_seconds=5.0,
            )
        except ConfigError as exc:
            return HealthSample(healthy=False, detail=f"health probe unreachable: {exc}")
        healthy = response.ok and response.status == "healthy"
        return HealthSample(healthy=healthy, detail=response.message or response.status)


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _persist_wheel(wheel: Path) -> Path:
    """Move a built wheel to a temp file that survives the build cleanup.

    ``uv build`` writes into a scratch ``out_dir`` this module then removes; the
    caller installs the wheel only after ``build`` returns, so the wheel must live
    somewhere that outlives that cleanup. The caller unlinks it after install.
    The original wheel basename is kept: ``uv pip install`` parses the filename and
    rejects a random one (no version/platform tags).
    """
    kept_dir = Path(tempfile.mkdtemp(prefix="repoforge-upgrade-wheel-"))
    kept = kept_dir / wheel.name
    shutil.move(str(wheel), str(kept))
    return kept


def _commit_tree_sha(worktree: Path, commit_sha: str) -> str:
    """The immutable content digest of ``commit_sha``'s tree.

    ``rev-parse <sha>^{tree}`` resolves the tree object the commit points at, which
    is a canonical digest of exactly the bytes the commit contains -- independent of
    any later edit to the working directory. Tree object ids are SHA-1 (40 hex) on
    standard repositories and SHA-256 (64 hex) on sha256 repositories; either is a
    valid immutable digest.
    """
    code, out, err = _run(
        ["git", "-C", str(worktree), "rev-parse", "--verify", f"{commit_sha}^{{tree}}"],
        timeout=30.0,
    )
    if code != 0:
        raise ConfigError(
            f"SOURCE_DIGEST_UNKNOWN: cannot resolve the tree of {commit_sha}: "
            f"{err.strip() or out.strip()}"
        )
    digest = out.strip().lower()
    if len(digest) not in (40, 64) or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ConfigError(f"SOURCE_DIGEST_INVALID: {commit_sha} tree is not a git object id")
    return digest


def _materialize_snapshot(worktree: Path, commit_sha: str) -> Path:
    """Materialize exactly ``commit_sha``'s tree into a fresh directory.

    ``git archive`` exports the committed tree, never the mutable working directory,
    and leaves no worktree metadata behind in the source repository. The caller owns
    the returned directory and must remove it.
    """
    snapshot = Path(tempfile.mkdtemp(prefix="repoforge-upgrade-snapshot-"))
    archive = snapshot.with_name(f"{snapshot.name}.tar")
    try:
        code, out, err = _run(
            ["git", "-C", str(worktree), "archive", "--format=tar", "-o", str(archive), commit_sha],
            timeout=120.0,
        )
        if code != 0:
            raise ConfigError(
                f"SNAPSHOT_FAILED: cannot archive {commit_sha}: {err.strip() or out.strip()}"
            )
        code, out, err = _run(["tar", "-xf", str(archive), "-C", str(snapshot)], timeout=120.0)
        if code != 0:
            raise ConfigError(
                f"SNAPSHOT_EXTRACT_FAILED: cannot extract {commit_sha} tree: "
                f"{err.strip() or out.strip()}"
            )
        return snapshot
    except BaseException:
        _remove_tree(snapshot)
        raise
    finally:
        with contextlib.suppress(OSError):
            archive.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_from_wheel(name: str) -> str:
    parts = name.split("-")
    if len(parts) < 2 or not parts[1]:
        raise ConfigError(f"BUILD_VERSION_UNKNOWN: cannot parse version from {name}")
    return parts[1]


def _first_lines(text: str, limit: int) -> str:
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[:limit]) + f"\n... (+{len(lines) - limit} more)"
