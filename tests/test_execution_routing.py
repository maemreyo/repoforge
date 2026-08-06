"""Unit tests for RoutingExecutionEnvironment (#384): dispatch-by-enforcement_requirement
and session-id-prefix stripping, using lightweight fakes -- no real backend needed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from repoforge.application.execution.routing import RoutingExecutionEnvironment
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.execution_environment import (
    CommandFailureMode,
    EffectiveExecutionPolicy,
    EnforcementRequirement,
    EnvironmentIdentity,
    ExecutionScope,
    ExecutionScopeKind,
    FilesystemAccess,
    NetworkAccess,
    RequestedExecutionPolicy,
)
from repoforge.ports.command import CommandResult
from repoforge.ports.execution_environment import (
    ArtifactResult,
    EnvironmentInspection,
    ExecutionRequest,
    PreparedEnvironmentSession,
)


class _FakeBackend:
    """Records every call it receives; returns a session_id unprefixed by the router."""

    def __init__(self, *, name: str) -> None:
        self.name = name
        self.prepared: list[ExecutionRequest] = []
        self.executed: list[tuple[str, tuple[str, ...]]] = []
        self.cleaned_up: list[str] = []

    def prepare_session(self, request: ExecutionRequest) -> PreparedEnvironmentSession:
        self.prepared.append(request)
        effective = EffectiveExecutionPolicy(
            network=request.requested_policy.network,
            filesystem=request.requested_policy.filesystem,
        )
        return PreparedEnvironmentSession(
            session_id=f"{self.name}-session-1",
            identity=EnvironmentIdentity(),
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
        )

    def inspect_session(
        self, request: ExecutionRequest, session: PreparedEnvironmentSession | None = None
    ) -> EnvironmentInspection:
        effective = EffectiveExecutionPolicy(
            network=request.requested_policy.network,
            filesystem=request.requested_policy.filesystem,
        )
        return EnvironmentInspection(
            identity=EnvironmentIdentity(),
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
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
        cancel_token: object | None = None,
        stdin_text: str | None = None,
        extra_env: tuple[tuple[str, str], ...] = (),
    ) -> CommandResult:
        self.executed.append((session.session_id, argv))
        return CommandResult(argv=argv, cwd=str(cwd), returncode=0, stdout="", stderr="")

    def execute_bytes_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        max_bytes: int,
    ) -> bytes:
        self.executed.append((session.session_id, argv))
        return b""

    def collect_session_artifacts(
        self, session: PreparedEnvironmentSession, artifact_paths: object, *, root: Path
    ) -> tuple[ArtifactResult, ...]:
        return ()

    def cleanup_session(self, session: PreparedEnvironmentSession) -> None:
        self.cleaned_up.append(session.session_id)

    # Transitional Protocol methods -- unused by these tests.
    def doctor(self, request: object) -> tuple[str, ...]:
        return ()

    def prepare(self, request: object) -> None:
        return None

    def identity(self, request: object) -> EnvironmentIdentity:
        return EnvironmentIdentity()

    def execute(self, execution: object) -> object:
        raise NotImplementedError

    def collect_artifacts(
        self, artifact_paths: object, *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]:
        return ()

    def cleanup(self, request: object) -> None:
        return None


def _request(*, enforcement: EnforcementRequirement) -> ExecutionRequest:
    return ExecutionRequest(
        scope=ExecutionScope(
            kind=ExecutionScopeKind.WORKSPACE,
            root=Path("/tmp"),
            command_cwd=Path("/tmp"),
            workspace_id="ws-1",
            working_directory_policy=".",
        ),
        reviewed_commands=(("echo", "hi"),),
        requested_policy=RequestedExecutionPolicy(
            network=NetworkAccess.OFFLINE,
            filesystem=FilesystemAccess.WORKSPACE_WRITE,
            enforcement_requirement=enforcement,
        ),
        timeout_seconds=10,
        output_limit=1000,
        failure_mode=CommandFailureMode.RETURN,
    )


def test_advisory_request_routes_to_native() -> None:
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    session = router.prepare_session(
        _request(enforcement=EnforcementRequirement.ADVISORY_BACKEND_ALLOWED)
    )

    assert len(native.prepared) == 1
    assert len(sandboxed.prepared) == 0
    assert session.session_id == "native:native-session-1"


def test_enforcement_required_request_routes_to_sandboxed() -> None:
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    session = router.prepare_session(
        _request(enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED)
    )

    assert len(sandboxed.prepared) == 1
    assert len(native.prepared) == 0
    assert session.session_id == "sandboxed:sandboxed-session-1"


def test_enforcement_required_without_a_sandboxed_backend_raises() -> None:
    native = _FakeBackend(name="native")
    router = RoutingExecutionEnvironment(native=native, sandboxed=None)

    with pytest.raises(RepoForgeError) as exc:
        router.prepare_session(_request(enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED))
    assert exc.value.code is ErrorCode.EXECUTION_BACKEND_UNAVAILABLE


def test_execute_in_session_strips_the_prefix_before_forwarding() -> None:
    """The backend's own session_id (used for e.g. internal cache lookups) must reach it
    unprefixed -- the router's prefix is purely its own dispatch mechanism."""
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    session = router.prepare_session(
        _request(enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED)
    )
    router.execute_in_session(
        session, ("echo", "hi"), cwd=Path("/tmp"), timeout=10, output_limit=1000, check=False
    )

    assert sandboxed.executed == [("sandboxed-session-1", ("echo", "hi"))]


