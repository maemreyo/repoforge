"""Routes execution sessions to the backend that can satisfy the requested enforcement (#384).

`ExecutionCoordinator` itself stays single-backend; this router sits behind it (constructed once
in bootstrap.py) so the coordinator does not need to know two backends exist at all.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.execution_environment import (
    EnforcementRequirement,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
)
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ...ports.execution_environment import (
    ApprovedExecution,
    ArtifactResult,
    EnvironmentInspection,
    ExecutionEnvironmentPort,
    ExecutionReceipt,
    ExecutionRequest,
    PreparedEnvironmentSession,
)

#: Matches the policy doc's own execution-backend vocabulary (docs/architecture/
#: autonomy-policy-model.md §4: "sandboxed" vs. "native_uncontained") rather than naming a
#: specific vendor, so a future non-Docker containment backend would not need a new prefix.
_NATIVE_PREFIX = "native:"
_SANDBOXED_PREFIX = "sandboxed:"


class RoutingExecutionEnvironment:
    """Dispatches to `native` for `ADVISORY_BACKEND_ALLOWED` requests and `sandboxed` for
    `ENFORCEMENT_REQUIRED` ones, chosen once at `prepare_session`/`inspect_session` time by the
    request's own `enforcement_requirement` -- never re-decided later. The chosen backend's own
    `session_id` is prefixed so later calls that only receive a `PreparedEnvironmentSession`
    (`execute_in_session`/`execute_bytes_in_session`/`collect_session_artifacts`/
    `cleanup_session`) route back to the same backend without this router holding any mutable
    per-session state of its own; the prefix is stripped back off before the call reaches the
    chosen backend, so a backend's own session_id-keyed bookkeeping (if any) still matches what
    it originally returned.
    """

    def __init__(
        self,
        native: ExecutionEnvironmentPort,
        sandboxed: ExecutionEnvironmentPort | None,
    ) -> None:
        self._native = native
        self._sandboxed = sandboxed

    def _select(self, requirement: EnforcementRequirement) -> tuple[ExecutionEnvironmentPort, str]:
        if requirement is EnforcementRequirement.ADVISORY_BACKEND_ALLOWED:
            return self._native, _NATIVE_PREFIX
        if self._sandboxed is None:
            raise RepoForgeError(
                "No execution backend is configured to satisfy the required enforcement",
                code=ErrorCode.EXECUTION_BACKEND_UNAVAILABLE,
                unchanged_state=("No repository command was started.",),
                safe_next_action=(
                    "Enroll this repository in a capability backed by a real containment "
                    "backend, or relax the requested execution policy."
                ),
            )
        return self._sandboxed, _SANDBOXED_PREFIX

    def _dispatch(
        self, session: PreparedEnvironmentSession
    ) -> tuple[ExecutionEnvironmentPort, PreparedEnvironmentSession]:
        session_id = session.session_id
        if session_id.startswith(_NATIVE_PREFIX):
            return self._native, dataclasses.replace(
                session, session_id=session_id[len(_NATIVE_PREFIX) :]
            )
        if session_id.startswith(_SANDBOXED_PREFIX):
            if self._sandboxed is None:
                raise RepoForgeError(
                    "The sandboxed backend that prepared this session is no longer configured",
                    code=ErrorCode.EXECUTION_BACKEND_UNAVAILABLE,
                    unchanged_state=("No repository command was started.",),
                )
            return self._sandboxed, dataclasses.replace(
                session, session_id=session_id[len(_SANDBOXED_PREFIX) :]
            )
        raise RepoForgeError(
            "Prepared session was not produced by this routing execution environment",
            code=ErrorCode.STATE_STALE,
            unchanged_state=("No repository command was started.",),
        )

    # -- ExecutionEnvironmentPort -----------------------------------------------------------

    def prepare_session(self, request: ExecutionRequest) -> PreparedEnvironmentSession:
        backend, prefix = self._select(request.requested_policy.enforcement_requirement)
        prepared = backend.prepare_session(request)
        return dataclasses.replace(prepared, session_id=prefix + prepared.session_id)

    def inspect_session(
        self,
        request: ExecutionRequest,
        session: PreparedEnvironmentSession | None = None,
    ) -> EnvironmentInspection:
        if session is None:
            backend, _ = self._select(request.requested_policy.enforcement_requirement)
            return backend.inspect_session(request, None)
        backend, unprefixed = self._dispatch(session)
        return backend.inspect_session(request, unprefixed)

    def execute_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        output_limit: int,
        check: bool,
        cancel_token: CancellationToken | None = None,
        stdin_text: str | None = None,
        extra_env: tuple[tuple[str, str], ...] = (),
    ) -> CommandResult:
        backend, unprefixed = self._dispatch(session)
        return backend.execute_in_session(
            unprefixed,
            argv,
            cwd=cwd,
            timeout=timeout,
            output_limit=output_limit,
            check=check,
            cancel_token=cancel_token,
            stdin_text=stdin_text,
            extra_env=extra_env,
        )

    def execute_bytes_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        max_bytes: int,
    ) -> bytes:
        backend, unprefixed = self._dispatch(session)
        return backend.execute_bytes_in_session(
            unprefixed, argv, cwd=cwd, timeout=timeout, max_bytes=max_bytes
        )

    def collect_session_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: Sequence[str],
        *,
        root: Path,
    ) -> tuple[ArtifactResult, ...]:
        backend, unprefixed = self._dispatch(session)
        return backend.collect_session_artifacts(unprefixed, artifact_paths, root=root)

    def cleanup_session(self, session: PreparedEnvironmentSession) -> None:
        backend, unprefixed = self._dispatch(session)
        backend.cleanup_session(unprefixed)

    # -- Transitional legacy methods: only `native` still implements these; a routed/sandboxed
    # session is never produced through this surface, so there is nothing to route.
    def doctor(self, request: EnvironmentIdentityRequest) -> tuple[str, ...]:
        return self._native.doctor(request)

    def prepare(self, request: EnvironmentIdentityRequest) -> None:
        self._native.prepare(request)

    def identity(self, request: EnvironmentIdentityRequest) -> EnvironmentIdentity:
        return self._native.identity(request)

    def execute(self, execution: ApprovedExecution) -> ExecutionReceipt:
        return self._native.execute(execution)

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]:
        return self._native.collect_artifacts(artifact_paths, workspace_root=workspace_root)

    def cleanup(self, request: EnvironmentIdentityRequest) -> None:
        self._native.cleanup(request)
