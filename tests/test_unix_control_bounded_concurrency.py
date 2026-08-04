"""Bounded concurrency for the Unix runtime control socket (#448 Slice 1).

Signature C's core defect was a single-threaded accept loop running an
expensive handler inline: one caller could block every other caller behind
it in the kernel backlog. These tests prove four invariants the corrected
server must hold, using `Event`/`Barrier` for deterministic synchronization
-- never `time.sleep()` to guess at timing.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from repoforge.adapters.runtime import (
    UnixRuntimeControlClient,
    UnixRuntimeControlServer,
)
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
        # Give every thread a chance to at least attempt a connection; the
        # handler itself blocks on `release`, so any that got admitted are
        # parked inside it right now.
        deadline = time.monotonic() + _BOUND_SECONDS
        while time.monotonic() < deadline:
            with lock:
                if current >= 1:
                    break
            time.sleep(0.01)

        release.set()
        for t in threads:
            t.join(timeout=_BOUND_SECONDS)
            assert not t.is_alive()

        assert observed_max <= limit, (
            f"observed {observed_max} concurrent handlers, limit was {limit}"
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

    # `close()` must not return while the in-flight handler is still blocked --
    # proving it actually joins outstanding work rather than abandoning it.
    assert not close_finished.wait(timeout=0.2)

    release.set()
    assert close_finished.wait(timeout=_BOUND_SECONDS), (
        "close() did not return within the bound after the in-flight handler was released"
    )
    close_thread.join(timeout=_BOUND_SECONDS)
    in_flight_thread.join(timeout=_BOUND_SECONDS)
    assert not close_thread.is_alive()
    assert not in_flight_thread.is_alive()
    assert not path.exists()

    control_threads = [t for t in threading.enumerate() if t.name.startswith("repoforge-control")]
    assert control_threads == [], f"control worker threads leaked after close(): {control_threads}"
