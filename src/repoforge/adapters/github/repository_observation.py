"""Observe one configured repository under an explicitly selected provider identity."""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ...config import RepositoryConfig
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.git_remote_identity import GitRemoteKind, ParsedGitRemote, ReviewedSshEndpoint
from ...domain.repository_auth_broker import ProcessAuthContext
from ...domain.repository_identity import AuthTargetKind, RepositoryProvider
from ...domain.repository_identity_resolution import RepositoryIdentityObservation
from ...ports.auth_inspection import RepositoryObservationTarget
from ...ports.command import CommandResult
from ...ports.git_remote_identity import GitRemoteParser
from ..git.remote_identity import ConstrainedGitRemoteParser

_SAFE_ENVIRONMENT_KEYS = ("PATH", "LANG", "LC_ALL")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_OUTPUT_BYTES = 1_000_000
_ABSENT_REPOSITORY_ID = "0"


class _Clock(Protocol):
    def now_iso(self) -> str: ...


class _IsolatedExecutor(Protocol):
    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]: ...

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
        cancel_token: Any | None = None,
    ) -> CommandResult: ...


def _unavailable(message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
        retryable=retryable,
        unchanged_state=("No identity was resolved, bound, or used for a write.",),
    )


def _mismatch(message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
        retryable=False,
        unchanged_state=(
            "No GitHub API request was issued for an unproved transport endpoint.",
            "No identity was resolved, bound, or used for a write.",
        ),
        safe_next_action="Review or migrate the repository SSH endpoint proof before retrying.",
    )


