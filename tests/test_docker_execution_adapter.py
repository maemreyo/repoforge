"""Tests for DockerExecutionAdapter (#384).

Two tiers:
- Pure unit tests (no Docker needed): policy resolution, the EXECUTION_POLICY_UNSUPPORTED
  raises, enforcement-level mapping, session-context lifecycle.
- Real-Docker integration tests, gated by `_docker_available()`: these are what make the
  adapter's ENFORCED claims honest rather than aspirational -- they run a real container
  and prove containment, not just that the adapter's Python objects are shaped correctly.
  Skipped cleanly (not escalated to the full suite) when Docker is unreachable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from repoforge.adapters.execution.docker_adapter import DockerExecutionAdapter
from repoforge.domain.errors import ErrorCode, RepoForgeError, SecurityError
from repoforge.domain.execution_environment import (
    CommandFailureMode,
    EnforcementLevel,
    EnforcementRequirement,
    EnvironmentAdapterKind,
    ExecutionScope,
    ExecutionScopeKind,
    FilesystemAccess,
    NetworkAccess,
    RequestedExecutionPolicy,
    RequestedResourceLimits,
)
from repoforge.ports.command import CommandExecutor, CommandResult
from repoforge.ports.execution_environment import ExecutionRequest


class _RecordingExecutor:
    """Records every argv the adapter asks it to run; returns a canned success result
    without touching Docker at all -- used only by the pure-unit tests below."""

    def __init__(self, *, returncode: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._returncode = returncode

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

    def run(
        self,
        argv,
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: object | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        return CommandResult(
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=self._returncode,
            stdout="",
            stderr="",
        )

    def run_isolated(self, *a: object, **k: object) -> CommandResult:
        raise NotImplementedError

    def run_bytes(self, argv, *, cwd: Path, timeout: int | None = None, max_bytes: int) -> bytes:
        self.calls.append(tuple(argv))
        return b""


def _request(
    *,
    root: Path,
    network: NetworkAccess = NetworkAccess.OFFLINE,
    filesystem: FilesystemAccess = FilesystemAccess.WORKSPACE_WRITE,
    resources: RequestedResourceLimits | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        scope=ExecutionScope(
            kind=ExecutionScopeKind.WORKSPACE,
            root=root,
            command_cwd=root,
            workspace_id="ws-1",
            working_directory_policy=".",
        ),
        reviewed_commands=(("echo", "hi"),),
        requested_policy=RequestedExecutionPolicy(
            network=network,
            filesystem=filesystem,
            resources=resources if resources is not None else RequestedResourceLimits(),
            enforcement_requirement=EnforcementRequirement.ENFORCEMENT_REQUIRED,
        ),
        timeout_seconds=10,
        output_limit=1000,
        failure_mode=CommandFailureMode.RETURN,
    )


# ---------------------------------------------------------------------------
# Pure unit tests -- no Docker needed (reachability is faked away).
# ---------------------------------------------------------------------------


def _adapter(executor: CommandExecutor) -> DockerExecutionAdapter:
    adapter = DockerExecutionAdapter(executor)
    adapter._reachable = True  # skip the docker-reachability probe for pure unit tests
    return adapter


def test_private_approved_network_is_unsupported(tmp_path: Path) -> None:
    adapter = _adapter(_RecordingExecutor())
    with pytest.raises(RepoForgeError) as exc:
        adapter.prepare_session(_request(root=tmp_path, network=NetworkAccess.PRIVATE_APPROVED))
    assert exc.value.code is ErrorCode.EXECUTION_POLICY_UNSUPPORTED


def test_offline_network_maps_to_enforced() -> None:
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(_request(root=Path("/tmp"), network=NetworkAccess.OFFLINE))
    assert session.effective_policy.network is NetworkAccess.OFFLINE
    assert session.effective_policy.enforcement.network is EnforcementLevel.ENFORCED
    assert session.effective_policy.degraded is False


def test_public_http_https_degrades_to_public_general_honestly() -> None:
    """Docker alone cannot scope egress to HTTP/HTTPS ports -- the adapter must say so,
    not silently claim a narrower enforcement than it actually provides."""
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(
        _request(root=Path("/tmp"), network=NetworkAccess.PUBLIC_HTTP_HTTPS)
    )
    assert session.effective_policy.network is NetworkAccess.PUBLIC_GENERAL
    assert session.effective_policy.degraded is True
    assert "network_port_scoping_unsupported" in session.effective_policy.degradation_reasons
    assert session.effective_policy.enforcement.network is EnforcementLevel.ADVISORY


def test_public_general_network_is_enforced_without_degradation() -> None:
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(
        _request(root=Path("/tmp"), network=NetworkAccess.PUBLIC_GENERAL)
    )
    assert session.effective_policy.network is NetworkAccess.PUBLIC_GENERAL
    assert session.effective_policy.degraded is False
    assert session.effective_policy.enforcement.network is EnforcementLevel.ENFORCED


def test_honest_enforcement_claims_for_the_dimensions_this_issue_is_about() -> None:
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(_request(root=Path("/tmp")))
    enforcement = session.effective_policy.enforcement
    assert enforcement.filesystem is EnforcementLevel.ENFORCED
    assert enforcement.mount is EnforcementLevel.ENFORCED
    assert enforcement.symlink is EnforcementLevel.ENFORCED
    assert enforcement.socket is EnforcementLevel.ENFORCED
    # Not yet backed by a real design in this pass -- must stay honestly UNSUPPORTED.
    assert enforcement.cpu is EnforcementLevel.UNSUPPORTED
    assert enforcement.disk is EnforcementLevel.UNSUPPORTED
    assert enforcement.network_bytes is EnforcementLevel.UNSUPPORTED


def test_memory_and_subprocess_limits_enforced_only_when_requested() -> None:
    adapter = _adapter(_RecordingExecutor())
    unset = adapter.prepare_session(_request(root=Path("/tmp")))
    assert unset.effective_policy.enforcement.memory is EnforcementLevel.UNSUPPORTED
    assert unset.effective_policy.enforcement.subprocess_count is EnforcementLevel.UNSUPPORTED

    limited = adapter.prepare_session(
        _request(
            root=Path("/tmp"),
            resources=RequestedResourceLimits(memory_bytes=100_000_000, subprocesses=8),
        )
    )
    assert limited.effective_policy.enforcement.memory is EnforcementLevel.ENFORCED
    assert limited.effective_policy.enforcement.subprocess_count is EnforcementLevel.ENFORCED
    assert limited.effective_policy.resource_limits.memory_bytes == 100_000_000
    assert limited.effective_policy.resource_limits.subprocesses == 8


def test_adapter_kind_is_hermetic_container() -> None:
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(_request(root=Path("/tmp")))
    assert session.identity.adapter_kind is EnvironmentAdapterKind.HERMETIC_CONTAINER


def test_execute_in_session_after_cleanup_fails_closed() -> None:
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(_request(root=Path("/tmp")))
    adapter.cleanup_session(session)
    with pytest.raises(RepoForgeError) as exc:
        adapter.execute_in_session(
            session,
            ("echo", "hi"),
            cwd=Path("/tmp"),
            timeout=10,
            output_limit=1000,
            check=False,
        )
    assert exc.value.code is ErrorCode.STATE_STALE


def test_execute_in_session_rejects_a_cwd_outside_the_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = _adapter(_RecordingExecutor())
    session = adapter.prepare_session(_request(root=workspace))
    with pytest.raises(SecurityError):
        adapter.execute_in_session(
            session,
            ("echo", "hi"),
            cwd=tmp_path / "outside",
            timeout=10,
            output_limit=1000,
            check=False,
        )


def test_execute_in_session_builds_a_docker_run_invocation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = _RecordingExecutor()
    adapter = _adapter(executor)
    session = adapter.prepare_session(_request(root=workspace, network=NetworkAccess.OFFLINE))

    adapter.execute_in_session(
        session,
        ("python3", "--version"),
        cwd=workspace,
        timeout=10,
        output_limit=1000,
        check=False,
    )

    # Two calls: the `docker run` itself, and the belt-and-suspenders `docker rm -f`
    # cleanup that always follows it (on top of `--rm`).
    assert len(executor.calls) == 2
    docker_argv = executor.calls[0]
    assert docker_argv[0] == "docker"
    assert docker_argv[1] == "run"
    assert "--rm" in docker_argv
    assert "--network" in docker_argv
    assert docker_argv[docker_argv.index("--network") + 1] == "none"
    assert "--cap-drop=ALL" in docker_argv
    assert any(str(workspace) in arg and ":/workspace:rw" in arg for arg in docker_argv)
    assert docker_argv[-2:] == ("python3", "--version")
    assert executor.calls[1][:2] == ("docker", "rm")


def test_execute_in_session_uses_read_only_mount_for_source_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = _RecordingExecutor()
    adapter = _adapter(executor)
    session = adapter.prepare_session(
        _request(root=workspace, filesystem=FilesystemAccess.SOURCE_READ)
    )

    adapter.execute_in_session(
        session, ("echo", "hi"), cwd=workspace, timeout=10, output_limit=1000, check=False
    )

    docker_argv = executor.calls[0]
    assert any(str(workspace) in arg and ":/workspace:ro" in arg for arg in docker_argv)


def test_legacy_transitional_methods_raise_not_implemented() -> None:
    adapter = _adapter(_RecordingExecutor())
    with pytest.raises(NotImplementedError):
        adapter.doctor(None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        adapter.prepare(None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        adapter.identity(None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        adapter.execute(None)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        adapter.collect_artifacts((), workspace_root=Path("/tmp"))
    with pytest.raises(NotImplementedError):
        adapter.cleanup(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Real-Docker integration tests -- these are what make the ENFORCED claims above
# honest. Skipped cleanly when Docker is unreachable (never escalated to the full
# suite): a CI image without Docker must not be told to run every test in the repo.
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon is not reachable on this host"
)


class _HostSubprocessExecutor:
    """A real CommandExecutor for the integration tests: actually spawns `docker run ...`
    as a host subprocess via subprocess.run, exactly like SubprocessCommandExecutor would,
    just without that class's full timeout/redaction machinery (not needed to prove
    containment itself)."""

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        import os

        return {**os.environ, **(extra or {})}

    def run(
        self,
        argv,
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: dict[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: object | None = None,
    ) -> CommandResult:
        import os

        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(extra_env or {})},
        )
        return CommandResult(
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def run_isolated(self, *a: object, **k: object) -> CommandResult:
        raise NotImplementedError

    def run_bytes(self, argv, *, cwd: Path, timeout: int | None = None, max_bytes: int) -> bytes:
        proc = subprocess.run(list(argv), cwd=str(cwd), capture_output=True, timeout=timeout)
        return proc.stdout[:max_bytes]


#: Overridable so a CI image with a different pre-pulled image can point here; deliberately
#: not defaulting to a floating tag that would require a registry pull -- these tests
#: exercise the adapter's containment logic, not registry connectivity, so they must not
#: depend on it. Pick any already-present image with a real shell on this host via
#: `docker images`.
_TEST_IMAGE_ENV = "REPOFORGE_TEST_DOCKER_IMAGE"


def _default_test_image() -> str:
    import os

    configured = os.environ.get(_TEST_IMAGE_ENV)
    if configured:
        return configured
    listing = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in listing.stdout.splitlines():
        if line and "<none>" not in line:
            return line.strip()
    return "debian:bookworm-slim"


def _integration_adapter() -> DockerExecutionAdapter:
    return DockerExecutionAdapter(_HostSubprocessExecutor(), image=_default_test_image())


@requires_docker
def test_offline_network_really_blocks_outbound_connectivity(tmp_path: Path) -> None:
    adapter = _integration_adapter()
    session = adapter.prepare_session(_request(root=tmp_path, network=NetworkAccess.OFFLINE))

    result = adapter.execute_in_session(
        session,
        # A hostname, not a literal IP: some libc's `getent hosts <IP>` trivially
        # echoes the address back without a real lookup, which would make this test
        # pass even with a working network. A hostname forces an actual resolution
        # attempt, which --network none genuinely cannot satisfy.
        ("sh", "-c", "getent hosts example.com >/dev/null 2>&1; echo exit=$?"),
        cwd=tmp_path,
        timeout=30,
        output_limit=10_000,
        check=False,
    )
    adapter.cleanup_session(session)
    assert "exit=0" not in result.stdout


@requires_docker
def test_workspace_write_mount_allows_a_write_visible_on_the_host(tmp_path: Path) -> None:
    adapter = _integration_adapter()
    session = adapter.prepare_session(
        _request(root=tmp_path, filesystem=FilesystemAccess.WORKSPACE_WRITE)
    )

    result = adapter.execute_in_session(
        session,
        ("sh", "-c", "echo written > /workspace/written.txt"),
        cwd=tmp_path,
        timeout=30,
        output_limit=10_000,
        check=False,
    )
    adapter.cleanup_session(session)
    assert result.returncode == 0
    assert (tmp_path / "written.txt").read_text().strip() == "written"


@requires_docker
def test_source_read_mount_rejects_a_write(tmp_path: Path) -> None:
    adapter = _integration_adapter()
    session = adapter.prepare_session(
        _request(root=tmp_path, filesystem=FilesystemAccess.SOURCE_READ)
    )

    result = adapter.execute_in_session(
        session,
        ("sh", "-c", "touch /workspace/should-fail.txt"),
        cwd=tmp_path,
        timeout=30,
        output_limit=10_000,
        check=False,
    )
    adapter.cleanup_session(session)
    assert result.returncode != 0
    assert not (tmp_path / "should-fail.txt").exists()


@requires_docker
def test_a_symlink_to_an_absolute_host_path_resolves_inside_the_container_only(
    tmp_path: Path,
) -> None:
    """#384 AC2/AC4: the container's mount namespace has nothing outside /workspace, so a
    symlink naming an absolute host path cannot escape to the real host file -- there is
    nothing there to escape to."""
    host_secret = tmp_path.parent / "host-secret.txt"
    host_secret.write_text("host secret content\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape-link").symlink_to(host_secret)

    adapter = _integration_adapter()
    session = adapter.prepare_session(
        _request(root=workspace, filesystem=FilesystemAccess.WORKSPACE_WRITE)
    )
    result = adapter.execute_in_session(
        session,
        ("sh", "-c", "cat /workspace/escape-link"),
        cwd=workspace,
        timeout=30,
        output_limit=10_000,
        check=False,
    )
    adapter.cleanup_session(session)
    assert "host secret content" not in result.stdout


@requires_docker
def test_the_docker_control_socket_is_unreachable_inside_the_container(tmp_path: Path) -> None:
    adapter = _integration_adapter()
    session = adapter.prepare_session(_request(root=tmp_path))
    result = adapter.execute_in_session(
        session,
        ("sh", "-c", "test -e /var/run/docker.sock && echo present || echo absent"),
        cwd=tmp_path,
        timeout=30,
        output_limit=10_000,
        check=False,
    )
    adapter.cleanup_session(session)
    assert "absent" in result.stdout


@requires_docker
def test_execute_in_session_leaves_no_orphaned_container(tmp_path: Path) -> None:
    adapter = _integration_adapter()
    session = adapter.prepare_session(_request(root=tmp_path))
    adapter.execute_in_session(
        session, ("echo", "hi"), cwd=tmp_path, timeout=30, output_limit=10_000, check=False
    )
    adapter.cleanup_session(session)

    listing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "label=repoforge.managed=true"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listing.stdout.strip() == ""
