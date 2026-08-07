"""Bounded concurrency for the Unix runtime control socket (#448 Slice 1).

Signature C's core defect was a single-threaded accept loop running an
expensive handler inline: one caller could block every other caller behind
it in the kernel backlog. These tests prove four invariants the corrected
server must hold, using `Event`/`Barrier` for deterministic synchronization
-- never `time.sleep()` to guess at timing.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from repoforge.adapters.runtime import (
    UnixRuntimeControlClient,
    UnixRuntimeControlServer,
)
from repoforge.domain.errors import ConfigError
from repoforge.domain.runtime import ControlCommand, ControlRequest, ControlResponse

_BOUND_SECONDS = 5.0


def test_a_blocked_handler_does_not_prevent_a_second_client_from_being_served(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path, max_concurrent_requests=4)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_completed = threading.Event()

    def handler(request: ControlRequest) -> ControlResponse:
        if request.correlation_id == "first":
            first_entered.set()
            # The invariant under test: this wait must not stall the second
            # client's own request/response cycle, which happens entirely
            # while this handler is still blocked here.
            release_first.wait(timeout=_BOUND_SECONDS)
        return ControlResponse(1, True, request.correlation_id, "ok")

    server.start(handler)
    try:
        first_thread = threading.Thread(
            target=lambda: UnixRuntimeControlClient(path).request(
                ControlRequest(1, ControlCommand.PING, "first"), timeout_seconds=_BOUND_SECONDS
            )
        )
        first_thread.start()
        assert first_entered.wait(timeout=_BOUND_SECONDS), "first handler never entered"

        second = UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "second"), timeout_seconds=_BOUND_SECONDS
        )
        second_completed.set()
        assert second.ok
        assert second.correlation_id == "second"

        release_first.set()
        first_thread.join(timeout=_BOUND_SECONDS)
        assert not first_thread.is_alive()
    finally:
        release_first.set()
        server.close()


def test_concurrent_handlers_never_exceed_the_configured_limit(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    limit = 3
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    lock = threading.Lock()
    current = 0
    observed_max = 0
    release = threading.Event()

    def handler(request: ControlRequest) -> ControlResponse:
        nonlocal current, observed_max
        with lock:
            current += 1
            observed_max = max(observed_max, current)
        release.wait(timeout=_BOUND_SECONDS)
        with lock:
            current -= 1
        return ControlResponse(1, True, request.correlation_id, "ok")

    server.start(handler)
    try:
        client_count = limit + 3
        threads = [
            threading.Thread(
                target=lambda i=i: UnixRuntimeControlClient(path).request(
                    ControlRequest(1, ControlCommand.PING, f"c{i}"), timeout_seconds=_BOUND_SECONDS
                )
            )
            for i in range(client_count)
        ]
        for t in threads:
            t.start()
        # Wait for the pool to actually fill to capacity, not just for one handler to
        # start -- a fully serialized (broken) implementation would otherwise satisfy
        # `observed_max <= limit` trivially at `observed_max == 1` and this test would
        # pass without ever proving concurrent handling happens at all.
        deadline = time.monotonic() + _BOUND_SECONDS
        while time.monotonic() < deadline:
            with lock:
                if current >= limit:
                    break
            time.sleep(0.01)

        release.set()
        for t in threads:
            t.join(timeout=_BOUND_SECONDS)
            assert not t.is_alive()

        assert observed_max == limit, (
            f"observed {observed_max} concurrent handlers, expected exactly {limit} -- "
            "either concurrency is broken (degenerated to serial execution) or the "
            "admission limit is not being enforced"
        )
    finally:
        release.set()
        server.close()


def test_capacity_exhaustion_returns_a_fast_typed_overload_response(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    release = threading.Event()
    admitted = threading.Barrier(limit + 1, timeout=_BOUND_SECONDS)

    def handler(request: ControlRequest) -> ControlResponse:
        admitted.wait(timeout=_BOUND_SECONDS)
        release.wait(timeout=_BOUND_SECONDS)
        return ControlResponse(1, True, request.correlation_id, "ok")

    server.start(handler)
    try:
        occupiers = [
            threading.Thread(
                target=lambda i=i: UnixRuntimeControlClient(path).request(
                    ControlRequest(1, ControlCommand.PING, f"occupy{i}"),
                    timeout_seconds=_BOUND_SECONDS,
                )
            )
            for i in range(limit)
        ]
        for t in occupiers:
            t.start()
        admitted.wait(timeout=_BOUND_SECONDS)  # every occupier handler is now running

        started = time.monotonic()
        overloaded = UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "overflow"), timeout_seconds=_BOUND_SECONDS
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, (
            f"overload response took {elapsed:.2f}s -- it must reject immediately, "
            "not wait in the backlog"
        )
        assert not overloaded.ok
        assert overloaded.error_code == "RUNTIME_CONTROL_OVERLOADED"
        overload_payload = dict(overloaded.payload)
        assert overload_payload.get("retryable") is True
        assert overload_payload.get("capacity") == limit
        assert overload_payload.get("in_flight") == limit

        release.set()
        for t in occupiers:
            t.join(timeout=_BOUND_SECONDS)
            assert not t.is_alive()
    finally:
        release.set()
        server.close()


def test_admission_permits_are_released_so_capacity_recovers_after_handlers_finish(
    tmp_path: Path,
) -> None:
    """Fill capacity, release it, then fill it again to the same limit.

    If a permit were ever leaked (not released after a handler completes), this
    second round would find fewer than `limit` slots available -- proving
    sequential reuse, not just that a single round respects the limit.
    """
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)

    # `server.start()` takes one fixed handler, so both rounds share a single
    # dispatching handler that reads whichever barrier/event is active for the
    # current round from a mutable holder -- simpler than restarting the server.
    active_barrier: dict[str, threading.Barrier] = {}
    active_release: dict[str, threading.Event] = {}

    def dispatch(request: ControlRequest) -> ControlResponse:
        active_barrier["barrier"].wait(timeout=_BOUND_SECONDS)
        active_release["release"].wait(timeout=_BOUND_SECONDS)
        return ControlResponse(1, True, request.correlation_id, "ok")

    server.start(dispatch)
    try:
        for round_id in ("round1", "round2"):
            active_barrier["barrier"] = threading.Barrier(limit + 1, timeout=_BOUND_SECONDS)
            active_release["release"] = threading.Event()
            occupiers = [
                threading.Thread(
                    target=lambda i=i, round_id=round_id: UnixRuntimeControlClient(path).request(
                        ControlRequest(1, ControlCommand.PING, f"{round_id}-{i}"),
                        timeout_seconds=_BOUND_SECONDS,
                    )
                )
                for i in range(limit)
            ]
            for t in occupiers:
                t.start()
            active_barrier["barrier"].wait(timeout=_BOUND_SECONDS)
            active_release["release"].set()
            for t in occupiers:
                t.join(timeout=_BOUND_SECONDS)
                assert not t.is_alive()
    finally:
        for release in active_release.values():
            release.set()
        server.close()


def test_shutdown_stops_new_admission_and_joins_in_flight_handlers_within_a_bound(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path, max_concurrent_requests=4)
    entered = threading.Event()
    release = threading.Event()

    def handler(request: ControlRequest) -> ControlResponse:
        entered.set()
        release.wait(timeout=_BOUND_SECONDS)
        return ControlResponse(1, True, request.correlation_id, "ok")

    server.start(handler)
    in_flight_thread = threading.Thread(
        target=lambda: UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "in-flight"), timeout_seconds=_BOUND_SECONDS
        )
    )
    in_flight_thread.start()
    assert entered.wait(timeout=_BOUND_SECONDS), "handler never entered"

    close_finished = threading.Event()

    def do_close() -> None:
        server.close()
        close_finished.set()

    close_thread = threading.Thread(target=do_close)
    close_thread.start()

    # `close()` must not return while the in-flight handler is still blocked -- proving
    # it actually joins outstanding work rather than abandoning it. A short window here
    # (e.g. 0.2s) is racy: the accept loop's own ~0.25s poll cadence can make `close()`
    # look "still blocked" by pure coincidence, independent of whether it waits for the
    # worker at all. This margin (1.0s, 4x that poll interval) makes a false "still
    # blocked" reading from an unrelated fast exit path implausible.
    margin_seconds = 1.0
    still_blocked = not close_finished.wait(timeout=margin_seconds)
    if not still_blocked:
        # If close() already returned, that is only correct when nothing was left
        # running -- fail loudly and specifically if a worker thread is still alive,
        # rather than only failing lower down at the final thread-leak assertion.
        worker_threads = [
            t for t in threading.enumerate() if t.name.startswith("repoforge-control-worker")
        ]
        assert not worker_threads, (
            f"close() returned within {margin_seconds}s while the in-flight handler was "
            f"still blocked, and a worker thread is still alive: {worker_threads} -- "
            "close() abandoned outstanding work instead of waiting for it"
        )
    assert still_blocked, (
        f"close() returned within {margin_seconds}s while the in-flight handler was "
        "still deliberately blocked on `release` -- it must wait for outstanding work"
    )

    release.set()
    assert close_finished.wait(timeout=_BOUND_SECONDS), (
        "close() did not return within the bound after the in-flight handler was released"
    )
    close_thread.join(timeout=_BOUND_SECONDS)
    in_flight_thread.join(timeout=_BOUND_SECONDS)
    assert not close_thread.is_alive()
    assert not in_flight_thread.is_alive()
    assert not path.exists()

    control_threads = [
        t for t in threading.enumerate() if t.name.startswith("repoforge-control-worker")
    ]
    assert control_threads == [], f"control worker threads leaked after close(): {control_threads}"


# --- Edge cases D (admission accounting) and E (lifecycle) -----------------------


def test_max_concurrent_requests_must_be_positive(tmp_path: Path) -> None:
    for invalid in (0, -1):
        with pytest.raises(ConfigError, match="max_concurrent_requests"):
            UnixRuntimeControlServer(tmp_path / "control.sock", max_concurrent_requests=invalid)


def test_drain_timeout_seconds_must_be_positive(tmp_path: Path) -> None:
    for invalid in (0, -1.0):
        with pytest.raises(ConfigError, match="drain_timeout_seconds"):
            UnixRuntimeControlServer(tmp_path / "control.sock", drain_timeout_seconds=invalid)


def test_a_handler_exception_still_releases_exactly_one_permit(tmp_path: Path) -> None:
    """A handler that raises must not leak its admission slot.

    Proven the same way as the permit-recovery test: fill capacity with
    exception-raising handlers, confirm they all complete (the exception is
    caught, not propagated), then fill capacity again to the same limit --
    which is only possible if every permit was released despite the exception.
    """
    path = tmp_path / "control.sock"
    limit = 2

    def raising_handler(request: ControlRequest) -> ControlResponse:
        raise RuntimeError("deliberate handler failure")

    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    server.start(raising_handler)
    try:
        for round_id in ("first", "second"):
            responses = [
                UnixRuntimeControlClient(path).request(
                    ControlRequest(1, ControlCommand.PING, f"{round_id}-{i}"),
                    timeout_seconds=_BOUND_SECONDS,
                )
                for i in range(limit)
            ]
            for response in responses:
                assert not response.ok
                assert response.error_code == "RuntimeError"
    finally:
        server.close()


def test_close_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path)
    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    server.close()
    server.close()  # must not raise


def test_close_before_start_does_not_raise(tmp_path: Path) -> None:
    server = UnixRuntimeControlServer(tmp_path / "control.sock")
    server.close()  # must not raise even though start() was never called


def test_start_twice_without_closing_raises(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path)
    handler = lambda request: ControlResponse(1, True, request.correlation_id, "ok")  # noqa: E731
    server.start(handler)
    try:
        with pytest.raises(ConfigError, match="already started"):
            server.start(handler)
    finally:
        server.close()


def test_server_can_restart_after_close(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path)
    handler = lambda request: ControlResponse(1, True, request.correlation_id, "ok")  # noqa: E731

    server.start(handler)
    first = UnixRuntimeControlClient(path).request(
        ControlRequest(1, ControlCommand.PING, "before-restart"), timeout_seconds=_BOUND_SECONDS
    )
    assert first.ok
    server.close()

    server.start(handler)
    try:
        second = UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "after-restart"), timeout_seconds=_BOUND_SECONDS
        )
        assert second.ok
    finally:
        server.close()


def test_repeated_concurrent_close_and_connect_never_leaks_or_hangs(tmp_path: Path) -> None:
    """Stress the close()-races-submit() window (#448 Slice 1 edge case A).

    The exact race -- a connection admitted just as `close()` begins shutting
    down the executor, so `submit()` raises `RuntimeError` -- is a narrow timing
    window that cannot be hit deterministically from outside the implementation
    without white-box hooks. This repeats the pattern many times across fresh
    server instances instead, to make hitting that window likely at least once,
    and asserts the invariants that must hold whether or not this particular
    repetition landed inside it: no hang, no leaked thread, no unhandled
    exception escaping either side.
    """
    for i in range(30):
        path = tmp_path / f"control-{i}.sock"
        server = UnixRuntimeControlServer(path, max_concurrent_requests=2)
        entered = threading.Event()

        def handler(request: ControlRequest, entered: threading.Event = entered) -> ControlResponse:
            entered.set()
            return ControlResponse(1, True, request.correlation_id, "ok")

        server.start(handler)

        def connect(path: Path = path, i: int = i) -> None:
            # Racing a live `close()` legitimately ends in either a normal response
            # or the connection being refused/reset -- both are acceptable outcomes
            # of the race this test stresses. `UnixRuntimeControlClient` only wraps
            # `connect()` failures in `ConfigError`; a `sendall`/`recv` racing an
            # in-progress close surfaces as a raw `OSError` (a pre-existing gap in
            # the client, out of scope for this server-side fix) -- both are
            # expected here, so both are swallowed rather than only one.
            with contextlib.suppress(ConfigError, OSError):
                UnixRuntimeControlClient(path).request(
                    ControlRequest(1, ControlCommand.PING, f"race-{i}"),
                    timeout_seconds=_BOUND_SECONDS,
                )

        connect_thread = threading.Thread(target=connect)
        close_thread = threading.Thread(target=server.close)
        connect_thread.start()
        close_thread.start()
        connect_thread.join(timeout=_BOUND_SECONDS)
        close_thread.join(timeout=_BOUND_SECONDS)

        assert not connect_thread.is_alive(), f"iteration {i}: connect thread did not finish"
        assert not close_thread.is_alive(), f"iteration {i}: close thread did not finish"
        assert not path.exists(), f"iteration {i}: socket path still exists after close()"

    leaked = [
        t
        for t in threading.enumerate()
        if t.name.startswith("repoforge-control-worker") or t.name == "repoforge-runtime-control"
    ]
    assert leaked == [], f"threads leaked across {30} close()/connect() race iterations: {leaked}"


def test_close_racing_the_admit_to_enqueue_window_never_leaks_a_permit_or_connection(
    tmp_path: Path,
) -> None:
    """Deterministically drive the exact "admitted but not yet enqueued" window
    (#448 Slice 1 edge case A / blocker 1), rather than only stress-testing it
    probabilistically like the repetition test above.

    Monkeypatches the server's own work queue's `put` to pause on a controlled
    `Event` immediately before the real enqueue happens -- pinning the accept
    thread at exactly the point where a connection has been admitted (permit
    acquired, `in_flight` incremented) but not yet hooked up to a worker. `close()`
    is then driven concurrently, and only released afterward, forcing the ordering
    edge case B's own fix (join the accept thread to completion *before* sending
    worker stop-sentinels and draining) actually has to handle.
    """
    path = tmp_path / "control.sock"
    server = UnixRuntimeControlServer(path, max_concurrent_requests=2, drain_timeout_seconds=3.0)
    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))

    assert server._run is not None
    real_queue = server._run.queue
    reached_put = threading.Event()
    release_put = threading.Event()
    paused_once = threading.Event()

    class _PausableQueue:
        """Wraps the real queue, pausing exactly the first `put()` on an `Event`.

        Only intercepts what the accept loop and `close()` see through
        `server._run.queue` (reassigned below); the already-started worker
        threads captured the *real* queue object directly via closure in
        `start()`, so they are unaffected and still service it normally once the
        paused `put()` is released.
        """

        def put(self, item: object) -> None:
            if not paused_once.is_set():
                paused_once.set()
                reached_put.set()
                release_put.wait(timeout=_BOUND_SECONDS)
            real_queue.put(item)  # type: ignore[arg-type]

        def get_nowait(self) -> object:
            return real_queue.get_nowait()

    server._run.queue = _PausableQueue()  # type: ignore[assignment]

    racing_thread = threading.Thread(
        target=lambda: UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "racing"), timeout_seconds=_BOUND_SECONDS
        )
    )
    racing_thread.start()
    assert reached_put.wait(timeout=_BOUND_SECONDS), "accept thread never reached the paused put()"

    # The connection is now admitted and paused immediately before enqueue. Drive
    # `close()` concurrently -- it must join this exact accept-thread iteration to
    # completion (waiting on `paused_put`, not returning early) before it sends
    # worker stop-sentinels and drains the queue, or the real `put()` below would
    # land after that drain already ran and be silently un-pickable-up.
    close_thread = threading.Thread(target=server.close)
    close_thread.start()

    # `close()` cannot have returned yet: the accept thread it must join is still
    # parked in `paused_put`, which nothing has released.
    close_thread.join(timeout=0.5)
    assert close_thread.is_alive(), (
        "close() returned while the accept thread was still paused mid-admission -- "
        "it must join that iteration to completion first"
    )

    release_put.set()
    close_thread.join(timeout=_BOUND_SECONDS)
    racing_thread.join(timeout=_BOUND_SECONDS)
    assert not close_thread.is_alive(), "close() did not complete after the race was released"
    assert not racing_thread.is_alive()
    assert not path.exists()

    control_threads = [
        t for t in threading.enumerate() if t.name.startswith("repoforge-control-worker")
    ]
    assert control_threads == [], f"control worker threads leaked: {control_threads}"
    # No thread/handle was left dangling above (proven by the two assertions and
    # the thread-leak check), and no exception propagated out of either thread --
    # together, that is the permit/connection accounting resolving correctly for
    # this exact race, whether the request happened to be genuinely served or
    # cleaned up by close()'s own drain.


_SUBPROCESS_EXIT_PROBE = """
import sys
import threading
from pathlib import Path

from repoforge.adapters.runtime import UnixRuntimeControlClient, UnixRuntimeControlServer
from repoforge.domain.runtime import ControlCommand, ControlRequest, ControlResponse

path = Path(sys.argv[1])
drain_timeout = float(sys.argv[2])
server = UnixRuntimeControlServer(path, max_concurrent_requests=2, drain_timeout_seconds=drain_timeout)


def stuck_handler(request):
    # Never returns: a callback permanently hung in non-socket code -- the one
    # thing no socket operation and no bounded `close()` can force to stop.
    threading.Event().wait()
    return ControlResponse(1, True, request.correlation_id, "unreachable")


server.start(stuck_handler)
entered = threading.Event()


def send_request():
    entered.set()
    try:
        UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "stuck"), timeout_seconds=1.0
        )
    except Exception:
        pass


