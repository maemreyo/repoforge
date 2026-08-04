"""Round-2 review regressions: real stop/start semantics and new-binary adoption.

These exercise the production `SupervisorRestarter`, `ReleaseAwareRuntimeLauncher`,
`RuntimeRecordReleaseObserver` and a real `JsonRuntimeStore` -- the boundary the
earlier fake-only tests could not see past.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from repoforge.adapters.activation.build import RuntimeRecordReleaseObserver, SupervisorRestarter
from repoforge.adapters.activation.launcher import ReleaseAwareRuntimeLauncher
from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
from repoforge.adapters.persistence.json_process_lease_adapter import JsonProcessLeaseAdapter
from repoforge.adapters.runtime import JsonRuntimeStore
from repoforge.adapters.runtime.state_store import process_identity
from repoforge.application.runtime.execution_worker_reconciler import (
    ExecutionWorkerReclamationReport,
)
from repoforge.application.runtime.worker_registrar import WorkerRegistrar
from repoforge.domain.errors import ConfigError
from repoforge.domain.process_lease import ProcessLeaseRole
from repoforge.domain.runtime import (
    ControlResponse,
    RuntimePhase,
    RuntimeRecord,
)
from repoforge.ports.activation import RestartOutcome
from repoforge.ports.admission_epoch import ADMISSION_OPEN, ADMISSION_PERMIT_ENV
from repoforge.testing import FixedClock, InMemoryLockManager, SequenceIdGenerator

_IDENTITY_OLD = "a" * 64
_IDENTITY_NEW = "b" * 64
_SURFACE = "c" * 64
_TARGET_SHA = "d" * 64
_PREVIOUS_SHA = "e" * 64
_RUNNING_SHA_ENV = "REPOFORGE_RUNNING_RELEASE_SHA"


def _record(
    *,
    phase: RuntimePhase,
    pid: int | None,
    identity: str | None,
    executable: str | None = None,
) -> RuntimeRecord:
    return RuntimeRecord(
        protocol_version=1,
        phase=phase,
        pid=pid,
        process_identity=identity,
        active_generation=1 if pid else None,
        accepted_generation=1,
        tunnel_profile="p",
        tunnel_profile_fingerprint=_SURFACE,
        tool_surface_hash=_SURFACE,
        started_at="2026-07-25T00:00:00+00:00",
        updated_at="2026-07-25T00:00:00+00:00",
        correlation_id="c" * 24,
        child_pid=999 if pid else None,
        child_process_identity=_IDENTITY_NEW if pid else None,
        executable=executable,
    )


class _Store:
    """Faithful in-memory RuntimeStore.

    `JsonRuntimeStore.read()` validates the recorded pid against a *live* process, so it
    cannot hold synthetic records. This keeps the production semantics that matter here
    -- a graceful shutdown REPLACES the record with STOPPED rather than deleting it --
    while the code under test stays the real `SupervisorRestarter`.
    (`test_a_graceful_stop_really_leaves_the_record_on_disk` pins that premise against
    the real JSON store.)
    """

    def __init__(self) -> None:
        self._record: RuntimeRecord | None = None

    def read(self) -> RuntimeRecord | None:
        return self._record

    def write(self, record: RuntimeRecord) -> None:
        self._record = record

    def clear(self, *, expected_pid: int | None = None) -> None:
        self._record = None


class _Sleeper:
    def sleep(self, seconds: float) -> None:  # no real waiting in tests
        return None


class _Control:
    """Control fake that flips the store to a graceful STOPPED record, as production does."""

    def __init__(self, store, *, stopped_executable: str | None = None) -> None:
        self._store = store
        self._stopped_executable = stopped_executable

    def request(self, request, *, timeout_seconds: float = 10.0) -> ControlResponse:
        # Exactly what the supervisor does on SHUTDOWN: write STOPPED, do NOT delete.
        self._store.write(
            _record(
                phase=RuntimePhase.STOPPED,
                pid=None,
                identity=None,
                executable=self._stopped_executable,
            )
        )
        return ControlResponse(1, True, request.correlation_id, "stopping")


class _RecordingLauncher:
    """Captures argv-equivalents and publishes a live record for the new process."""

    def __init__(self, store, *, new_pid: int = 4242) -> None:
        self._store = store
        self._new_pid = new_pid
        self.starts: list[Path] = []

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        self.starts.append(config_path)
        self._store.write(
            _record(phase=RuntimePhase.HEALTHY, pid=self._new_pid, identity=_IDENTITY_NEW)
        )
        return self._new_pid

    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
        return False


def _restarter(store, control, launcher, tmp_path: Path, **kwargs):
    return SupervisorRestarter(
        control=control,
        runtime=store,
        launcher=launcher,
        config_path=tmp_path / "config.toml",
        correlation_id="c" * 24,
        sleeper=_Sleeper(),
        poll_interval_seconds=0.01,
        stop_timeout_seconds=0.2,
        start_timeout_seconds=0.2,
        **kwargs,
    )


def test_graceful_shutdown_is_recognised_even_though_the_record_survives(
    tmp_path: Path,
) -> None:
    """Review finding 2: the supervisor writes STOPPED instead of deleting the record."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)

    outcome = _restarter(store, _Control(store), launcher, tmp_path).restart()

    assert outcome.ok is True, outcome.detail
    assert launcher.starts, "the restarter must start a replacement process"


