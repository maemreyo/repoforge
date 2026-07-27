"""Provider-neutral ephemeral repository-auth broker.

Raw secret bodies exist only in bounded in-memory ``EphemeralSecret`` values.
Durable payloads, reprs, diagnostics, and receipts expose identifiers and key names only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, TypeVar

from .errors import ErrorCode, RepoForgeError
from .repository_identity import (
    ActorClass,
    AuthTargetKind,
    CredentialProfile,
    OpaqueCredentialReference,
)

T = TypeVar("T")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_CONFIG_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_IDENTITY_ENVIRONMENT = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SSH_ASKPASS",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_DATE",
        "GPG_TTY",
    }
)
_IDENTITY_ENVIRONMENT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _safe_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a bounded safe identifier")
    return value


def _timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{field_name} must be a bounded timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return parsed


def _unique(values: tuple[str, ...], field_name: str, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or len(values) > 64:
        raise ValueError(f"{field_name} must be a bounded tuple")
    if any(not isinstance(value, str) or pattern.fullmatch(value) is None for value in values):
        raise ValueError(f"{field_name} contains an invalid value")
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} entries must be unique")
    return values


def _config_pairs(
    values: tuple[tuple[str, str], ...], field_name: str
) -> tuple[tuple[str, str], ...]:
    if not isinstance(values, tuple) or len(values) > 32:
        raise ValueError(f"{field_name} must be a bounded tuple")
    keys: list[str] = []
    for key, value in values:
        if not isinstance(key, str) or _CONFIG_KEY.fullmatch(key) is None:
            raise ValueError(f"{field_name} contains an invalid key")
        if not isinstance(value, str) or not value or len(value) > 2_000 or "\x00" in value:
            raise ValueError(f"{field_name} contains an invalid value")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError(f"{field_name} keys must be unique")
    return values


def _broker_error(code: ErrorCode, message: str, *, retryable: bool = False) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=retryable,
        unchanged_state=("No repository command or callback was started.",),
    )


class AuthMaterialState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(slots=True)
class EphemeralSecret:
    _buffer: bytearray = field(repr=False)
    _released: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_text(cls, value: str) -> EphemeralSecret:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1_000_000:
            raise ValueError("secret value must be bounded non-empty text")
        return cls(bytearray(value.encode("utf-8")))

    @property
    def released(self) -> bool:
        return self._released

    def reveal(self) -> str:
        if self._released:
            raise RuntimeError("secret value has been released")
        return self._buffer.decode("utf-8")

    def release(self) -> None:
        if self._released:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._released = True

    def __repr__(self) -> str:
        return "EphemeralSecret(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class AuthEnvironmentBinding:
    name: str
    value: EphemeralSecret = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _ENV_NAME.fullmatch(self.name) is None:
            raise ValueError("repository-auth environment name is invalid")
        if not isinstance(self.value, EphemeralSecret):
            raise ValueError("repository-auth environment value must be ephemeral")


@dataclass(slots=True)
class AuthMaterial:
    material_id: str
    profile_id: str
    actor_class: ActorClass
    target_kind: AuthTargetKind
    target_id: str
    capability_ids: tuple[str, ...]
    issued_at: str
    expires_at: str
    state: AuthMaterialState
    environment: tuple[AuthEnvironmentBinding, ...] = ()
    git_config: tuple[tuple[str, str], ...] = ()
    callback_config: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _safe_id(self.material_id, "material_id")
        _safe_id(self.profile_id, "profile_id")
        if not isinstance(self.actor_class, ActorClass):
            raise ValueError("actor_class must be an ActorClass")
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        _safe_id(self.target_id, "target_id")
        _unique(self.capability_ids, "capability_ids", _SAFE_ID)
        issued = _timestamp(self.issued_at, "issued_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("expires_at must be later than issued_at")
        if not isinstance(self.state, AuthMaterialState):
            raise ValueError("state must be an AuthMaterialState")
        if not isinstance(self.environment, tuple) or len(self.environment) > 32:
            raise ValueError("environment must be a bounded tuple")
        if any(not isinstance(item, AuthEnvironmentBinding) for item in self.environment):
            raise ValueError("environment contains an invalid binding")
        names = tuple(item.name for item in self.environment)
        if len(set(names)) != len(names):
            raise ValueError("environment names must be unique")
        _config_pairs(self.git_config, "git_config")
        _config_pairs(self.callback_config, "callback_config")

    def secret_values(self) -> tuple[str, ...]:
        return tuple(item.value.reveal() for item in self.environment)

    def release(self) -> None:
        for item in self.environment:
            item.value.release()

    def safe_payload(self) -> dict[str, object]:
        return {
            "material_id": self.material_id,
            "profile_id": self.profile_id,
            "actor_class": self.actor_class.value,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "capability_ids": list(self.capability_ids),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "state": self.state.value,
            "environment_keys": [item.name for item in self.environment],
            "git_config_keys": [key for key, _value in self.git_config],
            "callback_config_keys": [key for key, _value in self.callback_config],
        }

    def __repr__(self) -> str:
        return (
            "AuthMaterial("
            f"material_id={self.material_id!r}, profile_id={self.profile_id!r}, "
            f"state={self.state.value!r}, environment_keys="
            f"{tuple(item.name for item in self.environment)!r})"
        )


@dataclass(frozen=True, slots=True)
class AuthBrokerRequest:
    profile: CredentialProfile
    target_kind: AuthTargetKind
    target_id: str
    required_capability_ids: tuple[str, ...]
    allowed_environment_keys: tuple[str, ...]
    now: str
    allowed_git_config_keys: tuple[str, ...] = ()
    allowed_callback_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, CredentialProfile):
            raise ValueError("profile must be a CredentialProfile")
        if not isinstance(self.target_kind, AuthTargetKind):
            raise ValueError("target_kind must be an AuthTargetKind")
        _safe_id(self.target_id, "target_id")
        _unique(self.required_capability_ids, "required_capability_ids", _SAFE_ID)
        _unique(self.allowed_environment_keys, "allowed_environment_keys", _ENV_NAME)
        _unique(self.allowed_git_config_keys, "allowed_git_config_keys", _CONFIG_KEY)
        _unique(self.allowed_callback_keys, "allowed_callback_keys", _CONFIG_KEY)
        _timestamp(self.now, "now")


@dataclass(frozen=True, slots=True)
class ProcessAuthContext:
    profile_id: str
    material_id: str
    target_kind: AuthTargetKind
    target_id: str
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    git_config: tuple[tuple[str, str], ...] = ()
    callback_config: tuple[tuple[str, str], ...] = ()
    _secret_values: tuple[str, ...] = field(default=(), repr=False)

    def environment_dict(self) -> dict[str, str]:
        return dict(self.environment)

    @property
    def secret_values(self) -> tuple[str, ...]:
        return self._secret_values

    def safe_payload(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "material_id": self.material_id,
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "environment_keys": [key for key, _value in self.environment],
            "git_config_keys": [key for key, _value in self.git_config],
            "callback_config_keys": [key for key, _value in self.callback_config],
        }


class _AuthMaterialProvider(Protocol):
    def resolve(self, reference: OpaqueCredentialReference) -> AuthMaterial | None: ...

    def refresh(
        self,
        reference: OpaqueCredentialReference,
        previous: AuthMaterial,
    ) -> AuthMaterial | None: ...

    def release(self, material: AuthMaterial) -> None: ...


def _is_identity_environment(name: str) -> bool:
    return name in _IDENTITY_ENVIRONMENT or name.startswith(_IDENTITY_ENVIRONMENT_PREFIXES)


def _release(provider: _AuthMaterialProvider, material: AuthMaterial) -> None:
    try:
        provider.release(material)
    except Exception:
        material.release()


def _material_scope_matches(material: AuthMaterial, request: AuthBrokerRequest) -> bool:
    return (
        material.profile_id == request.profile.profile_id
        and material.actor_class is request.profile.actor_class
        and material.target_kind is request.target_kind
        and material.target_id == request.target_id
    )


def _validate_material(
    provider: _AuthMaterialProvider,
    material: AuthMaterial,
    request: AuthBrokerRequest,
) -> None:
    if material.state is AuthMaterialState.REVOKED:
        _release(provider, material)
        raise _broker_error(ErrorCode.CREDENTIAL_REVOKED, "Repository-auth material is revoked.")
    if not _material_scope_matches(material, request):
        _release(provider, material)
        raise _broker_error(
            ErrorCode.CREDENTIAL_SCOPE_MISMATCH,
            "Repository-auth material does not match the reviewed target.",
        )
    material_capabilities = set(material.capability_ids)
    if not set(request.required_capability_ids).issubset(
        material_capabilities
    ) or not material_capabilities.issubset(set(request.profile.capability_ids)):
        _release(provider, material)
        raise _broker_error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "Repository-auth material violates the reviewed capability ceiling.",
        )
    environment_keys = {item.name for item in material.environment}
    if not environment_keys.issubset(set(request.allowed_environment_keys)):
        _release(provider, material)
        raise _broker_error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "Repository-auth environment keys exceed the reviewed allowlist.",
        )
    if not {key for key, _value in material.git_config}.issubset(
        set(request.allowed_git_config_keys)
    ) or not {key for key, _value in material.callback_config}.issubset(
        set(request.allowed_callback_keys)
    ):
        _release(provider, material)
        raise _broker_error(
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
            "Repository-auth configuration keys exceed the reviewed allowlist.",
        )
    secret_values = material.secret_values()
    configured_values = tuple(
        value for _key, value in (*material.git_config, *material.callback_config)
    )
    if any(
        secret and secret in configured
        for secret in secret_values
        for configured in configured_values
    ):
        _release(provider, material)
        raise _broker_error(
            ErrorCode.CREDENTIAL_LEAK_BLOCKED,
            "Raw repository-auth material cannot be embedded in configuration text.",
        )


class AuthBrokerSession(AbstractContextManager["AuthBrokerSession"]):
    def __init__(
        self,
        provider: _AuthMaterialProvider,
        request: AuthBrokerRequest,
        material: AuthMaterial,
    ) -> None:
        self._provider = provider
        self._request = request
        self._material = material
        self._released = False

    def __enter__(self) -> AuthBrokerSession:
        return self

    def process_context(self, base_environment: Mapping[str, str]) -> ProcessAuthContext:
        if self._released:
            raise _broker_error(
                ErrorCode.CREDENTIAL_EXPIRED,
                "Repository-auth session has already been released.",
            )
        environment: dict[str, str] = {}
        for name, value in base_environment.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("base_environment must contain string pairs")
            if not _is_identity_environment(name):
                environment[name] = value
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GH_PROMPT_DISABLED"] = "1"
        for binding in self._material.environment:
            environment[binding.name] = binding.value.reveal()
        secrets = self._material.secret_values()
        return ProcessAuthContext(
            profile_id=self._material.profile_id,
            material_id=self._material.material_id,
            target_kind=self._material.target_kind,
            target_id=self._material.target_id,
            environment=tuple(sorted(environment.items())),
            git_config=self._material.git_config,
            callback_config=self._material.callback_config,
            _secret_values=secrets,
        )

    def invoke(
        self,
        callback: Callable[[ProcessAuthContext], T],
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> T:
        context = self.process_context(base_environment or {})
        try:
            return callback(context)
        except RepoForgeError:
            raise
        except Exception:
            pass
        raise _broker_error(
            ErrorCode.CREDENTIAL_CALLBACK_FAILED,
            "Repository-auth callback failed inside the bounded session.",
        )

    def release(self) -> None:
        if self._released:
            return
        _release(self._provider, self._material)
        self._released = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()


class RepositoryAuthBroker:
    def __init__(self, provider: _AuthMaterialProvider) -> None:
        self._provider = provider

    def session(self, request: AuthBrokerRequest) -> AuthBrokerSession:
        provider_failed = False
        try:
            material = self._provider.resolve(request.profile.credential_ref)
        except Exception:
            material = None
            provider_failed = True
        if provider_failed:
            raise _broker_error(
                ErrorCode.CREDENTIAL_BROKER_UNAVAILABLE,
                "Repository-auth material provider is unavailable.",
                retryable=True,
            )
        if material is None:
            raise _broker_error(
                ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND,
                "Repository-auth material reference was not found.",
            )
        if material.state is AuthMaterialState.REVOKED:
            _validate_material(self._provider, material, request)
        if _timestamp(request.now, "now") >= _timestamp(material.expires_at, "expires_at"):
            previous = material
            refresh_failed = False
            try:
                refreshed = self._provider.refresh(request.profile.credential_ref, previous)
            except Exception:
                refreshed = None
                refresh_failed = True
            if refresh_failed:
                _release(self._provider, previous)
                raise _broker_error(
                    ErrorCode.CREDENTIAL_BROKER_UNAVAILABLE,
                    "Repository-auth refresh provider is unavailable.",
                    retryable=True,
                )
            _release(self._provider, previous)
            if refreshed is None:
                raise _broker_error(
                    ErrorCode.CREDENTIAL_EXPIRED,
                    "Repository-auth material expired and refresh was unavailable.",
                )
            equivalent = (
                refreshed.profile_id == previous.profile_id
                and refreshed.actor_class is previous.actor_class
                and refreshed.target_kind is previous.target_kind
                and refreshed.target_id == previous.target_id
                and refreshed.capability_ids == previous.capability_ids
            )
            if not equivalent:
                _release(self._provider, refreshed)
                raise _broker_error(
                    ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH,
                    "Refreshed repository-auth material changed locked identity fields.",
                )
            material = refreshed
        _validate_material(self._provider, material, request)
        if _timestamp(request.now, "now") >= _timestamp(material.expires_at, "expires_at"):
            _release(self._provider, material)
            raise _broker_error(
                ErrorCode.CREDENTIAL_EXPIRED,
                "Repository-auth material is expired.",
            )
        return AuthBrokerSession(self._provider, request, material)
