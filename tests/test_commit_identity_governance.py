from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from repoforge.adapters.git.cli import GitCliRepository
from repoforge.adapters.git.commit_identity import GitCommitIdentityGateway
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig, load_config
from repoforge.domain.commit_identity import (
    CommitIdentityEvidence,
    CommitIdentityPolicy,
    CommitSigningMode,
    commit_identity_policy_from_payload,
)
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError
from repoforge.domain.repository_identity import ActorClass
from repoforge.ports.command import CommandResult

_GIT = shutil.which("git")


def _policy(
    *,
    profile_id: str = "company-agent",
    actor_class: ActorClass = ActorClass.AUTONOMOUS_AGENT,
    author_name: str = "RepoForge Agent",
    author_email: str = "agent@example.test",
    committer_name: str = "RepoForge Agent",
    committer_email: str = "agent@example.test",
    signing_mode: CommitSigningMode = CommitSigningMode.UNSIGNED_ATTESTED,
    signer_fingerprint: str | None = None,
    signing_key_reference: str | None = None,
    represented_actor_id: str | None = None,
    delegation_approval_id: str | None = None,
) -> CommitIdentityPolicy:
    return CommitIdentityPolicy(
        profile_id=profile_id,
        actor_class=actor_class,
        author_name=author_name,
        author_email=author_email,
        committer_name=committer_name,
        committer_email=committer_email,
        signing_mode=signing_mode,
        signer_fingerprint=signer_fingerprint,
        signing_key_reference=signing_key_reference,
        represented_actor_id=represented_actor_id,
        delegation_approval_id=delegation_approval_id,
    )