def test_cleanup_session_routes_by_prefix_and_strips_it() -> None:
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    session = router.prepare_session(
        _request(enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED)
    )
    router.cleanup_session(session)

    assert sandboxed.cleaned_up == ["sandboxed-session-1"]
    assert native.cleaned_up == []


def test_a_session_id_with_no_recognized_prefix_fails_closed() -> None:
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    bogus = PreparedEnvironmentSession(
        session_id="not-a-real-prefix-12345",
        identity=EnvironmentIdentity(),
        requested_policy_hash="a" * 64,
        effective_policy=EffectiveExecutionPolicy(
            network=NetworkAccess.OFFLINE, filesystem=FilesystemAccess.WORKSPACE_WRITE
        ),
        effective_policy_hash=EffectiveExecutionPolicy(
            network=NetworkAccess.OFFLINE, filesystem=FilesystemAccess.WORKSPACE_WRITE
        ).policy_hash,
    )
    with pytest.raises(RepoForgeError) as exc:
        router.cleanup_session(bogus)
    assert exc.value.code is ErrorCode.STATE_STALE


def test_cleanup_session_for_a_stale_sandboxed_reference_without_a_backend_configured() -> None:
    """A session prepared while sandboxed was configured, then presented for cleanup after
    the backend was removed (e.g. a config change) -- must fail closed, not silently no-op
    or dispatch to native by accident."""
    native = _FakeBackend(name="native")
    router = RoutingExecutionEnvironment(native=native, sandboxed=None)

    session = PreparedEnvironmentSession(
        session_id="sandboxed:orphaned-session",
        identity=EnvironmentIdentity(),
        requested_policy_hash="a" * 64,
        effective_policy=EffectiveExecutionPolicy(
            network=NetworkAccess.OFFLINE, filesystem=FilesystemAccess.WORKSPACE_WRITE
        ),
        effective_policy_hash=EffectiveExecutionPolicy(
            network=NetworkAccess.OFFLINE, filesystem=FilesystemAccess.WORKSPACE_WRITE
        ).policy_hash,
    )
    with pytest.raises(RepoForgeError) as exc:
        router.cleanup_session(session)
    assert exc.value.code is ErrorCode.EXECUTION_BACKEND_UNAVAILABLE


def test_inspect_session_without_a_prepared_session_selects_by_enforcement_requirement() -> None:
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    inspection = router.inspect_session(
        _request(enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED), None
    )
    assert inspection.effective_policy.network is NetworkAccess.OFFLINE


def test_replace_leaves_the_original_session_object_untouched() -> None:
    """dataclasses.replace on a frozen PreparedEnvironmentSession must not mutate the
    router-facing copy the caller still holds."""
    native = _FakeBackend(name="native")
    sandboxed = _FakeBackend(name="sandboxed")
    router = RoutingExecutionEnvironment(native=native, sandboxed=sandboxed)

    session = router.prepare_session(
        _request(enforcement=EnforcementRequirement.ENFORCEMENT_REQUIRED)
    )
    original_id = session.session_id
    router.execute_in_session(
        session, ("echo", "hi"), cwd=Path("/tmp"), timeout=10, output_limit=1000, check=False
    )
    assert session.session_id == original_id
    assert dataclasses.is_dataclass(session)
