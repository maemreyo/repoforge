"""Build and revalidate reviewed SSH endpoint proofs from one constrained authority path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...domain.errors import ErrorCode, RepoForgeError
from ...domain.git_remote_identity import GitRemoteKind, ParsedGitRemote, ReviewedSshEndpoint
from ...ports.clock import Clock
from ...ports.git_remote_identity import (
    GitRemoteParser,
    SshAliasResolver,
    SshKeyInspector,
    SshPrincipalVerifier,
)


def _endpoint_error(
    message: str,
    *,
    code: ErrorCode = ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH,
) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=(
            "No Git or GitHub network write was attempted.",
            "No repository remote or operator SSH configuration was modified.",
        ),
        safe_next_action="Re-run `rf auth migrate inspect` and review the fresh SSH endpoint proof.",
    )


def _proof_digest(
    remote: ParsedGitRemote,
    *,
    alias_digest: str,
    selected_block_digest: str,
    key_fingerprint: str,
    principal_digest: str,
    canonical_host: str,
    user: str,
    port: int,
) -> str:
    safe = {
        "schema_version": 1,
        "remote_kind": remote.kind.value,
        "raw_host": remote.raw_host,
        "canonical_host": canonical_host,
        "user": user,
        "port": port,
        "owner": remote.owner,
        "repository": remote.repository,
        "raw_url_digest": remote.raw_url_digest,
        "source_config_digest": alias_digest,
        "selected_block_digest": selected_block_digest,
        "key_fingerprint": key_fingerprint,
        "principal_proof_digest": principal_digest,
    }
    return hashlib.sha256(
        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ReviewedSshEndpointAuthority:
    """Resolve and re-prove aliased SSH endpoints without OpenSSH config evaluation."""

    def __init__(
        self,
        *,
        remote_parser: GitRemoteParser,
        aliases: SshAliasResolver,
        keys: SshKeyInspector,
        principals: SshPrincipalVerifier,
        clock: Clock,
    ) -> None:
        self._remote_parser = remote_parser
        self._aliases = aliases
        self._keys = keys
        self._principals = principals
        self._clock = clock

    def resolve(
        self,
        raw_remote_url: str,
        *,
        provider_host: str,
        expected_login: str,
        expected_actor_id: str,
    ) -> tuple[ParsedGitRemote, ReviewedSshEndpoint | None]:
        remote = self._remote_parser.parse(raw_remote_url)
        if remote.kind is GitRemoteKind.HTTPS:
            if remote.raw_host != provider_host:
                raise _endpoint_error(
                    "HTTPS remote host does not match the selected profile provider host."
                )
            return remote, None
        if remote.raw_host == provider_host:
            raise _endpoint_error(
                "Direct-host SSH migration has no constrained alias block to bind its private key; "
                "declare an exact SSH alias with IdentitiesOnly yes before migrating.",
                code=ErrorCode.GIT_TRANSPORT_MIGRATION_REQUIRED,
            )
        alias = self._aliases.resolve(remote.raw_host)
        if alias.canonical_host != provider_host:
            raise _endpoint_error(
                "SSH alias canonical host does not match the selected profile provider host."
            )
        if remote.user is not None and remote.user != alias.user:
            raise _endpoint_error("SSH remote user does not match the reviewed alias user.")
        if remote.port is not None and remote.port != alias.port:
            raise _endpoint_error("SSH remote port does not match the reviewed alias port.")
        observed_at = self._clock.now_iso()
        key = self._keys.inspect(alias.identity_file, observed_at=observed_at)
        principal = self._principals.verify(
            provider_host=provider_host,
            expected_login=expected_login,
            expected_actor_id=expected_actor_id,
            key=key,
            observed_at=observed_at,
        )
        endpoint = ReviewedSshEndpoint(
            schema_version=1,
            raw_host=remote.raw_host,
            canonical_host=provider_host,
            user=alias.user,
            port=alias.port,
            owner=remote.owner,
            repository=remote.repository,
            raw_url_digest=remote.raw_url_digest,
            alias=alias,
            key=key,
            principal=principal,
            proof_digest=_proof_digest(
                remote,
                alias_digest=alias.source_config_digest,
                selected_block_digest=alias.selected_block_digest,
                key_fingerprint=key.public_key_fingerprint,
                principal_digest=principal.proof_digest,
                canonical_host=provider_host,
                user=alias.user,
                port=alias.port,
            ),
        )
        return remote, endpoint

    def revalidate(
        self,
        *,
        cwd: Path,
        raw_remote_url: str,
        expected: ReviewedSshEndpoint,
    ) -> ReviewedSshEndpoint:
        del cwd
        remote, live = self.resolve(
            raw_remote_url,
            provider_host=expected.canonical_host,
            expected_login=expected.principal.principal_login,
            expected_actor_id=expected.principal.expected_actor_id,
        )
        if live is None or expected.alias is None or live.alias is None:
            raise _endpoint_error(
                "SSH endpoint proof is no longer an aliased reviewed endpoint.",
                code=ErrorCode.GIT_TRANSPORT_ENDPOINT_STALE,
            )
        stable_expected = (
            expected.raw_host,
            expected.canonical_host,
            expected.user,
            expected.port,
            expected.owner,
            expected.repository,
            expected.raw_url_digest,
            expected.alias.source_config_digest,
            expected.alias.selected_block_digest,
            expected.key.canonical_path,
            expected.key.public_key_fingerprint,
            expected.key.owner_uid,
            expected.key.mode,
            expected.principal.principal_login,
            expected.principal.expected_actor_id,
        )
        stable_live = (
            remote.raw_host,
            live.canonical_host,
            live.user,
            live.port,
            live.owner,
            live.repository,
            remote.raw_url_digest,
            live.alias.source_config_digest,
            live.alias.selected_block_digest,
            live.key.canonical_path,
            live.key.public_key_fingerprint,
            live.key.owner_uid,
            live.key.mode,
            live.principal.principal_login,
            live.principal.expected_actor_id,
        )
        if stable_live != stable_expected:
            raise _endpoint_error(
                "SSH endpoint authority changed since the reviewed proof was accepted.",
                code=ErrorCode.GIT_TRANSPORT_ENDPOINT_STALE,
            )
        return live


__all__ = ["ReviewedSshEndpointAuthority"]