def _run(cwd: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _run(tmp_path, "init", str(repo))
    _run(repo, "config", "user.name", "Legacy Human")
    _run(repo, "config", "user.email", "legacy@example.test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run(repo, "add", "tracked.txt")
    _run(repo, "commit", "-m", "base")
    return repo


def _gateway(tmp_path: Path) -> GitCommitIdentityGateway:
    return GitCommitIdentityGateway(
        SubprocessCommandExecutor(ServerConfig(tmp_path / "workspaces", tmp_path / "state"))
    )


def test_policy_matrix_requires_explicit_delegation_and_dedicated_signer() -> None:
    assert _policy(actor_class=ActorClass.HUMAN_OPERATED).actor_class is ActorClass.HUMAN_OPERATED
    with pytest.raises(ValueError, match="delegation"):
        _policy(actor_class=ActorClass.DELEGATED_HUMAN)
    delegated = _policy(
        actor_class=ActorClass.DELEGATED_HUMAN,
        represented_actor_id="human-42",
        delegation_approval_id="approval-84",
    )
    assert delegated.delegation_approval_id == "approval-84"

    with pytest.raises(ValueError, match="signer"):
        _policy(signing_mode=CommitSigningMode.SSH)
    signed = _policy(
        signing_mode=CommitSigningMode.SSH,
        signer_fingerprint="a" * 64,
        signing_key_reference="/run/repoforge/agent-signing-key",
    )
    assert signed.signer_fingerprint == "a" * 64


def test_policy_round_trip_keeps_opaque_signer_reference_out_of_safe_payload() -> None:
    policy = _policy(
        signing_mode=CommitSigningMode.GPG,
        signer_fingerprint="b" * 40,
        signing_key_reference="agent-key-ref",
    )
    loaded = commit_identity_policy_from_payload(policy.durable_payload())
    assert loaded == policy
    assert "agent-key-ref" not in repr(policy.safe_payload())

    malformed = policy.durable_payload()
    malformed["author_name"] = 42
    with pytest.raises(ValueError, match="author_name must be a string"):
        commit_identity_policy_from_payload(malformed)


def test_delegated_commit_evidence_retains_safe_approval_provenance() -> None:
    evidence = CommitIdentityEvidence(
        profile_id="delegated-profile",
        actor_class=ActorClass.DELEGATED_HUMAN,
        author_name="Represented Human",
        author_email="human@example.test",
        committer_name="RepoForge Agent",
        committer_email="agent@example.test",
        signing_mode=CommitSigningMode.UNSIGNED_ATTESTED,
        signer_fingerprint=None,
        attestation_digest="a" * 64,
        config_snapshot_digest="b" * 64,
        represented_actor_id="human-42",
        delegation_approval_id="approval-84",
    )
    assert evidence.safe_payload()["represented_actor_id"] == "human-42"
    assert evidence.safe_payload()["delegation_approval_id"] == "approval-84"


def test_commit_identity_config_rejects_unknown_and_non_string_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'''[repositories.demo]
path = "{repo}"

[repositories.demo.commit_identity]
profile_id = "agent"
actor_class = "autonomous_agent"
author_name = "RepoForge Agent"
author_email = "agent@example.test"
committer_name = "RepoForge Agent"
committer_email = "agent@example.test"
unknown_field = "typo"
''',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unsupported fields"):
        load_config(config)

    config.write_text(
        config.read_text(encoding="utf-8")
        .replace('author_name = "RepoForge Agent"', "author_name = 42")
        .replace('unknown_field = "typo"\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="author_name must be a string"):
        load_config(config)


@pytest.mark.skipif(_GIT is None, reason="git is required")
def test_legacy_identity_is_imported_once_as_explicit_unsigned_policy(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    policy = _gateway(tmp_path).resolve_policy(repo, None)
    assert policy.actor_class is ActorClass.HUMAN_OPERATED
    assert policy.author_name == "Legacy Human"
    assert policy.author_email == "legacy@example.test"
    assert policy.signing_mode is CommitSigningMode.UNSIGNED_ATTESTED

    _run(repo, "config", "user.name", "Changed Later")
    assert policy.author_name == "Legacy Human"


class ScriptedExecutor:
    def __init__(self, responses: list[CommandResult]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "HOME": "/home/demo",
            "PATH": "/safe/bin",
            "GIT_AUTHOR_NAME": "Wrong Author",
            "GIT_COMMITTER_EMAIL": "wrong@example.test",
            "SSH_AUTH_SOCK": "/tmp/personal-agent",
            **dict(extra or {}),
        }

    def run(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"mode": "run", "argv": tuple(argv), **kwargs})
        if not self.responses:
            raise AssertionError(f"unhandled command: {argv}")
        return self.responses.pop(0)

    def run_isolated(self, argv: list[str], **kwargs: Any) -> CommandResult:
        self.calls.append({"mode": "isolated", "argv": tuple(argv), **kwargs})
        if not self.responses:
            raise AssertionError(f"unhandled isolated command: {argv}")
        return self.responses.pop(0)


def _snapshot_responses(*, config_value: str = "Legacy Human") -> list[CommandResult]:
    return [
        CommandResult(("git",), "/repo", 0, "false\n", ""),
        CommandResult(
            ("git",),
            "/repo",
            0,
            f"user.name\n{config_value}\0remote.origin.url\nhttps://github.com/acme/widgets.git\0",
            "",
        ),
    ]


def test_shared_config_drift_fails_before_staging() -> None:
    baseline_executor = ScriptedExecutor(_snapshot_responses())
    baseline = GitCommitIdentityGateway(baseline_executor).config_snapshot(Path("/repo"))
    changed_executor = ScriptedExecutor(_snapshot_responses(config_value="Mutated Human"))
    gateway = GitCommitIdentityGateway(changed_executor)

    with pytest.raises(RepoForgeError) as drift:
        gateway.commit(
            Path("/repo"),
            "message",
            _policy(),
            expected_config_digest=baseline.digest,
        )
    assert drift.value.code is ErrorCode.COMMIT_IDENTITY_CONFIG_DRIFT
    assert not any(call["argv"][:2] == ("git", "add") for call in changed_executor.calls)


def test_unsigned_commit_uses_exact_identity_and_returns_safe_attestation() -> None:
    responses = [
        *_snapshot_responses(),
        *_snapshot_responses(),
        CommandResult(("git",), "/repo", 0, "", ""),
        CommandResult(("git",), "/repo", 0, "tracked.txt\n", ""),
        CommandResult(("git",), "/repo", 0, "", ""),
        CommandResult(("git",), "/repo", 0, "1" * 40 + "\n", ""),
        CommandResult(("git",), "/repo", 0, "summary\n", ""),
        CommandResult(
            ("git",),
            "/repo",
            0,
            "RepoForge Agent\0agent@example.test\0RepoForge Agent\0agent@example.test\0N\0\n",
            "",
        ),
    ]
    executor = ScriptedExecutor(responses)
    gateway = GitCommitIdentityGateway(executor)
    snapshot = gateway.config_snapshot(Path("/repo"))
    result = gateway.commit(
        Path("/repo"),
        "message",
        _policy(),
        expected_config_digest=snapshot.digest,
    )

    commit_call = next(call for call in executor.calls if call["argv"][:2] == ("git", "commit"))
    assert commit_call["mode"] == "isolated"
    environment = commit_call["environment"]
    assert environment["GIT_AUTHOR_NAME"] == "RepoForge Agent"
    assert environment["GIT_AUTHOR_EMAIL"] == "agent@example.test"
    assert environment["GIT_COMMITTER_NAME"] == "RepoForge Agent"
    assert environment["GIT_COMMITTER_EMAIL"] == "agent@example.test"
    assert "SSH_AUTH_SOCK" not in environment
    assert "Wrong Author" not in repr(environment)
    assert environment["GIT_CONFIG_VALUE_0"] == "true"
    assert result.evidence.attestation_digest is not None
    rendered = repr(result.evidence.safe_payload())
    assert "signing_key_reference" not in rendered


def test_direct_signing_requires_expected_fingerprint() -> None:
    policy = _policy(
        signing_mode=CommitSigningMode.SSH,
        signer_fingerprint="a" * 64,
        signing_key_reference="/run/repoforge/agent-signing-key",
    )
    responses = [
        *_snapshot_responses(),
        *_snapshot_responses(),
        CommandResult(("git",), "/repo", 0, "", ""),
        CommandResult(("git",), "/repo", 0, "tracked.txt\n", ""),
        CommandResult(("git",), "/repo", 0, "", ""),
        CommandResult(("git",), "/repo", 0, "2" * 40 + "\n", ""),
        CommandResult(("git",), "/repo", 0, "summary\n", ""),
        CommandResult(
            ("git",),
            "/repo",
            0,
            f"RepoForge Agent\0agent@example.test\0RepoForge Agent\0agent@example.test\0G\0{'b' * 64}\n",
            "",
        ),
    ]
    executor = ScriptedExecutor(responses)
    gateway = GitCommitIdentityGateway(executor)
    snapshot = gateway.config_snapshot(Path("/repo"))

    with pytest.raises(RepoForgeError) as mismatch:
        gateway.commit(
            Path("/repo"),
            "message",
            policy,
            expected_config_digest=snapshot.digest,
        )
    assert mismatch.value.code is ErrorCode.COMMIT_SIGNING_FAILED


def test_observed_author_or_committer_mismatch_is_typed() -> None:
    responses = [
        *_snapshot_responses(),
        *_snapshot_responses(),
        CommandResult(("git",), "/repo", 0, "", ""),
        CommandResult(("git",), "/repo", 0, "tracked.txt\n", ""),
        CommandResult(("git",), "/repo", 0, "", ""),
        CommandResult(("git",), "/repo", 0, "3" * 40 + "\n", ""),
        CommandResult(("git",), "/repo", 0, "summary\n", ""),
        CommandResult(
            ("git",),
            "/repo",
            0,
            "Wrong Human\0wrong@example.test\0RepoForge Agent\0agent@example.test\0N\0\n",
            "",
        ),
    ]
    executor = ScriptedExecutor(responses)
    gateway = GitCommitIdentityGateway(executor)
    snapshot = gateway.config_snapshot(Path("/repo"))
    with pytest.raises(RepoForgeError) as mismatch:
        gateway.commit(
            Path("/repo"),
            "message",
            _policy(),
            expected_config_digest=snapshot.digest,
        )
    assert mismatch.value.code is ErrorCode.COMMIT_IDENTITY_MISMATCH


@pytest.mark.skipif(_GIT is None, reason="git is required")
def test_two_worktrees_commit_with_conflicting_process_identities_without_cross_talk(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    remote_url = "https://github.com/acme/widgets.git"
    _run(repo, "remote", "add", "origin", remote_url)
    _run(repo, "branch", "work-a")
    _run(repo, "branch", "work-b")
    work_a = tmp_path / "work-a"
    work_b = tmp_path / "work-b"
    _run(repo, "worktree", "add", str(work_a), "work-a")
    _run(repo, "worktree", "add", str(work_b), "work-b")
    gateway = _gateway(tmp_path)
    snapshot_a = gateway.config_snapshot(work_a)
    snapshot_b = gateway.config_snapshot(work_b)
    policy_a = _policy(
        profile_id="agent-a",
        author_name="Agent A",
        author_email="a@example.test",
        committer_name="Committer A",
        committer_email="ca@example.test",
    )
    policy_b = _policy(
        profile_id="agent-b",
        author_name="Agent B",
        author_email="b@example.test",
        committer_name="Committer B",
        committer_email="cb@example.test",
    )
    (work_a / "a.txt").write_text("a\n", encoding="utf-8")
    (work_b / "b.txt").write_text("b\n", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            gateway.commit,
            work_a,
            "commit a",
            policy_a,
            expected_config_digest=snapshot_a.digest,
        )
        second = pool.submit(
            gateway.commit,
            work_b,
            "commit b",
            policy_b,
            expected_config_digest=snapshot_b.digest,
        )
        first.result()
        second.result()

    assert _run(work_a, "show", "-s", "--format=%an|%ae|%cn|%ce", "HEAD").stdout.strip() == (
        "Agent A|a@example.test|Committer A|ca@example.test"
    )
    assert _run(work_b, "show", "-s", "--format=%an|%ae|%cn|%ce", "HEAD").stdout.strip() == (
        "Agent B|b@example.test|Committer B|cb@example.test"
    )
    assert _run(repo, "config", "user.name").stdout.strip() == "Legacy Human"
    assert _run(repo, "remote", "get-url", "origin").stdout.strip() == remote_url


def test_generic_git_adapter_exposes_no_ambient_commit_bypass() -> None:
    assert not hasattr(GitCliRepository, "commit")
    assert not hasattr(GitCliRepository, "commit_merge")


def test_config_snapshot_contains_only_key_and_value_digests() -> None:
    secretish = "helper-with-sensitive-material"
    executor = ScriptedExecutor(_snapshot_responses(config_value=secretish))
    snapshot = GitCommitIdentityGateway(executor).config_snapshot(Path("/repo"))
    rendered = repr(snapshot.safe_payload())
    assert secretish not in rendered
    expected = hashlib.sha256(json.dumps([secretish], separators=(",", ":")).encode()).hexdigest()
    assert expected in rendered


def test_config_snapshot_aggregates_multi_valued_helpers_in_order() -> None:
    output = (
        "credential.helper\nmanager-core\0"
        "credential.helper\ncache --timeout=60\0"
        "user.name\nLegacy Human\0"
    )
    executor = ScriptedExecutor(
        [
            CommandResult(("git",), "/repo", 0, "false\n", ""),
            CommandResult(("git",), "/repo", 0, output, ""),
        ]
    )
    snapshot = GitCommitIdentityGateway(executor).config_snapshot(Path("/repo"))
    helpers = [item for item in snapshot.entries if item.key == "credential.helper"]
    assert len(helpers) == 1
    expected = hashlib.sha256(
        json.dumps(
            ["manager-core", "cache --timeout=60"],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert helpers[0].value_digest == expected


@pytest.mark.skipif(_GIT is None, reason="git is required")
def test_reviewed_merge_commit_uses_pinned_identity_and_attestation(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    base_branch = _run(repo, "branch", "--show-current").stdout.strip()
    _run(repo, "checkout", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _run(repo, "add", "feature.txt")
    _run(repo, "commit", "-m", "feature")
    _run(repo, "checkout", base_branch)
    (repo / "main.txt").write_text("main\n", encoding="utf-8")
    _run(repo, "add", "main.txt")
    _run(repo, "commit", "-m", "main")
    _run(repo, "merge", "--no-ff", "--no-commit", "feature")

    gateway = _gateway(tmp_path)
    snapshot = gateway.config_snapshot(repo)
    result = gateway.commit_merge(
        repo,
        _policy(
            profile_id="merge-agent",
            author_name="Merge Agent",
            author_email="merge-author@example.test",
            committer_name="Merge Committer",
            committer_email="merge-committer@example.test",
        ),
        expected_config_digest=snapshot.digest,
    )

    assert result.evidence.attestation_digest is not None
    assert _run(repo, "show", "-s", "--format=%an|%ae|%cn|%ce", "HEAD").stdout.strip() == (
        "Merge Agent|merge-author@example.test|Merge Committer|merge-committer@example.test"
    )
    assert _run(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.count(" ") == 2
