"""Pinned SSH and isolated HTTPS Git transport adapter."""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportEvidence,
    GitTransportKind,
    GitTransportSpec,
)
from ...domain.repository_auth_broker import ProcessAuthContext
from ...domain.repository_identity import AuthTargetKind
from ...ports.command import CommandResult

_SCP_REMOTE = re.compile(
    r"^(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\x00]+)$"
)
_OBJECT_ID = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_IDENTITY_ENVIRONMENT = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)
_PROMPT_MARKERS = (
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "terminal prompt",
    "authentication required",
    "interactive authentication",
)


class _IsolatedExecutor(Protocol):
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
        cancel_token: object | None = None,
    ) -> CommandResult: ...


def _transport_error(
    code: ErrorCode,
    message: str,
    *,
    details: dict[str, object] | None = None,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=("No alternate Git identity or ambient helper was attempted.",),
        details=details,
    )


def _remote_host(remote_url: str, kind: GitTransportKind) -> str:
    if (
        not isinstance(remote_url, str)
        or not remote_url
        or len(remote_url) > 4096
        or "\x00" in remote_url
    ):
        raise _transport_error(
            ErrorCode.GIT_TRANSPORT_HOST_MISMATCH,
            "Git transport remote URL is invalid.",
        )
    if "://" not in remote_url:
        match = _SCP_REMOTE.fullmatch(remote_url)
        if match is None or kind is not GitTransportKind.SSH:
            raise _transport_error(
                ErrorCode.GIT_TRANSPORT_HOST_MISMATCH,
                "Git transport remote URL does not match the selected transport kind.",
            )
        return match.group("host").lower()

    parsed = urlsplit(remote_url)
    expected_scheme = "https" if kind is GitTransportKind.HTTPS else "ssh"
    if parsed.scheme.lower() != expected_scheme or parsed.hostname is None:
        raise _transport_error(
            ErrorCode.GIT_TRANSPORT_HOST_MISMATCH,
            "Git transport remote URL does not match the selected transport kind.",
        )
    if parsed.password is not None or (
        kind is GitTransportKind.HTTPS and parsed.username is not None
    ):
        raise _transport_error(
            ErrorCode.CREDENTIAL_LEAK_BLOCKED,
            "Git transport credentials are not allowed in remote URLs.",
        )
    return parsed.hostname.lower()


def _assert_context(spec: GitTransportSpec, context: ProcessAuthContext) -> None:
    if (
        context.profile_id != spec.profile_id
        or context.target_kind is not AuthTargetKind.REPOSITORY
        or context.target_id != spec.target_id
    ):
        raise _transport_error(
            ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
            "Git transport context does not match the reviewed profile and repository target.",
            details={
                "profile_id": spec.profile_id,
                "repository_id": spec.repository_id,
                "target_id": spec.target_id,
            },
        )


def _assert_access(spec: GitTransportSpec, access: GitTransportAccess) -> None:
    if access not in spec.allowed_access:
        raise _transport_error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            f"Git transport profile does not allow {access.value} access.",
            details={
                "profile_id": spec.profile_id,
                "repository_id": spec.repository_id,
                "requested_access": access.value,
            },
        )


def _scrubbed_environment(context: ProcessAuthContext) -> dict[str, str]:
    return {
        key: value
        for key, value in context.environment_dict().items()
        if key not in _IDENTITY_ENVIRONMENT
        and not key.startswith("GIT_CONFIG_KEY_")
        and not key.startswith("GIT_CONFIG_VALUE_")
    }


def _ssh_environment(spec: GitTransportSpec, context: ProcessAuthContext) -> dict[str, str]:
    environment = _scrubbed_environment(context)
    environment.pop("REPOFORGE_GIT_HTTPS_TOKEN", None)
    identity_file = spec.ssh_identity_file
    if identity_file is None:
        raise _transport_error(
            ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
            "Pinned SSH transport is missing its reviewed identity-file reference.",
        )
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_SSH_COMMAND": (
                "ssh -F /dev/null -o IdentitiesOnly=yes -o IdentityAgent=none "
                "-o BatchMode=yes -o PasswordAuthentication=no "
                "-o KbdInteractiveAuthentication=no "
                f"-i {shlex.quote(identity_file)}"
            ),
        }
    )
    return environment