class _DigestReconciler:
    """Scripted reconciler: one digest for the first scan, another for later scans."""

    def __init__(self, *, second_digest: str | None = None) -> None:
        self.calls = 0
        self._second_digest = second_digest

    def reconcile(self, *, departing_releases=frozenset(), read_only: bool = False):
        del departing_releases, read_only
        self.calls += 1
        digest = "a" * 64
        if self.calls > 1 and self._second_digest is not None:
            digest = self._second_digest
        return ExecutionWorkerReclamationReport(
            inspected=0,
            reclaimed=0,
            already_gone=0,
            refused_unproven=0,
            survived_kill=0,
            possibly_alive_unproven=0,
            scan_complete=True,
            unreadable_record_ids=(),
            worker_ids=(),
            pids=(),
            release_shas=(),
            registry_digest=digest,
            detail="registry healthy",
        )


def test_restart_proceeds_when_the_registry_fence_is_stable(tmp_path: Path) -> None:
    """A stable registry between the preflight plan and the stop is a green light."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    reconciler = _DigestReconciler(second_digest="a" * 64)  # unchanged on re-scan

    outcome = _restarter(
        store, _Control(store), launcher, tmp_path, worker_reconciler=reconciler
    ).restart()

    assert outcome.ok is True, outcome.detail
    assert launcher.starts, "the restarter must start a replacement process"


def test_restart_refuses_to_stop_when_the_registry_changed_since_the_plan(
    tmp_path: Path,
) -> None:
    """A lease that appeared after the preflight must not be stopped past (F-004).

    The plan is fenced to the registry snapshot it was read from. If the registry
    changed between the preflight and the stop -- a new worker or a state
    transition that could become a blocker -- the incumbent is NOT stopped: the
    healthy runtime stays up and the caller replans instead of stopping on stale
    evidence.
    """
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    reconciler = _DigestReconciler(second_digest="b" * 64)  # registry changed

    outcome = _restarter(
        store, _Control(store), launcher, tmp_path, worker_reconciler=reconciler
    ).restart()

    assert outcome.ok is False
    assert "digest changed" in outcome.detail
    assert launcher.starts == []
    # The healthy runtime record was not touched: no stop was attempted.
    assert store.read().phase is RuntimePhase.HEALTHY


def test_restart_requires_a_live_record_for_a_new_process(tmp_path: Path) -> None:
    """A leftover STOPPED record must not be mistaken for a started runtime."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _LauncherThatNeverPublishes(_RecordingLauncher):
        def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
            self.starts.append(config_path)
            return 4242  # leaves the STOPPED record in place

    outcome = _restarter(
        store, _Control(store), _LauncherThatNeverPublishes(store), tmp_path
    ).restart()

    assert outcome.ok is False
    assert "live record" in outcome.detail


def test_restart_rejects_a_record_that_is_still_the_old_process(tmp_path: Path) -> None:
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _LauncherReusingOldPid(_RecordingLauncher):
        def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
            self.starts.append(config_path)
            # Republish the OLD pid: this is not a new process.
            self._store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
            return 1000

    outcome = _restarter(store, _Control(store), _LauncherReusingOldPid(store), tmp_path).restart()
    assert outcome.ok is False


