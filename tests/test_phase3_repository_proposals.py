from __future__ import annotations

import json
import subprocess
from pathlib import Path

from repoforge.adapters.repository import LocalRepositoryProbe
from repoforge.adapters.subprocess import SubprocessCommandExecutor
from repoforge.config import ServerConfig
from repoforge.domain.repository_detection import ManifestFact, RemoteFact, RepositoryFacts
from repoforge.domain.repository_proposal import (
    EnrollmentMode,
    ProposalConfidence,
    build_repository_proposal,
)


def _facts(**changes: object) -> RepositoryFacts:
    values = dict(
        root=Path("/repo"),
        common_dir=Path("/repo/.git"),
        repo_id="demo",
        display_name="demo",
        current_branch="main",
        default_branch_candidates=("main",),
        remotes=(RemoteFact("origin", "git@example/repo", "git@example/repo"),),
        manifests=(ManifestFact("package.json", "javascript", "pnpm", True),),
        lockfiles=("pnpm-lock.yaml",),
        toolchain_declarations=("pnpm@10.0.0",),
        scripts=("lint", "typecheck", "test", "build"),
        make_targets=(),
        instruction_files=("README.md",),
        ci_files=(".github/workflows/ci.yml",),
        workspace_packages=(),
        submodules=(),
        lfs_tracked=False,
        shallow=False,
        detached=False,
        symlink_count=0,
        large_file_count=0,
        binary_file_count=0,
        tracked_file_count=10,
        total_tracked_bytes=1000,
        existing_worktrees=("/repo",),
        warnings=(),
    )
    values.update(changes)
    return RepositoryFacts(**values)  # type: ignore[arg-type]


def test_proposal_is_deterministic_and_never_needs_command_execution() -> None:
    decisions = {"dependency_install": "exclude"}
    first = build_repository_proposal(_facts(), decisions=decisions)
    second = build_repository_proposal(_facts(), decisions=decisions)
    assert first == second
    assert first.proposal_id == second.proposal_id
    assert first.confidence is ProposalConfidence.HIGH
    assert {profile.name for profile in first.policy.profiles} >= {"quick", "test", "full"}


def test_ambiguous_repository_requires_all_security_decisions() -> None:
    proposal = build_repository_proposal(
        _facts(
            remotes=(RemoteFact("origin", "a", "a"), RemoteFact("upstream", "b", "b")),
            default_branch_candidates=("main", "develop"),
            lockfiles=("pnpm-lock.yaml", "package-lock.json"),
            manifests=(
                ManifestFact("package.json", "javascript", "pnpm", True),
                ManifestFact("apps/a/package.json", "javascript", "pnpm"),
            ),
            workspace_packages=("apps/*",),
            submodules=("vendor/x",),
            lfs_tracked=True,
            large_file_count=3,
        )
    )
    codes = {decision.code for decision in proposal.required_decisions}
    assert {
        "publish_remote",
        "default_base",
        "package_manager",
        "monorepo_scope",
        "submodules",
        "lfs",
        "repository_budget",
    }.issubset(codes)
    assert proposal.confidence is ProposalConfidence.LOW


def test_unsupported_ecosystem_is_read_only_not_executable() -> None:
    proposal = build_repository_proposal(_facts(manifests=(), lockfiles=(), scripts=()))
    assert proposal.policy.mode is EnrollmentMode.READ_ONLY
    assert proposal.policy.profiles == ()
    assert any(item.code == "UNSUPPORTED_ECOSYSTEM" for item in proposal.findings)


def test_local_probe_collects_facts_without_running_discovered_scripts(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    marker = repository / "executed"
    (repository / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@10.0.0", "scripts": {"test": f"touch {marker}"}}),
        encoding="utf-8",
    )
    (repository / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    (repository / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repository, check=True, capture_output=True)
    server = ServerConfig(tmp_path / "workspaces", tmp_path / "state")
    facts = LocalRepositoryProbe(SubprocessCommandExecutor(server)).inspect(repository)
    assert facts.scripts == ("test",)
    assert facts.lockfiles == ("pnpm-lock.yaml",)
    assert not marker.exists()


def test_network_setup_and_autofix_require_explicit_decisions() -> None:
    facts = _facts(scripts=("lint", "test", "fix"))
    pending = build_repository_proposal(facts)
    assert {"dependency_install", "autofix"}.issubset(
        {item.code for item in pending.required_decisions}
    )
    approved = build_repository_proposal(
        facts,
        decisions={
            "dependency_install": "include_non_verification",
            "autofix": "include_non_verification",
        },
    )
    profiles = {item.name: item for item in approved.policy.profiles}
    assert profiles["setup"].verification is False
    assert profiles["fix"].verification is False
    assert profiles["setup"].commands == (("pnpm", "install", "--frozen-lockfile"),)


def test_risky_commands_are_never_inferred_and_require_confirmation() -> None:
    facts = _facts(scripts=("test", "deploy:production", "db:migrate"))
    pending = build_repository_proposal(facts, decisions={"dependency_install": "exclude"})
    assert "risky_commands" in {item.code for item in pending.required_decisions}
    approved = build_repository_proposal(
        facts,
        decisions={"dependency_install": "exclude", "risky_commands": "exclude"},
    )
    argv = {
        arg
        for profile in approved.policy.profiles
        for command in profile.commands
        for arg in command
    }
    assert "deploy:production" not in argv
    assert "db:migrate" not in argv


def test_unverified_github_auth_can_enroll_local_only_without_disabling_local_writes() -> None:
    facts = _facts(
        remotes=(
            RemoteFact(
                "origin",
                "https://github.com/example/demo.git",
                "git@github.com:example/demo.git",
            ),
        ),
        github_authenticated=False,
    )
    pending = build_repository_proposal(facts, decisions={"dependency_install": "exclude"})
    assert "publishing_access" in {item.code for item in pending.required_decisions}
    local_only = build_repository_proposal(
        facts,
        decisions={
            "dependency_install": "exclude",
            "publishing_access": "local_only",
        },
    )
    assert local_only.policy.mode is EnrollmentMode.STANDARD
    assert local_only.policy.publish_enabled is False
    assert local_only.policy.profiles
