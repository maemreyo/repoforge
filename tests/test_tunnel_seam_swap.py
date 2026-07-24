"""Tests for #269: near-zero-downtime serve-child swap at the tunnel-client seam."""

from __future__ import annotations

from pathlib import Path

from repoforge.application.activation.handoff import GenerationHandoffReconciler, OwnerIdentity
from repoforge.application.activation.seam import TunnelSeamSwapCoordinator
from repoforge.domain.operation_worker import OperationWorkerBinding
from repoforge.domain.runtime import ChildProcess, HealthCheck, TunnelProfile
from repoforge.ports.process_reaper import ReapOutcome
from repoforge.testing.fakes import InMemoryWorkerBindingStore, RecordingProcessReaper

_SHA = "a" * 64
_HEALTHY = (HealthCheck(name="tunnel", ok=True, detail="ok"),)
_UNHEALTHY = (HealthCheck(name="tunnel", ok=False, detail="down"),)


def _profile(name: str) -> TunnelProfile:
    return TunnelProfile(
        tunnel_id_fingerprint=_SHA,
        profile=name,
        executable="rf",
        executable_version="2.2.0",
        mcp_argv=("rf", "serve"),
    )


def _child(pid: int) -> ChildProcess:
    return ChildProcess(pid=pid, process_identity=_SHA, started_at="2026-07-25T00:00:00+00:00")


class _Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _FakeTunnel:
    """Records lifecycle calls and returns scripted health for the candidate child."""

    def __init__(
        self, *, candidate_pid: int, health_sequence: list[tuple[HealthCheck, ...]]
    ) -> None:
        self._candidate_pid = candidate_pid
        self._health_sequence = health_sequence
        self._health_calls = 0
        self.started: list[TunnelProfile] = []
        self.terminated: list[int] = []
        self.alive = True

    def start(self, profile: TunnelProfile, *, env: dict[str, str], log_path: Path) -> ChildProcess:
        self.started.append(profile)
        return _child(self._candidate_pid)

    def terminate(self, child: ChildProcess, *, grace_seconds: float) -> None:
        self.terminated.append(child.pid)

    def is_alive(self, child: ChildProcess) -> bool:
        return self.alive

    def health(self, child: ChildProcess, *, timeout_seconds: float) -> tuple[HealthCheck, ...]:
        index = min(self._health_calls, len(self._health_sequence) - 1)
        self._health_calls += 1
        return self._health_sequence[index]


def _binding(op: str, *, server_pid: int, token: str) -> OperationWorkerBinding:
    return OperationWorkerBinding(
        operation_id=op,
        child_pid=4321,
        child_pgid=4321,
        child_start_token="tok-child",
        server_pid=server_pid,
        server_start_token=token,
        created_at="2026-07-25T00:00:00+00:00",
    )


class _Control:
    """Control-channel fake; `ok` drives whether DRAIN is granted."""

    def __init__(self, *, ok: bool = True, raises: bool = False) -> None:
        self._ok = ok
        self._raises = raises
        self.commands: list[str] = []

    def request(self, request, *, timeout_seconds: float = 10.0):
        from repoforge.domain.errors import ConfigError
        from repoforge.domain.runtime import ControlResponse

        self.commands.append(request.command.value)
        if self._raises:
            raise ConfigError("socket unreachable")
        return ControlResponse(
            1,
            self._ok,
            request.correlation_id,
            "drained" if self._ok else "drain_timeout",
            error_code=None if self._ok else "RUNTIME_DRAIN_TIMEOUT",
        )


class _Ids:
    def new_hex(self, length: int = 10) -> str:
        return "c" * length


def _coordinator(
    tunnel: _FakeTunnel,
    store: InMemoryWorkerBindingStore,
    *,
    control: _Control | None = None,
    reaper: RecordingProcessReaper | None = None,
) -> TunnelSeamSwapCoordinator:
    reconciler = GenerationHandoffReconciler(
        bindings=store, reaper=reaper or RecordingProcessReaper()
    )
    return TunnelSeamSwapCoordinator(
        tunnel=tunnel,
        reconciler=reconciler,
        sleeper=_Sleeper(),
        control=control if control is not None else _Control(),
        ids=_Ids(),
        health_attempts=3,
        health_interval_seconds=0.1,
    )