def test_bootstrap_replaces_the_loaded_job_so_launchd_keeps_owning_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 4: never hand-spawn when the OS manager owns the job.

    The replacement boot now crosses the REAL `WorkerRegistrar.create_intent`
    boundary on the `bootstrap_replacement` launch -- matching real launchd, where
    ``RunAtLoad`` makes the bootstrap the replacement's SINGLE launch. The test
    asserts the decisive handoff chain: admission lock released before the launch,
    the permit read from the staged job env, exactly one supervisor launch, and
    `kickstart` never called (a second launch would race the single-use permit).
    """
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    locks = InMemoryLockManager()
    registrar, _leases, epochs = _admission_wired_registrar(tmp_path, locks=locks)
    kickstarter = _FakeOsLaunchd(
        store, registrar=registrar, target_sha=_TARGET_SHA, monkeypatch=monkeypatch
    )

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=kickstarter,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_TARGET_SHA)

    assert outcome.ok is True, outcome.detail
    # The manual launcher must NOT have been used.
    assert launcher.starts == []
    # The replacement crossed the registrar while admission was CLOSING, exactly once.
    assert kickstarter.bootstrap_calls == 1
    assert kickstarter.boot_refused is False
    assert kickstarter.second_spawn_refused is True, "the permit is single-use"
    # The launch is the bootstrap alone: no second launch, no kickstart.
    assert kickstarter.kickstart_calls == 0
    # The permit was staged into the job definition and scrubbed after reopen.
    assert kickstarter.staged_env is not None
    assert kickstarter.staged_env[ADMISSION_PERMIT_ENV]
    assert kickstarter.scrub_calls == [(ADMISSION_PERMIT_ENV,)]
    assert epochs.read()[1] == ADMISSION_OPEN


def _admission_wired_registrar(
    tmp_path: Path, *, locks, epochs=None, admission_timeout_seconds: float = 5.0
):
    """A real registrar sharing the given admission lock manager and epoch store."""
    lease_store = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    epoch_store = epochs if epochs is not None else JsonAdmissionEpochStore(tmp_path / "state")
    registrar = WorkerRegistrar(
        leases=lease_store,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        epochs=epoch_store,
        locks=locks,
        admission_timeout_seconds=admission_timeout_seconds,
    )
    return registrar, lease_store, epoch_store


class _FakeOsLaunchd:
    """A stateful launchd stand-in that boots the replacement through the REAL registrar.

    Models the two states the OS-managed contract distinguishes: ``registered`` (the
    job definition is registered on disk; never changes here) and ``loaded`` (launchd
    currently has the job loaded). ``bootstrap_replacement`` mirrors the production
    state machine: a LOADED job is booted out first (``bootout_calls``), an
    already-UNLOADED job (a rollback after a failed candidate bootstrap) skips the
    bootout, and a successful bootstrap reloads the job. ``available()`` reproduces
    the PREVIOUS contract (loaded-only) so the RED run -- the rollback falling back
    to the manual launcher because the job is UNLOADED -- fails exactly as the review
    predicted; the new selector keys on ``registered()`` instead.
    """

    def __init__(
        self,
        store,
        *,
        registrar,
        target_sha,
        monkeypatch: pytest.MonkeyPatch,
        fail_bootstrap: bool = False,
        drop_permit: bool = False,
        fail_stage: bool = False,
    ) -> None:
        self._store = store
        self._registrar = registrar
        self.target_sha = target_sha
        self._mp = monkeypatch
        self.fail_bootstrap = fail_bootstrap
        self.drop_permit = drop_permit
        self.fail_stage = fail_stage
        self._registered = True
        self._loaded = True
        self.staged_env: dict[str, str] | None = None
        self.bootstrap_calls = 0
        self.bootout_calls = 0
        self.kickstart_calls = 0
        self.scrub_calls: list[tuple[str, ...]] = []
        self.boot_refused = False
        self.second_spawn_refused: bool | None = None
        self.new_pid = 7777

    def registered(self) -> bool:
        return self._registered

    def loaded(self) -> bool:
        return self._loaded

    # Reproduces the pre-fix selector contract (loaded-only) for the RED run.
    def available(self) -> bool:
        return self._loaded

    def stage_replacement_env(self, env: dict[str, str]) -> tuple[bool, str]:
        self.staged_env = dict(env)
        if self.fail_stage:
            return False, "LAUNCHD_STAGE_FAILED: staged permit rejected"
        return True, "staged"

    # Round-1 restarter compatibility for the RED run: that flow staged via
    # prepare_replacement and launched via kickstart, which never boots here.
    def prepare_replacement(self, env: dict[str, str]) -> tuple[bool, str]:
        return self.stage_replacement_env(env)

    def scrub_replacement_env(self, keys: tuple[str, ...]) -> tuple[bool, str]:
        self.scrub_calls.append(tuple(keys))
        return True, "scrubbed"

    def bootstrap_replacement(self) -> RestartOutcome:
        self.bootstrap_calls += 1
        if self._loaded:
            self.bootout_calls += 1
            self._loaded = False
        if self.fail_bootstrap:
            return RestartOutcome(ok=False, detail="launchctl bootstrap failed in test")
        env = {} if self.drop_permit else dict(self.staged_env or {})
        self._boot(env)
        self._loaded = True
        return RestartOutcome(ok=True, detail="bootstrapped (RunAtLoad launches)")

    def kickstart(self) -> RestartOutcome:
        self.kickstart_calls += 1
        return RestartOutcome(ok=True, detail="kickstarted")

    def _boot(self, env: dict[str, str]) -> None:
        boot_env = dict(env)
        boot_env.setdefault(_RUNNING_SHA_ENV, self.target_sha)
        with self._mp.context() as ctx:
            for key, value in boot_env.items():
                ctx.setenv(key, value)
            try:
                self._registrar.create_intent(
                    role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
                )
            except ConfigError:
                self.boot_refused = True
                return
            try:
                self._registrar.create_intent(
                    role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24
                )
                self.second_spawn_refused = False
            except ConfigError:
                self.second_spawn_refused = True
        self._store.write(
            _record(phase=RuntimePhase.HEALTHY, pid=self.new_pid, identity=_IDENTITY_NEW)
        )


def test_os_managed_boot_under_a_held_fence_lock_fails_with_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 P0 guard: the launch must happen OUTSIDE the admission lock.

    If a restarter ever bootstrapped (or kicked) the replacement while still holding
    the fence lock, the replacement's first `create_intent` would hit LOCK_TIMEOUT
    and fail closed. This encodes why the OS-managed launch belongs in Phase B.
    """
    from repoforge.ports.worker_registrar import WORKER_ADMISSION_LOCK

    locks = InMemoryLockManager()
    registrar, _leases, epochs = _admission_wired_registrar(
        tmp_path, locks=locks, admission_timeout_seconds=0.2
    )
    epochs.close()
    token = epochs.issue_permit(target=_TARGET_SHA)
    monkeypatch.setenv(ADMISSION_PERMIT_ENV, token)
    monkeypatch.setenv(_RUNNING_SHA_ENV, _TARGET_SHA)

    with (
        locks.lock(WORKER_ADMISSION_LOCK, timeout_seconds=5.0),
        pytest.raises(ConfigError, match="LOCK_TIMEOUT"),
    ):
        registrar.create_intent(role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24)


