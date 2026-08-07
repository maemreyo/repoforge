import hashlib
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from repoforge.adapters.codegraph.command import CodeGraphCommandOutput, CodeGraphCommandRunner
from repoforge.adapters.provider.config_registry import ConfigProviderRegistry
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
from repoforge.ports.command import CommandExecutor, CommandResult


class StaticExecutableLocator:
    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def which(self, executable: str, *, path: str | None = None) -> str | None:
        del path
        return self.paths.get(executable)


@dataclass(frozen=True)
class IsolatedCall:
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    secrets: tuple[str, ...]
    timeout: int | None
    check: bool
    output_limit: int | None
    cancel_token: CancellationToken | None


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[IsolatedCall] = []
        self.responses: list[CommandResult] = []

    def enqueue(self, *responses: CommandResult) -> None:
        self.responses.extend(responses)

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, extra_env
        return self.run_isolated(
            argv,
            cwd=cwd,
            environment={},
            secrets=(),
            timeout=timeout,
            check=check,
            output_limit=output_limit,
            cancel_token=cancel_token,
        )

    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text
        call = IsolatedCall(
            tuple(argv),
            cwd,
            dict(environment),
            tuple(secrets),
            timeout,
            check,
            output_limit,
            cancel_token,
        )
        self.calls.append(call)
        if self.responses:
            return self.responses.pop(0)
        return CommandResult(tuple(argv), str(cwd), 0, "", "")

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        return self.run(argv, cwd=cwd, timeout=timeout, output_limit=max_bytes).stdout.encode()


def _write_fake_executable(tmp_path: Path, *, version: str = "1.5.0") -> Path:
    body = Path("tests/fixtures/fake_codegraph.py").read_text(encoding="utf-8")
    body = body.replace('VERSION = "1.5.0"', f"VERSION = {version!r}")
    executable = tmp_path / "codegraph"
    executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _manifest(executable: Path, *, version: str = "1.5.0") -> ProviderManifest:
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    return ProviderManifest(
        provider_id="codegraph",
        kind=ProviderKind.ANALYZER,
        version=version,
        runtime=ProviderExecutableIdentity("codegraph", digest),
        supported_capabilities=("semantic_graph",),
        network_policy="none",
        filesystem=ProviderFilesystemRequirement(capability="managed_state_write"),
        output_bounds=ProviderOutputBounds(20_000, 20_000, 1_000_000),
        codegraph=CodeGraphOptions(),
    )


def _runner(
    executable: Path,
    executor: CommandExecutor,
    *,
    version: str = "1.5.0",
) -> CodeGraphCommandRunner:
    manifest = _manifest(executable, version=version)
    registry = ConfigProviderRegistry(
        (manifest,),
        StaticExecutableLocator({"codegraph": str(executable)}),
    )
    return CodeGraphCommandRunner(manifest, registry, executor)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    projection = tmp_path / "projection"
    home = tmp_path / "home"
    projection.mkdir()
    home.mkdir()
    return projection, home


def _result(argv: tuple[str, ...], cwd: Path, stdout: str, *, returncode: int = 0) -> CommandResult:
    return CommandResult(argv, str(cwd), returncode, stdout, "provider-private-detail")


