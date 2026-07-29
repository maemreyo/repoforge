"""One misbehaving client must not disable the control plane (#322).

The Unix control server served one connection at a time with no read deadline and an
unguarded write, which gave a single client two ways to take the whole control plane down:

* connect and stay silent -- `accept()` returns a BLOCKING socket regardless of the
  listener's timeout, so `recv` never returned and the loop never accepted anyone else;
* then disconnect -- `recv` returned `b""`, the loop fell through to `sendall` on a closed
  peer, and the resulting `BrokenPipeError` propagated out of the serve function, killing
  the thread.

The second is what made the live incident so misleading: the supervisor process stayed
alive and its watchdog kept recording `phase: healthy`, while every read that needed the
control socket timed out and both recovery commands -- `rf runtime restart` and
`rf runtime stop` -- were themselves routed through the dead control plane.

These tests use a temporary socket. The failure was originally reproduced against a live
production runtime, which wedged it and cost a second forced restart; there was never a
reason to do that here.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from pathlib import Path

from repoforge.adapters.runtime.unix_control import (
    UnixRuntimeControlServer,
    resolve_unix_socket_path,
)
from repoforge.domain.runtime import ControlRequest, ControlResponse


def _handler(request: ControlRequest) -> ControlResponse:
    return ControlResponse(1, True, request.correlation_id, "alive")


def _server(tmp_path: Path) -> UnixRuntimeControlServer:
    server = UnixRuntimeControlServer(tmp_path / "control.sock")
    server.start(_handler)
    return server


def _ping(path: Path, *, timeout: float = 4.0) -> dict[str, object] | None:
    """Send one PING; return the decoded reply, or None when nothing answered."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall(
            json.dumps(
                {
                    "protocol_version": 1,
                    "command": "ping",
                    "correlation_id": "a" * 24,
                    "payload": {},
                }
            ).encode()
            + b"\n"
        )
        raw = client.recv(8192)
        return dict(json.loads(raw.decode())) if raw else None
    except (TimeoutError, OSError):
        return None
    finally:
        client.close()


def test_a_silent_client_does_not_block_another_client(tmp_path: Path) -> None:
    """The wedge: one connection that sends nothing used to own the control plane."""
    server = _server(tmp_path)
    path = resolve_unix_socket_path(tmp_path / "control.sock")
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        silent.connect(str(path))  # connect, send nothing, hold it open

        # Long enough that the old code -- blocked in an unbounded recv -- could not have
        # answered, and short enough that a bounded exchange has to.
        reply = _ping(path, timeout=12.0)

        assert reply is not None, "a silent client wedged the control plane"
        assert reply["ok"] is True
    finally:
        silent.close()
        server.close()


def test_a_client_that_disconnects_mid_request_does_not_kill_the_server(
    tmp_path: Path,
) -> None:
    """The thread death: a closed peer made `sendall` raise out of the serve loop."""
    server = _server(tmp_path)
    path = resolve_unix_socket_path(tmp_path / "control.sock")
    try:
        for _ in range(3):
            rude = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            rude.connect(str(path))
            # A partial request with no newline, then an abrupt close.
            rude.sendall(b'{"protocol_version": 1, "command": "pi')
            rude.close()
            time.sleep(0.05)

        reply = _ping(path)

        assert reply is not None, "a disconnecting client killed the control server"
        assert reply["ok"] is True
    finally:
        server.close()


def test_the_serve_thread_survives_every_kind_of_peer(tmp_path: Path) -> None:
    """After a mix of abuse, the server is still the same live thread -- not restarted."""
    server = _server(tmp_path)
    path = resolve_unix_socket_path(tmp_path / "control.sock")
    thread = server._thread
    assert thread is not None
    try:
        # Connect and close immediately, sending nothing at all.
        for _ in range(3):
            noop = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            noop.connect(str(path))
            noop.close()
        # Send a complete but unparseable request.
        garbage = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        garbage.connect(str(path))
        garbage.sendall(b"not json at all\n")
        garbage.recv(8192)
        garbage.close()

        assert thread.is_alive(), "the serve thread died on a malformed peer"
        assert _ping(path) is not None
    finally:
        server.close()


