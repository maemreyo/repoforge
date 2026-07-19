import contextlib
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from repoforge.adapters.subprocess import SubprocessCommandExecutor, process_tree
from repoforge.adapters.subprocess import command_executor as command_executor_module
from repoforge.config import ServerConfig
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError
from repoforge.ports.cancellation import CancellationToken


def _executor(tmp_path: Path) -> SubprocessCommandExecutor:
    return SubprocessCommandExecutor(ServerConfig(tmp_path / "w", tmp_path / "s"))


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.EXECUTION_POLICY_UNSUPPORTED,
        ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
    ],
)
def test_execution_boundary_error_codes_are_stable_and_non_retryable(code: ErrorCode) -> None:
    error = RepoForgeError("execution boundary failure", code=code)

    assert error.code is code
    assert error.retryable is False


def test_run_returns_result_on_success(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    result = executor.run(["echo", "hello"], cwd=tmp_path)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_nonzero_exit_is_command_failed_regardless_of_output_text(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "print_timeout.py"
    script.write_text(
        "import sys\nsys.stdout.write('timeout: 60.0s exceeded while collecting\\n')\nsys.exit(1)\n"
    )
    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path)
    err = excinfo.value
    assert err.code is ErrorCode.COMMAND_FAILED
    assert err.retryable is False
    assert err.details["exit_code"] == 1
    assert err.details["argv"] == ["python3", str(script)]
    assert "timeout: 60.0s" in err.details["stdout_excerpt"]
    assert err.details["stderr_excerpt"] == ""
    assert err.details["stdout_truncated"] is False


def test_run_timeout_is_command_timeout(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(5)\n")
    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path, timeout=1)
    err = excinfo.value
    assert err.code is ErrorCode.COMMAND_TIMEOUT
    assert err.retryable is True
    assert err.details["timeout_seconds"] == 1


def test_timeout_cleanup_does_not_hang_when_killpg_reports_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killpg PermissionError (the Darwin already-reaped race) is treated as
    "process already gone", but that assumption can be wrong. Prove the final
    output drain is bounded so a process that is in fact still alive and still
    writing output cannot hang the caller forever, AND that the process is
    not left orphaned: when killpg keeps failing, a direct single-process
    kill() must still terminate it (#225 review: an earlier version bounded
    the caller's wait but could silently leave the child running)."""
    executor = _executor(tmp_path)
    script = tmp_path / "ignore_term.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True:\n"
        "    time.sleep(0.05)\n"
    )
    real_killpg = os.killpg
    signaled_pids: list[int] = []

    def fake_killpg(pid: int, sig: int) -> None:
        signaled_pids.append(pid)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", fake_killpg)
    try:
        started = time.monotonic()
        with pytest.raises(CommandError) as excinfo:
            executor.run(["python3", str(script)], cwd=tmp_path, timeout=1)
        elapsed = time.monotonic() - started
        assert excinfo.value.code is ErrorCode.COMMAND_TIMEOUT
        # 1s run timeout + 2s SIGTERM wait + 2s final drain, well under a hang.
        assert elapsed < 8
        assert signaled_pids
        pid = signaled_pids[0]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        monkeypatch.undo()
        for pid in signaled_pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                real_killpg(pid, signal.SIGKILL)


def test_timeout_cleanup_kills_a_descendant_that_escaped_the_process_group(
    tmp_path: Path,
) -> None:
    """A child can daemonize a grandchild via its own start_new_session/setsid,
    which leaves the process group killpg targets -- but not the kernel
    parent/child link, as long as the daemonizing child is still alive when
    the timeout fires (the realistic case: something in the tree is still
    blocked, which is *why* the overall command timed out). The cleanup path
    must sweep such escaped descendants directly by PID, not only killpg the
    group (#225 round-3 review: reproduced a surviving grandchild)."""
    if not process_tree.atomic_process_signalling_available():
        pytest.skip("atomic descendant signalling is unavailable on this host")

    executor = _executor(tmp_path)
    script = tmp_path / "daemonize.py"
    pid_file = tmp_path / "escaped.pid"
    script.write_text(
        "import pathlib, subprocess, time\n"
        "child = subprocess.Popen(['sleep', '120'], start_new_session=True,"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path('escaped.pid').write_text(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    failures: list[CommandError] = []

    def run_timed_command() -> None:
        try:
            executor.run(["python3", str(script)], cwd=tmp_path, timeout=1)
        except CommandError as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_timed_command)
    worker.start()
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    captured = process_tree.read_identity(int(pid_file.read_text()))
    if captured is None:
        pytest.skip("process identity inspection is unavailable in this test sandbox")
    worker.join(timeout=8)
    assert not worker.is_alive()
    assert failures and failures[0].code is ErrorCode.COMMAND_TIMEOUT
    assert process_tree.identity_is_current(captured) is False


def test_identity_safe_kill_skips_when_atomic_handle_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = process_tree.ProcessIdentity(pid=123, ppid=12, start_token="old")
    monkeypatch.setattr(process_tree, "_pidfd_open", lambda pid: None)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    assert process_tree.kill_identity(captured, signal.SIGKILL) is False
    assert kills == []


def test_identity_safe_kill_allows_same_process_after_reparenting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = process_tree.ProcessIdentity(pid=123, ppid=12, start_token="same-start")
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(
        process_tree,
        "read_identity",
        lambda pid: process_tree.ProcessIdentity(pid=pid, ppid=1, start_token="same-start"),
    )
    monkeypatch.setattr(process_tree, "_pidfd_open", lambda pid: read_fd)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree,
        "_pidfd_send_signal",
        lambda fd, sig: signals.append((fd, sig)) or True,
    )

    try:
        assert process_tree.kill_identity(captured, signal.SIGKILL) is True
        assert signals == [(read_fd, signal.SIGKILL)]
    finally:
        os.close(write_fd)


def test_identity_safe_kill_rechecks_after_opening_atomic_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = process_tree.ProcessIdentity(pid=123, ppid=12, start_token="old")
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(process_tree, "_pidfd_open", lambda pid: read_fd)
    monkeypatch.setattr(
        process_tree,
        "read_identity",
        lambda pid: process_tree.ProcessIdentity(pid=pid, ppid=1, start_token="reused"),
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree,
        "_pidfd_send_signal",
        lambda fd, sig: signals.append((fd, sig)),
    )

    try:
        assert process_tree.kill_identity(captured, signal.SIGKILL) is False
        assert signals == []
    finally:
        os.close(write_fd)


def test_wait_identities_gone_handles_delayed_terminal_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = process_tree.ProcessIdentity(pid=123, ppid=12, start_token="same-start")
    checks = 0

    def still_current(identity: process_tree.ProcessIdentity) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    monkeypatch.setattr(process_tree, "identity_is_current", still_current)

    assert process_tree.wait_identities_gone((captured,), timeout=0.1) == ()
    assert checks == 2


def test_wait_identities_gone_returns_survivors_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = process_tree.ProcessIdentity(pid=123, ppid=12, start_token="same-start")
    monkeypatch.setattr(process_tree, "identity_is_current", lambda identity: True)

    assert process_tree.wait_identities_gone((captured,), timeout=0) == (captured,)


def test_timeout_rescans_descendants_and_reports_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "ignore_term_for_rescan.py"
    script.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(0.05)\n"
    )
    late = process_tree.ProcessIdentity(pid=987654, ppid=123, start_token="late")
    snapshots = (
        process_tree.DescendantSnapshot((), True, "complete"),
        process_tree.DescendantSnapshot((late,), False, "process_limit_exceeded"),
    )
    inspection_calls: list[int] = []

    def inspect(pid: int) -> process_tree.DescendantSnapshot:
        inspection_calls.append(pid)
        return snapshots[(len(inspection_calls) - 1) % 2]

    monkeypatch.setattr(
        command_executor_module,
        "inspect_descendants",
        inspect,
    )
    signalled: list[process_tree.ProcessIdentity] = []
    monkeypatch.setattr(
        command_executor_module,
        "kill_identity",
        lambda identity, sig: signalled.append(identity) or True,
    )

    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path, timeout=1)

    assert set(signalled) == {late}
    assert len(inspection_calls) >= 2
    assert excinfo.value.details["descendant_inspection_complete"] is False
    assert excinfo.value.details["descendant_inspection_status"] == (
        "pre:complete;post:process_limit_exceeded"
    )


