import contextlib
import os
import signal
import subprocess
import sys
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


def test_run_preserves_complete_failed_selectors_before_output_truncation(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "many_failures.py"
    script.write_text(
        "import sys\n"
        "print('FAILED tests/test_alpha.py::test_one')\n"
        "print('x' * 5000)\n"
        "print('FAILED tests/test_beta.py::TestCase::test_two[param]')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    result = executor.run(
        ["python3", str(script)],
        cwd=tmp_path,
        check=False,
        output_limit=100,
    )

    assert result.stdout_truncated is True
    assert result.failed_selectors == (
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::TestCase::test_two[param]",
    )
    assert result.output_artifact_reference is not None
    prefix = "failure-output:"
    assert result.output_artifact_reference.startswith(prefix)
    digest = result.output_artifact_reference.removeprefix(prefix)
    artifact = tmp_path / "s" / "failure-output-artifacts" / f"{digest}.blob"
    body = artifact.read_text(encoding="utf-8")
    assert "tests/test_alpha.py::test_one" in body
    assert "tests/test_beta.py::TestCase::test_two[param]" in body


def test_run_persists_output_artifact_for_oversized_successful_output(
    tmp_path: Path,
) -> None:
    """Review finding (#377): output-artifact persistence used to be failure-only, so a
    successful command's oversized output was truncated with no way to retrieve the
    dropped middle. Generalized to persist whenever output is truncated, regardless of
    exit code -- same store, same reference shape, as the pre-existing failure path."""
    executor = _executor(tmp_path)
    script = tmp_path / "large_success.py"
    script.write_text("print('x' * 5000)\n", encoding="utf-8")

    result = executor.run(["python3", str(script)], cwd=tmp_path, output_limit=100)

    assert result.returncode == 0
    assert result.stdout_truncated is True
    assert result.output_artifact_status == "available"
    assert result.output_artifact_reference is not None
    prefix = "failure-output:"
    assert result.output_artifact_reference.startswith(prefix)
    digest = result.output_artifact_reference.removeprefix(prefix)
    artifact = tmp_path / "s" / "failure-output-artifacts" / f"{digest}.blob"
    assert "x" * 5000 in artifact.read_text(encoding="utf-8")


def test_run_does_not_persist_artifact_when_output_fits_inline(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    result = executor.run(["echo", "small"], cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout_truncated is False
    assert result.output_artifact_reference is None
    assert result.output_artifact_status == "not_applicable"


def test_command_error_carries_complete_failed_selectors_and_artifact_reference(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "failed_nodes.py"
    script.write_text(
        "import sys\n"
        "print('FAILED tests/test_alpha.py::test_one')\n"
        "print('x' * 5000)\n"
        "print('FAILED tests/test_beta.py::test_two')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    with pytest.raises(CommandError) as excinfo:
        executor.run(
            ["python3", str(script)],
            cwd=tmp_path,
            output_limit=100,
        )

    assert excinfo.value.details["failed_selectors"] == [
        "tests/test_alpha.py::test_one",
        "tests/test_beta.py::test_two",
    ]
    reference = excinfo.value.details["output_artifact_reference"]
    assert isinstance(reference, str)
    assert reference.startswith("failure-output:")


def test_run_timeout_is_command_timeout(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "sleep.py"
    script.write_text("import time\nprint('before timeout', flush=True)\ntime.sleep(5)\n")
    with pytest.raises(CommandError) as excinfo:
        executor.run(["python3", str(script)], cwd=tmp_path, timeout=1)
    err = excinfo.value
    assert err.code is ErrorCode.COMMAND_TIMEOUT
    assert err.retryable is True
    assert err.details["timeout_seconds"] == 1
    assert err.details["output_artifact_status"] == "available"
    reference = err.details["output_artifact_reference"]
    assert isinstance(reference, str)
    digest = reference.removeprefix("failure-output:")
    artifact = tmp_path / "s" / "failure-output-artifacts" / f"{digest}.blob"
    assert "before timeout" in artifact.read_text(encoding="utf-8")


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
    parsed = process_tree._parse_ps_line("S 123 42 Sun Jul 19 12:34:56 2026")
    assert parsed == process_tree.ProcessIdentity(
        pid=123,
        ppid=42,
        start_token="Sun Jul 19 12:34:56 2026",
    )
    # A process that has stopped and only awaits a wait() from its parent is not
    # live: reporting it as live makes a successful kill look like a survivor.
    assert process_tree._parse_ps_line("Z 123 42 Sun Jul 19 12:34:56 2026") is None
    assert process_tree._parse_ps_line("123 42 Sun Jul 19 12:34:56 2026") is None
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
        lambda argv: (
            "S 123 42 Sun Jul 19 12:34:56 2026\n"
            "Z 999 42 Sun Jul 19 12:34:56 2026\n"
            "S+ 124 42 Sun Jul 19 12:34:57 2026\n"
        ),
    )
    assert process_tree._read_ps_identities() == (
        process_tree.ProcessIdentity(123, 42, "Sun Jul 19 12:34:56 2026"),
        process_tree.ProcessIdentity(124, 42, "Sun Jul 19 12:34:57 2026"),
    )


def test_group_liveness_ignores_zombies_and_fails_closed_when_uninspectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group of stopped-but-unreaped members is contained; an unknown host is not."""
    if sys.platform.startswith("linux"):  # pragma: no cover - probe is /proc-based there
        pytest.skip("the ps-based group probe is only used off Linux")

    monkeypatch.setattr(process_tree, "_bounded_ps", lambda argv: "Z 4242\nS 77\n")
    assert process_tree.group_has_live_member(4242) is False

    monkeypatch.setattr(process_tree, "_bounded_ps", lambda argv: "Z 4242\nS 4242\n")
    assert process_tree.group_has_live_member(4242) is True

    monkeypatch.setattr(process_tree, "_bounded_ps", lambda argv: None)
    assert process_tree.group_has_live_member(4242) is None
    assert process_tree.group_has_live_member(0) is None


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
    assert err.details["selector_coverage"] == "unavailable"
    assert err.details["selectors_unavailable_reason"] == "artifact_unavailable"
    assert err.details["output_artifact_status"] == "not_applicable"


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


def test_spawn_boundary_failure_prevents_subprocess_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed durable spawn marker must fail closed before creating a child."""
    executor = _executor(tmp_path)
    popen_called = False

    def unexpected_popen(*args: object, **kwargs: object) -> None:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run after spawn-boundary persistence fails")

    def fail_marker() -> None:
        raise RepoForgeError(
            "cannot persist spawn boundary", code=ErrorCode.STATE_PERSISTENCE_FAILED
        )

    monkeypatch.setattr(command_executor_module.subprocess, "Popen", unexpected_popen)
    token = CancellationToken(on_spawn=fail_marker, raise_on_spawn_error=True)

    with pytest.raises(RepoForgeError, match="cannot persist spawn boundary"):
        executor.run(["echo", "never"], cwd=tmp_path, cancel_token=token)

    assert popen_called is False


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


def test_run_extracts_ruff_failure_location_and_exact_path_selector(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "ruff_failure.py"
    script.write_text(
        "import sys\nprint('src/demo.py:12:5: F821 Undefined name `missing`')\nsys.exit(1)\n",
        encoding="utf-8",
    )

    result = executor.run(
        ["python3", str(script)],
        cwd=tmp_path,
        check=False,
        output_limit=40,
    )

    assert result.failure_provider == "ruff"
    assert result.selector_coverage == "complete"
    assert result.selectors_unavailable_reason is None
    assert result.failed_selectors == ("src/demo.py",)
    assert len(result.failure_locations) == 1
    location = result.failure_locations[0]
    assert location.path == "src/demo.py"
    assert location.line == 12
    assert location.column == 5
    assert location.code == "F821"


def test_run_persists_every_failure_as_secret_safe_artifact(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    secret = "ghp_" + "a" * 36
    script = tmp_path / "secret_failure.py"
    script.write_text(
        f"import sys\nprint('token={secret}')\nprint('ordinary diagnostic context')\nsys.exit(1)\n",
        encoding="utf-8",
    )

    result = executor.run(["python3", str(script)], cwd=tmp_path, check=False)

    assert result.output_artifact_reference is not None
    assert result.output_artifact_status == "available"
    digest = result.output_artifact_reference.removeprefix("failure-output:")
    artifact = tmp_path / "s" / "failure-output-artifacts" / f"{digest}.blob"
    body = artifact.read_text(encoding="utf-8")
    assert secret not in body
    assert "<redacted>" in body
    assert "ordinary diagnostic context" in body


def test_run_reports_typed_selector_unavailability_for_unrecognized_output(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "custom_failure.py"
    script.write_text(
        "import sys\nprint('custom build exploded without a location')\nsys.exit(1)\n",
        encoding="utf-8",
    )

    result = executor.run(["python3", str(script)], cwd=tmp_path, check=False)

    assert result.failure_provider == "custom"
    assert result.selector_coverage == "unavailable"
    assert result.selectors_unavailable_reason == "output_unrecognized"
    assert result.failed_selectors == ()
    assert result.failure_locations == ()


def test_bounded_selector_coverage(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    script = tmp_path / "many_failures.py"
    script.write_text(
        "import sys\n"
        "[print(f'FAILED tests/test_many.py::test_{index}') for index in range(101)]\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )

    result = executor.run(["python3", str(script)], cwd=tmp_path, check=False)

    assert len(result.failed_selectors) == 100
    assert result.selector_coverage == "partial"
    assert result.selectors_unavailable_reason == "selectors_truncated"


def test_uncancelled_token_does_not_change_success_behavior(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    token = CancellationToken()
    result = executor.run(["echo", "hello"], cwd=tmp_path, cancel_token=token)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_launch_gate_prevents_target_after_owner_crashes_before_binding(tmp_path: Path) -> None:
    """The reviewed target must not execute until durable child binding succeeds."""
    marker = tmp_path / "target-ran"
    bind_started = tmp_path / "bind-started"
    worker_script = tmp_path / "crash_during_bind.py"
    target_code = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('ran', encoding='utf-8')"
    )
    worker_script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from repoforge.adapters.subprocess import SubprocessCommandExecutor\n"
        "from repoforge.config import ServerConfig\n"
        "from repoforge.ports.cancellation import CancellationToken\n"
        f"root = Path({str(tmp_path)!r})\n"
        f"marker = Path({str(marker)!r})\n"
        f"bind_started = Path({str(bind_started)!r})\n"
        "def block_binding(pid):\n"
        "    bind_started.write_text(str(pid), encoding='utf-8')\n"
        "    time.sleep(30)\n"
        "executor = SubprocessCommandExecutor(ServerConfig(root / 'w', root / 's'))\n"
        "token = CancellationToken(on_bind=block_binding, raise_on_bind_error=True)\n"
        f"executor.run([sys.executable, '-c', {target_code!r}, str(marker)], "
        "cwd=root, timeout=30, cancel_token=token)\n",
        encoding="utf-8",
    )

    worker = subprocess.Popen([sys.executable, str(worker_script)])
    try:
        deadline = time.monotonic() + 3
        while not bind_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert bind_started.exists()
        time.sleep(0.2)
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=3)
        time.sleep(0.2)

        assert not marker.exists()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


def test_launch_gate_runs_exact_argv_after_durable_binding(tmp_path: Path) -> None:
    """A successful durable bind releases exactly the reviewed target argv."""
    executor = _executor(tmp_path)
    bound_pids: list[int] = []
    token = CancellationToken(on_bind=bound_pids.append, raise_on_bind_error=True)
    target = "import sys; print('|'.join(sys.argv[1:]))"

    result = executor.run(
        [sys.executable, "-c", target, "--alpha", "two words"],
        cwd=tmp_path,
        cancel_token=token,
    )

    assert bound_pids
    assert result.returncode == 0
    assert result.stdout.strip() == "--alpha|two words"


def test_gated_popen_failure_closes_both_pipe_ends_and_is_not_target_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapper/cwd spawn failure must close the gate and preserve target semantics."""
    executor = _executor(tmp_path)
    closed: list[int] = []

    monkeypatch.setattr(command_executor_module.os, "pipe", lambda: (101, 102))
    monkeypatch.setattr(command_executor_module.os, "close", closed.append)

    def fail_popen(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("missing gated cwd")

    monkeypatch.setattr(command_executor_module.subprocess, "Popen", fail_popen)

    with pytest.raises(CommandError) as excinfo:
        executor.run(
            ["reviewed-target", "--flag"],
            cwd=tmp_path / "missing",
            cancel_token=CancellationToken(),
        )

    assert excinfo.value.code is ErrorCode.COMMAND_FAILED
    assert sorted(closed) == [101, 102]