def test_a_slow_client_is_bounded_rather_than_waited_on_forever(tmp_path: Path) -> None:
    """A client holding a connection open past the deadline is dropped, not served."""
    server = _server(tmp_path)
    path = resolve_unix_socket_path(tmp_path / "control.sock")
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.settimeout(20.0)
    try:
        slow.connect(str(path))
        started = time.monotonic()
        # The server closes this exchange once its deadline passes; the client observes
        # either an empty read or a reset -- what matters is that it does not hang forever.
        with contextlib.suppress(OSError):
            slow.recv(4096)
        elapsed = time.monotonic() - started

        assert elapsed < 20.0, "the server never bounded a silent exchange"
        # And the control plane is still serving afterwards.
        assert _ping(path) is not None
    finally:
        slow.close()
        server.close()


def test_no_client_is_starved_when_several_arrive_at_once(tmp_path: Path) -> None:
    """Several clients at once: the queue drains rather than stalling on the first.

    Deliberately generous with time and modest in count. The property under test is
    starvation -- a client that is never answered -- not latency: the server serves
    sequentially by design, and this suite runs on machines where a concurrent build can
    take most of the CPU. An earlier version used ten clients and a 15s budget, and failed
    exactly that way while an unrelated 25-minute verification saturated the machine.
    """
    server = _server(tmp_path)
    path = resolve_unix_socket_path(tmp_path / "control.sock")
    answered: list[bool] = []
    lock = threading.Lock()

    def ask() -> None:
        reply = _ping(path, timeout=45.0)
        with lock:
            answered.append(reply is not None and reply["ok"] is True)

    try:
        threads = [threading.Thread(target=ask) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60.0)

        assert len(answered) == 5, f"only {len(answered)} of 5 clients returned at all"
        assert all(answered), f"{answered.count(False)} of 5 clients were never answered"
    finally:
        server.close()


# ---------------- health must not claim healthy without a control plane (#322)


def test_a_stopped_control_server_reports_that_it_is_not_serving(tmp_path: Path) -> None:
    """The liveness the health record needs, at the boundary that owns it."""
    server = _server(tmp_path)
    try:
        assert server.is_serving() is True
        assert "accepting" in server.serving_diagnostic()
    finally:
        server.close()

    assert server.is_serving() is False
    assert server.serving_diagnostic() != ""


def test_a_never_started_server_is_not_serving(tmp_path: Path) -> None:
    server = UnixRuntimeControlServer(tmp_path / "unused.sock")

    assert server.is_serving() is False
    assert "never started" in server.serving_diagnostic()


def test_a_dead_serve_thread_is_reported_rather_than_assumed_alive(tmp_path: Path) -> None:
    """The state the live incident was in: process alive, control plane gone.

    Simulated by stopping the loop from underneath, because after this change no peer can
    kill it. What matters is that liveness is *observed* rather than inferred from the
    process still running -- the health record is written by a loop that survives this one.
    """
    server = _server(tmp_path)
    try:
        thread = server._thread
        assert thread is not None
        server._stop.set()
        thread.join(timeout=5.0)

        assert thread.is_alive() is False
        assert server.is_serving() is False
        assert server.serving_diagnostic() != "control socket is accepting requests"
    finally:
        server.close()