def test_ps_identity_parser_and_probe_failure_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = process_tree._parse_ps_line("123 42 Sun Jul 19 12:34:56 2026")
    assert parsed == process_tree.ProcessIdentity(
        pid=123,
        ppid=42,
        start_token="Sun Jul 19 12:34:56 2026",
    )
    monkeypatch.setattr(
        process_tree.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ps unavailable")),
    )
    assert process_tree._bounded_ps(["ps"]) is None


def test_ps_identity_probe_parses_rows_and_contains_selector_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_tree,
        "_bounded_ps",
        lambda argv: "123 42 Sun Jul 19 12:34:56 2026\n124 42 Sun Jul 19 12:34:57 2026\n",
    )
    assert process_tree._read_ps_identities() == (
        process_tree.ProcessIdentity(123, 42, "Sun Jul 19 12:34:56 2026"),
        process_tree.ProcessIdentity(124, 42, "Sun Jul 19 12:34:57 2026"),
    )


def test_bounded_ps_contains_selector_errors(monkeypatch: pytest.MonkeyPatch) -> None:

    class BrokenSelector:
        def register(self, fileobj: object, events: int) -> None:
            raise OSError("selector unavailable")

        def close(self) -> None:
            pass

    class Probe:
        stdout = object()
        returncode = None

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            pass

        def wait(self, timeout: float) -> None:
            pass

    monkeypatch.setattr(process_tree.subprocess, "Popen", lambda *args, **kwargs: Probe())
    monkeypatch.setattr(process_tree.selectors, "DefaultSelector", BrokenSelector)
    assert process_tree._bounded_ps(["ps"]) is None


