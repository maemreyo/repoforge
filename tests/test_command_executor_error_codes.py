import threading
import time
from pathlib import Path

import pytest

from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.domain.errors import CommandError, ErrorCode
from repoforge.ports.cancellation import CancellationToken


def _executor(tmp_path: Path) -> SubprocessCommandExecutor:
    return SubprocessCommandExecutor(ServerConfig(tmp_path / "w", tmp_path / "s"))


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
