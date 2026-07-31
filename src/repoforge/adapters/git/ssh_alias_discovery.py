"""Read-only resolution of one SSH alias into a concrete pinned transport candidate.

`ssh -G <alias>` prints the fully expanded effective configuration without connecting. Only a
single unambiguous result is accepted: one concrete lowercase hostname, at most one user, and
exactly one identity file with no token expansion left in it (a leading `~` is resolved
against the HOME the probe runs under, because OpenSSH prints `IdentityFile` literally).
Anything that would make the effective identity ambiguous or agent-mediated -- wildcards,
several identity files, a proxy command or jump host, an identity agent -- is refused rather
than guessed at.

There is no write path here. Adopting an alias produces a pinned `GitTransportSpec` in
RepoForge's own reviewed configuration; the operator's SSH configuration is never modified.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ...domain.auth_migration import SshAliasCandidate
from ...domain.errors import ErrorCode, RepoForgeError
from ...ports.command import CommandResult

_SAFE_ENVIRONMENT_KEYS = ("HOME", "PATH", "LANG", "LC_ALL")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_OUTPUT_BYTES = 100_000
_MAX_LINES = 400
#: Keys whose presence makes the effective identity ambiguous or agent-mediated.
_FORBIDDEN_KEYS = ("proxycommand", "proxyjump", "identityagent")


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


def _rejected(message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
        retryable=retryable,
        unchanged_state=(
            "No SSH configuration was read for writing or modified.",
            "No configuration was written.",
        ),
    )


class SshCommandAliasDiscovery:
    """Resolve an SSH alias with `ssh -G`, accepting only one unambiguous identity."""

    def __init__(
        self,
        executor: _IsolatedExecutor,
        *,
        cwd: Path,
        config_file: Path | None = None,
    ) -> None:
        """Resolve with `ssh -G`; an explicit ``config_file`` is used only when given.

        Production composition leaves ``config_file`` unset so `ssh -G` reads the operator's
        real configuration. Tests pass a temporary configuration file through ``-F``, because
        OpenSSH locates its default user configuration from the passwd entry, never from
        ``$HOME``.
        """

        self._executor = executor
        self._cwd = cwd
        self._config_file = config_file

    def inspect(self, alias: str) -> SshAliasCandidate:
        if not isinstance(alias, str) or _ALIAS.fullmatch(alias) is None:
            raise _rejected("The requested SSH alias is not a safe alias name.")
        environment = {
            key: value
            for key, value in self._executor.environment().items()
            if key in _SAFE_ENVIRONMENT_KEYS and isinstance(value, str)
        }
        argv = ["ssh", "-G", alias]
        if self._config_file is not None:
            argv = ["ssh", "-G", "-F", str(self._config_file), alias]
        try:
            result = self._executor.run_isolated(
                argv,
                cwd=self._cwd,
                environment=environment,
                secrets=(),
                output_limit=_MAX_OUTPUT_BYTES,
            )
        except RepoForgeError as exc:
            raise _rejected("Resolving the SSH alias was unavailable.", retryable=True) from exc
        return self._parse(alias, result, home=environment.get("HOME"))

    def _parse(self, alias: str, result: CommandResult, *, home: str | None) -> SshAliasCandidate:
        if result.stdout_truncated or len(result.stdout) > _MAX_OUTPUT_BYTES:
            raise _rejected("The SSH alias resolution exceeded the reviewed output bound.")
        lines = result.stdout.splitlines()
        if not lines or len(lines) > _MAX_LINES:
            raise _rejected("The SSH alias resolution returned no usable configuration.")

        hostname: str | None = None
        user: str | None = None
        identity_files: list[str] = []
        for line in lines:
            key, _, raw = line.strip().partition(" ")
            key = key.lower()
            value = raw.strip()
            if key in _FORBIDDEN_KEYS and value:
                raise _rejected(
                    f"The SSH alias resolves through {key}, so its effective identity is "
                    "not a single pinned key."
                )
            if key == "hostname" or (key == "host" and hostname is None):
                hostname = value
            elif key == "user":
                user = value
            elif key == "identityfile" and value:
                identity_files.append(value)

        if hostname is None or not hostname:
            raise _rejected("The SSH alias did not resolve to a concrete host name.")
        if "*" in hostname or "?" in hostname or hostname != hostname.lower():
            raise _rejected("The SSH alias must resolve to one concrete lowercase host name.")
        if len(identity_files) != 1:
            raise _rejected(
                "The SSH alias must resolve to exactly one identity file to be pinnable."
            )
        identity_file = self._expand_home(identity_files[0], home=home)
        if any(marker in identity_file for marker in ("%", "$", "~", "*", "?")):
            raise _rejected(
                "The SSH alias identity file still contains unexpanded tokens, so the "
                "effective key cannot be pinned."
            )
        try:
            return SshAliasCandidate(
                alias=alias,
                hostname=hostname,
                identity_file=identity_file,
                user=user or None,
            )
        except ValueError as exc:
            raise _rejected(
                f"The SSH alias resolution is not a safe pinned identity: {exc}"
            ) from exc

    def _expand_home(self, identity_file: str, *, home: str | None) -> str:
        """Resolve a leading ``~`` against the HOME the probe ran under.

        OpenSSH prints ``IdentityFile ~/.ssh/id_rsa`` literally in `ssh -G` output, while the
        domain contract requires one concrete absolute path; the token is expanded here rather
        than guessed at. A reference to another user's home can never be pinned, so it is
        refused.
        """

        if identity_file == "~":
            if not home:
                raise _rejected(
                    "The SSH alias identity file expands through ~, but no HOME is available "
                    "to pin it."
                )
            return home
        if identity_file.startswith("~/"):
            if not home:
                raise _rejected(
                    "The SSH alias identity file expands through ~, but no HOME is available "
                    "to pin it."
                )
            return f"{home.rstrip('/')}/{identity_file[2:]}"
        if identity_file.startswith("~"):
            raise _rejected(
                "The SSH alias identity file references another user's home, so its effective "
                "key cannot be pinned."
            )
        return identity_file