def test_linux_stat_parser_handles_parentheses_in_process_name() -> None:
    fields_after_name = ["S", "42", *("0" for _ in range(17)), "123456", "0"]
    parsed = process_tree._parse_linux_stat(f"123 (worker ) helper) {' '.join(fields_after_name)}")

    assert parsed == process_tree.ProcessIdentity(
        pid=123,
        ppid=42,
        start_token="123456",
    )


def test_linux_stat_parser_treats_zombie_as_not_live() -> None:
    fields_after_name = ["Z", "42", *("0" for _ in range(17)), "123456", "0"]

    assert (
        process_tree._parse_linux_stat(f"123 (finished worker) {' '.join(fields_after_name)}")
        is None
    )


def test_run_missing_executable_is_not_found_even_with_not_found_in_message(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    with pytest.raises(CommandError) as excinfo:
        executor.run(["definitely-not-a-real-executable"], cwd=tmp_path)
    err = excinfo.value
    assert err.code is ErrorCode.NOT_FOUND
    assert err.retryable is False


def test_run_output_containing_not_found_does_not_misclassify(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "print_not_found.py"
    script.write_text(
        "import sys\nsys.stdout.write('module not found: some_module\\n')\nsys.exit(1)\n"
    )
    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path)
    err = excinfo.value
    assert err.code is ErrorCode.COMMAND_FAILED
    assert err.retryable is False


def test_run_bytes_nonzero_exit_is_command_failed(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "fail_binary.py"
    script.write_text("import sys\nsys.stderr.write('timeout: 60.0s\\n')\nsys.exit(1)\n")
    with pytest.raises(CommandError) as excinfo:
        executor.run_bytes(["python3", str(script)], cwd=tmp_path, max_bytes=1000)
    err = excinfo.value
    assert err.code is ErrorCode.COMMAND_FAILED
    assert err.details["exit_code"] == 1


def test_cancel_token_terminates_a_running_process_group(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "sleep_long.py"
    script.write_text("import time\ntime.sleep(30)\n")
    token = CancellationToken()

    def cancel_soon() -> None:
        time.sleep(0.3)
        token.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()

    started = time.monotonic()
    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path, timeout=30, cancel_token=token)
    elapsed = time.monotonic() - started

    err = excinfo.value
    assert err.code is ErrorCode.COMMAND_FAILED
    assert err.details.get("cancelled") is True
    assert err.details["exit_code"] is not None and err.details["exit_code"] != 0
    assert "cancelled" in str(err).lower()
    # The process was killed almost immediately, nowhere near its own 30s timeout.
    assert elapsed < 5.0


def test_cancel_token_before_bind_is_honored_immediately_on_bind(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "sleep_long2.py"
    script.write_text("import time\ntime.sleep(30)\n")
    token = CancellationToken()
    token.cancel()  # Request cancellation before the process even starts.

    started = time.monotonic()
    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path, timeout=30, cancel_token=token)
    elapsed = time.monotonic() - started

    assert excinfo.value.details.get("cancelled") is True
    assert elapsed < 5.0


def test_cancel_token_is_released_after_the_process_exits(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    token = CancellationToken()
    result = executor.run(["echo", "done"], cwd=tmp_path, cancel_token=token)
    assert result.returncode == 0
    assert token.is_cancelled() is False
    # release() already ran; calling cancel() now must not raise or affect anything.
    token.cancel()
    assert token.is_cancelled() is True


def test_uncancelled_token_does_not_change_success_behavior(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    token = CancellationToken()
    result = executor.run(["echo", "hello"], cwd=tmp_path, cancel_token=token)
    assert result.returncode == 0
    assert "hello" in result.stdout