def test_os_managed_replacement_without_a_transported_permit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot that cannot carry the staged permit stays refused while CLOSING.

    Even though the restarter staged the permit, a launchd that fails to deliver it
    (the drop_permit case) leaves the replacement unable to pass the fence: the
    worker spawn is refused, no live record is ever published, and the activation
    fails closed instead of reporting a false success.
    """
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    locks = InMemoryLockManager()
    registrar, _leases, epochs = _admission_wired_registrar(tmp_path, locks=locks)
    kickstarter = _FakeOsLaunchd(
        store,
        registrar=registrar,
        target_sha=_TARGET_SHA,
        monkeypatch=monkeypatch,
        drop_permit=True,
    )

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=kickstarter,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_TARGET_SHA)

    assert outcome.ok is False
    assert "live record" in outcome.detail
    assert kickstarter.boot_refused is True
    # The failed handoff reopened admission: nothing stays fenced forever.
    assert epochs.read()[1] == ADMISSION_OPEN


def test_os_managed_bootstrap_failure_surfaces_and_reopens_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed bootstrap is surfaced, admission reopens, and the permit is scrubbed."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    locks = InMemoryLockManager()
    registrar, _leases, epochs = _admission_wired_registrar(tmp_path, locks=locks)
    kickstarter = _FakeOsLaunchd(
        store,
        registrar=registrar,
        target_sha=_TARGET_SHA,
        monkeypatch=monkeypatch,
        fail_bootstrap=True,
    )

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=kickstarter,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_TARGET_SHA)

    assert outcome.ok is False
    assert "bootstrap failed in test" in outcome.detail
    assert kickstarter.bootstrap_calls == 1
    assert kickstarter.kickstart_calls == 0, "no second launch after a failed bootstrap"
    assert kickstarter.scrub_calls == [(ADMISSION_PERMIT_ENV,)]
    assert epochs.read()[1] == ADMISSION_OPEN


def test_os_managed_stage_failure_refuses_to_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that cannot carry the permit is never launched without it."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    locks = InMemoryLockManager()
    registrar, _leases, epochs = _admission_wired_registrar(tmp_path, locks=locks)
    kickstarter = _FakeOsLaunchd(
        store,
        registrar=registrar,
        target_sha=_TARGET_SHA,
        monkeypatch=monkeypatch,
        fail_stage=True,
    )

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=kickstarter,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_TARGET_SHA)

    assert outcome.ok is False
    assert "refusing to launch without it" in outcome.detail
    assert kickstarter.bootstrap_calls == 0, "never launch without the staged permit"
    assert kickstarter.kickstart_calls == 0
    assert epochs.read()[1] == ADMISSION_OPEN


def test_os_managed_success_scrubs_the_permit_and_launches_the_job_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a successful OS-managed handoff the permit is scrubbed, no second launch."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    locks = InMemoryLockManager()
    registrar, _leases, epochs = _admission_wired_registrar(tmp_path, locks=locks)
    kickstarter = _FakeOsLaunchd(
        store, registrar=registrar, target_sha=_TARGET_SHA, monkeypatch=monkeypatch
    )

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=kickstarter,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_TARGET_SHA)

    assert outcome.ok is True, outcome.detail
    assert kickstarter.bootstrap_calls == 1, "the job is launched exactly once"
    assert kickstarter.kickstart_calls == 0, "the bootstrap IS the launch"
    assert kickstarter.staged_env is not None
    assert kickstarter.staged_env[ADMISSION_PERMIT_ENV]  # the token was staged
    assert kickstarter.scrub_calls == [(ADMISSION_PERMIT_ENV,)]
    assert epochs.read()[1] == ADMISSION_OPEN


class _RecordingPermitEpochs(JsonAdmissionEpochStore):
    """Admission epoch store that records the permit target issued per handoff."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root)
        self.issued_targets: list[str | None] = []

    def issue_permit(self, *, target: str | None) -> str:
        self.issued_targets.append(target)
        return super().issue_permit(target=target)


