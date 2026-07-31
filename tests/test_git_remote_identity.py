"""Pure contracts for repository-local Git transport identity evidence."""

from __future__ import annotations

import importlib

import pytest

NOW = "2026-08-01T00:00:00+00:00"
_KEY_A = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _contracts() -> object:
    try:
        return importlib.import_module("repoforge.domain.git_remote_identity")
    except ModuleNotFoundError:
        pytest.fail("git remote identity contracts are not implemented")


def test_reviewed_ssh_endpoint_builds_a_canonical_execution_url() -> None:
    contracts = _contracts()
    key = contracts.SshKeyProof(
        canonical_path="/Users/trung.ngo/.ssh/id_rsa_work",
        public_key_fingerprint=_KEY_A,
        owner_uid=501,
        mode=0o600,
        observed_at=NOW,
    )
    principal = contracts.SshPrincipalProof(
        provider_host="github.com",
        principal_kind="github_account",
        principal_login="matw-ngo",
        expected_actor_id="173029271",
        key_fingerprint=_KEY_A,
        observed_at=NOW,
        proof_digest="b" * 64,
    )
    endpoint = contracts.ReviewedSshEndpoint(
        schema_version=1,
        raw_host="github-work",
        canonical_host="github.com",
        user="git",
        port=22,
        owner="cicdata-io",
        repository="portal-spa",
        raw_url_digest="c" * 64,
        alias=None,
        key=key,
        principal=principal,
        proof_digest="d" * 64,
    )

    assert endpoint.canonical_url() == "ssh://git@github.com:22/cicdata-io/portal-spa.git"


def test_reviewed_ssh_endpoint_keeps_a_structured_alias_definition() -> None:
    contracts = _contracts()
    alias = contracts.SshAliasDefinition(
        alias="github-work",
        canonical_host="github.com",
        user="git",
        port=22,
        identity_file="/Users/trung.ngo/.ssh/id_rsa_work",
        source_config_digest="e" * 64,
        selected_block_digest="f" * 64,
    )
    key = contracts.SshKeyProof(
        canonical_path=alias.identity_file,
        public_key_fingerprint=_KEY_A,
        owner_uid=501,
        mode=0o600,
        observed_at=NOW,
    )
    principal = contracts.SshPrincipalProof(
        provider_host=alias.canonical_host,
        principal_kind="github_account",
        principal_login="matw-ngo",
        expected_actor_id="173029271",
        key_fingerprint=_KEY_A,
        observed_at=NOW,
        proof_digest="a" * 64,
    )
    endpoint = contracts.ReviewedSshEndpoint(
        schema_version=1,
        raw_host=alias.alias,
        canonical_host=alias.canonical_host,
        user=alias.user,
        port=alias.port,
        owner="cicdata-io",
        repository="portal-spa",
        raw_url_digest="b" * 64,
        alias=alias,
        key=key,
        principal=principal,
        proof_digest="c" * 64,
    )

    assert endpoint.alias == alias


def test_parsed_git_remote_keeps_transport_host_separate_from_repository_path() -> None:
    contracts = _contracts()

    remote = contracts.ParsedGitRemote(
        kind=contracts.GitRemoteKind.SSH,
        raw_host="github-work",
        owner="cicdata-io",
        repository="portal-spa",
        user="git",
        port=None,
        raw_url_digest="d" * 64,
    )

    assert remote.raw_host == "github-work"
    assert remote.repository_path == "cicdata-io/portal-spa"