class GhCliRepositoryObserver:
    """Confirm a GitHub target using only the host owned by the selected profile."""

    def __init__(
        self,
        executor: _IsolatedExecutor,
        *,
        clock: _Clock,
        remote_parser: GitRemoteParser | None = None,
    ) -> None:
        self._executor = executor
        self._clock = clock
        self._remote_parser = remote_parser or ConstrainedGitRemoteParser()

    def observe(
        self,
        repo: RepositoryConfig,
        *,
        expected_provider_host: str,
        config_revision: str,
        context: ProcessAuthContext,
        reviewed_ssh_endpoint: ReviewedSshEndpoint | None = None,
    ) -> RepositoryIdentityObservation:
        if not isinstance(config_revision, str) or _SHA256.fullmatch(config_revision) is None:
            raise RepoForgeError(
                "An identity observation must be bound to a lowercase SHA-256 config revision.",
                code=ErrorCode.CONFIG_INVALID,
                retryable=False,
                unchanged_state=("No identity was resolved.",),
            )
        if not isinstance(context, ProcessAuthContext):
            raise RepoForgeError(
                "Repository observation requires an explicitly selected auth context.",
                code=ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                retryable=False,
                unchanged_state=("No identity was resolved.",),
            )
        if context.target_kind is not AuthTargetKind.REPOSITORY:
            raise RepoForgeError(
                "Repository observation auth context is not repository-scoped.",
                code=ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                retryable=False,
                unchanged_state=("No identity was resolved.",),
            )

        target = self.target(
            repo,
            expected_provider_host=expected_provider_host,
            reviewed_ssh_endpoint=reviewed_ssh_endpoint,
        )
        exists = True
        repository_id = _ABSENT_REPOSITORY_ID
        owner = target.owner
        name = target.repository
        try:
            repository_id, owner, name = self._stable_identity(repo.path, target, context)
        except RepoForgeError as exc:
            if exc.code is not ErrorCode.NOT_FOUND:
                raise
            exists = False

        return RepositoryIdentityObservation(
            provider=RepositoryProvider.GITHUB,
            provider_host=target.provider_host,
            repository_id=repository_id,
            canonical_name=f"{target.provider_host}/{owner}/{name}",
            exists=exists,
            observed_at=self._clock.now_iso(),
            config_revision=config_revision,
        )

    def target(
        self,
        repo: RepositoryConfig,
        *,
        expected_provider_host: str,
        reviewed_ssh_endpoint: ReviewedSshEndpoint | None = None,
    ) -> RepositoryObservationTarget:
        if (
            not isinstance(expected_provider_host, str)
            or _HOST.fullmatch(expected_provider_host) is None
        ):
            raise _mismatch("The selected profile provider host is not a bounded lowercase host.")
        remote = self._read_remote(repo)
        parsed = self._remote_parser.parse(remote)
        self._assert_provider_authority(
            parsed,
            expected_provider_host=expected_provider_host,
            reviewed_ssh_endpoint=reviewed_ssh_endpoint,
        )
        return RepositoryObservationTarget(
            provider=RepositoryProvider.GITHUB,
            provider_host=expected_provider_host,
            owner=parsed.owner,
            repository=parsed.repository,
        )

    def _read_remote(self, repo: RepositoryConfig) -> str:
        with tempfile.TemporaryDirectory(prefix="repoforge-repository-observation-") as root:
            result = self._executor.run_isolated(
                ["git", "config", "--local", "--get", f"remote.{repo.remote}.url"],
                cwd=repo.path,
                environment=self._base_environment(Path(root)),
                secrets=(),
                output_limit=16_384,
            )
        remote = result.stdout.strip()
        if not remote:
            raise _unavailable("The configured repository remote is missing.")
        return remote

    @staticmethod
    def _assert_provider_authority(
        remote: ParsedGitRemote,
        *,
        expected_provider_host: str,
        reviewed_ssh_endpoint: ReviewedSshEndpoint | None,
    ) -> None:
        direct = remote.raw_host == expected_provider_host
        if remote.kind is GitRemoteKind.HTTPS:
            if not direct or reviewed_ssh_endpoint is not None:
                raise _mismatch("HTTPS remote host does not match the selected profile host.")
            return
        if direct and reviewed_ssh_endpoint is None:
            return
        endpoint = reviewed_ssh_endpoint
        if endpoint is None:
            raise _mismatch("Aliased SSH remote has no reviewed endpoint proof.")
        if (
            endpoint.raw_host != remote.raw_host
            or endpoint.canonical_host != expected_provider_host
            or endpoint.owner != remote.owner
            or endpoint.repository != remote.repository
            or endpoint.raw_url_digest != remote.raw_url_digest
        ):
            raise _mismatch("Reviewed SSH endpoint does not match the live repository remote.")

    def _base_environment(self, temporary_root: Path) -> dict[str, str]:
        inherited = self._executor.environment()
        environment = {
            key: inherited[key]
            for key in _SAFE_ENVIRONMENT_KEYS
            if key in inherited and isinstance(inherited[key], str)
        }
        environment.update(
            {
                "HOME": str(temporary_root / "home"),
                "GH_CONFIG_DIR": str(temporary_root / "gh-config"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GH_PROMPT_DISABLED": "1",
            }
        )
        return environment

    def _payload(
        self,
        argv: list[str],
        cwd: Path,
        *,
        environment: Mapping[str, str],
        secrets: Sequence[str],
    ) -> dict[str, object]:
        result = self._executor.run_isolated(
            argv,
            cwd=cwd,
            environment=environment,
            secrets=secrets,
            output_limit=_MAX_OUTPUT_BYTES,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise _unavailable("The repository observation returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise _unavailable("The repository observation returned an unexpected payload.")
        return payload

    def _stable_identity(
        self,
        cwd: Path,
        target: RepositoryObservationTarget,
        context: ProcessAuthContext,
    ) -> tuple[str, str, str]:
        selected = context.environment_dict()
        token = selected.get("GH_TOKEN")
        if not isinstance(token, str) or not token:
            raise RepoForgeError(
                "The selected repository observation context has no GitHub API token.",
                code=ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
                retryable=False,
                unchanged_state=("No identity was resolved.",),
            )
        with tempfile.TemporaryDirectory(prefix="repoforge-repository-observation-") as root:
            environment = self._base_environment(Path(root))
            environment["GH_TOKEN"] = token
            payload = self._payload(
                [
                    "gh",
                    "api",
                    "--hostname",
                    target.provider_host,
                    f"repos/{target.owner}/{target.repository}",
                ],
                cwd,
                environment=environment,
                secrets=context.secret_values,
            )
        raw = payload.get("id")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise _unavailable(
                "The repository observation did not return a stable numeric repository ID."
            )
        full_name = payload.get("full_name")
        if not isinstance(full_name, str):
            raise _unavailable("The repository observation did not return its canonical name.")
        parts = full_name.split("/")
        if len(parts) != 2 or any(_NAME_PART.fullmatch(part) is None for part in parts):
            raise _unavailable("The observed repository name is not <owner>/<repository>.")
        return str(raw), parts[0], parts[1]