def test_version_uses_verified_absolute_executable_and_exact_environment(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    executor.enqueue(_result((str(executable), "version"), projection, "1.5.0\n"))
    runner = _runner(executable, executor)

    assert runner.version(projection, home) == "1.5.0"

    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call.argv == (str(executable.resolve()), "version")
    assert call.cwd == projection.resolve()
    assert call.environment == {
        "CI": "1",
        "CODEGRAPH_DIR": ".index",
        "CODEGRAPH_NO_DAEMON": "1",
        "CODEGRAPH_NO_DOWNLOAD": "1",
        "CODEGRAPH_NO_UPDATE_CHECK": "1",
        "CODEGRAPH_TELEMETRY": "0",
        "DO_NOT_TRACK": "1",
        "HOME": str(home.resolve()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "XDG_CACHE_HOME": str((home / "cache").resolve()),
        "XDG_CONFIG_HOME": str((home / "config").resolve()),
        "XDG_DATA_HOME": str((home / "data").resolve()),
    }
    assert call.secrets == ()
    assert call.timeout == 15
    assert call.check is False
    assert call.output_limit == 20_000


def test_version_mismatch_is_sanitized_and_not_cached(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    executor.enqueue(
        _result((str(executable), "version"), projection, "unexpected-private-version\n"),
        _result((str(executable), "version"), projection, "unexpected-private-version\n"),
    )
    runner = _runner(executable, executor)

    for _ in range(2):
        with pytest.raises(RepoForgeError, match="version") as excinfo:
            runner.version(projection, home)
        assert "unexpected-private-version" not in str(excinfo.value)
    assert len(executor.calls) == 2


def test_registry_digest_failure_prevents_process_start(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    manifest = _manifest(executable)
    executable.write_text("changed", encoding="utf-8")
    registry = ConfigProviderRegistry(
        (manifest,), StaticExecutableLocator({"codegraph": str(executable)})
    )
    runner = CodeGraphCommandRunner(manifest, registry, executor)

    with pytest.raises(RepoForgeError, match="unavailable"):
        runner.version(projection, home)
    assert executor.calls == []


def test_init_sync_and_status_use_only_allowlisted_argv(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    exe = str(executable.resolve())
    executor.enqueue(
        _result((exe, "version"), projection, "1.5.0\n"),
        _result((exe, "init", str(projection.resolve())), projection, "initialized\n"),
        _result((exe, "sync", str(projection.resolve()), "--quiet"), projection, "synced\n"),
        _result((exe, "status", str(projection.resolve()), "--json"), projection, '{"ok":true}\n'),
    )
    runner = _runner(executable, executor)

    assert runner.init(projection, home).stdout == "initialized\n"
    assert runner.sync(projection, home).stdout == "synced\n"
    assert runner.status(projection, home).stdout == '{"ok":true}\n'

    assert [call.argv for call in executor.calls] == [
        (exe, "version"),
        (exe, "init", str(projection.resolve())),
        (exe, "sync", str(projection.resolve()), "--quiet"),
        (exe, "status", str(projection.resolve()), "--json"),
    ]
    assert [call.timeout for call in executor.calls] == [15, 120, 60, 15]


def test_query_commands_build_bounded_machine_readable_argv(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    exe = str(executable.resolve())
    executor.enqueue(
        _result((exe, "version"), projection, "1.5.0\n"),
        _result((), projection, "{}\n"),
        _result((), projection, "{}\n"),
        _result((), projection, "{}\n"),
        _result((), projection, "{}\n"),
        _result((), projection, "{}\n"),
    )
    runner = _runner(executable, executor)

    token = CancellationToken()
    assert runner.affected(projection, home, ("src/a.py", "src/b.py"), depth=3).stdout
    assert runner.query(projection, home, "UserService", limit=7, cancel_token=token).stdout
    assert runner.callers(projection, home, "api.handle", limit=8).stdout
    assert runner.callees(projection, home, "api.handle", limit=9).stdout
    assert runner.impact(projection, home, "api.handle", depth=4).stdout

    assert [call.argv for call in executor.calls[1:]] == [
        (exe, "affected", "src/a.py", "src/b.py", "--depth", "3", "--json"),
        (exe, "query", "UserService", "--limit", "7", "--json"),
        (exe, "callers", "api.handle", "--limit", "8", "--json"),
        (exe, "callees", "api.handle", "--limit", "9", "--json"),
        (exe, "impact", "api.handle", "--depth", "4", "--json"),
    ]
    assert executor.calls[2].cancel_token is token


@pytest.mark.parametrize(
    ("method", "argument"),
    [
        ("query", "--help"),
        ("callers", "bad\nname"),
        ("affected", "/absolute.py"),
        ("affected", "../escape.py"),
    ],
)
def test_untrusted_arguments_are_rejected_before_execution(
    tmp_path: Path,
    method: str,
    argument: str,
) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    runner = _runner(executable, executor)

    with pytest.raises(ValueError):
        if method == "query":
            runner.query(projection, home, argument)
        elif method == "callers":
            runner.callers(projection, home, argument)
        else:
            runner.affected(projection, home, (argument,))
    assert executor.calls == []


def test_managed_root_parent_symlink_is_rejected_before_execution(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    outside = tmp_path / "outside"
    projection = outside / "projection"
    home = outside / "home"
    projection.mkdir(parents=True)
    home.mkdir()
    alias = tmp_path / "managed-alias"
    alias.symlink_to(outside, target_is_directory=True)
    executor = RecordingExecutor()
    runner = _runner(executable, executor)

    with pytest.raises(ValueError, match="symlink"):
        runner.version(alias / "projection", alias / "home")

    assert executor.calls == []


def test_nonzero_exit_does_not_expose_provider_output(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    exe = str(executable.resolve())
    executor.enqueue(
        _result((exe, "version"), projection, "1.5.0\n"),
        _result((exe, "query"), projection, "private-stdout", returncode=7),
    )
    runner = _runner(executable, executor)

    with pytest.raises(RepoForgeError, match="query") as excinfo:
        runner.query(projection, home, "UserService")
    message = str(excinfo.value)
    assert "private-stdout" not in message
    assert "provider-private-detail" not in message


def test_output_truncation_is_typed_without_exposing_stderr(tmp_path: Path) -> None:
    executable = _write_fake_executable(tmp_path)
    projection, home = _roots(tmp_path)
    executor = RecordingExecutor()
    exe = str(executable.resolve())
    executor.enqueue(
        _result((exe, "version"), projection, "1.5.0\n"),
        CommandResult((exe, "status"), str(projection), 0, "{}", "hidden", True, True),
    )
    runner = _runner(executable, executor)

    output = runner.status(projection, home)

    assert output == CodeGraphCommandOutput(command="status", stdout="{}", truncated=True)
    assert not hasattr(output, "stderr")