def test_runtime_health_fails_when_the_control_plane_is_not_serving(tmp_path: Path) -> None:
    """A supervisor must not publish `healthy` while its control plane is gone.

    This is the state the live incident was in: the process alive, the watchdog loop still
    recording `phase: healthy`, and every read that needed the control socket timing out.
    The health record is written by a loop that survives the control loop, so the fact has
    to be observed rather than inferred from the process still running.
    """
    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess
    from repoforge.domain.runtime import ControlResponse as _Response
    from repoforge.testing import FixedClock, SequenceIdGenerator

    class _DeadControl:
        def start(self, handler: object) -> None:
            return None

        def close(self) -> None:
            return None

        def is_serving(self) -> bool:
            return False

        def serving_diagnostic(self) -> str:
            return "control server thread is no longer running"

    class _Mcp:
        def request(self, request: object, *, timeout_seconds: float = 10.0) -> _Response:
            return _Response(1, True, "c" * 24, "healthy")

    class _Tunnel:
        def is_alive(self, child: object) -> bool:
            return True

    class _Store:
        def read(self) -> None:
            return None

        def write(self, record: object) -> None:
            return None

        def clear(self, *, expected_pid: int | None = None) -> None:
            return None

    class _Configs:
        def active(self) -> None:
            return None

        def clear_activation_target(self, *, expected_generation: int | None = None) -> None:
            return None

    class _Locks:
        def lock(self, name: str, **kwargs: object) -> contextlib.AbstractContextManager[None]:
            return contextlib.nullcontext()

        def path_for(self, name: str) -> Path:
            return tmp_path / name

    class _Processes:
        def identity(self, pid: int) -> str:
            return "f" * 64

    supervisor = RuntimeSupervisor(
        store=_Store(),  # type: ignore[arg-type]
        configs=_Configs(),  # type: ignore[arg-type]
        locks=_Locks(),  # type: ignore[arg-type]
        control=_DeadControl(),  # type: ignore[arg-type]
        mcp_control=_Mcp(),  # type: ignore[arg-type]
        tunnel=_Tunnel(),  # type: ignore[arg-type]
        profile_store=None,  # type: ignore[arg-type]
        clock=FixedClock("2026-07-28T00:00:00+00:00"),
        ids=SequenceIdGenerator(("health",)),
        processes=_Processes(),  # type: ignore[arg-type]
        mcp_runtime_path=tmp_path / "runtime.json",
        log_path=tmp_path / "runtime.log",
    )

    healthy, checks = supervisor._observe_health(1, ChildProcess(222, "f" * 64, "now"))

    named = {name: (ok, detail) for name, ok, detail in checks}
    assert named["control_plane"][0] is False
    assert "no longer running" in named["control_plane"][1]
    assert healthy is False, "a runtime with no control plane reported itself healthy"


def test_restart_evidence_survives_the_stability_reset_that_clears_restart_count() -> None:
    """`restart_count` is policy, not evidence.

    It resets after `stable_health_reset_seconds` of calm, so sixty seconds after the last
    restart the record reads `0`. During the 2026-07-28 incident the connector was torn
    down twice in twelve minutes and the record afterwards said `healthy, 0 restarts` --
    every durable fact insisting nothing had happened, which is what sent diagnosis to the
    raw tunnel log. `restarts_total` and `last_restart_at` are never reset.
    """
    from dataclasses import replace as dataclass_replace

    from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

    after_two_restarts = RuntimeRecord(
        protocol_version=1,
        phase=RuntimePhase.HEALTHY,
        pid=100,
        process_identity="a" * 64,
        active_generation=12,
        accepted_generation=12,
        tunnel_profile="repoforge",
        tunnel_profile_fingerprint="b" * 64,
        tool_surface_hash="c" * 64,
        started_at="2026-07-28T17:16:17+00:00",
        updated_at="2026-07-28T17:16:17+00:00",
        correlation_id="d" * 24,
        child_pid=200,
        child_process_identity="e" * 64,
        restart_count=1,
        restarts_total=2,
        last_restart_at="2026-07-28T17:15:55+00:00",
    )

    settled = dataclass_replace(
        after_two_restarts,
        restart_count=0,
        updated_at="2026-07-28T17:17:18+00:00",
    )

    assert settled.restart_count == 0
    assert settled.restarts_total == 2
    assert settled.last_restart_at == "2026-07-28T17:15:55+00:00"


def test_a_record_from_before_the_evidence_fields_is_accepted_as_is() -> None:
    """`restarts_total` is deliberately NOT required to be at least `restart_count`.

    That invariant looked reasonable and made the runtime unstartable: a record written
    before these fields existed decodes with `restarts_total = 0` beside whatever
    `restart_count` it carried, and rejecting it left no release to roll back to, because
    every release since the field landed shared the decoder. `0` there is honest -- nothing
    was counting then -- so the record is accepted and says so.
    """
    from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

    record = RuntimeRecord(
        protocol_version=1,
        phase=RuntimePhase.DEGRADED,
        pid=None,
        process_identity=None,
        active_generation=None,
        accepted_generation=12,
        tunnel_profile="repoforge",
        tunnel_profile_fingerprint="b" * 64,
        tool_surface_hash="c" * 64,
        started_at=None,
        updated_at="2026-07-28T17:17:18+00:00",
        correlation_id="d" * 24,
        restart_count=3,
        restarts_total=0,
    )

    assert record.restart_count == 3
    assert record.restarts_total == 0