def test_os_managed_rollback_keeps_the_still_registered_job_os_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0: after a candidate bootstrap failure the job is UNLOADED but still REGISTERED.

    The rollback must stay OS-managed: the restarter selects the OS path because the
    definition is still registered, issues a fresh permit bound to the old release,
    and the bootstrap skips the bootout of the already-unloaded job -- the manual
    launcher is never used, so launchd keeps owning the supervisor across the failure
    and the recovery.
    """
    from repoforge.domain.errors import ConfigError

    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    locks = InMemoryLockManager()
    epochs = _RecordingPermitEpochs(tmp_path / "state")
    registrar, _leases, _ = _admission_wired_registrar(tmp_path, locks=locks, epochs=epochs)

    # Handoff 1 -- candidate bootstrap fails: the job goes UNLOADED but stays registered.
    fake = _FakeOsLaunchd(
        store,
        registrar=registrar,
        target_sha=_TARGET_SHA,
        monkeypatch=monkeypatch,
        fail_bootstrap=True,
    )
    outcome1 = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=fake,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_TARGET_SHA)
    assert outcome1.ok is False
    candidate_token = fake.staged_env[ADMISSION_PERMIT_ENV]
    assert fake.registered() is True and fake.loaded() is False, (
        "a failed bootstrap leaves the job UNLOADED but the definition registered"
    )
    assert fake.bootout_calls == 1

    # Handoff 2 -- rollback to the PREVIOUS release: still OS-managed (registered),
    # the bootout is skipped (already unloaded), and a FRESH permit bound to the old
    # release is staged and bootstrapped. The manual launcher must never be used.
    fake.fail_bootstrap = False
    fake.target_sha = _PREVIOUS_SHA  # the recovered supervisor serves the old release
    outcome2 = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        kickstarter=fake,
        admission_epochs=epochs,
        locks=locks,
    ).restart(target_release=_PREVIOUS_SHA)
    assert outcome2.ok is True, outcome2.detail
    rollback_token = fake.staged_env[ADMISSION_PERMIT_ENV]
    assert rollback_token != candidate_token, "the rollback must get a new permit"
    assert epochs.issued_targets == [_TARGET_SHA, _PREVIOUS_SHA], (
        "the rollback permit is bound to the old release, not the failed candidate"
    )
    assert fake.bootstrap_calls == 2
    assert fake.bootout_calls == 1, "the rollback skips the bootout of the unloaded job"
    assert fake.registered() is True and fake.loaded() is True
    assert launcher.starts == [], "the rollback must never fall back to the manual launcher"

    # The candidate token cannot pass a CLOSING fence: rotate, then present it.
    epochs.close()
    epochs.issue_permit(target=_TARGET_SHA)
    monkeypatch.setenv(ADMISSION_PERMIT_ENV, candidate_token)
    monkeypatch.setenv(_RUNNING_SHA_ENV, _TARGET_SHA)
    with pytest.raises(ConfigError, match="WORKER_ADMISSION_REFUSED"):
        registrar.create_intent(role=ProcessLeaseRole.EXECUTION_DAEMON, correlation_id="c" * 24)


# ------------------------------------------------- new-binary adoption (finding 1)


def test_release_aware_launcher_runs_the_stable_shim_not_the_calling_interpreter(
    tmp_path: Path,
) -> None:
    """Review finding 1: `sys.executable` is the OLD release; the shim resolves `current`."""
    shim = tmp_path / "bin" / "rf"
    shim.parent.mkdir(parents=True)
    # A shim that records its own argv so we can prove what was executed.
    marker = tmp_path / "argv.txt"
    shim.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$0" "$@" > ' + str(marker) + "\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    launcher = ReleaseAwareRuntimeLauncher(shim)
    code = launcher.start(tmp_path / "config.toml", foreground=True, extra_env={})

    assert code == 0
    recorded = marker.read_text(encoding="utf-8").splitlines()
    # Executed the stable shim (which resolves through `current`), not sys.executable.
    assert recorded[0] == str(shim)
    assert "start" in recorded


def test_release_aware_launcher_refuses_when_the_shim_is_missing(tmp_path: Path) -> None:
    launcher = ReleaseAwareRuntimeLauncher(tmp_path / "bin" / "rf")
    try:
        launcher.start(tmp_path / "config.toml", foreground=False, extra_env={})
    except ConfigError as exc:
        assert "LAUNCHER_SHIM_MISSING" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected ConfigError")


# ------------------------------------------------------- observer truthfulness (F6)


def test_a_graceful_stop_really_leaves_the_record_on_disk(tmp_path: Path) -> None:
    """Pins review finding 2's premise against the REAL JsonRuntimeStore."""
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(_record(phase=RuntimePhase.STOPPED, pid=None, identity=None))
    # The record survives a graceful stop, so "read() is None" is NOT a stop signal.
    persisted = store.read()
    assert persisted is not None
    assert persisted.phase is RuntimePhase.STOPPED


def test_observer_reports_no_release_for_a_stopped_runtime(tmp_path: Path) -> None:
    """A STOPPED record keeps `executable`; it must NOT be read as a serving release."""
    releases = tmp_path / "releases"
    executable = releases / "aaa1111" / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(
        _record(phase=RuntimePhase.STOPPED, pid=None, identity=None, executable=str(executable))
    )

    observed = RuntimeRecordReleaseObserver(runtime=store, releases_root=releases).observe()

    assert observed.phase == "stopped"
    assert observed.running_release_sha is None


def test_observer_trusts_the_published_release_identity(tmp_path: Path) -> None:
    """Round-5 finding 1: identity is what the process published, not a derived path.

    Uses this test process's own pid/identity so the real store validates liveness.
    """
    releases = tmp_path / "releases"
    executable = releases / "aaa1111" / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    live_pid = os.getpid()
    live_identity = process_identity(live_pid)
    assert live_identity is not None
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(
        dataclasses.replace(
            _record(
                phase=RuntimePhase.HEALTHY,
                pid=live_pid,
                identity=live_identity,
                executable=str(executable),
            ),
            child_pid=live_pid,
            child_process_identity=live_identity,
            running_release_sha="aaa1111",
        )
    )

    observed = RuntimeRecordReleaseObserver(runtime=store, releases_root=releases).observe()

    assert observed.running_release_sha == "aaa1111"
    assert observed.pid == live_pid


