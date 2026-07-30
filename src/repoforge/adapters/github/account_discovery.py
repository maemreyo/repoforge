"""Read-only discovery of the GitHub accounts the local `gh` installation already knows.

Enumeration parses `gh auth status --hostname <host>`, which is the only surface that lists
every stored account (`gh` offers no JSON form for it). Verification never trusts that text:
it issues a token for exactly one named account and probes the API under that token alone, in
an environment stripped of ambient GitHub state. `gh auth switch` is never invoked, so the
operator's globally active account is untouched and unobservable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ...domain.auth_migration import NamedAccountCandidate
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.repository_auth_broker import EphemeralSecret
from ...ports.command import CommandResult

_SAFE_ENVIRONMENT_KEYS = ("HOME", "PATH", "LANG", "LC_ALL")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
#: The whole entry must match, so a malformed line is never salvaged into a partial login.
_ACCOUNT_LINE = re.compile(r"Logged in to (?P<host>\S+) account (?P<login>\S+) \([^()]+\)\s*$")
_ACTIVE_LINE = re.compile(r"^\s*-\s*Active account:\s*(?P<value>true|false)\s*$")
_SCOPES_LINE = re.compile(r"^\s*-\s*Token scopes:\s*(?P<value>.*)$")
_MAX_STATUS_BYTES = 200_000
_MAX_ACCOUNTS = 32


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

    def run_secret_text(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str] = (),
        timeout: int | None = None,
        max_bytes: int = 100_000,
        cancel_token: Any | None = None,
    ) -> EphemeralSecret: ...


def _unavailable(message: str, *, retryable: bool = True) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.GITHUB_PROVIDER_UNAVAILABLE,
        retryable=retryable,
        unchanged_state=(
            "No GitHub account was switched, imported, or configured.",
            "No configuration was written.",
        ),
    )


def _mismatch(code: ErrorCode, message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=(
            "No GitHub account was switched, imported, or configured.",
            "No configuration was written.",
        ),
    )


def _isolated_environment(executor: _IsolatedExecutor) -> dict[str, str]:
    """Inherit only inert variables, so no ambient GitHub or SSH state can be observed."""

    inherited = executor.environment()
    return {
        key: inherited[key]
        for key in _SAFE_ENVIRONMENT_KEYS
        if key in inherited and isinstance(inherited[key], str)
    }


def _parse_scopes(raw: str) -> tuple[str, ...]:
    scopes = tuple(
        item.strip().strip("'\"") for item in raw.split(",") if item.strip().strip("'\"")
    )
    return tuple(sorted(set(scopes)))


class GhCliNamedAccountDiscovery:
    """Enumerate and verify named `gh` accounts without touching the active account."""

    def __init__(self, executor: _IsolatedExecutor, *, cwd: Path) -> None:
        self._executor = executor
        self._cwd = cwd

    def candidates(self, *, host: str) -> tuple[NamedAccountCandidate, ...]:
        if not isinstance(host, str) or _HOST.fullmatch(host) is None:
            raise _unavailable(
                "The requested GitHub host is not a safe host name.", retryable=False
            )
        try:
            result = self._executor.run_isolated(
                ["gh", "auth", "status", "--hostname", host],
                cwd=self._cwd,
                environment=_isolated_environment(self._executor),
                secrets=(),
                output_limit=_MAX_STATUS_BYTES,
            )
        except RepoForgeError as exc:
            raise _unavailable(
                "Listing the locally configured GitHub accounts was unavailable."
            ) from exc
        return self._parse_accounts(host, result)

    def _parse_accounts(
        self, host: str, result: CommandResult
    ) -> tuple[NamedAccountCandidate, ...]:
        if result.stdout_truncated or len(result.stdout) > _MAX_STATUS_BYTES:
            raise _unavailable(
                "The GitHub account listing exceeded the reviewed output bound.", retryable=False
            )
        candidates: list[NamedAccountCandidate] = []
        pending_login: str | None = None
        pending_active = False
        pending_scopes: tuple[str, ...] = ()

        def flush() -> None:
            nonlocal pending_login, pending_active, pending_scopes
            if pending_login is None:
                return
            candidates.append(
                NamedAccountCandidate(
                    host=host,
                    login=pending_login,
                    active=pending_active,
                    token_scopes=pending_scopes,
                )
            )
            pending_login, pending_active, pending_scopes = None, False, ()

        for line in result.stdout.splitlines():
            account = _ACCOUNT_LINE.search(line)
            if account is not None:
                flush()
                login = account.group("login")
                if account.group("host") != host or _LOGIN.fullmatch(login) is None:
                    raise _unavailable(
                        "The GitHub account listing contained an unrecognized account entry.",
                        retryable=False,
                    )
                pending_login = login
                continue
            if pending_login is None:
                continue
            active = _ACTIVE_LINE.match(line)
            if active is not None:
                pending_active = active.group("value") == "true"
                continue
            scopes = _SCOPES_LINE.match(line)
            if scopes is not None:
                pending_scopes = _parse_scopes(scopes.group("value"))
        flush()

        if not candidates:
            raise _unavailable(
                "No locally configured GitHub account was found for the requested host.",
                retryable=False,
            )
        if len(candidates) > _MAX_ACCOUNTS:
            raise _unavailable(
                "The GitHub account listing exceeded the reviewed account bound.", retryable=False
            )
        logins = [candidate.login for candidate in candidates]
        if len(set(logins)) != len(logins):
            raise _unavailable(
                "The same GitHub login is configured more than once for this host.",
                retryable=False,
            )
        return tuple(candidates)

    def verify(self, *, host: str, login: str) -> NamedAccountCandidate:
        """Prove one named account's live actor using only that account's own token."""

        if not isinstance(login, str) or _LOGIN.fullmatch(login) is None:
            raise _mismatch(
                ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                "The requested GitHub login is not a safe login name.",
            )
        matches = tuple(item for item in self.candidates(host=host) if item.login == login)
        if len(matches) != 1:
            raise _mismatch(
                ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                "The requested GitHub login is not configured exactly once for this host.",
            )
        candidate = matches[0]
        token = self._token(host, login)
        try:
            payload = self._probe_actor(host, token)
        finally:
            token.release()
        observed_login = payload.get("login")
        observed_id = payload.get("id")
        if observed_login != login:
            raise _mismatch(
                ErrorCode.GITHUB_API_ACTOR_MISMATCH,
                "The live GitHub actor behind the named account token is a different login.",
            )
        if not isinstance(observed_id, (int, str)) or isinstance(observed_id, bool):
            raise _unavailable(
                "The GitHub actor probe did not return a stable actor identifier.", retryable=False
            )
        return NamedAccountCandidate(
            host=candidate.host,
            login=candidate.login,
            active=candidate.active,
            token_scopes=candidate.token_scopes,
            actor_id=str(observed_id),
        )

    def _token(self, host: str, login: str) -> EphemeralSecret:
        try:
            return self._executor.run_secret_text(
                ["gh", "auth", "token", "--hostname", host, "--user", login],
                cwd=self._cwd,
                environment=_isolated_environment(self._executor),
                secrets=(),
                max_bytes=100_000,
            )
        except RepoForgeError as exc:
            raise _unavailable("Reading the named GitHub account token was unavailable.") from exc

    def _probe_actor(self, host: str, token: EphemeralSecret) -> dict[str, object]:
        revealed = token.reveal()
        environment = _isolated_environment(self._executor)
        environment["GH_TOKEN"] = revealed
        try:
            result = self._executor.run_isolated(
                ["gh", "api", "--hostname", host, "user"],
                cwd=self._cwd,
                environment=environment,
                secrets=(revealed,),
                output_limit=1_000_000,
            )
        except RepoForgeError as exc:
            raise _unavailable("The GitHub actor probe was unavailable.") from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise _unavailable(
                "The GitHub actor probe returned invalid JSON.", retryable=False
            ) from None
        if not isinstance(payload, dict):
            raise _unavailable(
                "The GitHub actor probe returned an unexpected payload.", retryable=False
            )
        return payload
