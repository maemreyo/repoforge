"""Worktree-safe commit attribution and signing contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .repository_identity import ActorClass

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EMAIL = re.compile(r"^[^\s<>@]+@[^\s<>@]+$")
_FINGERPRINT = re.compile(r"^[A-Za-z0-9:+/_=-]{16,160}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_KEY = re.compile(r"^[a-z0-9][a-z0-9.-]{0,255}$")


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _identity_text(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 200
        or any(character in value for character in "\x00\r\n<>")
    ):
        raise ValueError(f"{field_name} must be bounded single-line identity text")
    return value


def _email(value: str, field_name: str) -> str:
    if not isinstance(value, str) or len(value) > 320 or _EMAIL.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded email address")
    return value


def _optional_safe_id(value: str | None, field_name: str) -> str | None:
    return None if value is None else _safe_id(value, field_name)


class CommitSigningMode(str, Enum):
    UNSIGNED_ATTESTED = "unsigned_attested"
    SSH = "ssh"
    GPG = "gpg"


@dataclass(frozen=True, slots=True)
class CommitIdentityPolicy:
    profile_id: str
    actor_class: ActorClass
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    signing_mode: CommitSigningMode
    signer_fingerprint: str | None = None
    signing_key_reference: str | None = None
    represented_actor_id: str | None = None
    delegation_approval_id: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _identity_text(self.author_name, "author_name")
        _email(self.author_email, "author_email")
        _identity_text(self.committer_name, "committer_name")
        _email(self.committer_email, "committer_email")
        if not isinstance(self.signing_mode, CommitSigningMode):
            raise ValueError("signing_mode must be a CommitSigningMode")
        represented = _optional_safe_id(self.represented_actor_id, "represented_actor_id")
        approval = _optional_safe_id(self.delegation_approval_id, "delegation_approval_id")
        if self.actor_class is ActorClass.DELEGATED_HUMAN:
            if represented is None or approval is None:
                raise ValueError(
                    "delegation requires represented_actor_id and delegation_approval_id"
                )
        elif represented is not None or approval is not None:
            raise ValueError("delegation evidence is only valid for delegated-human policy")
        if self.signing_mode is CommitSigningMode.UNSIGNED_ATTESTED:
            if self.signer_fingerprint is not None or self.signing_key_reference is not None:
                raise ValueError("unsigned attestation cannot declare a direct signer")
        else:
            if (
                not isinstance(self.signer_fingerprint, str)
                or _FINGERPRINT.fullmatch(self.signer_fingerprint) is None
                or not isinstance(self.signing_key_reference, str)
                or not self.signing_key_reference
                or len(self.signing_key_reference) > 4096
                or "\x00" in self.signing_key_reference
            ):
                raise ValueError(
                    "direct signing requires a bounded signer fingerprint and key reference"
                )

    def durable_payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "actor_class": self.actor_class.value,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "signing_mode": self.signing_mode.value,
            "signer_fingerprint": self.signer_fingerprint,
            "signing_key_reference": self.signing_key_reference,
            "represented_actor_id": self.represented_actor_id,
            "delegation_approval_id": self.delegation_approval_id,
        }

    def safe_payload(self) -> dict[str, object]:
        payload = self.durable_payload()
        payload.pop("signing_key_reference", None)
        return payload


@dataclass(frozen=True, slots=True)
class CommitConfigEntryEvidence:
    scope: str
    key: str
    value_digest: str

    def __post_init__(self) -> None:
        if self.scope not in {"local", "worktree"}:
            raise ValueError("config evidence scope must be local or worktree")
        if not isinstance(self.key, str) or _CONFIG_KEY.fullmatch(self.key) is None:
            raise ValueError("config evidence key is invalid")
        if not isinstance(self.value_digest, str) or _SHA256.fullmatch(self.value_digest) is None:
            raise ValueError("config evidence value_digest must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CommitConfigSnapshot:
    digest: str
    worktree_config_enabled: bool
    entries: tuple[CommitConfigEntryEvidence, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or _SHA256.fullmatch(self.digest) is None:
            raise ValueError("config snapshot digest must be a lowercase SHA-256")
        if not isinstance(self.worktree_config_enabled, bool):
            raise ValueError("worktree_config_enabled must be a boolean")
        if (
            not isinstance(self.entries, tuple)
            or len(self.entries) > 512
            or any(not isinstance(item, CommitConfigEntryEvidence) for item in self.entries)
        ):
            raise ValueError("config snapshot entries must be a bounded tuple")
        identities = tuple((item.scope, item.key) for item in self.entries)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ValueError("config snapshot entries must be unique and sorted")

    def safe_payload(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "worktree_config_enabled": self.worktree_config_enabled,
            "entries": [
                {"scope": item.scope, "key": item.key, "value_digest": item.value_digest}
                for item in self.entries
            ],
        }


@dataclass(frozen=True, slots=True)
class CommitIdentityEvidence:
    profile_id: str
    actor_class: ActorClass
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    signing_mode: CommitSigningMode
    signer_fingerprint: str | None
    attestation_digest: str | None
    config_snapshot_digest: str
    represented_actor_id: str | None = None
    delegation_approval_id: str | None = None

    def __post_init__(self) -> None:
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        _identity_text(self.author_name, "author_name")
        _email(self.author_email, "author_email")
        _identity_text(self.committer_name, "committer_name")
        _email(self.committer_email, "committer_email")
        if not isinstance(self.signing_mode, CommitSigningMode):
            raise ValueError("signing_mode must be a CommitSigningMode")
        if (
            self.signer_fingerprint is not None
            and _FINGERPRINT.fullmatch(self.signer_fingerprint) is None
        ):
            raise ValueError("signer_fingerprint is invalid")
        if (
            self.attestation_digest is not None
            and _SHA256.fullmatch(self.attestation_digest) is None
        ):
            raise ValueError("attestation_digest must be a lowercase SHA-256")
        if _SHA256.fullmatch(self.config_snapshot_digest) is None:
            raise ValueError("config_snapshot_digest must be a lowercase SHA-256")
        represented = _optional_safe_id(self.represented_actor_id, "represented_actor_id")
        approval = _optional_safe_id(self.delegation_approval_id, "delegation_approval_id")
        if self.actor_class is ActorClass.DELEGATED_HUMAN:
            if represented is None or approval is None:
                raise ValueError(
                    "delegated-human evidence requires represented_actor_id and delegation_approval_id"
                )
        elif represented is not None or approval is not None:
            raise ValueError("delegation evidence is only valid for delegated-human commits")
        if self.signing_mode is CommitSigningMode.UNSIGNED_ATTESTED:
            if self.attestation_digest is None or self.signer_fingerprint is not None:
                raise ValueError("unsigned evidence requires attestation only")
        elif self.signer_fingerprint is None or self.attestation_digest is not None:
            raise ValueError("signed evidence requires signer fingerprint only")

    def safe_payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "actor_class": self.actor_class.value,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "signing_mode": self.signing_mode.value,
            "signer_fingerprint": self.signer_fingerprint,
            "attestation_digest": self.attestation_digest,
            "config_snapshot_digest": self.config_snapshot_digest,
            "represented_actor_id": self.represented_actor_id,
            "delegation_approval_id": self.delegation_approval_id,
        }


@dataclass(frozen=True, slots=True)
class ManagedCommitResult:
    head_sha: str
    summary: str
    evidence: CommitIdentityEvidence

    def __post_init__(self) -> None:
        if (
            not isinstance(self.head_sha, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", self.head_sha) is None
        ):
            raise ValueError("head_sha must be a lowercase Git object ID")
        if not isinstance(self.summary, str):
            raise ValueError("summary must be text")
        if not isinstance(self.evidence, CommitIdentityEvidence):
            raise ValueError("evidence must be CommitIdentityEvidence")


def commit_identity_policy_from_payload(payload: dict[str, object]) -> CommitIdentityPolicy:
    expected = {
        "profile_id",
        "actor_class",
        "author_name",
        "author_email",
        "committer_name",
        "committer_email",
        "signing_mode",
        "signer_fingerprint",
        "signing_key_reference",
        "represented_actor_id",
        "delegation_approval_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("commit identity policy fields do not match the schema")

    def optional_string(name: str) -> str | None:
        value = payload[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string or null")
        return value

    def required_string(name: str) -> str:
        value = payload[name]
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        return value

    return CommitIdentityPolicy(
        profile_id=required_string("profile_id"),
        actor_class=ActorClass(required_string("actor_class")),
        author_name=required_string("author_name"),
        author_email=required_string("author_email"),
        committer_name=required_string("committer_name"),
        committer_email=required_string("committer_email"),
        signing_mode=CommitSigningMode(required_string("signing_mode")),
        signer_fingerprint=optional_string("signer_fingerprint"),
        signing_key_reference=optional_string("signing_key_reference"),
        represented_actor_id=optional_string("represented_actor_id"),
        delegation_approval_id=optional_string("delegation_approval_id"),
    )
