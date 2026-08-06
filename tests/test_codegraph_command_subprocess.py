from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from repoforge.adapters.codegraph.command import CodeGraphCommandRunner
from repoforge.adapters.provider.config_registry import ConfigProviderRegistry
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.domain.codegraph_config import CodeGraphOptions
from repoforge.domain.errors import RepoForgeError
from repoforge.domain.provider_manifest import (
    ProviderExecutableIdentity,
    ProviderFilesystemRequirement,
    ProviderKind,
    ProviderManifest,
    ProviderOutputBounds,
)
from repoforge.ports.cancellation import CancellationToken


class StaticExecutableLocator:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def which(self, executable: str, *, path: str | None = None) -> str | None:
        del path
        return self.paths.get(executable)


def _write_fake_executable(tmp_path: Path, *, version: str = "1.5.0") -> Path:
    body = Path("tests/fixtures/fake_codegraph.py").read_text(encoding="utf-8")
    body = body.replace('VERSION = "1.5.0"', f"VERSION = {version!r}")
    executable = tmp_path / "codegraph"
    executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _manifest(
    executable: Path,
    *,
    options: CodeGraphOptions | None = None,
) -> ProviderManifest:
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return ProviderManifest(
        provider_id="codegraph",
        kind=ProviderKind.ANALYZER,
        version="1.5.0",
        runtime=ProviderExecutableIdentity("codegraph", digest),
        supported_capabilities=("semantic_graph",),
        network_policy="none",
        filesystem=ProviderFilesystemRequirement(capability="managed_state_write"),
        output_bounds=ProviderOutputBounds(20_000, 20_000, 1_000_000),
        codegraph=options or CodeGraphOptions(),
    )


def _runner(
    tmp_path: Path,
    executable: Path,
    *,
    options: CodeGraphOptions | None = None,
) -> CodeGraphCommandRunner:
    manifest = _manifest(executable, options=options)
    registry = ConfigProviderRegistry(
        (manifest,),
        StaticExecutableLocator({"codegraph": str(executable)}),
    )
    executor = SubprocessCommandExecutor(ServerConfig(tmp_path / "workspaces", tmp_path / "state"))
    return CodeGraphCommandRunner(manifest, registry, executor)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    projection = tmp_path / "projection"
    home = tmp_path / "home"
    projection.mkdir()
    home.mkdir()
    return projection, home


def test_real_subprocess_cancellation_reuses_process_group_containment(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    runner = _runner(tmp_path, executable)
    token = CancellationToken()
    assert runner.version(projection, home) == "1.5.0"

    def cancel_soon() -> None:
        time.sleep(0.3)
        token.cancel()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(RepoForgeError, match="query"):
        runner.query(projection, home, "__sleep__", cancel_token=token)
    assert time.monotonic() - started < 5


def test_real_subprocess_timeout_reuses_bounded_executor(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    runner = _runner(
        tmp_path,
        executable,
        options=CodeGraphOptions(query_timeout_seconds=1),
    )
    assert runner.version(projection, home) == "1.5.0"

    started = time.monotonic()
    with pytest.raises(RepoForgeError, match="query") as excinfo:
        runner.query(projection, home, "__sleep__")

    assert "timeout" not in str(excinfo.value).lower()
    assert time.monotonic() - started < 5


def test_real_malformed_utf8_is_replaced_at_existing_executor_boundary(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    runner = _runner(tmp_path, executable)

    output = runner.query(projection, home, "__invalid_utf8__")

    assert "\ufffd" in output.stdout


def test_real_stale_lock_failure_is_sanitized(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    runner = _runner(tmp_path, executable)

    with pytest.raises(RepoForgeError, match="query") as excinfo:
        runner.query(projection, home, "__stale_lock__")

    assert "private stale lock path" not in str(excinfo.value)


def test_real_cancellation_reaps_provider_descendants(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    runner = _runner(tmp_path, executable)
    token = CancellationToken()
    marker = tmp_path / "child.pid"
    assert runner.version(projection, home) == "1.5.0"

    def cancel_after_spawn() -> None:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        token.cancel()

    thread = threading.Thread(target=cancel_after_spawn, daemon=True)
    thread.start()
    with pytest.raises(RepoForgeError, match="query"):
        runner.query(projection, home, f"__spawn__:{marker}", cancel_token=token)
    thread.join(timeout=1)

    assert marker.is_file()
    child_pid = int(marker.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("CodeGraph descendant survived process-group cancellation")


def test_real_fake_binary_receives_no_ambient_environment(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    runner = _runner(tmp_path, executable)
    os.environ["REPOFORGE_CODEGRAPH_TEST_SECRET"] = "must-not-pass"
    try:
        output = runner.status(projection, home)
    finally:
        os.environ.pop("REPOFORGE_CODEGRAPH_TEST_SECRET", None)

    payload = json.loads(output.stdout)
    assert payload["argv"] == ["status", str(projection.resolve()), "--json"]
    assert "REPOFORGE_CODEGRAPH_TEST_SECRET" not in payload["environment"]
    assert payload["environment"]["CODEGRAPH_NO_DAEMON"] == "1"
