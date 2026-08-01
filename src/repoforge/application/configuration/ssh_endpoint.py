"""Parse and render secret-free reviewed SSH endpoint proofs."""

from __future__ import annotations

from typing import Any

from ...domain.git_remote_identity import (
    ReviewedSshEndpoint,
    SshAliasDefinition,
    SshKeyProof,
    SshPrincipalProof,
)

_FIELDS = {
    "schema_version",
    "raw_host",
    "canonical_host",
    "user",
    "port",
    "owner",
    "repository",
    "raw_url_digest",
    "canonical_path",
    "public_key_fingerprint",
    "owner_uid",
    "mode",
    "key_observed_at",
    "source_config_digest",
    "selected_block_digest",
    "principal_kind",
    "principal_login",
    "principal_actor_id",
    "principal_observed_at",
    "principal_proof_digest",
    "proof_digest",
}


def _required_string(raw: dict[str, Any], name: str, *, context: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"{context}.{name} must be a non-empty bounded string")
    return value


def _required_int(raw: dict[str, Any], name: str, *, context: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{context}.{name} must be an integer")
    return value


def parse_source_ssh_endpoint(raw: object, *, context: str) -> ReviewedSshEndpoint:
    if not isinstance(raw, dict):
        raise ValueError(f"{context} must be a TOML table")
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise ValueError(f"{context} contains unsupported fields: {unknown}")

    raw_host = _required_string(raw, "raw_host", context=context)
    canonical_host = _required_string(raw, "canonical_host", context=context)
    identity_file = _required_string(raw, "canonical_path", context=context)
    alias = (
        SshAliasDefinition(
            alias=raw_host,
            canonical_host=canonical_host,
            user=_required_string(raw, "user", context=context),
            port=_required_int(raw, "port", context=context),
            identity_file=identity_file,
            source_config_digest=_required_string(raw, "source_config_digest", context=context),
            selected_block_digest=_required_string(raw, "selected_block_digest", context=context),
        )
        if raw_host != canonical_host
        else None
    )
    key = SshKeyProof(
        canonical_path=identity_file,
        public_key_fingerprint=_required_string(raw, "public_key_fingerprint", context=context),
        owner_uid=_required_int(raw, "owner_uid", context=context),
        mode=_required_int(raw, "mode", context=context),
        observed_at=_required_string(raw, "key_observed_at", context=context),
    )
    principal = SshPrincipalProof(
        provider_host=canonical_host,
        principal_kind=_required_string(raw, "principal_kind", context=context),
        principal_login=_required_string(raw, "principal_login", context=context),
        expected_actor_id=_required_string(raw, "principal_actor_id", context=context),
        key_fingerprint=key.public_key_fingerprint,
        observed_at=_required_string(raw, "principal_observed_at", context=context),
        proof_digest=_required_string(raw, "principal_proof_digest", context=context),
    )
    return ReviewedSshEndpoint(
        schema_version=_required_int(raw, "schema_version", context=context),
        raw_host=raw_host,
        canonical_host=canonical_host,
        user=_required_string(raw, "user", context=context),
        port=_required_int(raw, "port", context=context),
        owner=_required_string(raw, "owner", context=context),
        repository=_required_string(raw, "repository", context=context),
        raw_url_digest=_required_string(raw, "raw_url_digest", context=context),
        alias=alias,
        key=key,
        principal=principal,
        proof_digest=_required_string(raw, "proof_digest", context=context),
    )


def source_ssh_endpoint_table(endpoint: ReviewedSshEndpoint) -> dict[str, object]:
    table: dict[str, object] = {
        "schema_version": endpoint.schema_version,
        "raw_host": endpoint.raw_host,
        "canonical_host": endpoint.canonical_host,
        "user": endpoint.user,
        "port": endpoint.port,
        "owner": endpoint.owner,
        "repository": endpoint.repository,
        "raw_url_digest": endpoint.raw_url_digest,
        "canonical_path": endpoint.key.canonical_path,
        "public_key_fingerprint": endpoint.key.public_key_fingerprint,
        "owner_uid": endpoint.key.owner_uid,
        "mode": endpoint.key.mode,
        "key_observed_at": endpoint.key.observed_at,
        "principal_kind": endpoint.principal.principal_kind,
        "principal_login": endpoint.principal.principal_login,
        "principal_actor_id": endpoint.principal.expected_actor_id,
        "principal_observed_at": endpoint.principal.observed_at,
        "principal_proof_digest": endpoint.principal.proof_digest,
        "proof_digest": endpoint.proof_digest,
    }
    if endpoint.alias is not None:
        table["source_config_digest"] = endpoint.alias.source_config_digest
        table["selected_block_digest"] = endpoint.alias.selected_block_digest
    return table


__all__ = ["parse_source_ssh_endpoint", "source_ssh_endpoint_table"]
