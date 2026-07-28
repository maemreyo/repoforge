"""Git CLI managed commit identity adapter."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ...domain.commit_identity import (
    CommitConfigEntryEvidence,
    CommitConfigSnapshot,
    CommitIdentityEvidence,
    CommitIdentityPolicy,
    CommitSigningMode,
    ManagedCommitResult,
)
from ...domain.errors import CommandError, ErrorCode, RepoForgeError, WorkspaceError
from ...domain.repository_identity import ActorClass
from ...ports.cancellation import CancellationToken
from ...ports.command import CommandResult

_SENSITIVE_CONFIG = (
    r"^(user\.(name|email|signingkey|useconfigonly)|commit\.gpgsign|"
    r"gpg\..*|core\.sshcommand|credential\..*|remote\..*\.(url|pushurl))$"
)
_IDENT = re.compile(r"^(?P<name>.+) <(?P<email>[^<>]+)> [0-9]+ [+-][0-9]{4}$")
_SAFE_ENVIRONMENT_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "GNUPGHOME")
_SIGNED_STATUSES = frozenset({"G", "U"})


class _Executor(Protocol):
    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult: ...

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


def _error(code: ErrorCode, message: str) -> RepoForgeError:
    return RepoForgeError(
        message,
        code=code,
        retryable=False,
        unchanged_state=(
            "No alternate author, signer, helper, or transport identity was attempted.",
        ),
    )


def _parse_ident(value: str) -> tuple[str, str]:
    match = _IDENT.fullmatch(value.strip())
    if match is None:
        raise _error(
            ErrorCode.COMMIT_IDENTITY_UNRESOLVED,
            "Git could not resolve a bounded author or committer identity for compatibility import.",
        )
    return match.group("name"), match.group("email")


def _config_records(output: str, scope: str) -> list[CommitConfigEntryEvidence]:
    values_by_key: dict[str, list[str]] = {}
    for raw in output.split("\x00"):
        if not raw:
            continue
        if "\n" not in raw:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_CONFIG_DRIFT,
                "Git returned malformed identity-sensitive configuration evidence.",
            )
        key, value = raw.split("\n", 1)
        normalized_key = key.strip().lower()
        if not normalized_key:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_CONFIG_DRIFT,
                "Git returned an empty identity-sensitive configuration key.",
            )
        values_by_key.setdefault(normalized_key, []).append(value)
    evidence: list[CommitConfigEntryEvidence] = []
    for normalized_key, values in values_by_key.items():
        encoded_values = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence.append(
            CommitConfigEntryEvidence(
                scope=scope,
                key=normalized_key,
                value_digest=hashlib.sha256(encoded_values).hexdigest(),
            )
        )
    return evidence


def _fingerprint(value: str) -> str:
    return value.strip().casefold()


class GitCommitIdentityGateway:
    """Pin commit attribution/signing without writing shared or worktree Git config."""

    def __init__(self, executor: _Executor) -> None:
        self._executor = executor

    def resolve_policy(
        self,
        path: Path,
        configured: CommitIdentityPolicy | None,
    ) -> CommitIdentityPolicy:
        if configured is not None:
            return configured
        try:
            author = self._executor.run(
                ["git", "var", "GIT_AUTHOR_IDENT"],
                cwd=path,
                output_limit=1_000,
            ).stdout
            committer = self._executor.run(
                ["git", "var", "GIT_COMMITTER_IDENT"],
                cwd=path,
                output_limit=1_000,
            ).stdout
        except CommandError:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_UNRESOLVED,
                "Repository commit identity is not configured and compatibility import failed.",
            ) from None
        author_name, author_email = _parse_ident(author)
        committer_name, committer_email = _parse_ident(committer)
        digest = hashlib.sha256(
            f"{author_name}\0{author_email}\0{committer_name}\0{committer_email}".encode()
        ).hexdigest()
        return CommitIdentityPolicy(
            profile_id=f"legacy-{digest[:24]}",
            actor_class=ActorClass.HUMAN_OPERATED,
            author_name=author_name,
            author_email=author_email,
            committer_name=committer_name,
            committer_email=committer_email,
            signing_mode=CommitSigningMode.UNSIGNED_ATTESTED,
        )

    def _scope_entries(self, path: Path, scope: str) -> list[CommitConfigEntryEvidence]:
        result = self._executor.run(
            ["git", "config", f"--{scope}", "--null", "--get-regexp", _SENSITIVE_CONFIG],
            cwd=path,
            check=False,
            output_limit=1_000_000,
        )
        if result.returncode == 1:
            return []
        if result.returncode != 0:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_CONFIG_DRIFT,
                f"Cannot inspect {scope} identity-sensitive Git configuration.",
            )
        return _config_records(result.stdout, scope)

    def config_snapshot(self, path: Path) -> CommitConfigSnapshot:
        worktree_flag = self._executor.run(
            ["git", "config", "--local", "--get", "--bool", "extensions.worktreeConfig"],
            cwd=path,
            check=False,
            output_limit=32,
        )
        if worktree_flag.returncode not in {0, 1}:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_CONFIG_DRIFT,
                "Cannot determine whether Git worktree-specific configuration is enabled.",
            )
        enabled = worktree_flag.returncode == 0 and worktree_flag.stdout.strip().lower() == "true"
        entries = self._scope_entries(path, "local")
        if enabled:
            entries.extend(self._scope_entries(path, "worktree"))
        ordered = tuple(sorted(entries, key=lambda item: (item.scope, item.key)))
        payload = {
            "worktree_config_enabled": enabled,
            "entries": [
                {"scope": item.scope, "key": item.key, "value_digest": item.value_digest}
                for item in ordered
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return CommitConfigSnapshot(digest, enabled, ordered)

    def _environment(self, policy: CommitIdentityPolicy) -> dict[str, str]:
        inherited = self._executor.environment()
        environment = {
            key: inherited[key]
            for key in _SAFE_ENVIRONMENT_KEYS
            if key in inherited and isinstance(inherited[key], str)
        }
        environment.update(
            {
                "GIT_AUTHOR_NAME": policy.author_name,
                "GIT_AUTHOR_EMAIL": policy.author_email,
                "GIT_COMMITTER_NAME": policy.committer_name,
                "GIT_COMMITTER_EMAIL": policy.committer_email,
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
            }
        )
        pairs: list[tuple[str, str]] = [
            ("user.useConfigOnly", "true"),
            ("user.name", policy.committer_name),
            ("user.email", policy.committer_email),
            (
                "commit.gpgSign",
                "false" if policy.signing_mode is CommitSigningMode.UNSIGNED_ATTESTED else "true",
            ),
            ("credential.helper", ""),
            ("core.sshCommand", "false"),
        ]
        if policy.signing_mode is not CommitSigningMode.UNSIGNED_ATTESTED:
            key_reference = policy.signing_key_reference
            if key_reference is None:
                raise _error(ErrorCode.COMMIT_SIGNING_FAILED, "Signing key reference is missing.")
            pairs.extend(
                [
                    (
                        "gpg.format",
                        "ssh" if policy.signing_mode is CommitSigningMode.SSH else "openpgp",
                    ),
                    ("user.signingKey", key_reference),
                ]
            )
        environment["GIT_CONFIG_COUNT"] = str(len(pairs))
        for index, (key, value) in enumerate(pairs):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        return environment

    def _run(
        self,
        argv: list[str],
        *,
        path: Path,
        environment: Mapping[str, str],
        check: bool = True,
        output_limit: int = 1_000_000,
    ) -> CommandResult:
        return self._executor.run_isolated(
            argv,
            cwd=path,
            environment=environment,
            secrets=(),
            check=check,
            output_limit=output_limit,
        )

    def _current_snapshot(
        self,
        path: Path,
        expected_config_digest: str,
    ) -> CommitConfigSnapshot:
        current = self.config_snapshot(path)
        if current.digest != expected_config_digest:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_CONFIG_DRIFT,
                "Shared or worktree Git identity/transport configuration changed after workspace creation.",
            )
        return current

    def _result(
        self,
        path: Path,
        policy: CommitIdentityPolicy,
        current: CommitConfigSnapshot,
        environment: Mapping[str, str],
    ) -> ManagedCommitResult:
        head = self._run(
            ["git", "rev-parse", "HEAD"],
            path=path,
            environment=environment,
            output_limit=256,
        ).stdout.strip()
        summary = self._run(
            ["git", "show", "-1", "--stat", "--oneline", "--decorate"],
            path=path,
            environment=environment,
        ).stdout
        observed = self._run(
            [
                "git",
                "show",
                "-s",
                "--format=%an%x00%ae%x00%cn%x00%ce%x00%G?%x00%GF",
                "HEAD",
            ],
            path=path,
            environment=environment,
            output_limit=4_000,
        ).stdout
        fields = [item.strip() for item in observed.rstrip("\n").split("\x00")]
        if len(fields) != 6:
            raise _error(
                ErrorCode.COMMIT_IDENTITY_MISMATCH,
                "Git returned malformed author, committer, or signer evidence.",
            )
        author_name, author_email, committer_name, committer_email, signature, fingerprint = fields
        if (
            author_name != policy.author_name
            or author_email != policy.author_email
            or committer_name != policy.committer_name
            or committer_email != policy.committer_email
        ):
            raise _error(
                ErrorCode.COMMIT_IDENTITY_MISMATCH,
                "Observed commit author or committer does not match the reviewed policy.",
            )
        signer_fingerprint: str | None = None
        attestation_digest: str | None = None
        if policy.signing_mode is CommitSigningMode.UNSIGNED_ATTESTED:
            if signature != "N" or fingerprint:
                raise _error(
                    ErrorCode.COMMIT_SIGNING_FAILED,
                    "Unsigned-attested policy produced unexpected direct signing evidence.",
                )
            attestation_payload = {
                "head_sha": head,
                "policy": policy.safe_payload(),
                "config_snapshot_digest": current.digest,
            }
            attestation_digest = hashlib.sha256(
                json.dumps(
                    attestation_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        else:
            if signature not in _SIGNED_STATUSES:
                raise _error(
                    ErrorCode.COMMIT_SIGNING_FAILED,
                    "Managed commit did not produce a valid direct signature.",
                )
            expected_fingerprint = policy.signer_fingerprint
            if expected_fingerprint is None or _fingerprint(fingerprint) != _fingerprint(
                expected_fingerprint
            ):
                raise _error(
                    ErrorCode.COMMIT_SIGNING_FAILED,
                    "Observed commit signer fingerprint does not match the reviewed policy.",
                )
            signer_fingerprint = fingerprint
        evidence = CommitIdentityEvidence(
            profile_id=policy.profile_id,
            actor_class=policy.actor_class,
            author_name=author_name,
            author_email=author_email,
            committer_name=committer_name,
            committer_email=committer_email,
            signing_mode=policy.signing_mode,
            signer_fingerprint=signer_fingerprint,
            attestation_digest=attestation_digest,
            config_snapshot_digest=current.digest,
            represented_actor_id=policy.represented_actor_id,
            delegation_approval_id=policy.delegation_approval_id,
        )
        return ManagedCommitResult(head, summary, evidence)

    def commit(
        self,
        path: Path,
        message: str,
        policy: CommitIdentityPolicy,
        *,
        expected_config_digest: str,
    ) -> ManagedCommitResult:
        current = self._current_snapshot(path, expected_config_digest)
        environment = self._environment(policy)
        try:
            self._run(["git", "add", "--all", "--"], path=path, environment=environment)
            staged = self._run(
                ["git", "diff", "--cached", "--name-only", "--"],
                path=path,
                environment=environment,
            ).stdout.strip()
            if not staged:
                raise WorkspaceError("No staged changes remain after git add")
            self._run(["git", "commit", "-m", message], path=path, environment=environment)
        except CommandError as exc:
            exc.details.setdefault("commit_stage", "managed_git_commit")
            raise
        return self._result(path, policy, current, environment)

    def commit_merge(
        self,
        path: Path,
        policy: CommitIdentityPolicy,
        *,
        expected_config_digest: str,
    ) -> ManagedCommitResult:
        current = self._current_snapshot(path, expected_config_digest)
        environment = self._environment(policy)
        merge_head = self._run(
            ["git", "rev-parse", "--verify", "MERGE_HEAD"],
            path=path,
            environment=environment,
            check=False,
            output_limit=256,
        )
        if merge_head.returncode != 0 or not merge_head.stdout.strip():
            raise WorkspaceError("No reviewed merge is in progress")
        try:
            self._run(["git", "commit", "--no-edit"], path=path, environment=environment)
        except CommandError as exc:
            exc.details.setdefault("commit_stage", "managed_merge_commit")
            raise
        return self._result(path, policy, current, environment)