def test_a_relocatable_venv_symlink_is_never_used_to_infer_identity(tmp_path: Path) -> None:
    """`uv venv --relocatable` makes bin/python a symlink OUT of the release tree.

    Resolving it lands in the shared uv-managed interpreter, so a path-derived identity
    silently yields nothing. A record with no published sha must report None -- never a
    guess -- and the path helper must not resolve its way out of the release directory.
    """
    releases = tmp_path / "releases"
    shared = tmp_path / "uv-python" / "bin" / "python3.13"
    shared.parent.mkdir(parents=True)
    shared.touch()
    executable = releases / "aaa1111" / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(shared)
    assert executable.resolve() == shared.resolve(), "precondition: resolves out of release"

    live_pid = os.getpid()
    live_identity = process_identity(live_pid)
    assert live_identity is not None
    store = JsonRuntimeStore(tmp_path / "managed-runtime-v3.json")
    store.write(
        dataclasses.replace(
            _record(
                phase=RuntimePhase.HEALTHY,
                pid=live_pid,
                identity=live_identity,
                executable=str(executable),
            ),
            child_pid=live_pid,
            child_process_identity=live_identity,
            running_release_sha=None,  # a legacy record: identity was never published
        )
    )
    observer = RuntimeRecordReleaseObserver(runtime=store, releases_root=releases)

    # No published identity -> no claim. The old model would have returned None here too,
    # but silently, while claiming the derived path was authoritative.
    assert observer.observe().running_release_sha is None
    # The supporting-evidence helper does NOT resolve, so it still recognises the release.
    assert observer.release_of_executable(str(executable)) == "aaa1111"


# ------------------------------- handoff: never two live supervisors (#304)


def test_the_incumbent_is_drained_before_the_os_manager_is_bootstrapped(
    tmp_path: Path,
) -> None:
    """Bootstrapping the staged job only replaces the process launchd owns.

    A supervisor started outside launchd -- a manual `rf start`, or a leftover from an
    earlier release -- survives it and keeps holding `runtime-single-instance`, so the
    incoming process dies on that lock and a healthy activation is reported as a failure.
    Draining by identity first is what makes the handoff single-instance.
    """
    events: list[str] = []
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _RecordingControl(_Control):
        def request(self, request, *, timeout_seconds: float = 10.0):
            events.append("shutdown")
            return super().request(request, timeout_seconds=timeout_seconds)

    class _Kickstarter:
        def registered(self) -> bool:
            return True

        def loaded(self) -> bool:
            return True

        def available(self) -> bool:  # legacy selector contract, RED-run only
            return True

        def stage_replacement_env(self, env: dict[str, str]) -> tuple[bool, str]:
            return True, "staged"

        def bootstrap_replacement(self) -> RestartOutcome:
            events.append("bootstrap")
            store.write(_record(phase=RuntimePhase.HEALTHY, pid=7777, identity=_IDENTITY_NEW))
            return RestartOutcome(ok=True, detail="bootstrapped")

    outcome = _restarter(
        store,
        _RecordingControl(store),
        _RecordingLauncher(store),
        tmp_path,
        kickstarter=_Kickstarter(),
    ).restart()

    assert outcome.ok is True, outcome.detail
    assert events == ["shutdown", "bootstrap"], "the incumbent must be gone before the launch"


def test_an_undrainable_incumbent_is_never_bootstrapped_into_a_race(tmp_path: Path) -> None:
    """If the outgoing supervisor cannot be stopped, starting a second one is worse than
    reporting the failure: that is the state the single-instance lock exists to prevent."""
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))

    class _DeafControl:
        """SHUTDOWN is acknowledged but the process never leaves."""

        def request(self, request, *, timeout_seconds: float = 10.0):
            return ControlResponse(1, True, request.correlation_id, "ignored")

    class _Kickstarter:
        def __init__(self) -> None:
            self.calls = 0

        def registered(self) -> bool:
            return True

        def loaded(self) -> bool:
            return True

        def available(self) -> bool:  # legacy selector contract, RED-run only
            return True

        def stage_replacement_env(self, env: dict[str, str]) -> tuple[bool, str]:
            return True, "staged"

        def bootstrap_replacement(self) -> RestartOutcome:
            self.calls += 1
            return RestartOutcome(ok=True, detail="bootstrapped")

    kickstarter = _Kickstarter()
    outcome = _restarter(
        store,
        _DeafControl(),
        _RecordingLauncher(store),  # force_stop() returns False: the process stays
        tmp_path,
        kickstarter=kickstarter,
    ).restart()

    assert outcome.ok is False
    assert "race" in outcome.detail
    assert kickstarter.calls == 0


