from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from repoforge.adapters.execution.native import NativeReviewedAdapter
from repoforge.application.execution.coordinator import ExecutionCoordinator
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError, SecurityError
from repoforge.domain.execution_environment import (
    CommandFailureMode,
    EffectiveExecutionPolicy,
    EffectiveResourceLimits,
    EnforcementAssessment,
    EnforcementRequirement,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
    ExecutionScope,
    ExecutionScopeKind,
    FilesystemAccess,
    NetworkAccess,
    RequestedExecutionPolicy,
    ToolVersion,
)
from repoforge.ports.command import CommandResult
from repoforge.ports.execution_environment import (
    ApprovedExecution,
    ArtifactResult,
    EnvironmentInspection,
    ExecutionRequest,
    PreparedEnvironmentSession,
)


class RecordingExecutor:
    def __init__(self, *, missing: frozenset[str] = frozenset()) -> None:
        self.missing = missing
        self.calls: list[tuple[str, ...]] = []

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"PATH": "/private/bin", "LANG": "en_US.UTF-8", **dict(extra or {})}

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
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit
        command = tuple(argv)
        self.calls.append(command)
        if command[0] in self.missing:
            raise CommandError(f"Executable not found: {command[0]}")
        stdout = f"{command[0]} 1.0\n" if command[1:] == ("--version",) else "ok"
        return CommandResult(command, str(cwd), 0, stdout, "")

    def run_bytes(
        self, argv: Sequence[str], *, cwd: Path, timeout: int | None = None, max_bytes: int
    ) -> bytes:
        del argv, cwd, timeout, max_bytes
        return b""


def request(root: Path, *commands: tuple[str, ...]) -> EnvironmentIdentityRequest:
    return EnvironmentIdentityRequest(root, root, commands, ".")


def session_request(
    root: Path,
    *,
    enforcement: EnforcementRequirement = EnforcementRequirement.ADVISORY_BACKEND_ALLOWED,
) -> ExecutionRequest:
    return ExecutionRequest(
        scope=ExecutionScope(
            kind=ExecutionScopeKind.WORKSPACE,
            root=root,
            command_cwd=root,
            workspace_id="workspace-1",
            working_directory_policy=".",
        ),
        reviewed_commands=(("python", "-m", "pytest"),),
        requested_policy=RequestedExecutionPolicy(
            network=NetworkAccess.OFFLINE,
            filesystem=FilesystemAccess.SOURCE_READ,
            enforcement_requirement=enforcement,
        ),
        timeout_seconds=30,
        output_limit=1_000,
        failure_mode=CommandFailureMode.RETURN,
    )


def test_native_session_reports_truthful_advisory_policy(tmp_path: Path) -> None:
    adapter = NativeReviewedAdapter(RecordingExecutor())
    requested = session_request(tmp_path)

    session = adapter.prepare_session(requested)
    inspection = adapter.inspect_session(requested, session)

    assert session.requested_policy_hash == requested.requested_policy.policy_hash
    assert session.effective_policy.network is NetworkAccess.HOST_INHERITED
    assert session.effective_policy.filesystem is FilesystemAccess.HOST_ACCOUNT_ACCESS
    assert session.effective_policy.enforcement.network.value == "advisory"
    assert session.identity.schema_version == 2
    assert inspection.identity.identity_hash == session.identity.identity_hash


def test_native_session_rejects_required_enforcement_before_process_start(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)

    with pytest.raises(RepoForgeError) as excinfo:
        adapter.prepare_session(
            session_request(tmp_path, enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED)
        )

    assert excinfo.value.code is ErrorCode.EXECUTION_POLICY_UNSUPPORTED
    assert executor.calls == []