t = threading.Thread(target=send_request, daemon=True)
t.start()
entered.wait(timeout=5.0)
# Give the handler a moment to actually be running inside `threading.Event().wait()`
# before close() is called, rather than racing the handler's own entry.
import time as _time
_time.sleep(0.3)

server.close()
print("close_returned", flush=True)
# Falling off the end here is the point of the test: nothing further explicitly
# stops or joins the still-alive daemon worker thread. If it were non-daemon
# (the `ThreadPoolExecutor`-based design this replaced), interpreter shutdown
# would block on it indefinitely regardless of what `close()` already did.
"""


def test_process_can_exit_even_when_a_control_handler_hangs_forever(tmp_path: Path) -> None:
    """`close()` returning within its bound does not, by itself, guarantee the
    process can exit (#448 Slice 1 blocker 2): a handler stuck in non-socket code
    leaves its worker thread alive regardless of what `close()` does. This is only
    safe if that thread is daemon, so it never blocks CPython's own `atexit`
    interpreter-shutdown hook. Proven with a real subprocess, since this is a
    property of process/interpreter shutdown, not observable within one test
    process without risking hanging the whole test run.

    Reproduced the failure this guards against directly before this fix existed:
    `close()` returned in ~2.2s (its own bound) but the subprocess did not exit
    for 15s+ after that with the `ThreadPoolExecutor`-based implementation, since
    its worker threads are not daemon.
    """
    script = tmp_path / "probe.py"
    script.write_text(_SUBPROCESS_EXIT_PROBE, encoding="utf-8")
    sock_dir = tmp_path / "sock"
    sock_dir.mkdir()
    drain_timeout = 2.0
    # Generous margin over `close()`'s own bound: this asserts the *process*
    # exits promptly after `close()` returns, not merely that `close()` itself
    # is bounded (already covered by the shutdown test above).
    process_exit_margin = 5.0

    proc = subprocess.Popen(
        [sys.executable, str(script), str(sock_dir / "control.sock"), str(drain_timeout)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=drain_timeout + process_exit_margin)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=_BOUND_SECONDS)
        raise AssertionError(
            "subprocess did not exit within "
            f"{drain_timeout + process_exit_margin}s of a permanently stuck control "
            f"handler, even though close() should return within {drain_timeout}s -- "
            f"a worker thread is blocking process exit.\nstdout: {stdout}\nstderr: {stderr}"
        ) from None

    assert "close_returned" in stdout, f"close() apparently never returned.\nstderr: {stderr}"
    assert proc.returncode == 0, f"subprocess exited nonzero: {proc.returncode}\nstderr: {stderr}"


def test_stuck_worker_from_previous_run_cannot_affect_restarted_server(tmp_path: Path) -> None:
    """A handler still stuck from a prior generation must not leak into a restart.

    `close()` can only bound *its own* return, not force a handler hung in
    non-socket code to actually stop (no safe way to kill a running thread in
    Python) -- so a worker thread from generation A can still be alive, still
    holding its admission permit, when the *same server object* is `start()`ed
    again as generation B. If any of the pool's accounting (the admission
    semaphore, the in-flight counter, the work queue, the worker list) were
    shared mutable state on `self` rather than captured fresh per generation,
    generation B would silently inherit generation A's leftover permit debt --
    reporting less capacity than configured, for requests that have nothing to
    do with the stuck handler (#448 Slice 1 cross-restart isolation).
    """
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(
        path, max_concurrent_requests=limit, drain_timeout_seconds=0.3
    )
    stuck_entered = threading.Event()
    stuck_release = threading.Event()

    def generation_a_handler(request: ControlRequest) -> ControlResponse:
        stuck_entered.set()
        # Never returns until explicitly released below -- simulates a handler
        # permanently hung in non-socket code, which `close()` cannot force to
        # stop (the same property the subprocess-exit test proves at the
        # process level; this test proves the accounting-isolation
        # consequence of it at the object level).
        stuck_release.wait(timeout=_BOUND_SECONDS)
        return ControlResponse(1, True, request.correlation_id, "ok", message="generation-a-late")

    server.start(generation_a_handler)
    stuck_thread = threading.Thread(
        target=lambda: UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "stuck"), timeout_seconds=_BOUND_SECONDS
        )
    )
    stuck_thread.start()
    assert stuck_entered.wait(timeout=_BOUND_SECONDS), "generation A handler never entered"

    # `close()` returns within its own bound even though the handler above is
    # still blocked -- that worker thread is still alive, still holding an
    # admission permit that was acquired under generation A.
    server.close()

    def generation_b_handler(request: ControlRequest) -> ControlResponse:
        return ControlResponse(1, True, request.correlation_id, "ok", message="generation-b")

    server.start(generation_b_handler)
    try:
        # Generation B must have its *full configured capacity* available,
        # independent of generation A's still-stuck worker: send exactly
        # `limit` concurrent requests and require every one of them to
        # succeed, none rejected as overloaded. A shared, un-isolated
        # semaphore/in-flight counter would leave only `limit - 1` permits
        # free here, and one of these would come back overloaded instead.
        responses: list[ControlResponse] = [None] * limit  # type: ignore[list-item]

        def send(i: int) -> None:
            responses[i] = UnixRuntimeControlClient(path).request(
                ControlRequest(1, ControlCommand.PING, f"b{i}"), timeout_seconds=_BOUND_SECONDS
            )

        threads = [threading.Thread(target=send, args=(i,)) for i in range(limit)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_BOUND_SECONDS)
            assert not t.is_alive()

        for i, response in enumerate(responses):
            assert response.ok, f"generation B request b{i} was rejected: {response}"
            assert response.status == "ok"
            assert response.correlation_id == f"b{i}"
            # Must never have been served by generation A's stale handler.
            assert response.message != "generation-a-late"
    finally:
        stuck_release.set()
        stuck_thread.join(timeout=_BOUND_SECONDS)
        assert not stuck_thread.is_alive()
        server.close()

    # generation A's worker thread must eventually finish (it was released
    # above) and generation B's own close() must not have hidden it forever --
    # give it a moment past both closes to actually exit, then confirm nothing
    # from either generation is still around.
    deadline = time.monotonic() + _BOUND_SECONDS
    leaked = [t for t in threading.enumerate() if t.name.startswith("repoforge-control-worker")]
    while leaked and time.monotonic() < deadline:
        time.sleep(0.01)
        leaked = [t for t in threading.enumerate() if t.name.startswith("repoforge-control-worker")]
    assert leaked == [], f"worker threads leaked across generations: {leaked}"


def _kill_workers(server: UnixRuntimeControlServer, count: int) -> None:
    """Make `count` of the current run's workers exit gracefully, in place.

    Injects the same `None` stop-sentinel `worker_loop` already treats as "no
    more work, exit" -- without calling `close()`, so the accept loop and any
    other workers keep running exactly as a real, unplanned worker death
    would leave them (#448 Slice 1 worker-pool observability).
    """
    run = server._run
    assert run is not None
    workers = list(run.workers)
    for _ in range(count):
        run.queue.put(None)
    deadline = time.monotonic() + _BOUND_SECONDS
    while time.monotonic() < deadline:
        if sum(1 for w in workers if not w.is_alive()) >= count:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"only {sum(1 for w in workers if not w.is_alive())}/{count} workers exited in time"
    )


def test_is_serving_is_false_once_the_entire_worker_pool_has_died(tmp_path: Path) -> None:
    """A live accept thread with zero live workers must not report "serving".

    Every admitted connection would queue forever with nothing to ever pick
    it up -- reporting `healthy` here would repeat exactly the gap #322 was
    written to close, just one layer down (#448 Slice 1 worker-pool
    observability).
    """
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    try:
        assert server.is_serving() is True
        assert server.is_healthy() is True
        _kill_workers(server, limit)

        assert server.is_serving() is False
        assert server.is_healthy() is False
        diagnostic = server.serving_diagnostic()
        assert "0" in diagnostic and str(limit) in diagnostic, diagnostic
    finally:
        server.close()


def test_serving_diagnostic_reports_degraded_when_some_but_not_all_workers_have_died(
    tmp_path: Path,
) -> None:
    """A partially-dead pool is still serving, but must say so honestly.

    `is_serving()` alone would still report `True` here (correctly -- some
    capacity remains), but silently reporting full health while running at
    reduced capacity is exactly the kind of gap that costs diagnosis time
    later; the detail string must surface it (#448 Slice 1 worker-pool
    observability).
    """
    path = tmp_path / "control.sock"
    limit = 3
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    try:
        _kill_workers(server, 1)

        assert server.is_serving() is True, "capacity remains -- must still report serving"
        assert server.is_healthy() is False, "a lost worker is a permanent capacity loss"
        diagnostic = server.serving_diagnostic()
        assert "degraded" in diagnostic, diagnostic
        assert "2" in diagnostic and str(limit) in diagnostic, diagnostic

        # And the remaining capacity is real, not just reported: a request must
        # still be served correctly at the reduced pool size.
        response = UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "still-works"), timeout_seconds=_BOUND_SECONDS
        )
        assert response.ok
    finally:
        server.close()


def test_a_worker_that_dies_from_an_unexpected_exception_is_reflected_in_capacity(
    tmp_path: Path,
) -> None:
    """`worker_loop`'s own outer boundary, not `handle()`'s.

    `handle()` already catches every `Exception` from a real exchange and always
    releases its permit/decrements in_flight in its own `finally` -- so this
    exercises the one thing outside that guarantee: a work item whose callable
    itself blows up before ever reaching `handle()`'s try/finally at all. That
    must not leave the worker thread silently spinning in a broken state; it
    must exit and be visible as reduced capacity (#448 Slice 1 worker-pool
    observability), the same as any other worker death.
    """
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    try:
        assert server._run is not None
        run = server._run

        def bad_fn(connection: socket.socket) -> None:
            raise RuntimeError("deliberate worker-loop failure, not a handler failure")

        run.queue.put((bad_fn, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)))

        deadline = time.monotonic() + _BOUND_SECONDS
        while time.monotonic() < deadline:
            if sum(1 for w in run.workers if w.is_alive()) == limit - 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("the worker running the bad work item never exited")

        assert server.is_serving() is True, "one live worker is still capacity"
        diagnostic = server.serving_diagnostic()
        assert "degraded" in diagnostic, diagnostic
        assert "RuntimeError" in diagnostic, diagnostic
    finally:
        server.close()


def test_partial_worker_loss_marks_control_plane_unhealthy(tmp_path: Path) -> None:
    """`is_serving()` and `is_healthy()` must disagree here, on purpose.

    A pool that has lost one of two workers can still answer requests -- so
    `is_serving()` staying `True` is correct, not a bug -- but that loss is
    permanent (this pool never replenishes a dead worker), which is exactly
    the state a health check must not call `healthy`. Conflating the two
    would let a runtime running at a silent, persistent capacity loss keep
    reporting `healthy` (#448 Slice 1 partial-worker health semantics).
    """
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    server.start(lambda request: ControlResponse(1, True, request.correlation_id, "ok"))
    try:
        assert server._run is not None
        run = server._run

        def bad_fn(connection: socket.socket) -> None:
            raise RuntimeError("deliberate worker-loop failure for partial-loss health test")

        run.queue.put((bad_fn, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)))

        deadline = time.monotonic() + _BOUND_SECONDS
        while time.monotonic() < deadline:
            if sum(1 for w in run.workers if w.is_alive()) == limit - 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("the worker running the bad work item never exited")

        # The remaining capacity is real: a request must still be served correctly.
        response = UnixRuntimeControlClient(path).request(
            ControlRequest(1, ControlCommand.PING, "still-works"), timeout_seconds=_BOUND_SECONDS
        )
        assert response.ok

        assert server.is_serving() is True, "one live worker out of two is still capacity"
        assert server.is_healthy() is False, "a permanently lost worker must not read healthy"
        diagnostic = server.serving_diagnostic()
        assert f"workers=1/{limit}" in diagnostic, diagnostic
        assert "RuntimeError" in diagnostic, diagnostic
    finally:
        server.close()


def test_a_silent_non_reading_overloaded_client_does_not_starve_the_accept_loop(
    tmp_path: Path,
) -> None:
    """A caller that connects while over capacity, then never reads its overload
    response and never sends anything, must not stall the accept loop for
    anywhere near a normal exchange's timeout (#448 Slice 1 hardening): rejection
    runs directly on the accept thread, so bounding it to the same budget as a
    real exchange would let one such caller serially deny service to everyone
    else for that whole duration.
    """
    path = tmp_path / "control.sock"
    limit = 2
    server = UnixRuntimeControlServer(path, max_concurrent_requests=limit)
    release = threading.Event()
    admitted = threading.Barrier(limit + 1, timeout=_BOUND_SECONDS)

    def handler(request: ControlRequest) -> ControlResponse:
        admitted.wait(timeout=_BOUND_SECONDS)
        release.wait(timeout=_BOUND_SECONDS)
        return ControlResponse(1, True, request.correlation_id, "ok")

    server.start(handler)
    try:
        occupiers = [
            threading.Thread(
                target=lambda i=i: UnixRuntimeControlClient(path).request(
                    ControlRequest(1, ControlCommand.PING, f"occupy{i}"),
                    timeout_seconds=_BOUND_SECONDS,
                )
            )
            for i in range(limit)
        ]
        for t in occupiers:
            t.start()
        admitted.wait(timeout=_BOUND_SECONDS)

        # A raw connection that connects while over capacity, then does nothing:
        # never sends a request, never reads the overload response the server
        # sends it. This is exactly the client the accept thread must not wait on.
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent.connect(str(server.bound_path))
        try:
            started = time.monotonic()
            # A second, normal caller submitted right after the silent one must
            # still be accepted/rejected quickly -- proving the accept loop moved
            # on rather than being stuck in the silent client's own rejection.
            next_overloaded = UnixRuntimeControlClient(path).request(
                ControlRequest(1, ControlCommand.PING, "after-silent-client"),
                timeout_seconds=_BOUND_SECONDS,
            )
            elapsed = time.monotonic() - started
        finally:
            silent.close()

        assert elapsed < 1.0, (
            f"a subsequent caller waited {elapsed:.2f}s behind a silent, "
            "non-reading overloaded client -- the accept loop was starved"
        )
        assert not next_overloaded.ok
        assert next_overloaded.error_code == "RUNTIME_CONTROL_OVERLOADED"

        release.set()
        for t in occupiers:
            t.join(timeout=_BOUND_SECONDS)
            assert not t.is_alive()
    finally:
        release.set()
        server.close()
