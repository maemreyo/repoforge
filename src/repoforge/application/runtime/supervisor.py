"""Long-lived tunnel supervisor with bounded restart and health-gated startup."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import random
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from repoforge import __version__

from ...domain.errors import ConfigError, ExecutionWorkerRegistrationError
from ...domain.redaction import redact_text
from ...domain.runtime import (
    RUNTIME_CONTROL_PROTOCOL_VERSION,
    ChildProcess,
    ControlCommand,
    ControlRequest,
    ControlResponse,
    HealthCheck,
    RestartHistoryRecord,
    RuntimePhase,
    RuntimeRecord,
    TunnelProfile,
    transition,
)
from ...domain.runtime_events import RuntimeEventV1, encode_runtime_event
from ...ports.clock import Clock
from ...ports.configuration import ConfigurationStore
from ...ports.execution_worker import ExecutionWorkerClient, ExecutionWorkerProgressHealth
from ...ports.ids import IdGenerator
from ...ports.locking import LockManager
from ...ports.process import ProcessInspector
from ...ports.runtime_control import (
    RestartHistoryStore,
    RuntimeControlClient,
    RuntimeControlServer,
    RuntimeStore,
)
from ...ports.tunnel import TunnelClient, TunnelProfileStore
from .execution_worker_reconciler import ExecutionWorkerReconciler

# Health-check name for a generation-adoption read failure (#448 Slice 6 review): an
# observability/persistence problem, never a service failure -- the watchdog filters
# this exact name out when deciding whether to count a restart or terminate the child,
# so the name is shared here rather than duplicated as a string literal.
_RUNTIME_STATE_READ_CHECK = "runtime_state_read"


def _install_origin() -> str | None:
    spec = importlib.util.find_spec("repoforge")
    origin = spec.origin if spec is not None else None
    if not origin:
        return None
    normalized = origin.replace("\\", "/")
    if "/site-packages/" in normalized:
        return "wheel"
    if "/src/repoforge/" in normalized:
        return "source"
    return "environment"


def _restart_backoff_seconds(
    restart_count: int,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
) -> float:
    """Bounded exponential backoff with jitter before the next restart attempt.

    Jitter, not just backoff, is the point (#448 Slice 5): a remote outage (Signature
    B) can knock several components' tunnel-children unhealthy at close to the same
    moment, and a purely deterministic delay would have every one of them retry in
    lockstep -- backoff alone bounds how OFTEN one supervisor retries, not whether many
    supervisors retry in the same instant. "Equal jitter" (half the bounded delay fixed,
    half randomized) keeps the same overall cap this formula always had while making
    that lockstep vanishingly unlikely, and still guarantees a real minimum delay
    between attempts (never near-zero, unlike "full jitter").

    ``jitter`` is injectable (#448 Slice 5 review) so tests can assert exact bounds
    against a scripted sequence instead of depending on the global `random` module's
    state; it defaults to `random.uniform`, a real random source, in production.
    """
    base: float = min(4.0, 0.25 * (2 ** (restart_count - 1)))
    return base / 2 + jitter(0, base / 2)


class RuntimeSupervisor:
    PROTOCOL_VERSION = RUNTIME_CONTROL_PROTOCOL_VERSION

    def __init__(
        self,
        *,
        store: RuntimeStore,
        restart_history: RestartHistoryStore | None = None,
        configs: ConfigurationStore,
        locks: LockManager,
        control: RuntimeControlServer,
        mcp_control: RuntimeControlClient,
        tunnel: TunnelClient,
        profile_store: TunnelProfileStore,
        clock: Clock,
        ids: IdGenerator,
        processes: ProcessInspector,
        mcp_runtime_path: Path,
        log_path: Path,
        execution_worker: ExecutionWorkerClient | None = None,
        execution_worker_log_path: Path | None = None,
        health_timeout_seconds: float = 30.0,
        max_restarts: int = 3,
        watchdog_interval_seconds: float = 2.0,
        health_failure_threshold: int = 3,
        stable_health_reset_seconds: float = 60.0,
        execution_worker_stale_seconds: float = 45.0,
        single_instance_wait_seconds: float = 45.0,
        # Generously above the watchdog's own cadence (`watchdog_interval_seconds`) plus
        # its bounded nested-probe timeouts, so a live watchdog always keeps the snapshot
        # this age is measured against fresh. Anything older means the watchdog loop
        # itself has stopped producing observations -- named and documented per #448
        # Signature C, which found the previous per-request probe timeout as a bare,
        # undocumented `2.0` literal duplicated in two unrelated files.
        health_snapshot_stale_after_seconds: float = 30.0,
        preflight: Callable[[], None] | None = None,
        worker_reconciler: ExecutionWorkerReconciler | None = None,
        # Injectable jitter source for restart backoff (#448 Slice 5 review): defaults
        # to a real random source in production; tests inject a scripted callable
        # instead of depending on the global `random` module's state.
        jitter: Callable[[float, float], float] | None = None,
    ) -> None:
        self._store = store
        self._restart_history = restart_history
        self._configs = configs
        self._locks = locks
        self._control = control
        self._mcp_control = mcp_control
        self._tunnel = tunnel
        self._profile_store = profile_store
        self._clock = clock
        self._ids = ids
        self._processes = processes
        self._mcp_runtime_path = mcp_runtime_path
        self._log_path = log_path
        self._execution_worker = execution_worker
        self._execution_worker_log_path = execution_worker_log_path
        if execution_worker is not None and execution_worker_log_path is None:
            raise ValueError("execution_worker_log_path is required with execution_worker")
        if (
            health_timeout_seconds <= 0
            or max_restarts < 0
            or watchdog_interval_seconds <= 0
            or health_failure_threshold <= 0
            or stable_health_reset_seconds <= 0
            or execution_worker_stale_seconds <= 0
            or single_instance_wait_seconds < 0
            or health_snapshot_stale_after_seconds <= 0
        ):
            raise ValueError("Runtime health and restart bounds must be positive")
        self._health_timeout = health_timeout_seconds
        self._single_instance_wait = single_instance_wait_seconds
        self._max_restarts = max_restarts
        self._watchdog_interval = watchdog_interval_seconds
        self._health_failure_threshold = health_failure_threshold
        self._stable_health_reset = stable_health_reset_seconds
        self._health_snapshot_stale_after = health_snapshot_stale_after_seconds
        self._execution_worker_stale = execution_worker_stale_seconds
        self._preflight = preflight
        self._worker_reconciler = worker_reconciler
        self._jitter = jitter if jitter is not None else random.uniform
        self._stop = threading.Event()
        self._child: ChildProcess | None = None
        self._execution_child: ChildProcess | None = None
        self._execution_generation: int | None = None
        # In-memory terminal latch (#448 Slice 6 review): set whenever the durable
        # restart-history ledger write fails, since the FAIL_CLOSED record that
        # decision depends on may ALSO fail to persist (the same underlying cause --
        # e.g. a full disk -- often breaks both writes at once). The control plane
        # must still answer with the true reason this incarnation is fail-closed even
        # when nothing durable says so; `_control_handler` prefers this over
        # `self._store.read()` whenever it is set.
        self._fail_closed_override: RuntimeRecord | None = None
        # Set whenever `_adopt_committed_runtime_generation` cannot read the store
        # (#448 Slice 6 review); folded into `_observe_health` so the watchdog reports
        # unhealthy rather than silently treating the read failure as "no committed
        # generation" and continuing as if everything were fine.
        self._generation_read_failed_detail: str | None = None

    def _execution_worker_progress(
        self,
        child: ChildProcess,
        *,
        process_alive: bool | None = None,
    ) -> ExecutionWorkerProgressHealth:
        worker = self._execution_worker
        if worker is None:
            return ExecutionWorkerProgressHealth(
                process_alive=False,
                heartbeat_available=False,
                heartbeat_age_seconds=None,
                progress_healthy=False,
                loop_state=None,
                current_operation_id=None,
                detail="isolated execution worker is not configured",
            )
        probe = getattr(worker, "progress_health", None)
        if not callable(probe):
            alive = process_alive if process_alive is not None else worker.is_alive(child)
            return ExecutionWorkerProgressHealth(
                process_alive=alive,
                heartbeat_available=False,
                heartbeat_age_seconds=None,
                progress_healthy=alive,
                loop_state=None,
                current_operation_id=None,
                detail=(
                    "execution worker progress probe is unavailable; process is alive"
                    if alive
                    else "isolated execution worker exited"
                ),
            )
        observed: object = probe(
            child,
            now=self._clock.now_iso(),
            stale_after_seconds=self._execution_worker_stale,
        )
        if not isinstance(observed, ExecutionWorkerProgressHealth):
            alive = process_alive if process_alive is not None else worker.is_alive(child)
            return ExecutionWorkerProgressHealth(
                process_alive=alive,
                heartbeat_available=False,
                heartbeat_age_seconds=None,
                progress_healthy=False,
                loop_state=None,
                current_operation_id=None,
                detail="execution worker progress probe returned an invalid result",
            )
        return observed

    def _ensure_execution_worker(
        self,
        generation: int,
        *,
        environment: dict[str, str],
        correlation_id: str,
    ) -> None:
        if self._execution_worker is None:
            return
        assert self._execution_worker_log_path is not None
        child = self._execution_child
        child_alive = bool(child is not None and self._execution_worker.is_alive(child))
        if child is not None and self._execution_generation == generation and child_alive:
            if self._execution_worker_progress(child, process_alive=True).progress_healthy:
                return
            self._execution_worker.terminate(child, grace_seconds=3)
        elif child is not None and child_alive:
            self._execution_worker.terminate(child, grace_seconds=3)
        self._execution_child = None
        self._execution_generation = None
        self._execution_child = self._execution_worker.start(
            generation,
            env=environment,
            log_path=self._execution_worker_log_path,
            correlation_id=correlation_id,
        )
        self._execution_generation = generation

    def _clear_target(self, generation: int) -> None:
        with contextlib.suppress(ConfigError):
            self._configs.clear_activation_target(expected_generation=generation)

    def _adopt_committed_runtime_generation(self, fallback: int) -> int:
        """Adopt a hot-reloaded generation only when disk and runtime state agree."""
        active = self._configs.active()
        # A broken store here (#448 Slice 6 review) must not crash the run loop before
        # it ever reaches the fail-closed machinery elsewhere -- but unreadable is NOT
        # equivalent to "no committed generation": that would erase the difference
        # between "nothing new to adopt" and "the durable state is currently broken".
        # Keeping `fallback` is still the safe choice here (never silently adopt an
        # unproven target), but the failure must not vanish -- `_observe_health` folds
        # `self._generation_read_failed_detail` into its check set so the watchdog
        # stops reporting `healthy` while this persists, rather than silently
        # continuing as if the read had simply found nothing.
        try:
            record = self._store.read()
            self._generation_read_failed_detail = None
        except Exception as exc:
            self._generation_read_failed_detail = redact_text(f"{type(exc).__name__}: {exc}")
            return fallback
        if (
            active is not None
            and record is not None
            and record.active_generation == active.generation
            and record.accepted_generation == active.generation
        ):
            return active.generation
        return fallback

    def _mcp_generation(self) -> int | None:
        if not self._mcp_runtime_path.is_file():
            return None
        try:
            import json

            raw = json.loads(self._mcp_runtime_path.read_text(encoding="utf-8"))
            pid = raw.get("pid")
            identity = raw.get("process_identity")
            generation = raw.get("active_generation")
            if (
                isinstance(pid, int)
                and isinstance(identity, str)
                and self._processes.identity(pid) == identity
                and isinstance(generation, int)
            ):
                return generation
        except (OSError, ValueError, TypeError):
            return None
        return None

    def _record(
        self,
        phase: RuntimePhase,
        *,
        accepted_generation: int,
        active_generation: int | None,
        profile: TunnelProfile,
        tool_surface_hash: str,
        correlation_id: str,
        incarnation_id: str,
        child: ChildProcess | None,
        restart_count: int = 0,
        error_code: str | None = None,
        error: str | None = None,
        health: tuple[tuple[str, bool, str], ...] = (),
        consecutive_health_failures: int = 0,
        restarts_total: int = 0,
        last_restart_at: str | None = None,
        restart_history_provenance: str = "unknown",
        fail_closed_since: str | None = None,
    ) -> RuntimeRecord:
        pid = os.getpid()
        identity = self._processes.identity(pid)
        if identity is None:
            raise ConfigError("Cannot determine supervisor process identity")
        now = self._clock.now_iso()
        return RuntimeRecord(
            protocol_version=self.PROTOCOL_VERSION,
            phase=phase,
            pid=pid,
            process_identity=identity,
            active_generation=active_generation,
            accepted_generation=accepted_generation,
            tunnel_profile=profile.profile,
            tunnel_profile_fingerprint=profile.fingerprint,
            tool_surface_hash=tool_surface_hash,
            started_at=now,
            updated_at=now,
            correlation_id=correlation_id,
            child_pid=child.pid if child else None,
            child_process_identity=child.process_identity if child else None,
            restart_count=restart_count,
            last_error_code=error_code,
            last_error=error,
            health=health,
            package_version=__version__,
            executable=sys.executable,
            install_origin=_install_origin(),
            # Captured by the launcher before exec; never re-derived here.
            running_release_sha=os.environ.get("REPOFORGE_RUNNING_RELEASE_SHA") or None,
            health_observed_at=now if health else None,
            consecutive_health_failures=consecutive_health_failures,
            restarts_total=restarts_total,
            last_restart_at=last_restart_at,
            fail_closed_since=fail_closed_since,
            incarnation_id=incarnation_id,
            restart_history_provenance=restart_history_provenance,
        )

    def _record_restart_or_fail_closed(
        self,
        *,
        generation: int,
        profile: TunnelProfile,
        tool_surface_hash: str,
        correlation_id: str,
        incarnation_id: str,
        reason: str,
        restart_count: int,
        occurred_at: str,
        restarts_total: int,
        last_restart_at: str | None,
        restart_history_provenance: str,
    ) -> RestartHistoryRecord | None:
        """Record one restart in the durable ledger, or fail closed if that write fails.

        A ledger write can fail (disk full, lock contention, a corrupt sibling lock
        file...) at the exact moment a caller is about to decide whether to respawn the
        tunnel child. Before this, an unhandled failure here fell through to the
        generic top-level handler, which writes a bare `FAILED` record and returns a
        nonzero exit -- exactly the launchd `KeepAlive` relaunch this whole issue exists
        to bound, now triggered by the ledger itself rather than the tunnel (#448 Slice
        4). Returning `None` means "already handled": the caller must return 0
        immediately without spawning a new child, since `_serve_fail_closed` below has
        already blocked until an explicit stop.

        The durable FAIL_CLOSED write below is itself best-effort (#448 Slice 6
        review): if the persistent store is unavailable for the same reason the ledger
        write just failed (e.g. the whole state directory's disk is out of space), that
        write can ALSO raise. Letting that second exception propagate would land right
        back in the same top-level handler this whole method exists to avoid -- staying
        resident-but-unrecorded is strictly safer than exiting nonzero and letting
        launchd relaunch into the identical failure. Every side effect here is
        independently suppressed so a failure in one (the durable record, the target
        clear, the event log) can never prevent `_serve_fail_closed` from still running.

        `self._fail_closed_override` is set unconditionally, before the durable write is
        even attempted: the control plane must report this exact typed reason for the
        rest of this incarnation's life regardless of whether the durable write below
        actually lands, since a caller reading `rf runtime status` during an outage has
        no other way to learn this isn't just a stale `healthy`/`degraded` snapshot.
        """
        try:
            if self._restart_history is None:
                # A caller that composes no restart-history store (tests that only
                # exercise worker liveness) cannot record an increment; the restart
                # counter simply carries no durable evidence in that composition.
                return None
            return self._restart_history.record_restart(
                incarnation_id=incarnation_id,
                reason=reason,
                occurred_at=occurred_at,
                event_id=f"{incarnation_id}:{restart_count}",
            )
        except Exception as exc:
            error_text = redact_text(f"{type(exc).__name__}: {exc}")
            fail_closed_record = self._record(
                RuntimePhase.FAIL_CLOSED,
                accepted_generation=generation,
                active_generation=None,
                profile=profile,
                tool_surface_hash=tool_surface_hash,
                correlation_id=correlation_id,
                incarnation_id=incarnation_id,
                child=None,
                restart_count=restart_count,
                restarts_total=restarts_total,
                last_restart_at=last_restart_at,
                restart_history_provenance=restart_history_provenance,
                error_code="RESTART_LEDGER_WRITE_FAILED",
                error=error_text,
                fail_closed_since=self._clock.now_iso(),
            )
            self._fail_closed_override = fail_closed_record
            with contextlib.suppress(Exception):
                self._store.write(fail_closed_record)
            with contextlib.suppress(Exception):
                self._clear_target(generation)
            with contextlib.suppress(Exception):
                self._append_supervisor_event(
                    event_kind="restart_ledger_write_failed",
                    level="ERROR",
                    message=(
                        "Restart-history ledger write failed; the durable fail-closed "
                        f"record may also be unwritable: {error_text}"
                    ),
                    correlation_id=correlation_id,
                    incarnation_id=incarnation_id,
                )
            self._serve_fail_closed(correlation_id, incarnation_id)
            return None

    def _run_preflight(self) -> tuple[str, str] | None:
        """Run the deterministic preflight; ``(error_code, message)`` on failure."""

        if self._preflight is None:
            return None
        try:
            self._preflight()
        except ConfigError as exc:
            code = str(exc).split(":", 1)[0] or "PREFLIGHT_FAILED"
            return code, redact_text(str(exc))
        except Exception as exc:
            return "PREFLIGHT_FAILED", redact_text(f"{type(exc).__name__}: {exc}")
        return None

    def _durable_fail_closed(self, prior: RuntimeRecord | None) -> bool:
        """Honor a prior supervisor's fail-closed state for the same release.

        launchd relaunching a supervisor must not reset a deterministic failure: if the
        last lifetime failed closed and this process runs the same release, stay
        fail-closed instead of re-probing and re-spawning. The latch is inherited ONLY
        when both release identities are proven and equal; a missing identity on either
        side means the release cannot be established, so the deterministic preflight
        re-runs (safe -- it spawns no child) rather than wrongly holding a different
        release resident (#420).
        """
        if prior is None or prior.fail_closed_since is None:
            return False
        if prior.phase not in {
            RuntimePhase.FAILED,
            RuntimePhase.FAIL_CLOSED,
            RuntimePhase.STOPPED,
        }:
            return False
        if prior.running_release_sha is None:
            return False
        current_release = os.environ.get("REPOFORGE_RUNNING_RELEASE_SHA")
        return current_release == prior.running_release_sha

    def _serve_fail_closed(self, correlation_id: str, incarnation_id: str) -> None:
        """Block serving the control socket until an explicit SHUTDOWN.

        Deterministic, non-retryable failures must never be respawned and must never
        exit: exiting lets launchd relaunch the supervisor and crash-loop across
        lifetimes. The control plane stays answerable in FAIL_CLOSED so ``rf doctor`` /
        ``rf runtime status`` can read the typed failure; an explicit SHUTDOWN
        terminalizes to STOPPED (which launchd does not relaunch).

        Persisting that terminal STOPPED transition is itself best-effort (#448 Slice 6
        review): a store that is unavailable at shutdown -- the same disk-full
        condition that may have caused this fail-closed state in the first place --
        must never turn a clean, explicit shutdown into a nonzero exit. `run()`'s
        caller always returns 0 after this method returns; an unhandled exception here
        would instead propagate to the top-level handler's `return 2`, which is exactly
        the launchd relaunch this whole method exists to prevent. Only the persistence
        errors this is actually expecting (`OSError`, `ConfigError`) are caught here --
        broad enough to cover a real broken store, narrow enough not to silently
        swallow a programming error -- and the failure is still surfaced, best-effort,
        via the runtime event log rather than vanishing entirely.
        """
        self._stop.wait()
        try:
            current = self._fail_closed_override or self._store.read()
            if current is not None:
                self._store.write(
                    replace(
                        current,
                        phase=RuntimePhase.STOPPED,
                        pid=None,
                        process_identity=None,
                        child_pid=None,
                        child_process_identity=None,
                        active_generation=None,
                        updated_at=self._clock.now_iso(),
                    )
                )
        except (OSError, ConfigError) as exc:
            with contextlib.suppress(Exception):
                self._append_supervisor_event(
                    event_kind="stopped_persist_failed",
                    level="ERROR",
                    message=(
                        "Failed to persist the terminal STOPPED state during shutdown "
                        f"(the process is still exiting cleanly): "
                        f"{redact_text(f'{type(exc).__name__}: {exc}')}"
                    ),
                    correlation_id=correlation_id,
                    incarnation_id=incarnation_id,
                )

    def _tunnel_health(self, child: ChildProcess) -> tuple[HealthCheck, ...]:
        probe = getattr(self._tunnel, "health", None)
        if callable(probe):
            try:
                checks = tuple(probe(child, timeout_seconds=1.0))
                if checks:
                    return checks
            except Exception as exc:
                return (
                    HealthCheck(
                        "tunnel_probe",
                        False,
                        redact_text(f"tunnel health probe failed: {type(exc).__name__}: {exc}"),
                    ),
                )
        alive = self._tunnel.is_alive(child)
        return (
            HealthCheck(
                "tunnel_child",
                alive,
                "managed child process is alive" if alive else "managed child process exited",
            ),
        )

    def _observe_health(
        self, generation: int, child: ChildProcess
    ) -> tuple[bool, tuple[tuple[str, bool, str], ...]]:
        checks = list(self._tunnel_health(child))
        # First, because a runtime that cannot answer control requests is not healthy no
        # matter what else is true. This record is written by the watchdog loop, which
        # survives the control loop, so without this check a supervisor whose control plane
        # had died kept publishing `phase: healthy` while every read needing the socket
        # timed out -- and the recovery commands, which go through that same socket, could
        # not run either (#322).
        #
        # `is_healthy()`, not `is_serving()`, drives this boolean: an implementation
        # backed by a fixed-size worker pool can keep `is_serving() == True` even after
        # losing some (not all) of its workers -- it can still answer requests -- but
        # that pool never replenishes, so the loss is permanent. Feeding `is_serving()`
        # into this check would let a runtime running at a silent, persistent capacity
        # loss keep publishing `healthy`, the same gap #322 closed, one layer down
        # (#448 Slice 1 partial-worker health semantics). `serving_diagnostic()` still
        # supplies the detail text either way.
        healthy = self._control.is_healthy()
        checks.append(HealthCheck("control_plane", healthy, self._control.serving_diagnostic()))
        if self._execution_worker is not None:
            worker_alive = bool(
                self._execution_child and self._execution_worker.is_alive(self._execution_child)
            )
            checks.append(
                HealthCheck(
                    "execution_worker",
                    worker_alive,
                    (
                        "isolated execution worker is alive"
                        if worker_alive
                        else "isolated execution worker exited"
                    ),
                )
            )
            progress = (
                self._execution_worker_progress(
                    self._execution_child,
                    process_alive=worker_alive,
                )
                if worker_alive and self._execution_child is not None
                else ExecutionWorkerProgressHealth(
                    process_alive=False,
                    heartbeat_available=False,
                    heartbeat_age_seconds=None,
                    progress_healthy=False,
                    loop_state=None,
                    current_operation_id=None,
                    detail="isolated execution worker exited",
                )
            )
            checks.append(
                HealthCheck(
                    "execution_worker_progress",
                    progress.progress_healthy,
                    progress.detail,
                )
            )
        mcp_generation = self._mcp_generation()
        generation_ok = mcp_generation == generation
        checks.append(
            HealthCheck(
                "mcp_generation",
                generation_ok,
                (
                    f"MCP reported generation {generation}"
                    if generation_ok
                    else f"expected generation {generation}; observed {mcp_generation}"
                ),
            )
        )
        mcp_ok = False
        mcp_detail = "MCP repository health did not pass"
        if all(check.ok for check in checks) and generation_ok:
            try:
                response = self._mcp_control.request(
                    ControlRequest(1, ControlCommand.HEALTH, self._ids.new_hex(24)),
                    timeout_seconds=2.0,
                )
                mcp_ok = response.ok and response.status == "healthy"
                mcp_detail = (
                    "repo_list completed through MCP control"
                    if mcp_ok
                    else response.message or response.status
                )
            except Exception as exc:
                mcp_detail = redact_text(f"MCP health probe failed: {type(exc).__name__}: {exc}")
        checks.append(HealthCheck("repository_self_check", mcp_ok, mcp_detail))
        # Appended LAST, deliberately after the MCP round-trip gate above (#448 Slice 6
        # review): a read failure in `_adopt_committed_runtime_generation` is not
        # evidence that nothing needs adopting -- it means the durable state is
        # currently unreadable -- and folding it in here rather than silently keeping
        # the fallback generation and moving on is what stops the watchdog from
        # continuing to report `healthy` while this persists. It must never itself
        # cascade into skipping the MCP repository check above (that gate gauges
        # actual repository health, not this), which is why it is added only now.
        if self._generation_read_failed_detail is not None:
            checks.append(
                HealthCheck(_RUNTIME_STATE_READ_CHECK, False, self._generation_read_failed_detail)
            )
        legacy = tuple(check.legacy() for check in checks)
        return all(check.ok for check in checks), legacy

    def _snapshot_age_seconds(self, observed_at: str | None) -> float | None:
        """Age of a durable health observation, or ``None`` if it was never observed.

        Deliberately parses two ISO-8601 timestamps rather than tracking a
        monotonic clock alongside the record: the record is durable across
        process restarts and a monotonic clock is not (#448 Signature C).
        """
        if not observed_at:
            return None
        try:
            observed = datetime.fromisoformat(observed_at)
            now = datetime.fromisoformat(self._clock.now_iso())
        except ValueError:
            return None
        return max(0.0, (now - observed).total_seconds())

    def _control_handler(self, request: ControlRequest) -> ControlResponse:
        # `self._fail_closed_override` (#448 Slice 6 review) short-circuits `read()`
        # entirely when set, so a broken store can never hide the true typed reason
        # this incarnation is fail-closed behind a stale snapshot or a read failure.
        record = self._fail_closed_override or self._store.read()
        child_alive = bool(self._child and self._tunnel.is_alive(self._child))
        execution_worker_alive = bool(
            self._execution_worker
            and self._execution_child
            and self._execution_worker.is_alive(self._execution_child)
        )
        payload: dict[str, object] = {
            "record": record.phase.value if record else "stopped",
            "active_generation": record.active_generation if record else None,
            "accepted_generation": record.accepted_generation if record else None,
            "child_alive": child_alive,
            "execution_worker_alive": execution_worker_alive,
            "health": list(record.health) if record else [],
            "health_observed_at": record.health_observed_at if record else None,
            # Surfaced on every command (#448 Slice 6 review), not only in each
            # command's own `error_code` field: PING and STATUS never carried the
            # phase's typed reason at all, so an operator reading `rf runtime status`
            # during an outage could see `record: fail_closed` with no way to learn
            # WHY without a separate, possibly-also-broken durable read.
            "last_error_code": record.last_error_code if record else None,
            "last_error": record.last_error if record else None,
        }
        if request.command is ControlCommand.PING:
            return ControlResponse(
                1, True, request.correlation_id, "alive", tuple(sorted(payload.items()))
            )
        if request.command is ControlCommand.STATUS:
            return ControlResponse(
                1,
                record is not None,
                request.correlation_id,
                str(payload["record"]),
                tuple(sorted(payload.items())),
                None if record is not None else "RUNTIME_NOT_STARTED",
            )
        if request.command is ControlCommand.HEALTH:
            # Snapshot read only (#448 Signature C): this control socket serves one
            # connection at a time on a single dedicated thread (`UnixRuntimeControlServer`).
            # A request that triggered its own fresh, nested MCP round-trip here used to
            # block that thread for the probe's full duration, so a second caller could
            # queue behind it in the kernel backlog and time out for no reason connected to
            # real service health. The watchdog loop is the sole producer of health
            # observations; this branch only ever reads what it already wrote.
            age_seconds = self._snapshot_age_seconds(record.health_observed_at if record else None)
            stale = age_seconds is None or age_seconds > self._health_snapshot_stale_after
            payload["health_age_seconds"] = age_seconds
            payload["health_freshness"] = (
                "unknown" if age_seconds is None else ("stale" if stale else "fresh")
            )
            snapshot_healthy = bool(record and all(ok for _name, ok, _detail in record.health))
            healthy = bool(
                record
                and record.phase is RuntimePhase.HEALTHY
                and child_alive
                and snapshot_healthy
                and not stale
            )
            status: str
            error_code: str | None
            message: str | None
            if stale:
                status, error_code, message = (
                    "unknown",
                    "HEALTH_SNAPSHOT_STALE",
                    (
                        "No fresh health observation from the watchdog "
                        f"(age={age_seconds!r}s, stale-after={self._health_snapshot_stale_after}s); "
                        "the watchdog loop may have stopped producing observations."
                    ),
                )
            else:
                status = "healthy" if healthy else "unhealthy"
                error_code = None if healthy else "RUNTIME_UNHEALTHY"
                message = None if healthy else "Supervisor or managed child is not healthy"
            return ControlResponse(
                1,
                healthy,
                request.correlation_id,
                status,
                tuple(sorted(payload.items())),
                error_code,
                message,
            )
        if request.command is ControlCommand.SHUTDOWN:
            self._stop.set()
            return ControlResponse(1, True, request.correlation_id, "stopping")
        return ControlResponse(
            1,
            False,
            request.correlation_id,
            "unsupported",
            error_code="UNSUPPORTED_CONTROL_COMMAND",
            message=request.command.value,
        )

    def _wait_healthy(
        self, generation: int, child: ChildProcess
    ) -> tuple[bool, tuple[tuple[str, bool, str], ...]]:
        deadline = time.monotonic() + self._health_timeout
        latest: tuple[tuple[str, bool, str], ...] = ()
        while time.monotonic() < deadline:
            healthy, latest = self._observe_health(generation, child)
            if healthy:
                return True, latest
            if not self._tunnel.is_alive(child):
                break
            time.sleep(0.1)
        return False, latest or (
            ("tunnel_child", False, "managed child process did not become healthy"),
        )

    @contextlib.contextmanager
    def _single_instance_lock(self, correlation_id: str, *, incarnation_id: str) -> Iterator[None]:
        """Take the single-instance lock, waiting a bounded interval for a handoff.

        An activation replaces this process: the incoming supervisor starts while the
        outgoing one is still shutting down and still holds this lock. Taking it with no
        wait therefore made every handoff a race the incoming process usually lost, which
        failed activations that were otherwise fine. Waiting is bounded and never
        unlimited -- a lock that is genuinely held by a supervisor which is not leaving is
        reported as a typed timeout, because two live supervisors is the exact state this
        lock exists to prevent.
        """
        entered = False
        try:
            with self._locks.lock(
                "runtime-single-instance",
                timeout_seconds=self._single_instance_wait,
                metadata={"correlation_id": correlation_id},
            ):
                entered = True
                yield
        except ConfigError as exc:
            if entered or "LOCK_TIMEOUT" not in str(exc):
                raise
            message = (
                "RUNTIME_SUPERVISOR_HANDOFF_TIMEOUT: another supervisor still holds "
                f"'runtime-single-instance' after {self._single_instance_wait:g}s "
                f"({self._single_instance_holder()}). Stop it with `rf runtime stop` "
                "before starting this one; two live supervisors are never started."
            )
            # Structured, not just an uncaught exception on stderr: this refused
            # incarnation exits before ever calling `self._store.write(...)`, so its own
            # runtime record is never touched -- exactly right, since overwriting the
            # *incumbent's* live record with this refused process's failure would falsely
            # report a healthy incumbent as failed (the bug an earlier version of this
            # fix had, caught by this file's own
            # `test_a_lock_that_is_never_released_is_a_typed_handoff_timeout`). This
            # event is appended to the runtime log instead -- the same file/format the
            # tunnel child's own events already use -- so an operator or agent checking
            # it first, per this fix's own acceptance bar, sees this rather than only a
            # raw Python traceback wherever launchd points stderr (#448 Slice 5).
            with contextlib.suppress(Exception):
                self._append_supervisor_event(
                    event_kind="handoff_timeout",
                    level="ERROR",
                    message=message,
                    correlation_id=correlation_id,
                    incarnation_id=incarnation_id,
                )
            raise ConfigError(message) from exc

    def _append_supervisor_event(
        self,
        *,
        event_kind: str,
        level: str,
        message: str,
        correlation_id: str,
        incarnation_id: str,
    ) -> None:
        """Append one structured supervisor-lifecycle event to the runtime log.

        Uses the same `RuntimeEventV1`/JSONL format the tunnel child's own log-pump
        already writes to this file, so an existing reader (`parse_runtime_event`, `rf
        doctor`'s log inspection) needs no new format to understand (#448 Slice 5).
        Deliberately minimal compared to `TunnelCliClient._append_runtime_event`
        (no rotation): supervisor-lifecycle events are rare, not a per-line stream.
        """
        event = RuntimeEventV1(
            observed_at=self._clock.now_iso(),
            component="supervisor",
            stream="lifecycle",
            level=level,
            event_kind=event_kind,
            message=redact_text(f"{message} (incarnation_id={incarnation_id})"),
            correlation_id=correlation_id,
        )
        encoded = (encode_runtime_event(event) + "\n").encode("utf-8", errors="replace")
        self._log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "ab", buffering=0) as handle:
            handle.write(encoded)

    def _single_instance_holder(self) -> str:
        """Describe the current lock holder from the lock file, for the timeout message.

        Distinguishes the two situations this timeout actually covers, which call for
        different operator responses (#448 Slice 5): a holder pid that is still alive is
        an in-progress handoff -- the prior incarnation is still shutting down, and
        waiting (or trying again shortly) is the right call; a holder pid that is no
        longer alive is unexpected (the OS releases `flock` when the holding process
        exits) and worth reporting honestly rather than folding into the same generic
        message, since it points at a different kind of problem (a lock genuinely stuck
        held by something else, e.g. a process that inherited the descriptor).
        """
        try:
            path = self._locks.path_for("runtime-single-instance")
            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (AttributeError, OSError, ValueError):
            return "holder unknown"
        if not isinstance(payload, dict) or not payload.get("pid"):
            return "holder unknown"
        pid = payload["pid"]
        if not isinstance(pid, int):
            return "holder unknown"
        if self._processes.identity(pid) is not None:
            return (
                f"holder pid {pid} is still alive (a prior incarnation likely still shutting down)"
            )
        return f"holder pid {pid} is no longer alive (lock is unexpectedly still held)"

    def run(
        self,
        *,
        generation: int,
        profile: TunnelProfile,
        tool_surface_hash: str,
        environment: dict[str, str],
    ) -> int:
        correlation_id = self._ids.new_hex(24)
        # A fresh, non-reused identifier for THIS supervisor process lifetime, distinct
        # from `correlation_id` above (which is also reused per-request in
        # `ControlRequest`/`ControlResponse` and is therefore unsuitable as a lifecycle
        # identifier on its own) -- #448 Slice 4.
        incarnation_id = self._ids.new_hex(24)
        with self._single_instance_lock(correlation_id, incarnation_id=incarnation_id):
            self._control.start(self._control_handler)
            previous_handlers: dict[signal.Signals, object] = {}

            def stop_handler(_signum: int, _frame: object) -> None:
                self._stop.set()

            # Signal handlers can only be installed from the main thread; an embedded
            # supervisor (or one under test) in another thread keeps `_stop` control-only.
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[signal.Signals(signum)] = signal.signal(signum, stop_handler)
            restart_count = 0
            # Carried across supervisor lifetimes from the durable restart-history ledger,
            # NOT `self._store` -- `JsonRuntimeStore.read()` self-heals to `None` whenever
            # the recorded pid no longer matches a live process, which is true on
            # essentially every real restart (a fresh incarnation is never the same pid as
            # the one that just died). Reading `restarts_total`/`last_restart_at` from that
            # same self-healing record was discarding them as collateral damage of a check
            # that has nothing to do with restart history (#448 Slice 4; the incident this
            # ledger fixes: a fresh incarnation reported `restarts_total: 0`,
            # `last_restart_at: null` despite real prior restarts).
            prior_history = (
                self._restart_history.read() if self._restart_history is not None else None
            )
            if prior_history is None:
                # Migration (#448 Slice 4): this release may be the first to run since
                # the ledger existed at all, while `self._store`'s file can still carry
                # real `restarts_total`/`last_restart_at` history from before -- seeding
                # from it (bypassing the pid-liveness self-heal, which would otherwise
                # discard it) beats silently starting over at a false `0`.
                # `seed_if_missing` itself re-checks the ledger under its own lock, so
                # two incarnations racing to seed for the first time cannot disagree.
                legacy_evidence = (
                    self._store.peek_restart_evidence()
                    if self._restart_history is not None
                    else None
                )
                if legacy_evidence is not None and self._restart_history is not None:
                    legacy_total, legacy_last_restart_at = legacy_evidence
                    prior_history = self._restart_history.seed_if_missing(
                        restarts_total=legacy_total,
                        last_restart_at=legacy_last_restart_at,
                        incarnation_id=incarnation_id,
                        occurred_at=self._clock.now_iso(),
                    )
            restarts_total = prior_history.restarts_total if prior_history is not None else 0
            last_restart_at: str | None = (
                prior_history.last_restart_at if prior_history is not None else None
            )
            # Distinguishes "verified zero restarts" from "the ledger predates this build
            # or was otherwise unavailable" -- an operator reading a bare `0` cannot tell
            # those apart otherwise (#448 Slice 4).
            restart_history_provenance = "durable" if prior_history is not None else "unknown"
            # A broken store here (#448 Slice 6 review) must not crash this incarnation
            # before it ever reaches the fail-closed machinery below -- there was no
            # enclosing `try` around this specific read at all before. But unreadable is
            # NOT equivalent to absent: this read is exactly what proves whether a prior
            # incarnation already latched a durable fail-closed state for this release
            # (`_durable_fail_closed` below). Silently treating a read failure as "no
            # prior record" would make the circuit breaker vanish precisely when it
            # cannot be disproven either -- the supervisor would run preflight and could
            # respawn the tunnel child, reopening the exact restart cycle Slice 5 exists
            # to keep closed. An unreadable prior state must fail closed itself, in
            # memory, before ever reaching preflight or a tunnel-start attempt.
            try:
                prior_record = self._store.read()
            except Exception as exc:
                error_text = redact_text(f"{type(exc).__name__}: {exc}")
                fail_closed_record = self._record(
                    RuntimePhase.FAIL_CLOSED,
                    accepted_generation=generation,
                    active_generation=None,
                    profile=profile,
                    tool_surface_hash=tool_surface_hash,
                    correlation_id=correlation_id,
                    incarnation_id=incarnation_id,
                    child=None,
                    restarts_total=restarts_total,
                    last_restart_at=last_restart_at,
                    restart_history_provenance=restart_history_provenance,
                    error_code="RUNTIME_STATE_READ_FAILED",
                    error=error_text,
                    fail_closed_since=self._clock.now_iso(),
                )
                self._fail_closed_override = fail_closed_record
                with contextlib.suppress(Exception):
                    self._store.write(fail_closed_record)
                with contextlib.suppress(Exception):
                    self._clear_target(generation)
                with contextlib.suppress(Exception):
                    self._append_supervisor_event(
                        event_kind="runtime_state_read_failed",
                        level="ERROR",
                        message=(
                            "Could not read durable runtime state at startup; a prior "
                            "fail-closed latch can neither be confirmed nor ruled out, "
                            f"so this incarnation fails closed rather than risk "
                            f"repeating an exhausted restart cycle: {error_text}"
                        ),
                        correlation_id=correlation_id,
                        incarnation_id=incarnation_id,
                    )
                self._serve_fail_closed(correlation_id, incarnation_id)
                return 0
            try:
                if prior_record is not None and self._durable_fail_closed(prior_record):
                    # A prior lifetime already failed closed for this release; honoring
                    # it writes the record for THIS process and stays resident.
                    code = prior_record.last_error_code or "PREFLIGHT_FAILED"
                    error = prior_record.last_error or (
                        "A previous supervisor failed closed for this release; the "
                        "durable fail-closed state is honored"
                    )
                    self._store.write(
                        self._record(
                            RuntimePhase.FAIL_CLOSED,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            child=None,
                            error_code=code,
                            error=error,
                            fail_closed_since=prior_record.fail_closed_since,
                        )
                    )
                    self._clear_target(generation)
                    self._serve_fail_closed(correlation_id, incarnation_id)
                    return 0

                preflight_error = self._run_preflight()
                if preflight_error is not None:
                    code, message = preflight_error
                    self._store.write(
                        self._record(
                            RuntimePhase.FAIL_CLOSED,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            child=None,
                            error_code=code,
                            error=message,
                            fail_closed_since=self._clock.now_iso(),
                        )
                    )
                    self._clear_target(generation)
                    self._serve_fail_closed(correlation_id, incarnation_id)
                    return 0

                # Reclaim execution workers whose owner supervisor is gone before
                # spawning this supervisor's own worker (#368). This is NOT
                # best-effort anymore: an unresolved worker (unproven identity that may
                # still run, a SIGKILL survivor, an incomplete registry scan, or an
                # unreadable registry record) can hold the exact locks that deadlocked
                # the last incident, so the supervisor fails closed in place rather
                # than spawn a contending replacement (#420).
                if self._worker_reconciler is not None:
                    try:
                        reclaim_report = self._worker_reconciler.reconcile()
                    except Exception:
                        reclaim_report = None
                    if (
                        reclaim_report is None
                        or not reclaim_report.evidence_complete
                        or reclaim_report.possibly_alive_unproven > 0
                        or reclaim_report.survived_kill > 0
                    ):
                        # Propagate the typed blocker so the fail-closed record stays
                        # machine-actionable instead of one collapsed code (#424).
                        code = (
                            reclaim_report.blocker_code if reclaim_report is not None else None
                        ) or "STALE_EXECUTION_WORKER_RECLAMATION_UNCERTAIN"
                        detail = (
                            "execution workers of a prior supervisor could not be proven reclaimed"
                            if reclaim_report is None
                            else reclaim_report.detail
                        )
                        self._store.write(
                            self._record(
                                RuntimePhase.FAIL_CLOSED,
                                accepted_generation=generation,
                                active_generation=None,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                incarnation_id=incarnation_id,
                                child=None,
                                error_code=code,
                                error=redact_text(detail),
                                fail_closed_since=self._clock.now_iso(),
                            )
                        )
                        self._clear_target(generation)
                        self._serve_fail_closed(correlation_id, incarnation_id)
                        return 0

                try:
                    initialize_profile = self._profile_store.fingerprint() != profile.fingerprint
                    if initialize_profile:
                        self._tunnel.initialize(profile, env=environment)
                    doctor_ok, doctor_detail = self._tunnel.doctor(profile, env=environment)
                    if doctor_ok and initialize_profile:
                        self._profile_store.commit(profile)
                except Exception as exc:
                    doctor_ok, doctor_detail = False, redact_text(f"{type(exc).__name__}: {exc}")
                if not doctor_ok:
                    self._store.write(
                        self._record(
                            RuntimePhase.FAILED,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            child=None,
                            error_code="TUNNEL_DOCTOR_FAILED",
                            error=redact_text(doctor_detail),
                        )
                    )
                    self._clear_target(generation)
                    return 2

                if self._execution_worker is not None:
                    try:
                        self._ensure_execution_worker(
                            generation,
                            environment=environment,
                            correlation_id=correlation_id,
                        )
                    except ExecutionWorkerRegistrationError as exc:
                        # Fail closed: the adapter terminated the unregistered worker;
                        # staying resident is safer than starting a contending one.
                        self._store.write(
                            self._record(
                                RuntimePhase.FAIL_CLOSED,
                                accepted_generation=generation,
                                active_generation=None,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                incarnation_id=incarnation_id,
                                child=None,
                                error_code="EXECUTION_WORKER_REGISTRATION_FAILED",
                                error=redact_text(str(exc)),
                                fail_closed_since=self._clock.now_iso(),
                            )
                        )
                        self._clear_target(generation)
                        self._serve_fail_closed(correlation_id, incarnation_id)
                        return 0
                    except Exception as exc:
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=None,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                incarnation_id=incarnation_id,
                                child=None,
                                error_code="EXECUTION_WORKER_START_FAILED",
                                error=redact_text(f"{type(exc).__name__}: {exc}"),
                            )
                        )
                        self._clear_target(generation)
                        return 2

                while not self._stop.is_set():
                    generation = self._adopt_committed_runtime_generation(generation)
                    self._store.write(
                        self._record(
                            RuntimePhase.STARTING,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            child=None,
                            restart_count=restart_count,
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                            restart_history_provenance=restart_history_provenance,
                        )
                    )
                    try:
                        child = self._tunnel.start(
                            profile,
                            env=environment,
                            log_path=self._log_path,
                            correlation_id=correlation_id,
                        )
                    except Exception as exc:
                        restart_count += 1
                        attempted_at = self._clock.now_iso()
                        # Durable across process replacement, unlike the live `RuntimeRecord`:
                        # this ledger is never subject to the pid-liveness self-heal that clears
                        # `self._store`'s record, AND the increment itself is atomic and
                        # idempotent (never lost to an overlapping writer, never double-
                        # counted by a replay) -- see `record_restart()` (#448 Slice 4).
                        recorded = self._record_restart_or_fail_closed(
                            generation=generation,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            reason=f"tunnel failed to start: {type(exc).__name__}: {exc}",
                            restart_count=restart_count,
                            occurred_at=attempted_at,
                            # The OLD, confirmed values -- not this attempt's, which the
                            # ledger write below may fail to durably record (#448 Slice 4).
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                            restart_history_provenance=restart_history_provenance,
                        )
                        if recorded is None:
                            return 0
                        restarts_total = recorded.restarts_total
                        last_restart_at = recorded.last_restart_at
                        restart_history_provenance = "durable"
                        if restart_count > self._max_restarts:
                            # Resident fail-closed, not a nonzero exit (#448 Slice 5): a
                            # `return 2` here is exactly what launchd's
                            # `KeepAlive: {SuccessfulExit: False, Crashed: True}` relaunches,
                            # and the fresh incarnation contends for the same
                            # `runtime-single-instance` lock and can hit this identical
                            # exhaustion again -- diagnostics and jitter make that loop
                            # slower, not bounded. Staying resident (via the same
                            # `_serve_fail_closed` every other fail-closed path already
                            # uses) means launchd never gets a chance to relaunch at all,
                            # and `fail_closed_since` makes this durable: if some OTHER
                            # mechanism ever does relaunch this release,
                            # `_durable_fail_closed` latches the next incarnation straight
                            # into fail-closed too, rather than re-attempting the same
                            # doomed restart cycle.
                            self._store.write(
                                self._record(
                                    RuntimePhase.FAIL_CLOSED,
                                    accepted_generation=generation,
                                    active_generation=None,
                                    profile=profile,
                                    tool_surface_hash=tool_surface_hash,
                                    correlation_id=correlation_id,
                                    incarnation_id=incarnation_id,
                                    child=None,
                                    restart_count=restart_count,
                                    restarts_total=restarts_total,
                                    last_restart_at=last_restart_at,
                                    restart_history_provenance=restart_history_provenance,
                                    error_code="TUNNEL_START_FAILED",
                                    error=redact_text(f"{type(exc).__name__}: {exc}"),
                                    fail_closed_since=self._clock.now_iso(),
                                )
                            )
                            self._clear_target(generation)
                            self._serve_fail_closed(correlation_id, incarnation_id)
                            return 0
                        time.sleep(_restart_backoff_seconds(restart_count, jitter=self._jitter))
                        continue
                    self._child = child
                    healthy, health = self._wait_healthy(generation, child)
                    if not healthy:
                        self._tunnel.terminate(child, grace_seconds=3)
                        self._child = None
                        restart_count += 1
                        attempted_at = self._clock.now_iso()
                        # Atomic and idempotent -- see the identical TUNNEL_START_FAILED
                        # comment above (#448 Slice 4).
                        recorded = self._record_restart_or_fail_closed(
                            generation=generation,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            reason="tunnel/mcp did not become healthy at startup",
                            restart_count=restart_count,
                            occurred_at=attempted_at,
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                            restart_history_provenance=restart_history_provenance,
                        )
                        if recorded is None:
                            return 0
                        restarts_total = recorded.restarts_total
                        last_restart_at = recorded.last_restart_at
                        restart_history_provenance = "durable"
                        if restart_count > self._max_restarts:
                            # Resident fail-closed, not a nonzero exit -- see the identical
                            # TUNNEL_START_FAILED comment above (#448 Slice 5).
                            self._store.write(
                                self._record(
                                    RuntimePhase.FAIL_CLOSED,
                                    accepted_generation=generation,
                                    active_generation=None,
                                    profile=profile,
                                    tool_surface_hash=tool_surface_hash,
                                    correlation_id=correlation_id,
                                    incarnation_id=incarnation_id,
                                    child=None,
                                    restart_count=restart_count,
                                    restarts_total=restarts_total,
                                    last_restart_at=last_restart_at,
                                    restart_history_provenance=restart_history_provenance,
                                    error_code="STARTUP_HEALTH_FAILED",
                                    error="Tunnel/MCP did not become healthy",
                                    health=health,
                                    fail_closed_since=self._clock.now_iso(),
                                )
                            )
                            self._clear_target(generation)
                            self._serve_fail_closed(correlation_id, incarnation_id)
                            return 0
                        time.sleep(_restart_backoff_seconds(restart_count, jitter=self._jitter))
                        continue

                    previous = self._configs.active()
                    try:
                        if previous is None or previous.generation != generation:
                            self._configs.activate(
                                generation,
                                expected_active=previous.generation if previous else None,
                            )
                    except Exception as exc:
                        self._tunnel.terminate(child, grace_seconds=3)
                        self._child = None
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=None,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                incarnation_id=incarnation_id,
                                child=None,
                                restart_count=restart_count,
                                restarts_total=restarts_total,
                                last_restart_at=last_restart_at,
                                restart_history_provenance=restart_history_provenance,
                                error_code="ACTIVE_POINTER_COMMIT_FAILED",
                                error=redact_text(f"{type(exc).__name__}: {exc}"),
                                health=health,
                            )
                        )
                        self._clear_target(generation)
                        return 2
                    self._store.write(
                        self._record(
                            RuntimePhase.HEALTHY,
                            accepted_generation=generation,
                            active_generation=generation,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            child=child,
                            restart_count=restart_count,
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                            restart_history_provenance=restart_history_provenance,
                            health=health,
                        )
                    )

                    consecutive_health_failures = 0
                    stable_since = time.monotonic()
                    while not self._stop.is_set() and self._tunnel.is_alive(child):
                        time.sleep(self._watchdog_interval)
                        generation = self._adopt_committed_runtime_generation(generation)
                        try:
                            self._ensure_execution_worker(
                                generation,
                                environment=environment,
                                correlation_id=correlation_id,
                            )
                        except ExecutionWorkerRegistrationError as exc:
                            # Same disposition as startup: a worker that cannot be
                            # durably registered is a fail-closed condition, never a
                            # silent blip. Swallowing it here would let the runtime
                            # keep serving while an unregistered (possibly running)
                            # worker holds locks it can never be proven to own, and
                            # the typed failure would vanish from the record.
                            self._store.write(
                                self._record(
                                    RuntimePhase.FAIL_CLOSED,
                                    accepted_generation=generation,
                                    active_generation=None,
                                    profile=profile,
                                    tool_surface_hash=tool_surface_hash,
                                    correlation_id=correlation_id,
                                    incarnation_id=incarnation_id,
                                    child=None,
                                    error_code="EXECUTION_WORKER_REGISTRATION_FAILED",
                                    error=redact_text(str(exc)),
                                    fail_closed_since=self._clock.now_iso(),
                                )
                            )
                            self._tunnel.terminate(child, grace_seconds=3)
                            self._child = None
                            self._clear_target(generation)
                            self._serve_fail_closed(correlation_id, incarnation_id)
                            return 0
                        except Exception:
                            # Only pre-spawn failures land here: the adapter converts
                            # every failure after Popen into
                            # ExecutionWorkerRegistrationError (which fails closed
                            # above) and reaps the child, so this branch never
                            # swallows a live, untraceable worker.
                            pass
                        observed_ok, observed_health = self._observe_health(generation, child)
                        # Best-effort (#448 Slice 6 review): this is a status-update read,
                        # not a decision input -- a failure here must degrade the durable
                        # record we'd otherwise refresh this iteration, never crash the
                        # watchdog thread or exit the process.
                        try:
                            current = self._store.read()
                        except Exception:
                            current = None
                        # A failing "runtime_state_read" check means the durable store is
                        # unreadable, NOT that the tunnel child or the service it runs is
                        # unhealthy (#448 Slice 6 review) -- restarting a genuinely healthy
                        # child because observability/persistence hiccuped is exactly the
                        # amplification this distinction exists to prevent. Only a failure
                        # in some OTHER check may count toward the restart streak or
                        # terminate the child.
                        service_ok = all(
                            ok
                            for name, ok, _detail in observed_health
                            if name != _RUNTIME_STATE_READ_CHECK
                        )
                        if observed_ok:
                            consecutive_health_failures = 0
                            if time.monotonic() - stable_since >= self._stable_health_reset:
                                restart_count = 0
                            if current is not None and (
                                current.phase is RuntimePhase.DEGRADED
                                or current.health != observed_health
                                or current.restart_count != restart_count
                            ):
                                self._store.write(
                                    replace(
                                        current,
                                        phase=RuntimePhase.HEALTHY,
                                        active_generation=generation,
                                        accepted_generation=generation,
                                        restart_count=restart_count,
                                        restarts_total=restarts_total,
                                        last_restart_at=last_restart_at,
                                        restart_history_provenance=restart_history_provenance,
                                        health=observed_health,
                                        health_observed_at=self._clock.now_iso(),
                                        consecutive_health_failures=0,
                                        last_error_code=None,
                                        last_error=None,
                                        updated_at=self._clock.now_iso(),
                                    )
                                )
                            continue
                        if service_ok:
                            # Observability-only failure: the tunnel child and the
                            # service it runs are fine, only the durable read failed.
                            # Degrade the record for visibility, but never touch the
                            # restart streak and never terminate a healthy child.
                            if current is not None:
                                self._store.write(
                                    replace(
                                        current,
                                        phase=RuntimePhase.DEGRADED,
                                        health=observed_health,
                                        health_observed_at=self._clock.now_iso(),
                                        last_error_code="RUNTIME_STATE_READ_FAILED",
                                        last_error=(
                                            "Runtime-state read failed during health "
                                            "observation; the tunnel child itself is healthy"
                                        ),
                                        updated_at=self._clock.now_iso(),
                                    )
                                )
                            continue
                        consecutive_health_failures += 1
                        stable_since = time.monotonic()
                        if current is not None:
                            self._store.write(
                                replace(
                                    current,
                                    phase=RuntimePhase.DEGRADED,
                                    health=observed_health,
                                    health_observed_at=self._clock.now_iso(),
                                    consecutive_health_failures=consecutive_health_failures,
                                    last_error_code="WATCHDOG_HEALTH_DEGRADED",
                                    last_error="A live tunnel child failed active runtime health probes",
                                    updated_at=self._clock.now_iso(),
                                )
                            )
                        if consecutive_health_failures < self._health_failure_threshold:
                            continue
                        self._tunnel.terminate(child, grace_seconds=3)
                        break
                    if self._stop.is_set():
                        break
                    self._child = None
                    generation = self._adopt_committed_runtime_generation(generation)
                    restart_count += 1
                    attempted_at = self._clock.now_iso()
                    # Atomic and idempotent -- see the identical TUNNEL_START_FAILED
                    # comment above (#448 Slice 4).
                    recorded = self._record_restart_or_fail_closed(
                        generation=generation,
                        profile=profile,
                        tool_surface_hash=tool_surface_hash,
                        correlation_id=correlation_id,
                        incarnation_id=incarnation_id,
                        reason="watchdog observed a live but unhealthy tunnel child",
                        restart_count=restart_count,
                        occurred_at=attempted_at,
                        restarts_total=restarts_total,
                        last_restart_at=last_restart_at,
                        restart_history_provenance=restart_history_provenance,
                    )
                    if recorded is None:
                        return 0
                    restarts_total = recorded.restarts_total
                    last_restart_at = recorded.last_restart_at
                    restart_history_provenance = "durable"
                    if restart_count > self._max_restarts:
                        # Resident fail-closed, not a nonzero exit -- see the identical
                        # TUNNEL_START_FAILED comment above (#448 Slice 5). This is the
                        # exact Signature A/B chain #448 was opened for: a remote outage
                        # exhausting the tunnel-child restart budget used to `return 2`
                        # here, which is precisely what let launchd's `KeepAlive` turn one
                        # remote 5xx episode into an unbounded respawn loop.
                        self._store.write(
                            self._record(
                                RuntimePhase.FAIL_CLOSED,
                                accepted_generation=generation,
                                active_generation=generation,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                incarnation_id=incarnation_id,
                                child=None,
                                restart_count=restart_count,
                                restarts_total=restarts_total,
                                last_restart_at=last_restart_at,
                                restart_history_provenance=restart_history_provenance,
                                error_code="RESTART_LIMIT",
                                error="Tunnel child exceeded bounded restart policy",
                                fail_closed_since=self._clock.now_iso(),
                            )
                        )
                        self._clear_target(generation)
                        self._serve_fail_closed(correlation_id, incarnation_id)
                        return 0
                    try:
                        doctor_ok, doctor_detail = self._tunnel.doctor(profile, env=environment)
                    except Exception as exc:
                        doctor_ok, doctor_detail = (
                            False,
                            redact_text(f"{type(exc).__name__}: {exc}"),
                        )
                    if not doctor_ok:
                        self._store.write(
                            self._record(
                                RuntimePhase.FAILED,
                                accepted_generation=generation,
                                active_generation=generation,
                                profile=profile,
                                tool_surface_hash=tool_surface_hash,
                                correlation_id=correlation_id,
                                incarnation_id=incarnation_id,
                                child=None,
                                restart_count=restart_count,
                                restarts_total=restarts_total,
                                last_restart_at=last_restart_at,
                                restart_history_provenance=restart_history_provenance,
                                error_code="NON_RETRYABLE_DOCTOR_FAILURE",
                                error=redact_text(doctor_detail),
                            )
                        )
                        self._clear_target(generation)
                        return 2
                    time.sleep(_restart_backoff_seconds(restart_count, jitter=self._jitter))

                # Best-effort (#448 Slice 6 review): a graceful shutdown must persist
                # cleanly through a healthy exit even if the store happens to be
                # unreadable at this exact moment -- never crash into the top-level
                # handler's `return 2` over a transition write that is advisory anyway.
                try:
                    current = self._store.read()
                except Exception:
                    current = None
                if current and current.phase not in {RuntimePhase.STOPPED, RuntimePhase.FAILED}:
                    with contextlib.suppress(Exception):
                        self._store.write(
                            transition(
                                current,
                                RuntimePhase.STOPPING,
                                updated_at=self._clock.now_iso(),
                                correlation_id=correlation_id,
                            )
                        )
                if self._child and self._tunnel.is_alive(self._child):
                    self._tunnel.terminate(self._child, grace_seconds=15)
                self._child = None
                if (
                    self._execution_worker is not None
                    and self._execution_child is not None
                    and self._execution_worker.is_alive(self._execution_child)
                ):
                    self._execution_worker.terminate(self._execution_child, grace_seconds=15)
                self._execution_child = None
                self._execution_generation = None
                # Same best-effort disposition as the STOPPING read above -- the
                # process is exiting cleanly regardless of whether this final durable
                # write succeeds.
                try:
                    current = self._store.read()
                except Exception:
                    current = None
                if current:
                    with contextlib.suppress(Exception):
                        self._store.write(
                            replace(
                                current,
                                phase=RuntimePhase.STOPPED,
                                pid=None,
                                process_identity=None,
                                child_pid=None,
                                child_process_identity=None,
                                active_generation=None,
                                updated_at=self._clock.now_iso(),
                            )
                        )
                return 0
            except Exception as exc:
                if self._child and self._tunnel.is_alive(self._child):
                    self._tunnel.terminate(self._child, grace_seconds=3)
                self._child = None
                if (
                    self._execution_worker is not None
                    and self._execution_child is not None
                    and self._execution_worker.is_alive(self._execution_child)
                ):
                    self._execution_worker.terminate(self._execution_child, grace_seconds=3)
                self._execution_child = None
                self._execution_generation = None
                with contextlib.suppress(Exception):
                    self._store.write(
                        self._record(
                            RuntimePhase.FAILED,
                            accepted_generation=generation,
                            active_generation=None,
                            profile=profile,
                            tool_surface_hash=tool_surface_hash,
                            correlation_id=correlation_id,
                            incarnation_id=incarnation_id,
                            child=None,
                            restart_count=restart_count,
                            restarts_total=restarts_total,
                            last_restart_at=last_restart_at,
                            restart_history_provenance=restart_history_provenance,
                            error_code="SUPERVISOR_FAILURE",
                            error=redact_text(f"{type(exc).__name__}: {exc}"),
                        )
                    )
                self._clear_target(generation)
                return 2
            finally:
                self._control.close()
                if threading.current_thread() is threading.main_thread():
                    for signum, handler in previous_handlers.items():
                        signal.signal(signum, handler)  # type: ignore[arg-type]