def test_identity_inspects_only_profile_tools_and_hashes_reviewed_inputs(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("locked", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)

    identity = adapter.identity(request(tmp_path, ("python", "-m", "pytest")))

    assert executor.calls == [("python", "--version")]
    assert identity.tools[0].version == "python 1.0"
    assert identity.lockfile_digests[0][0] == "uv.lock"
    assert identity.manifest_digests[0][0] == "pyproject.toml"
    assert identity.approved_env_var_names == ("LANG", "PATH")
    assert "/private/bin" not in repr(identity)


def test_missing_tool_produces_partial_identity_and_warning(tmp_path: Path) -> None:
    adapter = NativeReviewedAdapter(RecordingExecutor(missing=frozenset({"missing"})))
    identity_request = request(tmp_path, ("missing", "check"))

    identity = adapter.identity(identity_request)

    assert identity.tools == (identity.tools[0],)
    assert identity.tools[0].version is None
    assert identity.cache_eligible is False
    assert "missing" in adapter.doctor(identity_request)[0]


def test_execute_preserves_profile_command_contract(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)
    identity_request = request(tmp_path, ("python", "-m", "pytest"))
    identity = adapter.identity(identity_request)

    receipt = adapter.execute(
        ApprovedExecution(("python", "-m", "pytest"), identity_request, identity, 30)
    )

    assert receipt.argv == ("python", "-m", "pytest")
    assert receipt.identity_hash == identity.identity_hash
    assert receipt.result.stdout == "ok"


def test_unknown_profile_executable_is_not_probed(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)

    identity = adapter.identity(request(tmp_path, ("custom-build", "verify")))

    assert executor.calls == []
    assert identity.tools[0].version is None


def test_artifact_collection_rejects_escape_symlink_and_oversize(tmp_path: Path) -> None:
    adapter = NativeReviewedAdapter(RecordingExecutor(), max_artifact_bytes=3)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    (tmp_path / "large.txt").write_text("large", encoding="utf-8")

    with pytest.raises(SecurityError, match="escapes workspace"):
        adapter.collect_artifacts(("../outside.txt",), workspace_root=tmp_path)
    with pytest.raises(SecurityError, match="symlink"):
        adapter.collect_artifacts(("link.txt",), workspace_root=tmp_path)
    with pytest.raises(SecurityError, match="byte limit"):
        adapter.collect_artifacts(("large.txt",), workspace_root=tmp_path)


def test_collects_bounded_regular_artifact(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_text("ok", encoding="utf-8")
    adapter = NativeReviewedAdapter(RecordingExecutor(), max_artifact_bytes=3)

    artifacts = adapter.collect_artifacts(("result.txt", "missing.txt"), workspace_root=tmp_path)

    assert len(artifacts) == 1
    assert artifacts[0].path == "result.txt"
    assert artifacts[0].size_bytes == 2


class RecordingSessionBackend:
    def __init__(self) -> None:
        self.prepared = 0
        self.executed: list[tuple[tuple[str, ...], bool]] = []
        self.inspected = 0
        self.cleaned = 0
        self.collected = 0

    @staticmethod
    def _effective_policy() -> EffectiveExecutionPolicy:
        return EffectiveExecutionPolicy(
            network=NetworkAccess.HOST_INHERITED,
            filesystem=FilesystemAccess.HOST_ACCOUNT_ACCESS,
            resource_limits=EffectiveResourceLimits(),
            enforcement=EnforcementAssessment(),
            degraded=True,
            degradation_reasons=("network_not_isolated", "filesystem_not_isolated"),
        )

    def _session(self, execution_request: ExecutionRequest) -> PreparedEnvironmentSession:
        effective = self._effective_policy()
        identity = EnvironmentIdentity(
            platform="linux",
            architecture="arm64",
            python_version="3.13",
            runtime_version="python/3.13",
            tools=(ToolVersion("python", "3.13"),),
            requested_policy_hash=execution_request.requested_policy.policy_hash,
            effective_policy_hash=effective.policy_hash,
            effective_network=effective.network,
            effective_filesystem=effective.filesystem,
            enforcement_assessment=effective.enforcement,
            backend_capability_hash="a" * 64,
            working_directory_policy_hash="b" * 64,
        )
        return PreparedEnvironmentSession(
            session_id="session-1",
            identity=identity,
            requested_policy_hash=execution_request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
        )

    def prepare_session(self, execution_request: ExecutionRequest) -> PreparedEnvironmentSession:
        self.prepared += 1
        return self._session(execution_request)

    def inspect_session(
        self,
        execution_request: ExecutionRequest,
        session: PreparedEnvironmentSession | None = None,
    ) -> EnvironmentInspection:
        self.inspected += 1
        prepared = session or self._session(execution_request)
        return EnvironmentInspection(
            identity=prepared.identity,
            requested_policy_hash=prepared.requested_policy_hash,
            effective_policy=prepared.effective_policy,
            effective_policy_hash=prepared.effective_policy_hash,
        )

    def execute_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        output_limit: int,
        check: bool,
        cancel_token=None,
    ) -> CommandResult:
        del session, timeout, output_limit, cancel_token
        self.executed.append((argv, check))
        return CommandResult(argv, str(cwd), 0, "ok", "")

    def collect_session_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: tuple[str, ...],
        *,
        root: Path,
    ) -> tuple[ArtifactResult, ...]:
        del session, root
        self.collected += 1
        return tuple(ArtifactResult(path, 2, "a" * 64) for path in artifact_paths)

    def cleanup_session(self, session: PreparedEnvironmentSession) -> None:
        del session
        self.cleaned += 1


