from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import execution_coordinator_for_tests

from repoforge.adapters.configuration import ConfigGenerationStore
from repoforge.adapters.git import GitCliRepository
from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.runtime.state_store import JsonRuntimeStore
from repoforge.application.configuration.document import (
    apply_proposal,
    parse_resolved,
    render_resolved,
)
from repoforge.application.configuration.source import (
    SourceConfiguration,
    SourceRepository,
    parse_source,
    render_source,
)
from repoforge.application.context import ApplicationContext
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig
from repoforge.domain.config_generation import (
    ApprovalEvent,
    CapabilityDeltaKind,
    ConfigMutation,
    classify_capability_delta,
    sha256_text,
)
from repoforge.domain.errors import ConfigError, SecurityError
from repoforge.domain.redaction import redact_text
from repoforge.domain.repository_detection import ManifestFact, RemoteFact, RepositoryFacts
from repoforge.domain.repository_proposal import EnrollmentMode, build_repository_proposal
from repoforge.domain.runtime import RuntimePhase, RuntimeRecord, TunnelProfile
from repoforge.ports.command import CommandResult
from repoforge.testing import (
    FixedClock,
    InMemoryLockManager,
    InMemoryOperationGate,
    InMemoryWorkspaceStore,
    ScriptedCommandExecutor,
    SequenceIdGenerator,
)


def _resolved(
    *,
    display_name: str = "Demo",
    max_files: int = 100,
    allowed_paths: tuple[str, ...] = (),
    denied_paths: tuple[str, ...] = (".git", ".env"),
    read_only: bool = False,
) -> str:
    allowed = ", ".join(json.dumps(item) for item in allowed_paths)
    denied = ", ".join(json.dumps(item) for item in denied_paths)
    return f"""[server]
workspace_root = "/tmp/workspaces"
state_root = "/tmp/state"
max_file_bytes = 2000000
max_tool_output_chars = 120000
max_batch_files = 20
allowed_environment = ["HOME"]
path_prefixes = ["/usr/bin"]

[repositories.demo]
path = "/tmp/demo"
display_name = {json.dumps(display_name)}
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main"]
read_only = {str(read_only).lower()}
require_verification_before_commit = true
fetch_before_workspace = true
max_changed_files = {max_files}
max_diff_lines = 12000
max_total_changed_bytes = 1000000
allowed_paths = [{allowed}]
denied_paths = [{denied}]
pr_labels = []
pr_reviewers = []
no_maintainer_edit = false

[repositories.demo.profiles.full]
description = "Full"
verification = true
commands = [["python", "-m", "pytest"]]
"""


def _source() -> str:
    return 'version = 2\n[tunnel]\nid = "tunnel"\nprofile = "repoforge"\n[[repo]]\nid = "demo"\npath = "/tmp/demo"\n'


def _approval(proposal: str) -> ApprovalEvent:
    return ApprovalEvent("tester", "2026-07-13T00:00:00+00:00", proposal, sha256_text(proposal))


def _store(tmp_path: Path) -> ConfigGenerationStore:
    source_path = tmp_path / "config.toml"
    source_path.write_text(_source(), encoding="utf-8")
    return ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )


def _accept(
    store: ConfigGenerationStore,
    resolved: str,
    *,
    proposal: str | None,
    approval: ApprovalEvent | None,
    expected: int,
    reason: str = "test",
) -> Any:
    source = store.read_source_text()
    return store.accept(
        ConfigMutation(
            source,
            resolved,
            (("demo", "d" * 64),),
            reason,
            "2026-07-13T00:00:00+00:00",
            expected,
            sha256_text(source),
            proposal,
            approval,
            "corr",
        )
    )


def _facts(**changes: object) -> RepositoryFacts:
    values: dict[str, object] = {
        "root": Path("/repo"),
        "common_dir": Path("/repo/.git"),
        "repo_id": "demo",
        "display_name": "demo",
        "current_branch": "main",
        "default_branch_candidates": ("main",),
        "remotes": (RemoteFact("origin", "fetch", "push"),),
        "manifests": (
            ManifestFact("package.json", "javascript", "pnpm", True, ("lint", "test", "build")),
        ),
        "lockfiles": ("pnpm-lock.yaml",),
        "toolchain_declarations": ("pnpm@10",),
        "scripts": ("lint", "test", "build"),
        "make_targets": (),
        "instruction_files": ("README.md",),
        "ci_files": (".github/workflows/ci.yml",),
        "workspace_packages": (),
        "submodules": (),
        "lfs_tracked": False,
        "shallow": False,
        "detached": False,
        "symlink_count": 0,
        "large_file_count": 0,
        "binary_file_count": 0,
        "tracked_file_count": 10,
        "total_tracked_bytes": 1000,
        "existing_worktrees": ("/repo",),
        "policy_files": (),
        "scan_truncated": False,
        "warnings": (),
    }
    values.update(changes)
    return RepositoryFacts(**values)  # type: ignore[arg-type]


