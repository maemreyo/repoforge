from pathlib import Path

import pytest

from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.domain.errors import CommandError, ErrorCode


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