def coordinator_request(root: Path, failure_mode: CommandFailureMode) -> ExecutionRequest:
    return ExecutionRequest(
        scope=ExecutionScope(
            kind=ExecutionScopeKind.WORKSPACE,
            root=root,
            command_cwd=root,
            workspace_id="workspace-1",
            working_directory_policy=".",
        ),
        reviewed_commands=(("python", "-m", "pytest"), ("python", "-m", "mypy")),
        requested_policy=RequestedExecutionPolicy(
            network=NetworkAccess.OFFLINE,
            filesystem=FilesystemAccess.SOURCE_READ,
        ),
        timeout_seconds=30,
        output_limit=1_000,
        artifact_paths=("result.json",),
        failure_mode=failure_mode,
    )


def test_coordinator_prepares_executes_inspects_and_cleans_exactly_once(
    tmp_path: Path,
) -> None:
    backend = RecordingSessionBackend()
    coordinator = ExecutionCoordinator(backend)

    with coordinator.prepare(coordinator_request(tmp_path, CommandFailureMode.RAISE)) as session:
        first = session.execute(("python", "-m", "pytest"))
        second = session.execute(("python", "-m", "mypy"))
        inspection = session.inspect()

    assert first.result.stdout == "ok"
    assert second.result.stdout == "ok"
    assert inspection.identity.identity_hash == first.session_start_identity_hash
    assert backend.prepared == 1
    assert backend.executed == [
        (("python", "-m", "pytest"), True),
        (("python", "-m", "mypy"), True),
    ]
    assert backend.inspected == 1
    assert backend.collected == 2
    assert backend.cleaned == 1


def test_coordinator_rejects_unreviewed_argv_and_cleans(tmp_path: Path) -> None:
    backend = RecordingSessionBackend()
    coordinator = ExecutionCoordinator(backend)

    with (
        coordinator.prepare(coordinator_request(tmp_path, CommandFailureMode.RETURN)) as session,
        pytest.raises(SecurityError, match="reviewed command set"),
    ):
        session.execute(("sh", "-c", "pytest"))

    assert backend.executed == []
    assert backend.cleaned == 1


def test_coordinator_return_mode_disables_check(tmp_path: Path) -> None:
    backend = RecordingSessionBackend()
    coordinator = ExecutionCoordinator(backend)

    with coordinator.prepare(coordinator_request(tmp_path, CommandFailureMode.RETURN)) as session:
        receipt = session.execute(("python", "-m", "pytest"))

    assert receipt.requested_policy_hash
    assert receipt.effective_policy_hash
    assert backend.executed == [(("python", "-m", "pytest"), False)]
