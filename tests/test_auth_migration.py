"""Adopting an existing local setup as reviewed auth profiles, without adopting its ambiguity.

`inspect()` reports what it found and binds the proposal to the exact source digest and
configuration generation it saw. `apply()` re-proves every input before writing a generation and
refuses any plan that still needs a human decision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomli as tomllib

from repoforge.adapters.configuration import ConfigGenerationStore
from repoforge.adapters.locking import FcntlLockManager
from repoforge.application.auth_migration import AuthMigrationService
from repoforge.application.configuration.document import parse_resolved, render_resolved
from repoforge.application.configuration.source import (
    SourceConfiguration,
    SourceRepository,
    parse_source,
    render_source,
)
from repoforge.domain.auth_migration import (
    AuthMigrationChangeKind,
    AuthMigrationFindingCode,
    AuthMigrationSeverity,
    NamedAccountCandidate,
    SshAliasCandidate,
)
from repoforge.domain.config_generation import sha256_text
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.repository_identity import RepositoryProvider
from repoforge.domain.repository_identity_resolution import RepositoryIdentityObservation
from repoforge.testing import FixedClock, SequenceIdGenerator

NOW = "2026-07-30T00:00:00+00:00"
_SHA = "a" * 64


def _observation(
    *, repository_id: str = "987654", canonical_name: str = "github.com/acme/demo"
) -> RepositoryIdentityObservation:
    return RepositoryIdentityObservation(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.com",
        repository_id=repository_id,
        canonical_name=canonical_name,
        exists=True,
        observed_at=NOW,
        config_revision=_SHA,
    )


class Accounts:
    """Named-account discovery double that records exactly what was asked for."""

    def __init__(self, candidates: tuple[NamedAccountCandidate, ...]) -> None:
        self.available = candidates
        self.calls: list[str] = []
        self.failure: RepoForgeError | None = None

    def candidates(self, *, host: str) -> tuple[NamedAccountCandidate, ...]:
        self.calls.append(f"candidates:{host}")
        if self.failure is not None:
            raise self.failure
        return tuple(item for item in self.available if item.host == host)

    def verify(self, *, host: str, login: str) -> NamedAccountCandidate:
        self.calls.append(f"verify:{host}:{login}")
        if self.failure is not None:
            raise self.failure
        matches = tuple(
            item for item in self.available if item.host == host and item.login == login
        )
        if len(matches) != 1:
            raise RepoForgeError("no such account", code=ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND)
        return matches[0]


class Ssh:
    def __init__(self, candidate: SshAliasCandidate | None = None) -> None:
        self.candidate = candidate
        self.calls: list[str] = []

    def inspect(self, alias: str) -> SshAliasCandidate:
        self.calls.append(alias)
        if self.candidate is None:
            raise RepoForgeError("ambiguous", code=ErrorCode.GIT_TRANSPORT_IDENTITY_MISMATCH)
        return self.candidate


class Ambient:
    """Read-only ambient state view. Recording proves nothing is ever mutated."""

    def __init__(
        self,
        *,
        environment: tuple[str, ...] = (),
        git_config: dict[str, tuple[tuple[str, str], ...]] | None = None,
    ) -> None:
        self._environment = environment
        self._git_config = git_config or {}
        self.reads: list[str] = []

    def environment_names(self) -> tuple[str, ...]:
        self.reads.append("environment")
        return self._environment

    def git_config_values(self, cwd: Path, key: str) -> tuple[tuple[str, str], ...]:
        del cwd
        self.reads.append(f"git_config:{key}")
        return self._git_config.get(key, ())


def _store(tmp_path: Path, *, source: SourceConfiguration) -> ConfigGenerationStore:
    repo_root = tmp_path / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    source_path = tmp_path / "config.toml"
    source_path.write_text(render_source(source), encoding="utf-8")
    store = ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )
    document = parse_resolved(None)
    document["repositories"]["demo"] = {"path": str(repo_root)}
    resolved = render_resolved(
        document,
        generation=1,
        source_path=str(source_path),
        source_sha256=sha256_text(store.read_source_text()),
        created_at=NOW,
        reason="test bootstrap",
        proposal_id=None,
        repository_fingerprints=(("demo", "c" * 64),),
    )
    store.import_legacy(store.read_source_text(), resolved, created_at=NOW)
    return store


def _service(
    tmp_path: Path,
    *,
    accounts: Accounts | None = None,
    ssh: Ssh | None = None,
    ambient: Ambient | None = None,
    observation: RepositoryIdentityObservation | None = None,
    source: SourceConfiguration | None = None,
) -> AuthMigrationService:
    repo_root = tmp_path / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    store = _store(
        tmp_path,
        source=source
        or SourceConfiguration("tunnel", "repoforge", (SourceRepository("demo", str(repo_root)),)),
    )
    resolved_observation = observation or _observation()
    return AuthMigrationService(
        store=store,
        clock=FixedClock(NOW),
        ids=SequenceIdGenerator(),
        accounts=accounts or Accounts(()),
        ssh=ssh or Ssh(),
        ambient=ambient or Ambient(),
        observe=lambda repo_id: resolved_observation,
    )


def _personal(active: bool = False, login: str = "acme-operator") -> NamedAccountCandidate:
    return NamedAccountCandidate(
        host="github.com",
        login=login,
        active=active,
        token_scopes=("read:org", "repo"),
        actor_id="4242",
    )


# ---------------------------------------------------------------------------
# inspect(): what was found, and what still needs a human
# ---------------------------------------------------------------------------


def test_legacy_config_with_no_profiles_is_reported_as_migration_required(tmp_path: Path) -> None:
    service = _service(tmp_path)

    plan = service.inspect(repo_id="demo")

    codes = [finding.code for finding in plan.findings]
    assert AuthMigrationFindingCode.LEGACY_NO_AUTH_PROFILE in codes
    # Nothing was found to adopt, so there is nothing to apply.
    assert plan.changes == ()
    assert plan.ready is False
    assert plan.config_generation == 1


def test_one_exact_named_account_produces_a_ready_no_prompt_plan(tmp_path: Path) -> None:
    accounts = Accounts((_personal(),))
    ssh = Ssh(
        SshAliasCandidate(
            alias="github-work",
            hostname="github.com",
            identity_file="/home/demo/.ssh/id_ed25519_work",
            user="git",
        )
    )
    service = _service(tmp_path, accounts=accounts, ssh=ssh)

    plan = service.inspect(repo_id="demo")

    assert plan.ready is True
    assert plan.manual_remediation_required is False
    assert [change.kind for change in plan.changes] == [
        AuthMigrationChangeKind.CREATE_PROFILE,
        AuthMigrationChangeKind.PIN_TRANSPORT,
        AuthMigrationChangeKind.CREATE_BINDING,
    ]
    assert {change.profile_id for change in plan.changes} == {"acme-operator"}
    # The named account was proved live, not just listed.
    assert "verify:github.com:acme-operator" in accounts.calls


def test_multiple_named_accounts_require_manual_remediation(tmp_path: Path) -> None:
    accounts = Accounts((_personal(), _personal(login="acme-bot", active=True)))
    service = _service(tmp_path, accounts=accounts)

    plan = service.inspect(repo_id="demo")

    ambiguous = [
        finding
        for finding in plan.findings
        if finding.code is AuthMigrationFindingCode.NAMED_ACCOUNT_AMBIGUOUS
    ]
    assert len(ambiguous) == 1
    assert ambiguous[0].severity is AuthMigrationSeverity.BLOCKING
    assert AuthMigrationChangeKind.MANUAL_REMEDIATION in {change.kind for change in plan.changes}
    assert plan.ready is False
    assert plan.manual_remediation_required is True
    # An ambiguous set is never resolved by picking the globally active account.
    assert not any(call.startswith("verify:") for call in accounts.calls)


def test_a_different_globally_active_account_is_reported_but_never_selected(
    tmp_path: Path,
) -> None:
    # Exactly one account matches this repository's owner; it is simply not the active one.
    accounts = Accounts((_personal(active=False),))
    service = _service(tmp_path, accounts=accounts, ssh=Ssh())
    service_active = _service(tmp_path / "other", accounts=Accounts((_personal(active=True),)))

    inactive_plan = service.inspect(repo_id="demo")
    active_plan = service_active.inspect(repo_id="demo")

    assert AuthMigrationFindingCode.ACTIVE_ACCOUNT_DIFFERS in {
        finding.code for finding in inactive_plan.findings
    }
    assert AuthMigrationFindingCode.ACTIVE_ACCOUNT_DIFFERS not in {
        finding.code for finding in active_plan.findings
    }
    # Reporting the difference does not change which profile is proposed.
    assert {change.profile_id for change in inactive_plan.changes} == {"acme-operator"}


def test_ambient_tokens_and_credential_helpers_block_a_plan(tmp_path: Path) -> None:
    ambient = Ambient(
        environment=("GH_TOKEN", "PATH", "GITHUB_TOKEN"),
        git_config={
            "credential.helper": (
                ("file:/home/demo/.gitconfig", "osxkeychain"),
                ("file:/repos/demo/.git/config", "store"),
            )
        },
    )
    service = _service(tmp_path, accounts=Accounts((_personal(),)), ambient=ambient)

    plan = service.inspect(repo_id="demo")

    blocking = {
        finding.code: finding
        for finding in plan.findings
        if finding.severity is AuthMigrationSeverity.BLOCKING
    }
    assert AuthMigrationFindingCode.AMBIENT_TOKEN_ENVIRONMENT in blocking
    assert AuthMigrationFindingCode.CREDENTIAL_HELPER_CONFIGURED in blocking
    assert plan.ready is False
    # The ambient names are reported; their values never are.
    detail = blocking[AuthMigrationFindingCode.AMBIENT_TOKEN_ENVIRONMENT].detail
    assert "GH_TOKEN" in detail and "GITHUB_TOKEN" in detail
    assert "PATH" not in detail
    # Both scopes of the helper are named so the operator knows where to look.
    helper_detail = blocking[AuthMigrationFindingCode.CREDENTIAL_HELPER_CONFIGURED].detail
    assert "/home/demo/.gitconfig" in helper_detail
    assert "/repos/demo/.git/config" in helper_detail


def test_author_and_signer_conflicts_block_a_plan(tmp_path: Path) -> None:
    ambient = Ambient(
        git_config={
            "user.email": (
                ("file:/home/demo/.gitconfig", "personal@example.com"),
                ("file:/repos/demo/.git/config", "work@example.com"),
            ),
            "user.signingkey": (("file:/home/demo/.gitconfig", "ABC123"),),
            "commit.gpgsign": (("file:/home/demo/.gitconfig", "true"),),
        }
    )
    service = _service(tmp_path, accounts=Accounts((_personal(),)), ambient=ambient)

    plan = service.inspect(repo_id="demo")

    codes = {
        finding.code
        for finding in plan.findings
        if finding.severity is AuthMigrationSeverity.BLOCKING
    }
    assert AuthMigrationFindingCode.COMMIT_IDENTITY_CONFLICT in codes
    assert AuthMigrationFindingCode.COMMIT_SIGNER_CONFLICT in codes
    assert plan.ready is False


def test_ambiguous_ssh_configuration_is_reported_without_pinning_a_transport(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, accounts=Accounts((_personal(),)), ssh=Ssh(candidate=None))

    plan = service.inspect(repo_id="demo")

    assert AuthMigrationFindingCode.SSH_CONFIGURATION_AMBIGUOUS in {
        finding.code for finding in plan.findings
    }
    # Falling back to HTTPS is a reviewed decision, not a silent SSH guess.
    pinned = [
        change for change in plan.changes if change.kind is AuthMigrationChangeKind.PIN_TRANSPORT
    ]
    assert len(pinned) == 1
    assert dict(pinned[0].attributes)["transport_kind"] == "https"


def test_a_remote_disagreeing_with_the_observed_target_blocks_a_plan(tmp_path: Path) -> None:
    # The checkout's own remote points somewhere other than the repository we observed, so
    # which target a pinned transport would actually reach is not decidable here.
    ambient = Ambient(
        git_config={
            "remote.origin.url": (
                ("file:/repos/demo/.git/config", "https://github.com/other-owner/demo.git"),
            )
        }
    )
    service = _service(tmp_path, accounts=Accounts((_personal(),)), ambient=ambient)

    plan = service.inspect(repo_id="demo")

    mismatch = [
        finding
        for finding in plan.findings
        if finding.code is AuthMigrationFindingCode.REMOTE_TARGET_MISMATCH
    ]
    assert len(mismatch) == 1
    assert mismatch[0].severity is AuthMigrationSeverity.BLOCKING
    assert plan.ready is False


def test_a_remote_matching_the_observed_target_is_not_a_finding(tmp_path: Path) -> None:
    for url in (
        "https://github.com/acme/demo.git",
        "git@github.com:acme/demo.git",
        "ssh://git@github.com/acme/demo",
    ):
        ambient = Ambient(
            git_config={"remote.origin.url": (("file:/repos/demo/.git/config", url),)}
        )
        service = _service(tmp_path / url[-12:], accounts=Accounts((_personal(),)), ambient=ambient)

        plan = service.inspect(repo_id="demo")

        assert AuthMigrationFindingCode.REMOTE_TARGET_MISMATCH not in {
            finding.code for finding in plan.findings
        }, url
        assert plan.ready is True


def test_inspect_is_deterministic_and_bound_to_its_exact_inputs(tmp_path: Path) -> None:
    service = _service(tmp_path, accounts=Accounts((_personal(),)))

    first = service.inspect(repo_id="demo")
    second = service.inspect(repo_id="demo")

    assert first.plan_hash == second.plan_hash
    assert first.source_sha256 == sha256_text(service._store.read_source_text())
    assert first.config_generation == 1
    # Plan identity is stable content addressing, so a repeat inspect is idempotent.
    assert first.payload() == second.payload()
    rendered = json.dumps(first.payload(), sort_keys=True)
    assert "gho_" not in rendered and "ghp_" not in rendered


def test_inspect_never_reads_a_secret_or_mutates_ambient_state(tmp_path: Path) -> None:
    ambient = Ambient()
    service = _service(tmp_path, accounts=Accounts((_personal(),)), ambient=ambient)

    service.inspect(repo_id="demo")

    # Every ambient read is a named read-only key; nothing resembles a write.
    assert ambient.reads
    assert all(read == "environment" or read.startswith("git_config:") for read in ambient.reads)


# ---------------------------------------------------------------------------
# apply(): re-prove everything, or refuse
# ---------------------------------------------------------------------------


def test_apply_writes_one_reviewed_generation_from_a_ready_plan(tmp_path: Path) -> None:
    accounts = Accounts((_personal(),))
    service = _service(tmp_path, accounts=accounts)
    plan = service.inspect(repo_id="demo")

    result = service.apply(
        repo_id="demo", plan_id=plan.plan_id, plan_hash=plan.plan_hash, actor="operator"
    )

    assert result["status"] == "applied"
    assert result["generation"] == 2
    persisted = parse_source(service._store.read_source_text())
    assert [profile.profile_id for profile in persisted.auth_profiles] == ["acme-operator"]
    profile = persisted.auth_profiles[0]
    assert profile.github_login == "acme-operator"
    assert profile.repository_id == "987654"
    assert profile.expected_actor_id == "4242"
    resolved = tomllib.loads(service._store.read_resolved_text(2))
    assert resolved["auth_profiles"]["acme-operator"]["github_login"] == "acme-operator"
    # The reviewed profile carries a reference, never a credential.
    rendered = service._store.read_source_text() + service._store.read_resolved_text(2)
    assert "gho_" not in rendered and "ghp_" not in rendered
    # The named account was re-proved at apply time, not trusted from the plan.
    assert accounts.calls.count("verify:github.com:acme-operator") == 2


def test_apply_refuses_a_plan_that_still_needs_a_human(tmp_path: Path) -> None:
    accounts = Accounts((_personal(), _personal(login="acme-bot")))
    service = _service(tmp_path, accounts=accounts)
    plan = service.inspect(repo_id="demo")

    with pytest.raises(RepoForgeError) as failure:
        service.apply(
            repo_id="demo", plan_id=plan.plan_id, plan_hash=plan.plan_hash, actor="operator"
        )

    assert failure.value.code is ErrorCode.INPUT_REQUIRED
    assert service._store.current() is not None
    assert service._store.current().generation == 1


def test_apply_refuses_a_stale_plan_hash_or_changed_source(tmp_path: Path) -> None:
    service = _service(tmp_path, accounts=Accounts((_personal(),)))
    plan = service.inspect(repo_id="demo")

    with pytest.raises(RepoForgeError) as wrong_hash:
        service.apply(repo_id="demo", plan_id=plan.plan_id, plan_hash="b" * 64, actor="operator")
    assert wrong_hash.value.code is ErrorCode.CONFIG_STALE

    # A source edit between inspect and apply invalidates the plan.
    source_path = service._store.source_path
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + '\n[[repo]]\nid = "second"\npath = "/tmp/x"\n',
        encoding="utf-8",
    )
    with pytest.raises(RepoForgeError) as stale:
        service.apply(
            repo_id="demo", plan_id=plan.plan_id, plan_hash=plan.plan_hash, actor="operator"
        )
    assert stale.value.code is ErrorCode.CONFIG_STALE
    assert service._store.current().generation == 1


def test_apply_refuses_when_live_evidence_changed_after_inspect(tmp_path: Path) -> None:
    accounts = Accounts((_personal(),))
    service = _service(tmp_path, accounts=accounts)
    plan = service.inspect(repo_id="demo")

    # The account disappeared from the local gh installation between inspect and apply.
    accounts.available = ()

    with pytest.raises(RepoForgeError) as failure:
        service.apply(
            repo_id="demo", plan_id=plan.plan_id, plan_hash=plan.plan_hash, actor="operator"
        )

    assert failure.value.code is ErrorCode.CREDENTIAL_REFERENCE_NOT_FOUND
    assert service._store.current().generation == 1


def test_apply_is_idempotent_for_an_already_migrated_repository(tmp_path: Path) -> None:
    service = _service(tmp_path, accounts=Accounts((_personal(),)))
    plan = service.inspect(repo_id="demo")
    service.apply(repo_id="demo", plan_id=plan.plan_id, plan_hash=plan.plan_hash, actor="operator")

    # The profile now exists, so a fresh inspect has nothing left to create.
    repeat = service.inspect(repo_id="demo")

    assert repeat.ready is False
    assert repeat.changes == ()
    assert AuthMigrationFindingCode.LEGACY_NO_AUTH_PROFILE not in {
        finding.code for finding in repeat.findings
    }
    assert service._store.current().generation == 2