def test_restart_fails_closed_when_the_admission_lock_is_wedged(tmp_path: Path) -> None:
    """P1-3: a wedged admission holder must not let the incumbent be stopped."""
    from repoforge.ports.worker_registrar import WORKER_ADMISSION_LOCK
    from repoforge.testing import InMemoryLockManager

    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    shared_locks = InMemoryLockManager()
    restarter = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        locks=shared_locks,
        admission_timeout_seconds=0.05,
    )

    with shared_locks.lock(WORKER_ADMISSION_LOCK):
        outcome = restarter.restart()

    assert outcome.ok is False
    assert "WORKER_ADMISSION_LOCK_TIMEOUT" in outcome.detail
    assert launcher.starts == [], "the replacement must never start under a wedged fence"
    assert store.read().pid == 1000, "the incumbent must never be stopped"


class _ReopenFailingEpoch:
    """AdmissionEpochStore whose reopen cannot be durably written."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.open_calls = 0

    def read(self) -> tuple[int, str]:
        return 1, "open"

    def open_next(self) -> int:
        self.open_calls += 1
        raise ConfigError("REOPEN_DISK_FAILURE: simulated")

    def close(self) -> int:
        self.close_calls += 1
        return 1

    def issue_permit(self, *, target: str | None) -> str:
        del target
        return "permit-token"

    def claim_permit(self, epoch: int, *, token: str, target: str | None) -> bool:
        del epoch, token, target
        return False


def test_restart_reports_reopen_failure_instead_of_silently_swallowing(
    tmp_path: Path,
) -> None:
    """P1-3: a stuck CLOSING admission must surface, never report a clean restart.

    The replacement is live by the time the reopen is attempted (the fence must
    stay closed while the outgoing process drains, so no dying supervisor spawns
    into the new epoch) -- but the restart must then be reported as FAILED, never
    as a clean success, because a CLOSING admission refuses every future worker
    spawn.
    """
    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    launcher = _RecordingLauncher(store)
    epochs = _ReopenFailingEpoch()

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        admission_epochs=epochs,
    ).restart()

    assert outcome.ok is False, "a stuck admission must never be reported as success"
    assert "WORKER_ADMISSION_REOPEN_FAILED" in outcome.detail
    assert "REOPEN_DISK_FAILURE" in outcome.detail
    assert epochs.close_calls == 1
    assert epochs.open_calls == 1
    assert launcher.starts, "the replacement is live before the reopen is attempted"


# ---------------------------------------------------------------------------
# F-012: replacement-scoped admission permit (TDD — RED first).
# ---------------------------------------------------------------------------


class _RecordingEpochs:
    """AdmissionEpochStore fake recording the handoff call order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self._epoch = 1

    def read(self) -> tuple[int, str]:
        return self._epoch, "open"

    def open_next(self) -> int:
        self.events.append("open")
        self._epoch += 1
        return self._epoch

    def close(self) -> int:
        self.events.append("close")
        return self._epoch

    def issue_permit(self, *, target: str | None) -> str:
        self.events.append(f"issue:{target}")
        return f"permit-token-{len(self.events)}"

    def claim_permit(self, epoch: int, *, token: str, target: str | None) -> bool:
        del epoch, token, target
        return False


class _PermitCaptureLauncher(_RecordingLauncher):
    """Launcher that captures the extra_env handed to the replacement."""

    def __init__(self, store, *, new_pid: int = 4242) -> None:
        super().__init__(store, new_pid=new_pid)
        self.extra_envs: list[dict[str, str]] = []

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        self.extra_envs.append(dict(extra_env))
        return super().start(config_path, foreground=foreground, extra_env=extra_env)


def test_restarter_fenced_orders_close_stop_issue_launch_open(tmp_path: Path) -> None:
    """F-012: the fenced handoff issues the permit AFTER the stop, BEFORE the launch."""
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV
    from repoforge.testing import InMemoryLockManager

    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    epochs = _RecordingEpochs()
    launcher = _PermitCaptureLauncher(store)
    events = epochs.events

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        admission_epochs=epochs,
        locks=InMemoryLockManager(),
        admission_timeout_seconds=0.05,
    ).restart(target_release="abc123")

    assert outcome.ok, outcome.detail
    assert launcher.extra_envs, "the replacement must be launched with the permit env"
    assert ADMISSION_PERMIT_ENV in launcher.extra_envs[0]
    assert events.index("issue:abc123") > events.index("close"), "permit AFTER close"
    assert events.index("issue:abc123") < events.index("open"), "permit BEFORE reopen"
    assert events[-1] == "open", "reopen is the final handoff step"


def test_rollback_launch_receives_a_fresh_permit(tmp_path: Path) -> None:
    """F-012: a rollback rotates the permit; the failed candidate's is not reused."""
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV
    from repoforge.testing import InMemoryLockManager

    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    epochs = _RecordingEpochs()

    def restart_for(target: str, launcher: _PermitCaptureLauncher) -> None:
        outcome = _restarter(
            store,
            _Control(store),
            launcher,
            tmp_path,
            admission_epochs=epochs,
            locks=InMemoryLockManager(),
            admission_timeout_seconds=0.05,
        ).restart(target_release=target)
        assert outcome.ok, outcome.detail

    first_launcher = _PermitCaptureLauncher(store, new_pid=4242)
    restart_for("candidate-sha", first_launcher)
    rollback_launcher = _PermitCaptureLauncher(store, new_pid=4243)
    restart_for("previous-sha", rollback_launcher)  # the rollback launch

    assert first_launcher.extra_envs and rollback_launcher.extra_envs
    first = first_launcher.extra_envs[0][ADMISSION_PERMIT_ENV]
    second = rollback_launcher.extra_envs[0][ADMISSION_PERMIT_ENV]
    assert first != second, "a rollback must rotate to a fresh permit, never reuse"