def test_active_pointer_is_committed_only_after_staged_health_gate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    generation = _accept(
        store, _resolved(), proposal="initial", approval=_approval("initial"), expected=0
    )
    assert store.active() is None
    store.stage_activation(generation.generation, expected_active=None)
    assert store.active() is None
    assert store.activation_target() == generation
    committed = store.activate(generation.generation, expected_active=None)
    assert committed.active
    assert store.active() is not None and store.active().generation == 1
    assert store.activation_target() is None


def test_staging_new_generation_never_claims_it_is_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _accept(
        store, _resolved(), proposal="initial", approval=_approval("initial"), expected=0
    )
    store.stage_activation(first.generation, expected_active=None)
    store.activate(first.generation, expected_active=None)
    expanded = _accept(
        store,
        _resolved(max_files=200),
        proposal="expand",
        approval=_approval("expand"),
        expected=1,
    )
    store.stage_activation(expanded.generation, expected_active=1)
    assert store.activation_target() is not None
    assert store.activation_target().generation == 2
    assert store.active() is not None and store.active().generation == 1


def test_semantic_noop_does_not_create_generation_but_metadata_does(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _accept(
        store, _resolved(), proposal="initial", approval=_approval("initial"), expected=0
    )
    same_policy = _resolved().replace("[server]", "# formatting only\n[server]")
    no_op = _accept(store, same_policy, proposal=None, approval=None, expected=1, reason="refresh")
    assert no_op.generation == first.generation
    metadata = _accept(
        store,
        _resolved(display_name="Renamed"),
        proposal=None,
        approval=None,
        expected=1,
        reason="metadata",
    )
    assert metadata.generation == 2
    assert metadata.delta is CapabilityDeltaKind.METADATA_ONLY


def test_accept_allocates_after_immutable_history_when_accepted_pointer_rolls_back(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _accept(
        store, _resolved(), proposal="initial", approval=_approval("initial"), expected=0
    )
    store.stage_activation(first.generation, expected_active=None)
    store.activate(first.generation, expected_active=None)
    second = _accept(
        store,
        _resolved(display_name="Second"),
        proposal=None,
        approval=None,
        expected=1,
        reason="metadata",
    )
    store.stage_activation(second.generation, expected_active=1)
    store.activate(second.generation, expected_active=1)

    rolled_back = store.rollback(first.generation, expected_active=second.generation)
    assert rolled_back.generation == 1
    assert store.current() is not None and store.current().generation == 1
    assert [item.generation for item in store.history()] == [2, 1]

    third = _accept(
        store,
        _resolved(display_name="Third"),
        proposal=None,
        approval=None,
        expected=1,
        reason="metadata after rollback",
    )

    assert third.generation == 3
    assert third.previous_generation == 1
    assert [item.generation for item in store.history()] == [3, 2, 1]


def test_semantic_delta_handles_path_sets_budgets_and_profiles() -> None:
    restriction = classify_capability_delta(
        _resolved(),
        _resolved(max_files=50, allowed_paths=("src/**",), denied_paths=(".git", ".env", "*.pem")),
    )
    assert restriction.kind is CapabilityDeltaKind.RESTRICTION
    expansion = classify_capability_delta(
        _resolved(max_files=50, allowed_paths=("src/**",)), _resolved(max_files=100)
    )
    assert expansion.kind is CapabilityDeltaKind.EXPANSION
    incompatible = classify_capability_delta(
        _resolved().replace(
            'commands = [["python", "-m", "pytest"]]',
            'commands = [["bash", "-c", "curl example.com"]]',
        ),
        _resolved(max_files=50),
    )
    assert incompatible.kind is CapabilityDeltaKind.INCOMPATIBLE


def test_generation_history_fails_closed_on_noncurrent_corruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _accept(store, _resolved(), proposal="initial", approval=_approval("initial"), expected=0)
    _accept(store, _resolved(display_name="two"), proposal=None, approval=None, expected=1)
    (store.generations / "1" / "resolved.toml").write_text("corrupt", encoding="utf-8")
    with pytest.raises(ConfigError, match="Resolved hash mismatch"):
        store.history()
    with pytest.raises(ConfigError, match="Resolved hash mismatch"):
        _accept(store, _resolved(display_name="three"), proposal=None, approval=None, expected=2)


def test_accept_compensates_source_and_generation_when_pointer_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original_source = store.read_source_text()
    real_atomic = ConfigGenerationStore._atomic_write

    def failing_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
        if path == store.accepted_pointer:
            raise OSError("injected pointer failure")
        real_atomic(path, data, mode=mode)

    monkeypatch.setattr(ConfigGenerationStore, "_atomic_write", staticmethod(failing_atomic))
    with pytest.raises(OSError, match="pointer failure"):
        _accept(store, _resolved(), proposal="initial", approval=_approval("initial"), expected=0)
    assert store.source_path.read_text(encoding="utf-8") == original_source
    assert not (store.generations / "1").exists()
    assert store.current() is None


def test_proposal_package_manager_choice_changes_executable_profile_and_id() -> None:
    facts = _facts(
        manifests=(ManifestFact("package.json", "javascript", "pnpm", True, ("test",)),),
        lockfiles=("pnpm-lock.yaml", "package-lock.json"),
        toolchain_declarations=(),
    )
    pending = build_repository_proposal(facts)
    assert "package_manager" in {item.code for item in pending.required_decisions}
    pnpm = build_repository_proposal(facts, decisions={"package_manager": "pnpm"})
    npm = build_repository_proposal(facts, decisions={"package_manager": "npm"})
    assert pnpm.proposal_id != npm.proposal_id
    assert pnpm.policy.profiles[0].commands[0][0] == "pnpm"
    assert npm.policy.profiles[0].commands[0][0] == "npm"


def test_scoped_monorepo_requires_consistent_scope_and_working_directory() -> None:
    facts = _facts(
        manifests=(
            ManifestFact("package.json", "javascript", "pnpm", True, ("lint",)),
            ManifestFact("apps/web/package.json", "javascript", "pnpm", False, ("test",)),
        ),
        workspace_packages=("apps/*",),
    )
    pending = build_repository_proposal(facts, decisions={"monorepo_scope": "scoped"})
    codes = {item.code for item in pending.required_decisions}
    assert {"working_directory_override", "allowed_paths_override"}.issubset(codes)
    with pytest.raises(ValueError, match="working_directory"):
        build_repository_proposal(
            facts,
            decisions={"monorepo_scope": "scoped", "dependency_install": "exclude"},
            overrides={"working_directory": "apps/web", "allowed_paths": "packages/api/**"},
        )
    proposal = build_repository_proposal(
        facts,
        decisions={"monorepo_scope": "scoped", "dependency_install": "exclude"},
        overrides={"working_directory": "apps/web", "allowed_paths": "apps/web/**"},
    )
    assert not proposal.required_decisions
    assert proposal.policy.allowed_paths == ("apps/web/**",)
    assert all(item.working_directory == "apps/web" for item in proposal.policy.profiles)
    assert any(
        command[1:] == ("run", "test") for p in proposal.policy.profiles for command in p.commands
    )


def test_truncated_scan_requires_explicit_repository_budget() -> None:
    proposal = build_repository_proposal(_facts(scan_truncated=True))
    assert "repository_budget" in {item.code for item in proposal.required_decisions}


def test_existing_policy_metadata_is_reported_without_executing_it() -> None:
    proposal = build_repository_proposal(_facts(policy_files=(".repoforge/policy.toml",)))
    finding = next(item for item in proposal.findings if item.code == "EXISTING_REPOFORGE_POLICY")
    assert finding.evidence == (".repoforge/policy.toml",)


def test_read_only_proposal_disables_profiles_and_remote_fetch() -> None:
    proposal = build_repository_proposal(
        _facts(remotes=()), decisions={"publish_remote": "read_only"}
    )
    assert proposal.policy.mode is EnrollmentMode.READ_ONLY
    assert proposal.policy.profiles == ()
    document = apply_proposal(parse_resolved(None), proposal)
    repo = document["repositories"]["demo"]
    assert repo["read_only"] is True
    assert repo["fetch_before_workspace"] is False


def test_source_configuration_round_trips_reviewed_decisions_and_overrides() -> None:
    source = SourceConfiguration(
        "tunnel",
        "repoforge",
        (
            SourceRepository(
                "demo",
                "/repo",
                "proposal",
                "strict",
                (("default_base", "main"),),
                (("allowed_paths", "src/**"),),
            ),
        ),
    )
    assert parse_source(render_source(source)) == source


class _NullAudit:
    path = Path("/tmp/audit")

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None:
        del action, success, details


class _NullFiles:
    pass


class _NullGit:
    pass


class _NullGithub:
    pass


class _NullCommands:
    def environment(self, extra=None):  # type: ignore[no-untyped-def]
        return {}


class _NullExecutables:
    pass


def test_read_only_repository_is_enforced_centrally_before_write_adapter(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        {"demo": RepositoryConfig("demo", repo_path, read_only=True)},
    )
    ctx = ApplicationContext(
        config,
        _NullCommands(),  # type: ignore[arg-type]
        _NullGit(),  # type: ignore[arg-type]
        _NullGithub(),  # type: ignore[arg-type]
        _NullFiles(),  # type: ignore[arg-type]
        InMemoryWorkspaceStore(),
        InMemoryLockManager(),
        InMemoryOperationGate(),
        _NullAudit(),
        FixedClock(),
        SequenceIdGenerator(),
        _NullExecutables(),  # type: ignore[arg-type]
        execution_coordinator_for_tests(),
    )
    with pytest.raises(SecurityError, match="read-only"):
        ctx.audited("workspace_write_file", {"repo_id": "demo"}, lambda: None)


def test_read_only_worktree_uses_local_base_without_fetch(tmp_path: Path) -> None:
    executor = ScriptedCommandExecutor()
    executor.enqueue(
        CommandResult(("git", "worktree"), str(tmp_path), 0, "", ""),
        CommandResult(("git", "rev-parse"), str(tmp_path), 0, "a" * 40 + "\n", ""),
    )
    server = ServerConfig(tmp_path / "workspaces", tmp_path / "state")
    git = GitCliRepository(executor, server)
    repo = RepositoryConfig(
        "demo",
        tmp_path,
        read_only=True,
        fetch_before_workspace=False,
        default_base="main",
    )
    assert git.create_worktree(repo, tmp_path / "workspace", "ai/read-only", "main") == "a" * 40
    assert executor.calls[0][-1] == "main"
    assert not any(call[:2] == ("git", "fetch") for call in executor.calls)


def test_tunnel_profile_fingerprint_includes_executable_version() -> None:
    base = TunnelProfile("a" * 64, "repoforge", "/bin/tunnel", "1.0", ("python", "-m", "x"))
    changed = replace(base, executable_version="2.0")
    assert base.fingerprint != changed.fingerprint


def test_redaction_removes_assignments_bearer_tokens_urls_and_explicit_values() -> None:
    secret = "super-secret-value"
    text = (
        f"CONTROL_PLANE_API_KEY={secret} Authorization: Bearer abc.def "
        "https://user:password@example.com"
    )
    redacted = redact_text(text, secrets=(secret,))
    assert secret not in redacted
    assert "abc.def" not in redacted
    assert "password@example" not in redacted
    assert redacted.count("<redacted>") >= 3


def test_runtime_store_persists_child_identity_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repoforge.adapters.runtime.state_store as state_store

    identities = {100: "a" * 64, 101: "b" * 64}
    monkeypatch.setattr(state_store, "process_identity", lambda pid: identities.get(pid))
    store = JsonRuntimeStore(tmp_path / "runtime.json")
    record = RuntimeRecord(
        1,
        RuntimePhase.HEALTHY,
        100,
        "a" * 64,
        1,
        1,
        "profile",
        "f" * 64,
        "t" * 64,
        "now",
        "now",
        "corr",
        child_pid=101,
        child_process_identity="b" * 64,
    )
    store.write(record)
    identities.pop(101)
    degraded = store.read()
    assert degraded is not None and degraded.phase is RuntimePhase.DEGRADED
    raw = json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8"))
    assert raw["phase"] == "degraded"
    assert raw["last_error_code"] == "CHILD_IDENTITY_MISMATCH"


def test_rendered_resolved_profile_keeps_working_directory() -> None:
    proposal = build_repository_proposal(
        _facts(
            manifests=(
                ManifestFact("package.json", "javascript", "pnpm", True, ("lint",)),
                ManifestFact("apps/web/package.json", "javascript", "pnpm", False, ("test",)),
            ),
            workspace_packages=("apps/*",),
        ),
        decisions={"monorepo_scope": "scoped", "dependency_install": "exclude"},
        overrides={"working_directory": "apps/web", "allowed_paths": "apps/web/**"},
    )
    document = apply_proposal(parse_resolved(None), proposal)
    text = render_resolved(
        document,
        generation=1,
        source_path="/config.toml",
        source_sha256="a" * 64,
        created_at="now",
        reason="test",
        proposal_id=proposal.proposal_id,
        repository_fingerprints=(("demo", proposal.facts_fingerprint),),
    )
    assert 'working_directory = "apps/web"' in text


def test_cli_preserves_legacy_config_option_order_and_diagnostics_output_dest() -> None:
    from repoforge.interfaces.cli.main import _normalize_global_config, build_parser

    assert _normalize_global_config(["start", "--config", "/tmp/config.toml"]) == [
        "--config",
        "/tmp/config.toml",
        "start",
    ]
    assert _normalize_global_config(["runtime", "--config=/tmp/config.toml", "status"]) == [
        "--config",
        "/tmp/config.toml",
        "runtime",
        "status",
    ]
    args = build_parser().parse_args(
        [
            "--config",
            "/tmp/config.toml",
            "--output",
            "human",
            "diagnostics",
            "bundle",
            "--output",
            "/tmp/bundle.json",
        ]
    )
    assert args.output == "human"
    assert args.bundle_output == "/tmp/bundle.json"
