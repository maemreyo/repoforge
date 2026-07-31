"""Provider-neutral Git transport identity and evidence contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .git_remote_identity import ReviewedSshEndpoint

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class GitTransportKind(str, Enum):
    SSH = "ssh"
    HTTPS = "https"


class GitTransportAccess(str, Enum):
    READ = "read"
    WRITE = "write"


class GitTransportActorEvidence(str, Enum):
    UNOBSERVABLE = "unobservable"


@dataclass(frozen=True, slots=True)
class GitTransportSpec:
    profile_id: str
    repository_id: str
    target_id: str
    provider_host: str
    kind: GitTransportKind
    credential_fingerprint: str
    allowed_access: tuple[GitTransportAccess, ...]
    ssh_identity_file: str | None = None
    https_token_environment: str | None = None
    ssh_endpoint: ReviewedSshEndpoint | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.profile_id, "profile_id"),
            (self.repository_id, "repository_id"),
            (self.target_id, "target_id"),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a bounded safe identifier")
        if not isinstance(self.provider_host, str) or _HOST.fullmatch(self.provider_host) is None:
            raise ValueError("provider_host is invalid")
        if not isinstance(self.kind, GitTransportKind):
            raise ValueError("kind must be a GitTransportKind")
        if _SHA256.fullmatch(self.credential_fingerprint) is None:
            raise ValueError("credential_fingerprint must be a lowercase SHA-256")
        if (
            not isinstance(self.allowed_access, tuple)
            or not self.allowed_access
            or any(not isinstance(item, GitTransportAccess) for item in self.allowed_access)
            or len(set(self.allowed_access)) != len(self.allowed_access)
        ):
            raise ValueError("allowed_access must be a non-empty unique tuple")
        if self.kind is GitTransportKind.SSH:
            if self.ssh_endpoint is None and (
                not isinstance(self.ssh_identity_file, str)
                or not self.ssh_identity_file.startswith("/")
                or "\x00" in self.ssh_identity_file
                or len(self.ssh_identity_file) > 4096
            ):
                raise ValueError(
                    "SSH transport requires a reviewed endpoint or an absolute "
                    "identity-file reference"
                )
            if (
                self.ssh_endpoint is not None
                and self.ssh_endpoint.canonical_host != self.provider_host.lower()
            ):
                raise ValueError("SSH endpoint host must match provider_host")
            if self.https_token_environment is not None:
                raise ValueError("SSH transport cannot declare an HTTPS token environment")
        else:
            if self.ssh_identity_file is not None:
                raise ValueError("HTTPS transport cannot declare an SSH identity file")
            if (
                not isinstance(self.https_token_environment, str)
                or _ENV.fullmatch(self.https_token_environment) is None
            ):
                raise ValueError("HTTPS transport requires a safe token environment name")
            if self.ssh_endpoint is not None:
                raise ValueError("HTTPS transport cannot declare an SSH endpoint")


@dataclass(frozen=True, slots=True)
class GitTransportEvidence:
    profile_id: str
    repository_id: str
    provider_host: str
    kind: GitTransportKind
    credential_fingerprint: str
    access: GitTransportAccess
    remote_url_digest: str
    requested_ref: str | None
    observed_sha: str | None
    actor_evidence: GitTransportActorEvidence = GitTransportActorEvidence.UNOBSERVABLE

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.profile_id, "profile_id"),
            (self.repository_id, "repository_id"),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a bounded safe identifier")
        if not isinstance(self.provider_host, str) or _HOST.fullmatch(self.provider_host) is None:
            raise ValueError("provider_host is invalid")
        if not isinstance(self.kind, GitTransportKind):
            raise ValueError("kind must be a GitTransportKind")
        if not isinstance(self.access, GitTransportAccess):
            raise ValueError("access must be a GitTransportAccess")
        if _SHA256.fullmatch(self.credential_fingerprint) is None:
            raise ValueError("credential_fingerprint must be a lowercase SHA-256")
        if _SHA256.fullmatch(self.remote_url_digest) is None:
            raise ValueError("remote_url_digest must be a lowercase SHA-256")
        if self.requested_ref is not None and (
            not isinstance(self.requested_ref, str)
            or not self.requested_ref
            or len(self.requested_ref) > 4096
            or "\x00" in self.requested_ref
        ):
            raise ValueError("requested_ref must be bounded text")
        if self.observed_sha is not None and _OBJECT_ID.fullmatch(self.observed_sha) is None:
            raise ValueError("observed_sha must be a lowercase Git object ID")
        if not isinstance(self.actor_evidence, GitTransportActorEvidence):
            raise ValueError("actor_evidence must be a GitTransportActorEvidence")

    def safe_payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "repository_id": self.repository_id,
            "provider_host": self.provider_host,
            "kind": self.kind.value,
            "credential_fingerprint": self.credential_fingerprint,
            "access": self.access.value,
            "remote_url_digest": self.remote_url_digest,
            "requested_ref": self.requested_ref,
            "observed_sha": self.observed_sha,
            "actor_evidence": self.actor_evidence.value,
        }
