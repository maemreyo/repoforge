"""`rf auth`: parser defaults, dispatch, exit codes, and command isolation.

Two things this has to pin. Reads must work with no flags when exactly one profile is eligible,
so the deterministic default is not something a caller has to know about. And no handler may
reach for global state: nothing may run `gh auth switch`, mutate Git configuration, open SSH
configuration for writing, or let a token reach stdout or stderr.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.json_repository_binding_store import (
    JsonRepositoryBindingStore,
)
from repoforge.application.auth_ux import AuthUxService
from repoforge.config import AppConfig, AuthProfileConfig, RepositoryConfig, ServerConfig
from repoforge.domain.auth_migration import NamedAccountCandidate, SshAliasCandidate
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.git_transport_identity import (
    GitTransportAccess,
    GitTransportKind,
    GitTransportSpec,
)
from repoforge.domain.github_api_identity import StoredGhAccountSpec
from repoforge.domain.github_capability_preflight import GitHubOperationCapability
from repoforge.domain.repository_identity import (
    ActorClass,
    CredentialKind,
    CredentialProfile,
    OpaqueCredentialReference,
    RepositoryProvider,
)
from repoforge.domain.repository_identity_resolution import (
    CredentialProfileEligibility,
    RepositoryIdentityObservation,
)
from repoforge.interfaces.cli.auth import add_auth_parsers, run_auth_command
from repoforge.testing import FixedClock

cli = importlib.import_module("repoforge.interfaces.cli.main")

NOW = "2026-07-30T00:00:00+00:00"
_SHA = "a" * 64
_TOKEN = "gho_cli_token_canary_31337"
_CAPABILITIES = (
    GitHubOperationCapability.CONTENTS_READ.value,
    GitHubOperationCapability.CONTENTS_WRITE.value,
)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_auth_parsers(commands)
    return parser.parse_args(argv)


def _profile_config(
    profile_id: str = "personal", *, actor_class: ActorClass = ActorClass.HUMAN_OPERATED
) -> AuthProfileConfig:
    profile = CredentialProfile(
        profile_id=profile_id,
        provider=RepositoryProvider.GITHUB,
        credential_kind=CredentialKind.STORED_ACCOUNT,
        credential_ref=OpaqueCredentialReference(
            scheme="repoforge", reference_id=f"gh-account-{profile_id}"
        ),
        actor_class=actor_class,
        expected_actor_id="4242",
        capability_ids=_CAPABILITIES,
        revision=_SHA,
    )
    return AuthProfileConfig(
        profile=profile,
        eligibility=CredentialProfileEligibility(
            profile=profile,
            enabled=True,
            repository_patterns=("github.com/acme/*",),
            boundary_id="acme",
        ),
        api_identity=StoredGhAccountSpec(
            reference_id=f"gh-account-{profile_id}",
            profile_id=profile_id,
            host="github.com",
            login="acme-operator",
            actor_id="4242",
            actor_class=ActorClass.HUMAN_OPERATED,
            repository_id="987654",
            capability_ids=_CAPABILITIES,
        ),
        transport=GitTransportSpec(
            profile_id=profile_id,
            repository_id="987654",
            target_id="987654",
            provider_host="github.com",
            kind=GitTransportKind.HTTPS,
            credential_fingerprint=_SHA,
            allowed_access=(GitTransportAccess.READ, GitTransportAccess.WRITE),
            https_token_environment="REPOFORGE_GH_TOKEN",
        ),
    )


class Accounts:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _candidate(self, login: str = "acme-operator") -> NamedAccountCandidate:
        return NamedAccountCandidate(host="github.com", login=login, active=False, actor_id="4242")

    def candidates(self, *, host: str) -> tuple[NamedAccountCandidate, ...]:
        self.calls.append(f"candidates:{host}")
        return (self._candidate(),)

    def verify(self, *, host: str, login: str) -> NamedAccountCandidate:
        self.calls.append(f"verify:{host}:{login}")
        return self._candidate(login)


class Ssh:
    def inspect(self, alias: str) -> SshAliasCandidate:
        return SshAliasCandidate(
            alias=alias,
            hostname="github.com",
            identity_file="/home/demo/.ssh/id_ed25519",
            user="git",
        )


class Migration:
    def __init__(self) -> None:
        self.applied: list[dict[str, str]] = []

    def inspect(self, *, repo_id: str) -> Any:
        from repoforge.domain.auth_migration import build_auth_migration_plan

        return build_auth_migration_plan(
            plan_id="authmig-000000000000000000000000",
            source_sha256=_SHA,
            config_generation=1,
        )

    def apply(self, *, repo_id: str, plan_id: str, plan_hash: str, actor: str) -> dict[str, object]:
        self.applied.append(
            {"repo_id": repo_id, "plan_id": plan_id, "plan_hash": plan_hash, "actor": actor}
        )
        return {"status": "applied", "generation": 2}


def _service(
    tmp_path: Path, *, profiles: dict[str, AuthProfileConfig] | None = None
) -> AuthUxService:
    repo_root = tmp_path / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    config = AppConfig(
        source_path=tmp_path / "config.toml",
        server=ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        repositories={"demo": RepositoryConfig(repo_id="demo", path=repo_root)},
        auth_profiles=profiles if profiles is not None else {"personal": _profile_config()},
    )
    return AuthUxService(
        config=config,
        bindings=JsonRepositoryBindingStore(
            tmp_path / "state", FcntlLockManager(tmp_path / "locks")
        ),
        observe=lambda repo_id: RepositoryIdentityObservation(
            provider=RepositoryProvider.GITHUB,
            provider_host="github.com",
            repository_id="987654",
            canonical_name="github.com/acme/demo",
            exists=True,
            observed_at=NOW,
            config_revision=_SHA,
        ),
        clock=FixedClock(NOW),
    )


def _run(
    tmp_path: Path,
    argv: list[str],
    *,
    service: AuthUxService | None = None,
    accounts: Accounts | None = None,
    migration: Migration | None = None,
) -> tuple[int, object]:
    captured: list[object] = []
    code = run_auth_command(
        _parse(argv),
        service=service or _service(tmp_path),
        migration=migration or Migration(),  # type: ignore[arg-type]
        accounts=accounts or Accounts(),
        ssh=Ssh(),
        render=captured.append,
    )
    return code, captured[0] if captured else None


# ---------------------------------------------------------------------------
# Parser surface and defaults
# ---------------------------------------------------------------------------


def test_every_documented_auth_command_parses() -> None:
    invocations = [
        ["auth", "profile", "list"],
        ["auth", "profile", "inspect", "personal"],
        ["auth", "bind", "demo"],
        ["auth", "unbind", "demo", "--expected-revision", "1"],
        ["auth", "resolve", "demo"],
        ["auth", "whoami", "demo", "--check", "all"],
        ["auth", "doctor", "demo"],
        ["auth", "lease", "inspect", "op-1"],
        ["auth", "lease", "revoke", "op-1", "--expected-revision", "1"],
        ["auth", "import", "gh"],
        ["auth", "import", "ssh", "github-work"],
        ["auth", "migrate", "inspect", "demo"],
        [
            "auth",
            "migrate",
            "apply",
            "demo",
            "--plan-id",
            "authmig-1",
            "--plan-hash",
            "b" * 64,
        ],
    ]
    for argv in invocations:
        assert _parse(argv).command == "auth", argv


def test_read_commands_default_to_the_deterministic_selector() -> None:
    for argv in (["auth", "resolve", "demo"], ["auth", "bind", "demo"]):
        args = _parse(argv)
        assert args.auth_profile == "auto"
        assert args.actor_class == "human"


def test_mutating_commands_require_the_exact_state_they_reviewed() -> None:
    # unbind and lease revoke need a revision; migrate apply needs a plan id and hash.
    for argv in (
        ["auth", "unbind", "demo"],
        ["auth", "lease", "revoke", "op-1"],
        ["auth", "migrate", "apply", "demo"],
        ["auth", "migrate", "apply", "demo", "--plan-id", "authmig-1"],
    ):
        with pytest.raises(SystemExit):
            _parse(argv)


def test_rf_auth_is_registered_on_the_real_cli_parser() -> None:
    parsed = cli.build_parser().parse_args(["auth", "whoami", "demo"])

    assert parsed.command == "auth"
    assert parsed.auth_command == "whoami"
    assert parsed.repo_id == "demo"


# ---------------------------------------------------------------------------
# Dispatch and exit codes
# ---------------------------------------------------------------------------


def test_one_eligible_profile_needs_no_prompt_and_no_flags(tmp_path: Path) -> None:
    code, payload = _run(tmp_path, ["auth", "resolve", "demo"])

    assert code == 0
    assert isinstance(payload, dict)
    assert payload["outcome"] == "proposal_required"
    assert payload["selector"] == {"auth_profile": "auto", "actor_class": "human"}


def test_profile_list_and_inspect_render_safe_metadata(tmp_path: Path) -> None:
    _, listed = _run(tmp_path, ["auth", "profile", "list"])
    _, inspected = _run(tmp_path, ["auth", "profile", "inspect", "personal"])

    assert isinstance(listed, dict)
    assert [item["profile_id"] for item in listed["profiles"]] == ["personal"]
    rendered = json.dumps([listed, inspected], sort_keys=True)
    assert "gh-account-personal" not in rendered
    assert "REPOFORGE_GH_TOKEN" not in rendered


def test_bind_then_whoami_reports_the_binding_it_actually_wrote(tmp_path: Path) -> None:
    service = _service(tmp_path)

    bind_code, bound = _run(tmp_path, ["auth", "bind", "demo"], service=service)
    whoami_code, reported = _run(tmp_path, ["auth", "whoami", "demo"], service=service)

    assert bind_code == 0
    assert isinstance(bound, dict) and bound["status"] == "created"
    assert isinstance(reported, dict)
    assert reported["profile_id"] == "personal"
    binding_surface = reported["surfaces"][0]
    assert binding_surface["surface"] == "repository_binding"
    assert binding_surface["state"] == "verified"
    # Surfaces with no composed inspector are unavailable, so the report is not ready.
    assert whoami_code == 3
    assert reported["ready"] is False


def test_whoami_reports_a_requested_subset_in_the_stable_order(tmp_path: Path) -> None:
    _, reported = _run(
        tmp_path, ["auth", "whoami", "demo", "--check", "publication", "--check", "api"]
    )

    assert isinstance(reported, dict)
    assert [item["surface"] for item in reported["surfaces"]] == ["api", "publication"]


def test_doctor_exits_three_when_something_blocks_a_write(tmp_path: Path) -> None:
    code, payload = _run(
        tmp_path, ["auth", "doctor", "demo"], service=_service(tmp_path, profiles={})
    )

    assert code == 3
    assert isinstance(payload, dict)
    assert payload["blocking"] >= 1
    assert any(item["code"] == "migration_required" for item in payload["findings"])


def test_an_unsafe_selector_fails_with_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(RepoForgeError) as failure:
        _run(tmp_path, ["auth", "resolve", "demo", "--auth-profile", "ghp_not_a_profile_id"])

    assert failure.value.code is ErrorCode.CONFIG_INVALID
    assert failure.value.safe_next_action


def test_lease_inspection_without_durable_state_is_typed_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(RepoForgeError) as failure:
        _run(tmp_path, ["auth", "lease", "inspect", "op-1"])

    assert failure.value.code is ErrorCode.OPERATION_IDENTITY_NOT_FOUND


def test_import_gh_lists_candidates_and_verifies_one_named_account(tmp_path: Path) -> None:
    accounts = Accounts()

    _, listed = _run(tmp_path, ["auth", "import", "gh"], accounts=accounts)
    _, verified = _run(
        tmp_path, ["auth", "import", "gh", "--login", "acme-operator"], accounts=accounts
    )

    assert isinstance(listed, dict)
    assert [item["login"] for item in listed["candidates"]] == ["acme-operator"]
    assert isinstance(verified, dict)
    assert verified["verified"]["actor_id"] == "4242"
    assert accounts.calls == [
        "candidates:github.com",
        "verify:github.com:acme-operator",
    ]


def test_import_ssh_reports_one_pinnable_identity(tmp_path: Path) -> None:
    _, payload = _run(tmp_path, ["auth", "import", "ssh", "github-work"])

    assert isinstance(payload, dict)
    assert payload["alias"]["identity_file"] == "/home/demo/.ssh/id_ed25519"


def test_migrate_inspect_hands_back_the_exact_apply_command(tmp_path: Path) -> None:
    code, payload = _run(tmp_path, ["auth", "migrate", "inspect", "demo"])

    assert code == 3  # nothing to adopt, so not ready
    assert isinstance(payload, dict)
    assert payload["ready"] is False
    assert "Resolve the blocking findings" in str(payload["safe_next_action"])


def test_migrate_apply_records_the_operator_who_reviewed_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USER", "reviewing-operator")
    migration = Migration()

    code, payload = _run(
        tmp_path,
        [
            "auth",
            "migrate",
            "apply",
            "demo",
            "--plan-id",
            "authmig-1",
            "--plan-hash",
            "b" * 64,
        ],
        migration=migration,
    )

    assert code == 0
    assert isinstance(payload, dict) and payload["status"] == "applied"
    assert migration.applied == [
        {
            "repo_id": "demo",
            "plan_id": "authmig-1",
            "plan_hash": "b" * 64,
            "actor": "reviewing-operator",
        }
    ]


# ---------------------------------------------------------------------------
# Command isolation
# ---------------------------------------------------------------------------


def test_no_auth_handler_can_mutate_global_github_git_or_ssh_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorded: list[tuple[str, ...]] = []

    class Recording:
        def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
            return {"HOME": "/home/demo", "PATH": "/bin", "GH_TOKEN": _TOKEN}

        def run_isolated(self, argv: list[str], **kwargs: Any) -> Any:
            recorded.append(tuple(argv))
            raise RepoForgeError("not configured", code=ErrorCode.COMMAND_FAILED)

    from repoforge.adapters.git.ambient_auth import GitAmbientAuthConflictReader
    from repoforge.adapters.git.ssh_alias_discovery import SshCommandAliasDiscovery
    from repoforge.adapters.github.account_discovery import GhCliNamedAccountDiscovery

    executor = Recording()
    ambient = GitAmbientAuthConflictReader(executor, environ={"GH_TOKEN": _TOKEN})
    ambient.git_config_values(tmp_path, "credential.helper")
    ambient.git_config_values(tmp_path, "user.email")
    with pytest.raises(RepoForgeError):
        GhCliNamedAccountDiscovery(executor, cwd=tmp_path).candidates(host="github.com")
    with pytest.raises(RepoForgeError):
        SshCommandAliasDiscovery(executor, cwd=tmp_path).inspect("github-work")

    assert recorded
    for argv in recorded:
        joined = " ".join(argv)
        assert "auth switch" not in joined
        assert "--global" not in argv and "--system" not in argv
        assert "--replace-all" not in argv and "--unset" not in argv
        assert "ssh-keygen" not in joined and "ssh-add" not in joined
    captured = capsys.readouterr()
    assert _TOKEN not in captured.out and _TOKEN not in captured.err


def test_no_auth_payload_carries_a_token_or_credential_reference(tmp_path: Path) -> None:
    service = _service(tmp_path)
    payloads: list[object] = []
    for argv in (
        ["auth", "profile", "list"],
        ["auth", "resolve", "demo"],
        ["auth", "bind", "demo"],
        ["auth", "whoami", "demo"],
        ["auth", "doctor", "demo"],
        ["auth", "import", "gh"],
        ["auth", "import", "ssh", "github-work"],
    ):
        payloads.append(_run(tmp_path, argv, service=service)[1])

    rendered = json.dumps(payloads, sort_keys=True, default=str)
    for canary in ("gho_", "ghp_", "github_pat_", "Authorization", "REPOFORGE_GH_TOKEN"):
        assert canary not in rendered, canary
