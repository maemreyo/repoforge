"""Safe migration contracts for adopting already-configured repository identities.

Every value here is safe metadata. Discovery candidates carry logins, hosts, aliases, and
absolute identity-file paths -- never a token, a private key, an authorization header, or a
credential-helper payload. A plan is bound to the exact source digest and configuration
generation it was inspected against, so a stale apply is refused instead of guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from .repository_identity import RecoveryAction

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SCOPE = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
#: Anything token-, key-, or header-shaped is refused before it can reach a durable payload.
_SECRET_SHAPED = re.compile(
    r"gh[pousr]_|github_pat_|xox[baprs]-|sk-[A-Za-z0-9]|-----BEGIN|"
    r"Authorization:|Bearer\s|(?:token|secret|password|passphrase)\s*[=:]",
    re.IGNORECASE,
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _safe_text(value: str, field: str, *, maximum: int = 500) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character < " " for character in value)
    ):
        raise ValueError(f"{field} must be bounded single-line text")
    if _SECRET_SHAPED.search(value) is not None:
        raise ValueError(f"{field} must not contain secret-shaped material")
    return value


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded safe identifier")
    return value


def _absolute_identity_file(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or any(character < " " for character in value)
        or any(marker in value for marker in ("%", "$", "~", "*", "?"))
    ):
        raise ValueError("identity_file must be one concrete absolute path")
    return value


class NamedAccountKind(str, Enum):
    """Where an already-configured GitHub account credential is stored."""

    STORED_ACCOUNT = "stored_account"


@dataclass(frozen=True, slots=True)
class NamedAccountCandidate:
    """One GitHub account the local `gh` installation already knows about.

    ``active`` records whether this is the globally active account. It exists so a plan can
    report the difference to the operator; selection never depends on it.
    """

    host: str
    login: str
    active: bool
    token_scopes: tuple[str, ...] = ()
    actor_id: str | None = None
    kind: NamedAccountKind = NamedAccountKind.STORED_ACCOUNT

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or _HOST.fullmatch(self.host) is None:
            raise ValueError("host must be a bounded lowercase host")
        if not isinstance(self.login, str) or _LOGIN.fullmatch(self.login) is None:
            raise ValueError("login must be a bounded GitHub login")
        if not isinstance(self.active, bool):
            raise ValueError("active must be boolean")
        if not isinstance(self.token_scopes, tuple) or len(self.token_scopes) > 64:
            raise ValueError("token_scopes must be a bounded tuple")
        if any(
            not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None
            for scope in self.token_scopes
        ):
            raise ValueError("token_scopes contains an invalid scope")
        if len(set(self.token_scopes)) != len(self.token_scopes):
            raise ValueError("token_scopes must be unique")
        if self.actor_id is not None:
            _safe_id(self.actor_id, "actor_id")
        if not isinstance(self.kind, NamedAccountKind):
            raise ValueError("kind must be a NamedAccountKind")

    def payload(self) -> dict[str, object]:
        return {
            "host": self.host,
            "login": self.login,
            "active": self.active,
            "token_scopes": list(self.token_scopes),
            "actor_id": self.actor_id,
            "kind": self.kind.value,
        }


@dataclass(frozen=True, slots=True)
class SshAliasCandidate:
    """One concrete resolved SSH alias with exactly one pinned identity file."""

    alias: str
    hostname: str
    identity_file: str
    user: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or _ALIAS.fullmatch(self.alias) is None:
            raise ValueError("alias must be a bounded safe SSH alias")
        if not isinstance(self.hostname, str) or _HOST.fullmatch(self.hostname) is None:
            raise ValueError("hostname must be one concrete lowercase host")
        if self.hostname in _LOCAL_HOSTS:
            raise ValueError("hostname must not be a local transport")
        _absolute_identity_file(self.identity_file)
        if self.user is not None and (
            not isinstance(self.user, str) or _LOGIN.fullmatch(self.user) is None
        ):
            raise ValueError("user must be a bounded login when present")

    def payload(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "hostname": self.hostname,
            "identity_file": self.identity_file,
            "user": self.user,
        }


class AuthMigrationFindingCode(str, Enum):
    LEGACY_NO_AUTH_PROFILE = "legacy_no_auth_profile"
    NAMED_ACCOUNT_CANDIDATE = "named_account_candidate"
    NAMED_ACCOUNT_AMBIGUOUS = "named_account_ambiguous"
    NAMED_ACCOUNT_MISSING = "named_account_missing"
    ACTIVE_ACCOUNT_DIFFERS = "active_account_differs"
    AMBIENT_TOKEN_ENVIRONMENT = "ambient_token_environment"
    CREDENTIAL_HELPER_CONFIGURED = "credential_helper_configured"
    COMMIT_IDENTITY_CONFLICT = "commit_identity_conflict"
    COMMIT_SIGNER_CONFLICT = "commit_signer_conflict"
    SSH_CONFIGURATION_AMBIGUOUS = "ssh_configuration_ambiguous"
    REMOTE_TARGET_MISMATCH = "remote_target_mismatch"


class AuthMigrationSeverity(str, Enum):
    #: Recorded for the operator; does not stop an otherwise complete plan from applying.
    INFO = "info"
    #: Something must be decided or removed by a human before any plan can apply.
    BLOCKING = "blocking"


class AuthMigrationChangeKind(str, Enum):
    CREATE_PROFILE = "create_profile"
    CREATE_BINDING = "create_binding"
    PIN_TRANSPORT = "pin_transport"
    SET_COMMIT_IDENTITY = "set_commit_identity"
    MANUAL_REMEDIATION = "manual_remediation"


@dataclass(frozen=True, slots=True)
class AuthMigrationFinding:
    code: AuthMigrationFindingCode
    severity: AuthMigrationSeverity
    subject: str
    detail: str
    recovery_actions: tuple[RecoveryAction, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, AuthMigrationFindingCode):
            raise ValueError("code must be an AuthMigrationFindingCode")
        if not isinstance(self.severity, AuthMigrationSeverity):
            raise ValueError("severity must be an AuthMigrationSeverity")
        _safe_text(self.subject, "subject", maximum=300)
        _safe_text(self.detail, "detail", maximum=1_000)
        if not isinstance(self.recovery_actions, tuple) or len(self.recovery_actions) > 8:
            raise ValueError("recovery_actions must be a bounded tuple")
        if any(not isinstance(item, RecoveryAction) for item in self.recovery_actions):
            raise ValueError("recovery_actions must contain RecoveryAction values")

    def payload(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "subject": self.subject,
            "detail": self.detail,
            "recovery_actions": [item.payload() for item in self.recovery_actions],
        }


@dataclass(frozen=True, slots=True)
class AuthMigrationChange:
    kind: AuthMigrationChangeKind
    repo_id: str
    profile_id: str
    summary: str
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AuthMigrationChangeKind):
            raise ValueError("kind must be an AuthMigrationChangeKind")
        _safe_id(self.repo_id, "repo_id")
        _safe_id(self.profile_id, "profile_id")
        _safe_text(self.summary, "summary", maximum=500)
        if not isinstance(self.attributes, tuple) or len(self.attributes) > 32:
            raise ValueError("attributes must be a bounded tuple")
        names: list[str] = []
        for name, value in self.attributes:
            names.append(_safe_id(name, "attribute name"))
            _safe_text(value, f"attribute {name}", maximum=4_096)
        if len(set(names)) != len(names):
            raise ValueError("attribute names must be unique")

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "repo_id": self.repo_id,
            "profile_id": self.profile_id,
            "summary": self.summary,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class AuthMigrationPlan:
    """A hash-bound migration proposal that only applies against unchanged inputs."""

    plan_id: str
    plan_hash: str
    source_sha256: str
    config_generation: int
    findings: tuple[AuthMigrationFinding, ...]
    changes: tuple[AuthMigrationChange, ...]
    ready: bool

    def __post_init__(self) -> None:
        _safe_id(self.plan_id, "plan_id")
        for value, field in ((self.plan_hash, "plan_hash"), (self.source_sha256, "source_sha256")):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"{field} must be a lowercase SHA-256")
        if (
            not isinstance(self.config_generation, int)
            or isinstance(self.config_generation, bool)
            or self.config_generation <= 0
        ):
            raise ValueError("config_generation must be a positive integer")
        if not isinstance(self.findings, tuple) or len(self.findings) > 64:
            raise ValueError("findings must be a bounded tuple")
        if any(not isinstance(item, AuthMigrationFinding) for item in self.findings):
            raise ValueError("findings must contain AuthMigrationFinding values")
        if not isinstance(self.changes, tuple) or len(self.changes) > 64:
            raise ValueError("changes must be a bounded tuple")
        if any(not isinstance(item, AuthMigrationChange) for item in self.changes):
            raise ValueError("changes must contain AuthMigrationChange values")
        if not isinstance(self.ready, bool):
            raise ValueError("ready must be boolean")
        if self.ready and not is_applicable(self.findings, self.changes):
            raise ValueError("a ready plan cannot need manual remediation or have no changes")
        if self.plan_hash != canonical_plan_hash(
            source_sha256=self.source_sha256,
            config_generation=self.config_generation,
            findings=self.findings,
            changes=self.changes,
        ):
            raise ValueError("plan_hash does not match the plan contents")

    @property
    def manual_remediation_required(self) -> bool:
        return not is_applicable(self.findings, self.changes)

    def payload(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "source_sha256": self.source_sha256,
            "config_generation": self.config_generation,
            "findings": [item.payload() for item in self.findings],
            "changes": [item.payload() for item in self.changes],
            "ready": self.ready,
        }


def is_applicable(
    findings: tuple[AuthMigrationFinding, ...], changes: tuple[AuthMigrationChange, ...]
) -> bool:
    """Whether these findings and changes could be applied without a human decision."""

    if not changes:
        return False
    if any(change.kind is AuthMigrationChangeKind.MANUAL_REMEDIATION for change in changes):
        return False
    return all(finding.severity is not AuthMigrationSeverity.BLOCKING for finding in findings)


def canonical_plan_hash(
    *,
    source_sha256: str,
    config_generation: int,
    findings: tuple[AuthMigrationFinding, ...],
    changes: tuple[AuthMigrationChange, ...],
) -> str:
    """Hash the exact safe plan contents, so any content edit invalidates a stale apply."""

    canonical = json.dumps(
        {
            "source_sha256": source_sha256,
            "config_generation": config_generation,
            "findings": [item.payload() for item in findings],
            "changes": [item.payload() for item in changes],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_auth_migration_plan(
    *,
    plan_id: str,
    source_sha256: str,
    config_generation: int,
    findings: tuple[AuthMigrationFinding, ...] = (),
    changes: tuple[AuthMigrationChange, ...] = (),
) -> AuthMigrationPlan:
    """Build a plan whose hash and readiness are derived, never supplied by a caller."""

    return AuthMigrationPlan(
        plan_id=plan_id,
        plan_hash=canonical_plan_hash(
            source_sha256=source_sha256,
            config_generation=config_generation,
            findings=findings,
            changes=changes,
        ),
        source_sha256=source_sha256,
        config_generation=config_generation,
        findings=findings,
        changes=changes,
        ready=is_applicable(findings, changes),
    )


def ambient_token_environment_names(names: tuple[str, ...]) -> tuple[str, ...]:
    """Return the reviewed subset of environment names that would authenticate ambiently."""

    ambient = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
    observed = tuple(
        name
        for name in names
        if isinstance(name, str) and _ENVIRONMENT.fullmatch(name) is not None and name in ambient
    )
    return tuple(sorted(set(observed)))
