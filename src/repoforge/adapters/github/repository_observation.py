"""Observe the stable identity of one configured repository, read-only.

Identity resolution is anchored to the provider host plus the stable numeric repository ID, not
to a name, so a rename is observable as the same repository and a name that now points at a
different repository is observable as a different one. Both facts come from the provider:

1. `gh repo view --json nameWithOwner,url` in the repository worktree gives the current name
   and, through the URL, the provider host -- including an enterprise host.
2. `gh api --hostname <host> repos/<owner>/<repo>` gives the stable numeric ID, the same
   identifier the API identity verifier proves a token is scoped to.

Both run with ambient GitHub state stripped from the environment, so what is observed is what
the reviewed profile would reach, not whatever account happens to be active.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ...config import RepositoryConfig
from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.repository_identity import RepositoryProvider
from ...domain.repository_identity_resolution import RepositoryIdentityObservation
from ...ports.command import CommandResult

_SAFE_ENVIRONMENT_KEYS = ("HOME", "PATH", "LANG", "LC_ALL")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_NAME_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_URL_HOST = re.compile(r"^https://(?P<host>[A-Za-z0-9.-]+)/")
_MAX_OUTPUT_BYTES = 1_000_000
#: The repository ID reported when the provider does not confirm the repository exists. It is
#: never a valid GitHub id, so nothing can accidentally resolve against it.
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


class GhCliRepositoryObserver:
    """Read the provider's own answer for what this worktree's repository is."""

    def __init__(self, executor: _IsolatedExecutor, *, clock: _Clock) -> None:
        self._executor = executor
        self._clock = clock

    def observe(
        self, repo: RepositoryConfig, *, config_revision: str
    ) -> RepositoryIdentityObservation:
        if not isinstance(config_revision, str) or _SHA256.fullmatch(config_revision) is None:
            raise RepoForgeError(
                "An identity observation must be bound to a lowercase SHA-256 config revision.",
                code=ErrorCode.CONFIG_INVALID,
                retryable=False,
                unchanged_state=("No identity was resolved.",),
            )
        host, owner, name = self._name_and_host(repo.path)
        exists = True
        repository_id = _ABSENT_REPOSITORY_ID
        try:
            repository_id = self._stable_id(repo.path, host, owner, name)
        except RepoForgeError as exc:
            if exc.code is ErrorCode.GITHUB_PROVIDER_UNAVAILABLE:
                raise
            # The provider answered, and its answer is that this repository is not reachable
            # under this identity. That is an observation the resolver must fail closed on,
            # not an error to retry.
            exists = False
        return RepositoryIdentityObservation(
            provider=RepositoryProvider.GITHUB,
            provider_host=host,
            repository_id=repository_id,
            canonical_name=f"{host}/{owner}/{name}",
            exists=exists,
            observed_at=self._clock.now_iso(),
            config_revision=config_revision,
        )

    def _environment(self) -> dict[str, str]:
        inherited = self._executor.environment()
        return {
            key: inherited[key]
            for key in _SAFE_ENVIRONMENT_KEYS
            if key in inherited and isinstance(inherited[key], str)
        }

    def _payload(self, argv: list[str], cwd: Path) -> dict[str, object]:
        result = self._executor.run_isolated(
            argv,
            cwd=cwd,
            environment=self._environment(),
            secrets=(),
            output_limit=_MAX_OUTPUT_BYTES,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise _unavailable("The repository observation returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise _unavailable("The repository observation returned an unexpected payload.")
        return payload

    def _name_and_host(self, cwd: Path) -> tuple[str, str, str]:
        payload = self._payload(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd)
        name_with_owner = payload.get("nameWithOwner")
        url = payload.get("url")
        if not isinstance(name_with_owner, str) or not isinstance(url, str):
            raise _unavailable("The repository observation is missing its name or URL.")
        match = _URL_HOST.match(url)
        if match is None:
            raise _unavailable("The repository URL is not an https provider URL.")
        host = match.group("host")
        if _HOST.fullmatch(host) is None:
            raise _unavailable("The repository URL host is not a bounded lowercase host.")
        parts = name_with_owner.split("/")
        if len(parts) != 2 or any(_NAME_PART.fullmatch(part) is None for part in parts):
            raise _unavailable("The observed repository name is not <owner>/<repository>.")
        return host, parts[0], parts[1]

    def _stable_id(self, cwd: Path, host: str, owner: str, name: str) -> str:
        payload = self._payload(["gh", "api", "--hostname", host, f"repos/{owner}/{name}"], cwd)
        raw = payload.get("id")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise _unavailable(
                "The repository observation did not return a stable numeric repository ID."
            )
        return str(raw)