def _https_environment(spec: GitTransportSpec, context: ProcessAuthContext) -> dict[str, str]:
    environment = _scrubbed_environment(context)
    token_environment = spec.https_token_environment
    if token_environment is None:
        raise _transport_error(
            ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
            "Isolated HTTPS transport is missing its reviewed token environment.",
        )
    token = context.environment_dict().get(token_environment)
    if not token or token not in context.secret_values:
        raise _transport_error(
            ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
            "Isolated HTTPS transport token is not bound to the reviewed ephemeral context.",
        )
    environment[token_environment] = token
    helper = (
        '!f() { test "$1" = get || exit 0; '
        f"printf 'username=x-access-token\\npassword=%s\\n' \"${token_environment}\"; "
        "}; f"
    )
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": "false",
            "SSH_ASKPASS": "false",
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.useHttpPath",
            "GIT_CONFIG_VALUE_1": "true",
            "GIT_CONFIG_KEY_2": f"credential.https://{spec.provider_host}.helper",
            "GIT_CONFIG_VALUE_2": helper,
        }
    )
    return environment


class GitTransportRouter:
    """Execute one reviewed Git transport without ambient identity fallback."""

    def __init__(self, executor: _IsolatedExecutor) -> None:
        self._executor = executor

    def _environment(
        self,
        remote_url: str,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> dict[str, str]:
        _assert_context(spec, context)
        host = _remote_host(remote_url, spec.kind)
        if host != spec.provider_host.lower():
            raise _transport_error(
                ErrorCode.GIT_TRANSPORT_HOST_MISMATCH,
                "Git transport remote host does not match the reviewed provider host.",
                details={"expected_host": spec.provider_host, "actual_host": host},
            )
        return (
            _ssh_environment(spec, context)
            if spec.kind is GitTransportKind.SSH
            else _https_environment(spec, context)
        )

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
        access: GitTransportAccess,
    ) -> CommandResult:
        _assert_access(spec, access)
        environment = self._environment(argv[2], spec, context)
        result = self._executor.run_isolated(
            argv,
            cwd=cwd,
            environment=environment,
            secrets=context.secret_values,
            check=False,
            output_limit=1_000_000,
        )
        if result.returncode == 0:
            return result
        rendered = result.combined.lower()
        code = (
            ErrorCode.CREDENTIAL_INTERACTION_REQUIRED
            if any(marker in rendered for marker in _PROMPT_MARKERS)
            else ErrorCode.GIT_TRANSPORT_AUTHENTICATION_FAILED
        )
        raise _transport_error(
            code,
            (
                "Git transport required an interactive credential prompt, which is disabled."
                if code is ErrorCode.CREDENTIAL_INTERACTION_REQUIRED
                else "Git transport authentication failed for the reviewed identity."
            ),
            details={
                "profile_id": spec.profile_id,
                "repository_id": spec.repository_id,
                "provider_host": spec.provider_host,
                "transport_kind": spec.kind.value,
                "requested_access": access.value,
                "exit_code": result.returncode,
            },
        )

    def ls_remote(
        self,
        cwd: Path,
        remote_url: str,
        requested_ref: str | None,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> GitTransportEvidence:
        argv = ["git", "ls-remote", remote_url]
        if requested_ref is not None:
            argv.append(requested_ref)
        result = self._run(
            argv,
            cwd=cwd,
            spec=spec,
            context=context,
            access=GitTransportAccess.READ,
        )
        observed_sha: str | None = None
        rendered = result.stdout.strip()
        if rendered:
            matching: list[str] = []
            for line in rendered.splitlines():
                fields = line.split()
                if len(fields) != 2 or _OBJECT_ID.fullmatch(fields[0]) is None:
                    raise _transport_error(
                        ErrorCode.EVIDENCE_INVALID,
                        "Git ls-remote returned malformed transport evidence.",
                    )
                if requested_ref is None or fields[1] == requested_ref:
                    matching.append(fields[0].lower())
            if len(matching) > 1:
                raise _transport_error(
                    ErrorCode.EVIDENCE_INVALID,
                    "Git ls-remote returned ambiguous transport evidence.",
                )
            observed_sha = matching[0] if matching else None
        return GitTransportEvidence(
            profile_id=spec.profile_id,
            repository_id=spec.repository_id,
            provider_host=spec.provider_host,
            kind=spec.kind,
            credential_fingerprint=spec.credential_fingerprint,
            access=GitTransportAccess.READ,
            remote_url_digest=hashlib.sha256(remote_url.encode("utf-8")).hexdigest(),
            requested_ref=requested_ref,
            observed_sha=observed_sha,
        )

    def fetch(
        self,
        cwd: Path,
        remote_url: str,
        refspec: str,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> CommandResult:
        return self._run(
            ["git", "fetch", remote_url, refspec],
            cwd=cwd,
            spec=spec,
            context=context,
            access=GitTransportAccess.READ,
        )

    def push(
        self,
        cwd: Path,
        remote_url: str,
        refspec: str,
        spec: GitTransportSpec,
        context: ProcessAuthContext,
    ) -> CommandResult:
        return self._run(
            ["git", "push", remote_url, refspec],
            cwd=cwd,
            spec=spec,
            context=context,
            access=GitTransportAccess.WRITE,
        )