def _swap(coordinator: TunnelSeamSwapCoordinator, *, old_surface: str, new_surface: str):
    return coordinator.swap(
        old_child=_child(1000),
        candidate_profile=_profile("candidate"),
        env={},
        log_path=Path("/tmp/x.log"),
        current_owner=OwnerIdentity(server_pid=2000, server_start_token="srv-NEW"),
        old_surface_hash=old_surface,
        new_surface_hash=new_surface,
    )


def test_healthy_candidate_swaps_and_retires_the_old_child() -> None:
    store = InMemoryWorkerBindingStore()
    # A prior-generation binding must be reconciled during the swap.
    store.put(_binding("op-" + "0".rjust(24, "0"), server_pid=1000, token="srv-OLD"))
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    coordinator = _coordinator(tunnel, store)

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "swapped"
    assert result.candidate_pid == 2000
    assert tunnel.terminated == [1000]  # only the OLD child retired
    assert result.handoff is not None
    assert result.handoff.reaped  # prior-gen binding was reconciled


def test_unhealthy_candidate_aborts_without_touching_the_old_child() -> None:
    store = InMemoryWorkerBindingStore()
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_UNHEALTHY, _UNHEALTHY, _UNHEALTHY])
    coordinator = _coordinator(tunnel, store)

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "aborted"
    # Fail-safe: the candidate is retired, the OLD child is never terminated -> no downtime.
    assert tunnel.terminated == [2000]
    assert result.handoff is None


def test_candidate_that_exits_before_health_aborts() -> None:
    store = InMemoryWorkerBindingStore()
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    tunnel.alive = False
    coordinator = _coordinator(tunnel, store)

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "aborted"
    assert "exited" in result.detail


def test_rediscovery_flagged_only_when_the_tool_surface_changes() -> None:
    store = InMemoryWorkerBindingStore()
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    coordinator = _coordinator(tunnel, store)
    unchanged = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)
    assert unchanged.rediscovery_required is False

    tunnel2 = _FakeTunnel(candidate_pid=2001, health_sequence=[_HEALTHY])
    coordinator2 = _coordinator(tunnel2, InMemoryWorkerBindingStore())
    changed = _swap(coordinator2, old_surface=_SHA, new_surface="b" * 64)
    assert changed.rediscovery_required is True


def test_drain_is_requested_before_the_old_child_is_retired() -> None:
    store = InMemoryWorkerBindingStore()
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    control = _Control()
    coordinator = _coordinator(tunnel, store, control=control)

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "swapped"
    assert control.commands == ["drain"]
    assert tunnel.terminated == [1000]


def test_a_refused_drain_aborts_instead_of_cutting_in_flight_work() -> None:
    """Fail closed: never terminate a child that did not drain."""
    store = InMemoryWorkerBindingStore()
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    coordinator = _coordinator(tunnel, store, control=_Control(ok=False))

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "aborted"
    assert "did not drain" in result.detail
    # The old child keeps serving; only the candidate is retired.
    assert tunnel.terminated == [2000]


def test_an_unreachable_drain_channel_aborts() -> None:
    store = InMemoryWorkerBindingStore()
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    coordinator = _coordinator(tunnel, store, control=_Control(raises=True))

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "aborted"
    assert tunnel.terminated == [2000]


def test_a_failed_handoff_aborts_the_swap_and_keeps_the_old_child() -> None:
    """If a prior worker survived, the candidate must not take over its work."""
    store = InMemoryWorkerBindingStore()
    store.put(_binding("op-" + "0".rjust(24, "0"), server_pid=1000, token="srv-OLD"))
    tunnel = _FakeTunnel(candidate_pid=2000, health_sequence=[_HEALTHY])
    survivor = RecordingProcessReaper(
        outcome=ReapOutcome(
            attempted=True, reaped=False, still_alive=True, detail="survived SIGKILL"
        )
    )
    coordinator = _coordinator(tunnel, store, reaper=survivor)

    result = _swap(coordinator, old_surface=_SHA, new_surface=_SHA)

    assert result.status == "aborted"
    assert "ownership" in result.detail
    assert tunnel.terminated == [2000]  # old child retained
    assert result.handoff is not None and result.handoff.ok is False
