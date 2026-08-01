"""Bounded GitHub SSH principal proof using verified operation-scoped key material."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.git_remote_identity import SshKeyProof, SshPrincipalProof
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult
from ...ports.git_remote_identity import SshIdentityMaterialProvider

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_GITHUB_GREETING = re.compile(
    r"(?:^|\n)Hi (?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))! "
    r"You've successfully authenticated, but GitHub does not provide shell access\.?(?:\n|$)"
)


class _Executor(Protocol):
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
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult: ...


def _mismatch(message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=ErrorCode.GIT_TRANSPORT_PRINCIPAL_MISMATCH,
        retryable=retryable,
        unchanged_state=("No Git or GitHub write was attempted.",),
        safe_next_action="Re-inspect the selected SSH key and GitHub account before migrating.",
    )


class GitHubSshPrincipalVerifier:
    def __init__(
        self,
        executor: _Executor,
        *,
        materials: SshIdentityMaterialProvider,
        cwd: Path,
    ) -> None:
        self._executor = executor
        self._materials = materials
        self._cwd = cwd

    def verify(
        self,
        *,
        provider_host: str,
        expected_login: str,
        expected_actor_id: str,
        key: SshKeyProof,
        observed_at: str,
    ) -> SshPrincipalProof:
        if _HOST.fullmatch(provider_host) is None or _LOGIN.fullmatch(expected_login) is None:
            raise _mismatch("SSH principal probe inputs are not bounded provider identifiers.")
        with self._materials.open_verified(key) as material:
            result = self._executor.run_isolated(
                [
                    "ssh",
                    "-T",
                    "-F",
                    "/dev/null",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "IdentityAgent=none",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "PasswordAuthentication=no",
                    "-o",
                    "KbdInteractiveAuthentication=no",
                    "-i",
                    str(material.path),
                    f"git@{provider_host}",
                ],
                cwd=self._cwd,
                environment={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
                secrets=(),
                check=False,
                timeout=20,
                output_limit=16_384,
            )
        if result.stdout_truncated or result.stderr_truncated:
            raise _mismatch("GitHub SSH principal response exceeded the reviewed bound.")
        matches = tuple(_GITHUB_GREETING.finditer(result.combined))
        if len(matches) != 1:
            raise _mismatch("GitHub SSH did not return one recognized account principal.")
        observed_login = matches[0].group("login")
        if observed_login != expected_login:
            raise _mismatch(
                "GitHub SSH authenticated a different account than the selected profile."
            )
        stable = {
            "provider_host": provider_host,
            "principal_kind": "github_account",
            "principal_login": observed_login,
            "expected_actor_id": expected_actor_id,
            "key_fingerprint": key.public_key_fingerprint,
        }
        return SshPrincipalProof(
            **stable,
            observed_at=observed_at,
            proof_digest=hashlib.sha256(
                json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        )


__all__ = ["GitHubSshPrincipalVerifier"]
