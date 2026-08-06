"""Docker-backed containment execution adapter (#384 `sandboxed_turbo` backend).

Each `execute_in_session` call runs in a fresh, single-use container (``docker run --rm``) --
there is no long-lived container to reuse across calls, matching the existing
prepare -> execute -> cleanup boundary `run_adhoc.py` already holds once per ad-hoc/sequence
element (confirmed by reading that code before this adapter was written; container pooling
across a sequence is explicit future work, not this pass).

Enforcement claims here are made only where a real Docker-Desktop-backed container run was used
to confirm the behavior directly (see the #384 implementation notes): `--network none` genuinely
blocks name resolution and connectivity; `-v <root>:/workspace:ro` genuinely rejects writes at
the filesystem layer; `--cap-drop=ALL` genuinely turns `mount(2)` into `EPERM`; a symlink inside
`/workspace` pointing at an absolute host-looking path resolves inside the container's own mount
namespace (nothing outside `/workspace` is mounted, so there is nothing there to escape to); and
`/var/run/docker.sock` is never mounted, so it is unreachable from inside the container. Nothing
here claims CPU-second or disk-quota enforcement, or port/protocol-scoped network egress --
those stay honestly `UNSUPPORTED`/`ADVISORY` until a design backs them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import shutil
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path

from ...domain.errors import ErrorCode, RepoForgeError, SecurityError
from ...domain.execution_environment import (
    EffectiveExecutionPolicy,
    EffectiveResourceLimits,
    EnforcementAssessment,
    EnforcementLevel,
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    EnvironmentIdentityRequest,
    FilesystemAccess,
    NetworkAccess,
    RequestedExecutionPolicy,
)
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandExecutor, CommandResult
from ...ports.execution_environment import (
    ApprovedExecution,
    ArtifactResult,
    EnvironmentInspection,
    ExecutionReceipt,
    ExecutionRequest,
    PreparedEnvironmentSession,
)
from .artifact_collection import collect_workspace_artifacts

_ADAPTER_VERSION = "1"
_CONTAINER_LABEL = "repoforge.managed=true"
_CONTAINER_NAME_PREFIX = "repoforge-sbx-"
#: A general-purpose minimal Debian base -- has a real shell, coreutils, and apt for anything a
#: CLI workflow needs to install. Configurable per adapter instance so a deployment can pin a
#: verified digest; left as a floating tag here because this dev environment's Docker daemon
#: could not reach the registry to resolve a fresh digest at implementation time (see #384's
#: closing notes) -- pin a `sha256:` digest once a working registry path is available.
_DEFAULT_IMAGE = "docker.io/library/debian:bookworm-slim"

#: `HOST_INHERITED` and `FilesystemAccess.HOST_ACCOUNT_ACCESS` are already rejected by
#: `RequestedExecutionPolicy.__post_init__` itself (they are effective-backend facts, not
#: requests), so the only network value actually reachable here that this adapter cannot
#: sensibly provide is `PRIVATE_APPROVED` -- a specific private destination with no mechanism to
#: route to selectively from inside a container.
_UNSUPPORTED_NETWORK = frozenset({NetworkAccess.PRIVATE_APPROVED})


def _stable_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class _SessionContext:
    """What `execute_in_session`/`execute_bytes_in_session` need that the Protocol does not pass
    them directly (unlike `collect_session_artifacts`, which receives `root` as a parameter).
    Keyed by `session.session_id`, populated in `prepare_session`, dropped in `cleanup_session` --
    the same lifecycle the coordinator's context-manager `with ... as session:` block already
    drives once per ad-hoc/sequence-element call. `session_id` is a fresh uuid per prepare, not a
    content hash of policy/identity, so two concurrent requests with identical policy never
    collide on the same cache entry."""

    root: Path
    filesystem: FilesystemAccess
    effective_network: NetworkAccess
    resources: EffectiveResourceLimits


class DockerExecutionAdapter:
    """Execute inside a fresh, single-use Docker container per command."""

    def __init__(
        self,
        executor: CommandExecutor,
        *,
        image: str = _DEFAULT_IMAGE,
        docker_bin: str = "docker",
    ) -> None:
        self._executor: CommandExecutor = executor
        self._image = image
        self._docker_bin = docker_bin
        self._reachable = False
        self._swept_orphans = False
        # Not lock-guarded: keys are unique per prepare_session call, and CPython's GIL makes a
        # single dict __setitem__/__delitem__/get atomic, so distinct keys never race each other.
        self._sessions: dict[str, _SessionContext] = {}

    # -- reachability and orphan cleanup -------------------------------------------------

    def _ensure_reachable(self) -> None:
        if self._reachable:
            return
        if shutil.which(self._docker_bin) is None:
            raise RepoForgeError(
                f"The sandboxed execution backend ({self._docker_bin!r}) is not installed "
                "on this host",
                code=ErrorCode.EXECUTION_BACKEND_UNAVAILABLE,
                unchanged_state=("No repository command was started.",),
                safe_next_action=(
                    "Install Docker and ensure its daemon is running, or use a repository not "
                    "enrolled in sandboxed_turbo."
                ),
            )
        probe = self._executor.run(
            [self._docker_bin, "info", "--format", "{{.ServerVersion}}"],
            cwd=Path("/"),
            timeout=10,
            check=False,
        )
        if probe.returncode != 0:
            raise RepoForgeError(
                "The sandboxed execution backend's daemon is not reachable",
                code=ErrorCode.EXECUTION_BACKEND_UNAVAILABLE,
                unchanged_state=("No repository command was started.",),
                safe_next_action=(
                    "Start the Docker daemon, or use a repository not enrolled in sandboxed_turbo."
                ),
                details={"stderr": probe.stderr[:500]},
            )
        self._reachable = True
        self._sweep_orphans()

    def _sweep_orphans(self) -> None:
        """One-time-per-adapter-instance cleanup for containers this process's own `finally`
        block never got to remove -- only reachable via a hard crash between spawn and cleanup.
        `docker ps` is authoritative (unlike the OS-process reaper's PID-reuse concern), so no
        identity/liveness check is needed: anything still carrying the managed label is ours."""
        if self._swept_orphans:
            return
        self._swept_orphans = True
        with contextlib.suppress(Exception):
            listing = self._executor.run(
                [self._docker_bin, "ps", "-aq", "--filter", f"label={_CONTAINER_LABEL}"],
                cwd=Path("/"),
                timeout=10,
                check=False,
            )
            ids = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
            if ids:
                self._executor.run(
                    [self._docker_bin, "rm", "-f", *ids],
                    cwd=Path("/"),
                    timeout=30,
                    check=False,
                )

    def _remove_container(self, name: str) -> None:
        with contextlib.suppress(Exception):
            self._executor.run(
                [self._docker_bin, "rm", "-f", name],
                cwd=Path("/"),
                timeout=30,
                check=False,
            )

    # -- policy and identity --------------------------------------------------------------

    @staticmethod
    def _effective_policy(requested: RequestedExecutionPolicy) -> EffectiveExecutionPolicy:
        if requested.network in _UNSUPPORTED_NETWORK:
            raise RepoForgeError(
                f"Sandboxed containment cannot provide {requested.network.value!r} network access",
                code=ErrorCode.EXECUTION_POLICY_UNSUPPORTED,
                unchanged_state=("No repository command was started.",),
                safe_next_action=(
                    "Request offline or public network access, or use a backend that provides "
                    "private/host-inherited network reachability."
                ),
            )
        degraded = False
        reasons: list[str] = []
        effective_network = requested.network
        network_enforcement = EnforcementLevel.ENFORCED
        if requested.network is NetworkAccess.PUBLIC_HTTP_HTTPS:
            # Docker alone cannot scope egress to HTTP/HTTPS ports without a proxy this adapter
            # does not implement -- honest about the gap rather than silently over-claiming it.
            effective_network = NetworkAccess.PUBLIC_GENERAL
            degraded = True
            reasons.append("network_port_scoping_unsupported")
            network_enforcement = EnforcementLevel.ADVISORY

        memory_enforced = requested.resources.memory_bytes is not None
        subprocess_enforced = requested.resources.subprocesses is not None
        enforcement = EnforcementAssessment(
            network=network_enforcement,
            filesystem=EnforcementLevel.ENFORCED,
            timeout=EnforcementLevel.ENFORCED,
            output=EnforcementLevel.ENFORCED,
            process_cleanup=EnforcementLevel.ENFORCED,
            cpu=EnforcementLevel.UNSUPPORTED,
            memory=EnforcementLevel.ENFORCED if memory_enforced else EnforcementLevel.UNSUPPORTED,
            disk=EnforcementLevel.UNSUPPORTED,
            subprocess_count=(
                EnforcementLevel.ENFORCED if subprocess_enforced else EnforcementLevel.UNSUPPORTED
            ),
            network_bytes=EnforcementLevel.UNSUPPORTED,
            socket=EnforcementLevel.ENFORCED,
            mount=EnforcementLevel.ENFORCED,
            symlink=EnforcementLevel.ENFORCED,
        )
        return EffectiveExecutionPolicy(
            network=effective_network,
            filesystem=requested.filesystem,
            credential_capabilities=requested.credentials,
            resource_limits=EffectiveResourceLimits(
                memory_bytes=requested.resources.memory_bytes,
                subprocesses=requested.resources.subprocesses,
            ),
            enforcement=enforcement,
            degraded=degraded,
            degradation_reasons=tuple(reasons),
        )

    @staticmethod
    def _backend_capability_hash(effective: EffectiveExecutionPolicy) -> str:
        return _stable_hash(
            {
                "adapter_kind": EnvironmentAdapterKind.HERMETIC_CONTAINER.value,
                "adapter_version": _ADAPTER_VERSION,
                "effective_policy": effective.policy_hash,
            }
        )

    def _identity(
        self, *, request: ExecutionRequest, effective: EffectiveExecutionPolicy
    ) -> EnvironmentIdentity:
        # Only the credential-profile env this run was actually granted is hashed here -- unlike
        # native execution, a fresh container does not inherit the host's allowed_environment at
        # all, so there is nothing broader to reflect.
        env_names = tuple(sorted(name for name, _ in request.extra_env))
        env_hashes = tuple(
            sorted(
                (name, hashlib.sha256(value.encode("utf-8")).hexdigest())
                for name, value in request.extra_env
            )
        )
        return EnvironmentIdentity(
            adapter_kind=EnvironmentAdapterKind.HERMETIC_CONTAINER,
            adapter_version=_ADAPTER_VERSION,
            platform="linux",
            architecture=platform.machine().lower(),
            python_version="",
            runtime_version="docker",
            tools=(),
            lockfile_digests=(),
            manifest_digests=(),
            approved_env_var_names=env_names,
            approved_env_value_hashes=env_hashes,
            effective_path="",
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy_hash=effective.policy_hash,
            effective_network=effective.network,
            effective_filesystem=effective.filesystem,
            enforcement_assessment=effective.enforcement,
            backend_capability_hash=self._backend_capability_hash(effective),
            working_directory_policy_hash=hashlib.sha256(
                request.scope.working_directory_policy.encode("utf-8")
            ).hexdigest(),
        )

    # -- ExecutionEnvironmentPort -----------------------------------------------------------

    def prepare_session(self, request: ExecutionRequest) -> PreparedEnvironmentSession:
        self._ensure_reachable()
        effective = self._effective_policy(request.requested_policy)
        identity = self._identity(request=request, effective=effective)
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = _SessionContext(
            root=request.scope.root,
            filesystem=request.requested_policy.filesystem,
            effective_network=effective.network,
            resources=effective.resource_limits,
        )
        return PreparedEnvironmentSession(
            session_id=session_id,
            identity=identity,
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
        )

    def inspect_session(
        self,
        request: ExecutionRequest,
        session: PreparedEnvironmentSession | None = None,
    ) -> EnvironmentInspection:
        effective = self._effective_policy(request.requested_policy)
        identity = self._identity(request=request, effective=effective)
        if session is not None and session.effective_policy_hash != effective.policy_hash:
            raise RepoForgeError(
                "Sandboxed execution policy changed during the prepared session",
                code=ErrorCode.EXECUTION_ENVIRONMENT_DRIFT,
                unchanged_state=("No additional repository command was started.",),
            )
        return EnvironmentInspection(
            identity=identity,
            requested_policy_hash=request.requested_policy.policy_hash,
            effective_policy=effective,
            effective_policy_hash=effective.policy_hash,
            warnings=(),
        )

    def _session_context(self, session_id: str) -> _SessionContext:
        context = self._sessions.get(session_id)
        if context is None:
            raise RepoForgeError(
                "Sandboxed session context is missing or already closed",
                code=ErrorCode.STATE_STALE,
                unchanged_state=("No repository command was started.",),
                safe_next_action="Prepare a new session and reissue the command.",
            )
        return context

    def _container_cwd(self, *, root: Path, cwd: Path) -> str:
        resolved_root = root.resolve(strict=True)
        resolved_cwd = cwd.resolve(strict=False)
        try:
            relative = resolved_cwd.relative_to(resolved_root)
        except ValueError as exc:
            raise SecurityError(f"Command cwd escapes the sandboxed workspace root: {cwd}") from exc
        return "/workspace" if str(relative) == "." else f"/workspace/{relative.as_posix()}"

    def _base_docker_argv(
        self, *, context: _SessionContext, container_name: str, container_cwd: str
    ) -> list[str]:
        root = context.root.resolve(strict=True)
        mount_mode = "ro" if context.filesystem is FilesystemAccess.SOURCE_READ else "rw"
        network_mode = "none" if context.effective_network is NetworkAccess.OFFLINE else "bridge"
        argv = [
            self._docker_bin,
            "run",
            "--rm",
            "--name",
            container_name,
            "--label",
            _CONTAINER_LABEL,
            "--network",
            network_mode,
            "--cap-drop=ALL",
            "-v",
            f"{root}:/workspace:{mount_mode}",
            "-w",
            container_cwd,
        ]
        if context.resources.memory_bytes is not None:
            argv += ["--memory", str(context.resources.memory_bytes)]
        if context.resources.subprocesses is not None:
            argv += ["--pids-limit", str(context.resources.subprocesses)]
        return argv

    @staticmethod
    def _write_env_file(extra_env: tuple[tuple[str, str], ...]) -> Path:
        """Write credential-profile env to a private (mode 0600, `tempfile.mkstemp` default)
        file and pass it via `--env-file` rather than `-e KEY=VALUE` -- the latter would put
        secret values directly into the `docker run` argv, visible to any other process on this
        host via `ps`. Does not support values containing literal newlines (the env-file format
        has no escaping for them); credential-profile values are opaque tokens, not free text, so
        this is an accepted limitation, not silently unsound.
        """
        fd, raw_path = tempfile.mkstemp(prefix="repoforge-sbx-env-")
        path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for name, value in extra_env:
                    handle.write(f"{name}={value}\n")
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        return path

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
        context = self._session_context(session.session_id)
        container_cwd = self._container_cwd(root=context.root, cwd=cwd)
        container_name = f"{_CONTAINER_NAME_PREFIX}{uuid.uuid4().hex[:24]}"
        docker_argv = self._base_docker_argv(
            context=context, container_name=container_name, container_cwd=container_cwd
        )
        docker_argv.append("-i")

        env_file: Path | None = None
        try:
            if extra_env:
                env_file = self._write_env_file(extra_env)
                docker_argv += ["--env-file", str(env_file)]
            docker_argv += [self._image, *argv]
            try:
                # NOTE: a non-zero return here can mean either the containerized command
                # failed (the common, expected case) or `docker run` itself failed to start the
                # container (an infrastructure failure) -- this adapter does not yet distinguish
                # the two by parsing Docker's own CLI error text, which would be fragile across
                # Docker versions. Accepted as a known first-pass limitation.
                result = self._executor.run(
                    docker_argv,
                    cwd=context.root.resolve(strict=True),
                    timeout=timeout,
                    check=check,
                    output_limit=output_limit,
                    input_text=stdin_text,
                    cancel_token=cancel_token,
                )
            finally:
                self._remove_container(container_name)
        finally:
            if env_file is not None:
                with contextlib.suppress(OSError):
                    env_file.unlink()
        return dataclasses.replace(result, argv=argv, cwd=str(cwd))

    def execute_bytes_in_session(
        self,
        session: PreparedEnvironmentSession,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: int,
        max_bytes: int,
    ) -> bytes:
        context = self._session_context(session.session_id)
        container_cwd = self._container_cwd(root=context.root, cwd=cwd)
        container_name = f"{_CONTAINER_NAME_PREFIX}{uuid.uuid4().hex[:24]}"
        docker_argv = self._base_docker_argv(
            context=context, container_name=container_name, container_cwd=container_cwd
        )
        docker_argv += [self._image, *argv]
        try:
            return self._executor.run_bytes(
                docker_argv,
                cwd=context.root.resolve(strict=True),
                timeout=timeout,
                max_bytes=max_bytes,
            )
        finally:
            self._remove_container(container_name)

    def collect_session_artifacts(
        self,
        session: PreparedEnvironmentSession,
        artifact_paths: Sequence[str],
        *,
        root: Path,
    ) -> tuple[ArtifactResult, ...]:
        _ = session
        # The workspace is bind-mounted read/write into the container, so artifacts land on the
        # host filesystem exactly like native execution's do -- no `docker cp` needed.
        return collect_workspace_artifacts(
            artifact_paths, workspace_root=root, max_artifact_bytes=2_000_000
        )

    def cleanup_session(self, session: PreparedEnvironmentSession) -> None:
        self._sessions.pop(session.session_id, None)

    # -- Transitional Protocol methods, never routed to for this adapter --------------------

    def doctor(self, request: EnvironmentIdentityRequest) -> tuple[str, ...]:
        raise NotImplementedError(
            "DockerExecutionAdapter only supports the session-based ExecutionEnvironmentPort "
            "surface; no live caller uses the legacy transitional methods."
        )

    def prepare(self, request: EnvironmentIdentityRequest) -> None:
        raise NotImplementedError(
            "DockerExecutionAdapter only supports the session-based ExecutionEnvironmentPort "
            "surface; no live caller uses the legacy transitional methods."
        )

    def identity(self, request: EnvironmentIdentityRequest) -> EnvironmentIdentity:
        raise NotImplementedError(
            "DockerExecutionAdapter only supports the session-based ExecutionEnvironmentPort "
            "surface; no live caller uses the legacy transitional methods."
        )

    def execute(self, execution: ApprovedExecution) -> ExecutionReceipt:
        raise NotImplementedError(
            "DockerExecutionAdapter only supports the session-based ExecutionEnvironmentPort "
            "surface; no live caller uses the legacy transitional methods."
        )

    def collect_artifacts(
        self, artifact_paths: Sequence[str], *, workspace_root: Path
    ) -> tuple[ArtifactResult, ...]:
        raise NotImplementedError(
            "DockerExecutionAdapter only supports the session-based ExecutionEnvironmentPort "
            "surface; no live caller uses the legacy transitional methods."
        )

    def cleanup(self, request: EnvironmentIdentityRequest) -> None:
        raise NotImplementedError(
            "DockerExecutionAdapter only supports the session-based ExecutionEnvironmentPort "
            "surface; no live caller uses the legacy transitional methods."
        )