class _SpawnClaimingLauncher:
    """Launcher simulating the replacement booting and claiming the permit mid-handoff.

    The restarter issues the permit, releases the admission lock, and launches the
    replacement; the replacement (here) immediately calls ``create_intent`` while
    admission is still CLOSING. With a valid permit this must succeed and create a
    lease; without one it must be refused (the pre-fix RED behavior).
    """

    def __init__(self, store, registrar, *, new_pid: int = 4242) -> None:
        self._store = store
        self._registrar = registrar
        self._new_pid = new_pid
        self.extra_env: dict[str, str] = {}
        self.lease_ids: list[str] = []

    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        del config_path, foreground
        self.extra_env = dict(extra_env)
        from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

        keys = (ADMISSION_PERMIT_ENV, "REPOFORGE_RUNNING_RELEASE_SHA")
        previous = {key: os.environ.get(key) for key in keys}
        os.environ[ADMISSION_PERMIT_ENV] = extra_env.get(ADMISSION_PERMIT_ENV, "")
        os.environ["REPOFORGE_RUNNING_RELEASE_SHA"] = extra_env.get(
            "REPOFORGE_RUNNING_RELEASE_SHA", "abc123"
        )
        try:
            lease, _ = self._registrar.create_intent(
                role=__import__(
                    "repoforge.domain.process_lease", fromlist=["ProcessLeaseRole"]
                ).ProcessLeaseRole.EXECUTION_DAEMON,
                correlation_id="c" * 24,
            )
            self.lease_ids.append(lease.lease_id)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self._store.write(
            _record(phase=RuntimePhase.HEALTHY, pid=self._new_pid, identity=_IDENTITY_NEW)
        )
        return self._new_pid

    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
        del record, grace_seconds
        return False


def test_replacement_spawn_claims_permit_while_admission_is_closing(
    tmp_path: Path,
) -> None:
    """F-012: the replacement's create_intent succeeds during the CLOSING handoff.

    This is the exact timing that deadlocked the live-activation gate: the new
    supervisor boots (launcher.start) and spawns its execution worker while
    admission is still CLOSING, so without a permit the spawn is refused and the
    supervisor never publishes a live record.
    """
    from repoforge.adapters.persistence.json_admission_epoch import JsonAdmissionEpochStore
    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.application.runtime.worker_registrar import WorkerRegistrar
    from repoforge.testing import FixedClock, InMemoryLockManager, SequenceIdGenerator

    store = _Store()
    store.write(_record(phase=RuntimePhase.HEALTHY, pid=1000, identity=_IDENTITY_OLD))
    shared_locks = InMemoryLockManager()
    epochs = JsonAdmissionEpochStore(tmp_path / "state")
    leases = JsonProcessLeaseAdapter(tmp_path / "leases", InMemoryLockManager())
    registrar = WorkerRegistrar(
        leases=leases,
        ids=SequenceIdGenerator(("0",)),
        clock=FixedClock("2026-07-30T00:00:00+00:00"),
        epochs=epochs,
        locks=shared_locks,
        admission_timeout_seconds=0.05,
    )
    launcher = _SpawnClaimingLauncher(store, registrar)

    outcome = _restarter(
        store,
        _Control(store),
        launcher,
        tmp_path,
        admission_epochs=epochs,
        locks=shared_locks,
        admission_timeout_seconds=0.05,
    ).restart(target_release="abc123")

    assert outcome.ok, outcome.detail
    assert launcher.lease_ids, "the replacement's worker must claim the permit and spawn"
    assert len(leases.list_all().records) == 1


def test_runtime_environment_forwards_the_replacement_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-012: the shim -> supervisor env hop must carry the handoff permit.

    The restarter launches the replacement through the stable shim; without this
    forwarding the supervisor's registrar never sees the permit and the first
    worker spawn is refused while admission is CLOSING (the live-activation
    deadlock, reproduced with a real sandbox on the fix head).
    """
    from repoforge.interfaces.cli.main import _runtime_environment
    from repoforge.ports.admission_epoch import ADMISSION_PERMIT_ENV

    monkeypatch.setenv(ADMISSION_PERMIT_ENV, "handoff-token")
    args = type("Args", (), {"tunnel_id": None, "profile": None})()
    env = _runtime_environment(args)
    assert env.get(ADMISSION_PERMIT_ENV) == "handoff-token"

    monkeypatch.delenv(ADMISSION_PERMIT_ENV, raising=False)
    args = type("Args", (), {"tunnel_id": None, "profile": None})()
    env = _runtime_environment(args)
    assert ADMISSION_PERMIT_ENV not in env
