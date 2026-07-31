"""Secret-free evidence for repository-local Git transport identity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SSH_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_PRINCIPAL_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _require_host(value: str, field: str) -> None:
    if not isinstance(value, str) or _HOST.fullmatch(value) is None:
        raise ValueError(f"{field} must be one lowercase host")


def _require_digest(value: str, field: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")


def _require_fingerprint(value: str, field: str) -> None:
    if not isinstance(value, str) or _SSH_FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{field} must be an OpenSSH SHA256 fingerprint")


class GitRemoteKind(str, Enum):
    SSH = "ssh"
    HTTPS = "https"


@dataclass(frozen=True, slots=True)
class ParsedGitRemote:
    kind: GitRemoteKind
    raw_host: str
    owner: str
    repository: str
    user: str | None
    port: int | None
    raw_url_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, GitRemoteKind):
            raise ValueError("kind must be a GitRemoteKind")
        _require_host(self.raw_host, "raw_host")
        for value, field in ((self.owner, "owner"), (self.repository, "repository")):
            if not isinstance(value, str) or _NAME.fullmatch(value) is None:
                raise ValueError(f"{field} must be a bounded repository name")
        if self.user is not None and (
            not isinstance(self.user, str) or not self.user or len(self.user) > 64
        ):
            raise ValueError("user must be bounded text when present")
        if self.port is not None and (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("port must be in 1..65535 when present")
        _require_digest(self.raw_url_digest, "raw_url_digest")

    @property
    def repository_path(self) -> str:
        return f"{self.owner}/{self.repository}"


@dataclass(frozen=True, slots=True)
class SshKeyProof:
    canonical_path: str
    public_key_fingerprint: str
    owner_uid: int
    mode: int
    observed_at: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.canonical_path, str)
            or not self.canonical_path.startswith("/")
            or "\x00" in self.canonical_path
            or len(self.canonical_path) > 4096
        ):
            raise ValueError("canonical_path must be one absolute path")
        _require_fingerprint(self.public_key_fingerprint, "public_key_fingerprint")
        if (
            not isinstance(self.owner_uid, int)
            or isinstance(self.owner_uid, bool)
            or self.owner_uid < 0
        ):
            raise ValueError("owner_uid must be a non-negative integer")
        if (
            not isinstance(self.mode, int)
            or isinstance(self.mode, bool)
            or not 0 <= self.mode <= 0o777
        ):
            raise ValueError("mode must be a Unix permission value")
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("observed_at must be non-empty text")


@dataclass(frozen=True, slots=True)
class SshPrincipalProof:
    provider_host: str
    principal_kind: str
    principal_login: str
    expected_actor_id: str
    key_fingerprint: str
    observed_at: str
    proof_digest: str

    def __post_init__(self) -> None:
        _require_host(self.provider_host, "provider_host")
        if (
            not isinstance(self.principal_kind, str)
            or _PRINCIPAL_KIND.fullmatch(self.principal_kind) is None
        ):
            raise ValueError("principal_kind must be a bounded identifier")
        if (
            not isinstance(self.principal_login, str)
            or _LOGIN.fullmatch(self.principal_login) is None
        ):
            raise ValueError("principal_login must be a bounded GitHub login")
        if not isinstance(self.expected_actor_id, str) or not self.expected_actor_id:
            raise ValueError("expected_actor_id must be non-empty text")
        _require_fingerprint(self.key_fingerprint, "key_fingerprint")
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("observed_at must be non-empty text")
        _require_digest(self.proof_digest, "proof_digest")


@dataclass(frozen=True, slots=True)
class SshAliasDefinition:
    alias: str
    canonical_host: str
    user: str
    port: int
    identity_file: str
    source_config_digest: str
    selected_block_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or _ALIAS.fullmatch(self.alias) is None:
            raise ValueError("alias must be a bounded SSH alias")
        _require_host(self.canonical_host, "canonical_host")
        if self.user != "git":
            raise ValueError("user must be git")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("port must be in 1..65535")
        if (
            not isinstance(self.identity_file, str)
            or not self.identity_file.startswith("/")
            or "\x00" in self.identity_file
            or len(self.identity_file) > 4096
        ):
            raise ValueError("identity_file must be one absolute path")
        _require_digest(self.source_config_digest, "source_config_digest")
        _require_digest(self.selected_block_digest, "selected_block_digest")


@dataclass(frozen=True, slots=True)
class ReviewedSshEndpoint:
    schema_version: int
    raw_host: str
    canonical_host: str
    user: str
    port: int
    owner: str
    repository: str
    raw_url_digest: str
    alias: SshAliasDefinition | None
    key: SshKeyProof
    principal: SshPrincipalProof
    proof_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        _require_host(self.raw_host, "raw_host")
        _require_host(self.canonical_host, "canonical_host")
        if self.user != "git":
            raise ValueError("user must be git")
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("port must be in 1..65535")
        for value, field in ((self.owner, "owner"), (self.repository, "repository")):
            if not isinstance(value, str) or _NAME.fullmatch(value) is None:
                raise ValueError(f"{field} must be a bounded repository name")
        _require_digest(self.raw_url_digest, "raw_url_digest")
        _require_digest(self.proof_digest, "proof_digest")
        if self.principal.provider_host != self.canonical_host:
            raise ValueError("principal provider host must match canonical host")
        if self.principal.key_fingerprint != self.key.public_key_fingerprint:
            raise ValueError("principal key fingerprint must match key proof")
        if self.alias is not None:
            if self.alias.alias != self.raw_host:
                raise ValueError("alias must match raw_host")
            if self.alias.canonical_host != self.canonical_host:
                raise ValueError("alias canonical host must match endpoint")
            if self.alias.user != self.user or self.alias.port != self.port:
                raise ValueError("alias user and port must match endpoint")
            if self.alias.identity_file != self.key.canonical_path:
                raise ValueError("alias identity file must match key proof")

    def canonical_url(self) -> str:
        return (
            f"ssh://{self.user}@{self.canonical_host}:{self.port}/"
            f"{self.owner}/{self.repository}.git"
        )


__all__ = [
    "GitRemoteKind",
    "ParsedGitRemote",
    "ReviewedSshEndpoint",
    "SshAliasDefinition",
    "SshKeyProof",
    "SshPrincipalProof",
]
