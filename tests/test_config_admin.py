"""Agent-facing configuration administration: patches, delta gating, approval flow."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
import tomli as tomllib
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.configuration import ConfigGenerationStore
from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence.failure_output_artifact_store import (
    persist_failure_output,
)
from repoforge.application.config_admin import ConfigAdminService
from repoforge.application.configuration.document import (
    apply_auth_profiles,
    apply_policy_patch,
    apply_proposal,
    apply_risk_policy,
    apply_ticket_graph,
    parse_resolved,
    render_resolved,
)
from repoforge.application.configuration.source import (
    SourceAuthProfile,
    SourceConfiguration,
    SourceRepository,
    SourceTicketGraph,
    parse_source,
    render_source,
)
from repoforge.application.repository_admin.proposals import RepositoryProposalService
from repoforge.bootstrap import (
    build_pending_policy_change_store,
    read_audit_event_page,
    read_audit_events,
    read_runtime_log,
    read_runtime_log_page,
)
from repoforge.config import load_config
from repoforge.domain.config_generation import sha256_text
from repoforge.domain.errors import ConfigError
from repoforge.domain.policy_patch import (
    PolicyPatchError,
    ProfilePatch,
    RepositoryPolicyPatch,
)
from repoforge.domain.repository_detection import ManifestFact, RemoteFact, RepositoryFacts
from repoforge.domain.repository_proposal import EnrollmentMode
from repoforge.domain.runtime_contract import RuntimeContractIdentity
from repoforge.domain.verification_steps import (
    HygieneBaselinePolicy,
    VerificationStep,
    VerificationStepKind,
)
from repoforge.interfaces.mcp.server import create_server
from repoforge.testing import FixedClock, SequenceIdGenerator

cli = importlib.import_module("repoforge.interfaces.cli.main")

NOW = "2026-07-16T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Domain: RepositoryPolicyPatch validation and merge semantics
# ---------------------------------------------------------------------------


def _profile(
    name: str = "debug", commands: tuple[tuple[str, ...], ...] = (("echo", "ok"),)
) -> ProfilePatch:
    return ProfilePatch(name, "test profile", commands)


def test_profile_patch_validates_names_commands_and_bounds() -> None:
    with pytest.raises(PolicyPatchError, match="profile name"):
        ProfilePatch("bad name!", "", (("echo",),))
    with pytest.raises(PolicyPatchError, match="at least one command"):
        ProfilePatch("empty", "", ())
    with pytest.raises(PolicyPatchError, match="non-empty"):
        ProfilePatch("blank", "", (("",),))
    with pytest.raises(PolicyPatchError, match="timeout_seconds"):
        ProfilePatch("slow", "", (("echo",),), timeout_seconds=0)
    with pytest.raises(PolicyPatchError, match="safe relative path"):
        ProfilePatch("escape", "", (("echo",),), working_directory="../up")
    normalized = ProfilePatch("ok", "", (("echo",),), working_directory="apps/web/")
    assert normalized.working_directory == "apps/web"


def test_policy_patch_rejects_unrenderable_keys_and_conflicts() -> None:
    with pytest.raises(PolicyPatchError, match="execution_mode"):
        RepositoryPolicyPatch(execution_mode="unrestricted")
    with pytest.raises(PolicyPatchError, match="adhoc_runners"):
        RepositoryPolicyPatch(adhoc_runners=("../shell",))
    with pytest.raises(PolicyPatchError, match="adhoc_timeout_seconds"):
        RepositoryPolicyPatch(adhoc_timeout_seconds=0)
    with pytest.raises(PolicyPatchError, match="unrenderable or unsupported"):
        RepositoryPolicyPatch(diagnostics=(("d", {"argv": ["x"], "shell": "sh"}),))
    with pytest.raises(PolicyPatchError, match="check_argv"):
        RepositoryPolicyPatch(formatters=(("f", {"summary": "no check argv"}),))
    with pytest.raises(PolicyPatchError, match="both set and remove"):
        RepositoryPolicyPatch(profiles=(_profile("x"),), remove_profiles=("x",))
    with pytest.raises(PolicyPatchError, match="duplicate"):
        RepositoryPolicyPatch(profiles=(_profile("x"), _profile("x")))


def test_policy_patch_merge_layering() -> None:
    base = RepositoryPolicyPatch(profiles=(_profile("a"), _profile("b")), remove_profiles=("gone",))
    update = RepositoryPolicyPatch(
        profiles=(_profile("b", (("make", "b2"),)), _profile("c")),
        remove_profiles=("a",),
    )
    merged = base.merge(update)
    names = {profile.name: profile for profile in merged.profiles}
    assert set(names) == {"b", "c"}
    assert names["b"].commands == (("make", "b2"),)
    assert set(merged.remove_profiles) == {"gone", "a"}
    revived = merged.merge(RepositoryPolicyPatch(profiles=(_profile("a"),)))
    assert "a" not in revived.remove_profiles
    assert "a" in {profile.name for profile in revived.profiles}


def test_policy_patch_table_round_trip() -> None:
    patch = RepositoryPolicyPatch(
        profiles=(_profile("debug", (("uv", "run", "pytest", "-x"),)),),
        execution_mode="relaxed",
        adhoc_runners=("uv", "python3"),
        adhoc_timeout_seconds=600,
        diagnostics=(
            (
                "dx",
                {
                    "argv": ["echo", "{selector}"],
                    "selector_kind": "values",
                    "selector_values": ["a"],
                },
            ),
        ),
        formatters=(("fx", {"check_argv": ["ruff", "format", "--check"]}),),
        remove_profiles=("legacy",),
    )
    assert RepositoryPolicyPatch.from_table(patch.as_table()) == patch


# ---------------------------------------------------------------------------
# Source configuration round-trip
# ---------------------------------------------------------------------------


def test_source_round_trips_policy_patch() -> None:
    commands = (("ruff", "format", "--check", "."), ("pytest", "-q"))
    patch = RepositoryPolicyPatch(
        profiles=(
            ProfilePatch(
                "debug.v2",
                "test profile",
                commands,
                steps=(
                    VerificationStep(
                        "format",
                        VerificationStepKind.HYGIENE,
                        commands[0],
                    ),
                    VerificationStep(
                        "tests",
                        VerificationStepKind.BUSINESS_TESTS,
                        commands[1],
                    ),
                ),
                baseline_policy=HygieneBaselinePolicy.NO_REGRESSION,
            ),
        ),
        remove_diagnostics=("stale",),
    )
    config = SourceConfiguration(
        "tunnel", "repoforge", (SourceRepository("demo", "/tmp/demo", policy_patch=patch),)
    )
    text = render_source(config)
    assert parse_source(text) == config


def test_source_round_trips_generated_paths_metadata() -> None:
    text = """version = 2
[[repo]]
id = "demo"
path = "/tmp/demo"

[repositories.demo]
generated_paths = [
  { glob = "docs/contracts/*.json", regeneration_command = ["uv", "run", "python", "scripts/render_contract.py"], description = "Generated MCP contracts" },
]
"""

    parsed = parse_source(text)
    generated = parsed.repositories[0].generated_paths

    assert len(generated) == 1
    assert generated[0].glob == "docs/contracts/*.json"
    assert generated[0].regeneration_command == (
        "uv",
        "run",
        "python",
        "scripts/render_contract.py",
    )
    assert generated[0].description == "Generated MCP contracts"
    assert parse_source(render_source(parsed)) == parsed


def test_source_round_trips_issue_write_policy() -> None:
    text = """version = 2
[[repo]]
id = "demo"
path = "/tmp/demo"

[repositories.demo]
issue_writes = { enabled_ops = ["comment", "close", "create"], approval_required_ops = ["close"], max_writes_per_call = 3, max_writes_per_window = 12, window_seconds = 900, create_title_prefix = "[FOLLOWUP]", create_body_template = "## Objective\\n{body}\\n\\n## Evidence\\n{evidence_ref}" }
"""

    parsed = parse_source(text)
    policy = parsed.repositories[0].issue_writes

    assert policy.enabled_ops == ("comment", "close", "create")
    assert policy.approval_required_ops == ("close",)
    assert policy.max_writes_per_window == 12
    assert parse_source(render_source(parsed)) == parsed


def test_resolved_config_loads_generated_paths(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{repo}"
default_base = "main"
allowed_base_branches = ["main"]
generated_paths = [
  {{ glob = "docs/contracts/*.json", regeneration_command = ["python", "render.py"], description = "Generated contracts" }},
]
''',
        encoding="utf-8",
    )

    loaded = load_config(config_path).repositories["demo"].generated_paths

    assert loaded[0].glob == "docs/contracts/*.json"
    assert loaded[0].regeneration_command == ("python", "render.py")


def test_source_round_trips_ticket_graph_metadata() -> None:
    text = """version = 2
[[repo]]
id = "demo"
path = "/tmp/demo"

[repositories.demo.ticket_graph]
root_issue = 3
repository = "acme/demo"
project_owner = "acme"
project_number = 7
project_owner_type = "organization"
status_field = "Delivery Status"
priority_field = "Delivery Priority"
initiative_field = "Initiative"
type_field = "Ticket Type"
"""

    parsed = parse_source(text)
    graph = parsed.repositories[0].ticket_graph

    assert graph is not None
    assert graph.root_issue == 3
    assert graph.repository == "acme/demo"
    assert graph.project_owner == "acme"
    assert graph.project_number == 7
    assert graph.status_field == "Delivery Status"
    assert parse_source(render_source(parsed)) == parsed


def test_source_round_trips_risk_metadata() -> None:
    text = """version = 2
[[repo]]
id = "demo"
path = "/tmp/demo"

[repositories.demo.risk]
low_max = 20
medium_max = 45
high_max = 70
final_profile = "full"
ordered_profiles = ["quick", "full"]
narrow_diagnostics = ["pytest-target"]
critical_globs = ["src/**/security*.py"]
public_contract_globs = ["src/**/interfaces/**"]
manifest_globs = ["pyproject.toml"]
docs_globs = ["docs/**"]
"""

    parsed = parse_source(text)
    risk = parsed.repositories[0].risk_policy

    assert risk is not None
    assert risk.low_max == 20
    assert risk.ordered_profiles == ("quick", "full")
    assert risk.critical_globs == ("src/**/security*.py",)
    resolved = apply_risk_policy({"repositories": {"demo": {}}}, "demo", risk)
    assert resolved["repositories"]["demo"]["risk"] == risk.as_table()
    assert parse_source(render_source(parsed)) == parsed


def test_source_rejects_ticket_graph_for_unknown_repository() -> None:
    text = """version = 2
[[repo]]
id = "demo"
path = "/tmp/demo"

[repositories.missing.ticket_graph]
root_issue = 3
"""

    with pytest.raises(ValueError, match="unknown repository"):
        parse_source(text)


def test_source_rejects_invalid_policy_patch() -> None:
    text = (
        'version = 2\n[[repo]]\nid = "demo"\npath = "/tmp/demo"\n'
        '[repo.policy_patch.profiles.bad]\ndescription = "x"\n'
    )
    with pytest.raises(ValueError, match="policy_patch is invalid"):
        parse_source(text)


# ---------------------------------------------------------------------------
# Resolved document merge
# ---------------------------------------------------------------------------


def _facts(root: Path) -> RepositoryFacts:
    return RepositoryFacts(
        root=root,
        common_dir=root / ".git",
        repo_id="demo",
        display_name="demo",
        current_branch="main",
        default_branch_candidates=("main",),
        remotes=(RemoteFact("origin", "fetch", "push"),),
        manifests=(
            ManifestFact("package.json", "javascript", "pnpm", True, ("lint", "test", "build")),
        ),
        lockfiles=("pnpm-lock.yaml",),
        toolchain_declarations=("pnpm@10",),
        scripts=("lint", "test", "build"),
        make_targets=(),
        instruction_files=("README.md",),
        ci_files=(),
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
        existing_worktrees=(str(root),),
        policy_files=(),
        scan_truncated=False,
        warnings=(),
    )


class _FakeProbe:
    def __init__(self, root: Path) -> None:
        self._root = root

    def inspect(self, path: Path, *, repo_id: str | None = None) -> RepositoryFacts:
        return _facts(self._root)


def _proposal(root: Path) -> Any:
    return RepositoryProposalService(_FakeProbe(root)).propose(
        root,
        repo_id="demo",
        decisions={"dependency_install": "exclude"},
        template=EnrollmentMode.STANDARD,
        overrides={},
    )


def test_apply_policy_patch_sets_and_removes_entries(tmp_path: Path) -> None:
    proposal = _proposal(tmp_path / "demo")
    document = apply_proposal(parse_resolved(None), proposal)
    profiles = document["repositories"]["demo"]["profiles"]
    assert "full" in profiles
    patch = RepositoryPolicyPatch(
        profiles=(ProfilePatch("debug", "One-off", (("pnpm", "run", "lint"),)),),
        remove_profiles=("quick",),
        diagnostics=(("dx", {"argv": ["pnpm", "run", "test"]}),),
    )
    updated = apply_policy_patch(document, "demo", patch)
    repo = updated["repositories"]["demo"]
    assert "debug" in repo["profiles"] and "quick" not in repo["profiles"]
    assert repo["diagnostics"]["dx"]["argv"] == ["pnpm", "run", "test"]
    with pytest.raises(ValueError, match="Unknown repository id"):
        apply_policy_patch(document, "missing", patch)


def test_apply_policy_patch_repairs_default_verification_profile(tmp_path: Path) -> None:
    proposal = _proposal(tmp_path / "demo")
    document = apply_proposal(parse_resolved(None), proposal)
    repo = document["repositories"]["demo"]
    assert repo["default_verification_profile"] == "full"
    removed_all = apply_policy_patch(
        document,
        "demo",
        RepositoryPolicyPatch(remove_profiles=("full",)),
    )
    repo = removed_all["repositories"]["demo"]
    assert repo["default_verification_profile"] != "full"
    assert repo["default_verification_profile"] in repo["profiles"]


# ---------------------------------------------------------------------------
# ConfigAdminService gating pipeline against a real generation store
# ---------------------------------------------------------------------------


def _admin(
    tmp_path: Path,
    *,
    reload_calls: list[int] | None = None,
    runtime_status: dict[str, object] | None = None,
    contract_identity: RuntimeContractIdentity | None = None,
    latest_activation_receipt: object | None = None,
    ticket_graph: SourceTicketGraph | None = None,
    preserve_ticket_graph_in_resolved: bool = False,
    runtime_record: object | None = None,
    auth_profiles: tuple[SourceAuthProfile, ...] = (),
    mcp_connection_max_ttl_seconds: int | None = None,
) -> ConfigAdminService:
    repo_root = tmp_path / "demo"
    repo_root.mkdir(parents=True, exist_ok=True)
    source_path = tmp_path / "config.toml"
    source = SourceConfiguration(
        None,
        "repoforge",
        (
            SourceRepository(
                "demo",
                str(repo_root),
                decisions=(("dependency_install", "exclude"),),
                ticket_graph=ticket_graph,
            ),
        ),
        mcp_connection_max_ttl_seconds,
        auth_profiles,
    )
    source_path.write_text(render_source(source), encoding="utf-8")
    store = ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )
    proposal = _proposal(repo_root)
    document = apply_proposal(parse_resolved(None), proposal)
    # Acceptance projects reviewed auth profiles into the resolved generation, so a bootstrap
    # that declares them must start with them already accepted.
    document = apply_auth_profiles(document, auth_profiles)
    if preserve_ticket_graph_in_resolved:
        document = apply_ticket_graph(document, "demo", ticket_graph)
    resolved = render_resolved(
        document,
        generation=1,
        source_path=str(source_path),
        source_sha256=sha256_text(store.read_source_text()),
        created_at=NOW,
        reason="test bootstrap",
        proposal_id=None,
        repository_fingerprints=(("demo", proposal.facts_fingerprint),),
    )
    store.import_legacy(store.read_source_text(), resolved, created_at=NOW)

    def reload_runtime(generation: int) -> dict[str, Any]:
        if reload_calls is not None:
            reload_calls.append(generation)
        store.stage_activation(generation)
        store.activate(generation)
        return {"status": "hot_reloaded", "active_generation": generation}

    identity_options: dict[str, Any] = {}
    if contract_identity is not None:
        identity_options["contract_identity_provider"] = lambda: contract_identity
    if latest_activation_receipt is not None:
        identity_options["latest_activation_receipt"] = lambda: latest_activation_receipt
    return ConfigAdminService(
        store=store,
        proposals=RepositoryProposalService(_FakeProbe(repo_root)),
        clock=FixedClock(NOW),
        ids=SequenceIdGenerator(),
        pending=build_pending_policy_change_store(
            store.root, locks=FcntlLockManager(tmp_path / "locks")
        ),
        audit_log_path=tmp_path / "state" / "audit.jsonl",
        runtime_log_path=tmp_path / "state" / "managed-runtime.log",
        read_audit=read_audit_events,
        read_log=read_runtime_log,
        read_log_page=read_runtime_log_page,
        read_audit_page=read_audit_event_page,
        reload_runtime=reload_runtime,
        read_runtime_status=(lambda: dict(runtime_status)) if runtime_status is not None else None,
        runtime_record_provider=((lambda: runtime_record) if runtime_record is not None else None),
        **identity_options,
    )


def test_shared_approval_queue_migrates_legacy_payload_and_retains_decision(
    tmp_path: Path,
) -> None:
    approvals_module = importlib.import_module("repoforge.application.approvals")
    persistence_module = importlib.import_module(
        "repoforge.adapters.persistence.json_approval_store"
    )
    legacy_root = tmp_path / "state" / "pending-policy-changes"
    legacy_root.mkdir(parents=True)
    record = {
        "change_id": "chg-0123456789abcdef0123",
        "repo_id": "demo",
        "reason": "expand profile capability",
        "created_at": NOW,
        "capability_delta": "expansion",
        "changes": [{"path": "profiles.debug", "direction": "expansion"}],
        "source_text": "version = 2\n",
        "resolved_text": "schema_version = 3\n",
        "repository_fingerprints": [["demo", "f" * 64]],
        "expected_generation": 1,
        "expected_source_sha256": "a" * 64,
        "proposal_id": "chg-0123456789abcdef0123",
    }
    (legacy_root / "chg-0123456789abcdef0123.json").write_text(json.dumps(record), encoding="utf-8")
    locks = FcntlLockManager(tmp_path / "locks")
    approvals = persistence_module.JsonApprovalStore(tmp_path / "state", locks)
    payloads = persistence_module.JsonApprovalPayloadStore(tmp_path / "state", locks)

    queue = approvals_module.PendingPolicyChangeStore(
        approvals=approvals,
        payloads=payloads,
        legacy_root=legacy_root,
    )

    assert queue.summaries() == [
        {
            "change_id": record["change_id"],
            "repo_id": "demo",
            "reason": record["reason"],
            "created_at": NOW,
            "capability_delta": "expansion",
            "changes": record["changes"],
            "expected_generation": 1,
        }
    ]
    assert queue.load(record["change_id"]) == record
    request = approvals.read(record["change_id"])
    assert request is not None
    assert request.value.status.value == "pending"
    assert "source_text" not in json.dumps(request.value.summary(), sort_keys=True)
    assert not list(legacy_root.glob("*.json"))

    queue.reject(record["change_id"], actor="operator", decided_at=NOW)

    decided = approvals.read(record["change_id"])
    assert decided is not None
    assert decided.value.status.value == "declined"
    assert decided.value.decision is not None
    assert decided.value.decision.actor == "operator"
    assert payloads.read(record["change_id"]) is None
    assert queue.summaries() == []


def test_config_inspect_reports_source_ticket_graph_drift(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    source_path = tmp_path / "config.toml"
    source_path.write_text(
        source_path.read_text(encoding="utf-8")
        + '\n[repositories.demo.ticket_graph]\nroot_issue = 3\nrepository = "acme/demo"\n',
        encoding="utf-8",
    )

    inspected = admin.config_inspect("demo")["repositories"]["demo"]["ticket_graph"]

    assert inspected["source"]["root_issue"] == 3
    assert inspected["source"]["repository"] == "acme/demo"
    assert inspected["accepted"] is None
    assert inspected["drift"] == "source_only"


def test_config_inspect_includes_runtime_health_without_raw_runtime_state(tmp_path: Path) -> None:
    admin = _admin(
        tmp_path,
        runtime_status={
            "state": "healthy",
            "package_version_skew": False,
            "client_rediscovery_recommended": True,
        },
    )

    inspected = admin.config_inspect("demo")

    assert inspected["runtime_health"] == {
        "state": "healthy",
        "package_version_skew": False,
        "client_rediscovery_recommended": True,
    }


def test_restriction_is_applied_immediately_with_hot_reload(tmp_path: Path) -> None:
    reload_calls: list[int] = []
    admin = _admin(tmp_path, reload_calls=reload_calls)
    result = admin.repo_policy_apply("demo", remove_profiles=["quick"])
    assert result["status"] == "applied"
    assert result["capability_delta"] == "restriction"
    assert result["generation"] == 2
    assert reload_calls == [2]
    inspected = admin.config_inspect("demo")
    assert "quick" not in inspected["repositories"]["demo"]["profiles"]
    inspected_repo = inspected["repositories"]["demo"]
    assert inspected_repo["policy_patch"]["remove_profiles"] == ["quick"]
    assert inspected_repo["execution_mode"] == "strict"
    assert inspected_repo["adhoc_runners"] == []
    assert inspected_repo["adhoc_timeout_seconds"] == 300
    projection = admin.config_inspect_v2(repo_id="demo")["repository_projections"][0]
    assert projection["drift_reason"] == "intentionally_disabled"
    assert projection["capability_projection_status"] == "disabled"
    assert projection["active_generation"] == 2
    # The durable source now carries the patch, so a later refresh preserves it.
    persisted = parse_source(admin._store.read_source_text())
    assert persisted.repositories[0].policy_patch.remove_profiles == ("quick",)


def _auth_profile() -> SourceAuthProfile:
    return SourceAuthProfile(
        profile_id="personal",
        provider="github",
        credential_kind="stored_account",
        credential_reference="gh-account-personal",
        actor_class="human_operated",
        expected_actor_id="github-user-123",
        enabled=True,
        repository_id="987654",
        repository_patterns=("github.com/acme/*",),
        boundary_id="acme",
        capability_ids=("github.contents.write",),
        github_host="github.com",
        transport_kind="https",
        credential_fingerprint="a" * 64,
        allowed_access=("read", "write"),
        github_login="acme-operator",
        https_token_environment="REPOFORGE_GH_PERSONAL_TOKEN",
    )


def test_policy_apply_preserves_reviewed_auth_profiles(tmp_path: Path) -> None:
    admin = _admin(tmp_path, auth_profiles=(_auth_profile(),))

    result = admin.repo_policy_apply("demo", remove_profiles=["quick"])

    assert result["status"] == "applied"
    persisted = parse_source(admin._store.read_source_text())
    assert persisted.auth_profiles == (_auth_profile(),)
    resolved = tomllib.loads(admin._store.read_resolved_text(int(result["generation"])))
    assert resolved["auth_profiles"]["personal"]["github_login"] == "acme-operator"
    assert "credential_fingerprint" in resolved["auth_profiles"]["personal"]


def test_expansion_requires_operator_approval_and_never_applies(tmp_path: Path) -> None:
    reload_calls: list[int] = []
    admin = _admin(tmp_path, reload_calls=reload_calls)
    result = admin.repo_policy_apply(
        "demo",
        set_profiles=[
            {"name": "debug", "commands": [["pnpm", "run", "debug:server"]], "description": "d"}
        ],
    )
    assert result["status"] == "pending_approval"
    assert result["capability_delta"] == "expansion"
    change_id = result["change_id"]
    assert change_id.startswith("chg-")
    assert f"rf config approve {change_id}" in result["safe_next_action"]
    assert reload_calls == []
    assert admin._store.current().generation == 1
    pending = admin.pending.summaries()
    assert [item["change_id"] for item in pending] == [change_id]
    projection = admin.config_inspect_v2(repo_id="demo")["repository_projections"][0]
    assert projection["drift_reason"] == "pending_approval"
    assert projection["capability_projection_status"] == "pending"
    assert "pending configuration approval" in projection["safe_reconciliation_action"]
    # The unapproved patch is not persisted in the editable source.
    persisted = parse_source(admin._store.read_source_text())
    assert persisted.repositories[0].policy_patch.is_empty()


def test_denied_paths_remove_workflow_is_gated_as_expansion(tmp_path: Path) -> None:
    reload_calls: list[int] = []
    admin = _admin(tmp_path, reload_calls=reload_calls)
    result = admin.repo_policy_apply(
        "demo", policy_overrides={"denied_paths_remove": ".github/workflows/**"}
    )
    assert result["status"] == "pending_approval"
    assert result["capability_delta"] == "expansion"
    change_id = result["change_id"]
    assert f"rf config approve {change_id}" in result["safe_next_action"]
    assert reload_calls == []
    assert admin._store.current().generation == 1
    inspected = admin.config_inspect("demo")["repositories"]["demo"]
    assert ".github/workflows/**" in inspected["denied_paths"]


def test_denied_paths_remove_refuses_protected_default_denies(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    with pytest.raises(ConfigError, match="protected denied path"):
        admin.repo_policy_apply("demo", policy_overrides={"denied_paths_remove": ".git/**"})


def test_repo_policy_preview_token_binds_exact_apply_request(tmp_path: Path) -> None:
    reload_calls: list[int] = []
    admin = _admin(tmp_path, reload_calls=reload_calls)

    preview = admin.repo_policy(
        "demo",
        action="preview",
        mutations=[
            {
                "section": "profile",
                "name": "quick",
                "operation": "remove",
                "value": None,
            }
        ],
        generated_paths=[
            {
                "glob": "docs/contracts/*.json",
                "regeneration_command": ["python", "scripts/render_contract.py"],
                "description": "Generated contracts",
            }
        ],
        issue_writes={
            "enabled_ops": ["comment", "close"],
            "approval_required_ops": ["close"],
            "max_writes_per_call": 2,
            "max_writes_per_window": 20,
            "window_seconds": 3600,
            "create_title_prefix": "[TASK]",
            "create_body_template": "## Objective\n{body}\n\n## Evidence\n{evidence_ref}",
        },
    )

    assert preview["result"] == "preview"
    assert preview["preview_token"].startswith("apr-")
    assert preview["changes"]
    assert preview["issue_writes"]["enabled_ops"] == ["comment", "close"]
    assert admin._store.current().generation == 1

    applied = admin.repo_policy(
        "demo",
        action="apply",
        preview_token=preview["preview_token"],
    )

    assert applied["result"] in {"applied", "pending_approval"}
    assert applied["issue_writes"]["approval_required_ops"] == ["close"]
    assert admin.pending.payloads.read(preview["preview_token"]) is None


def test_repo_policy_preview_supports_override_removal(tmp_path: Path) -> None:
    admin = _admin(tmp_path)

    preview = admin.repo_policy(
        "demo",
        action="preview",
        mutations=[
            {
                "section": "override",
                "name": "dependency_install",
                "operation": "remove",
                "value": None,
            }
        ],
    )

    assert preview["result"] == "preview"
    assert preview["changes"] == [
        {
            "section": "override",
            "name": "dependency_install",
            "operation": "remove",
            "value": None,
        }
    ]


def test_repo_policy_rejects_stale_or_mismatched_preview_token(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    preview = admin.repo_policy(
        "demo",
        action="preview",
        mutations=[
            {
                "section": "profile",
                "name": "quick",
                "operation": "remove",
                "value": None,
            }
        ],
    )
    admin.repo_policy_apply("demo", remove_profiles=["quick"])

    with pytest.raises(ConfigError, match="stale"):
        admin.repo_policy("demo", action="apply", preview_token=preview["preview_token"])
    with pytest.raises(ConfigError, match="preview_token"):
        admin.repo_policy("other", action="apply", preview_token=preview["preview_token"])


def test_dry_run_previews_without_state_change(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    result = admin.repo_policy_apply(
        "demo",
        set_profiles=[{"name": "debug", "commands": [["pnpm", "run", "x"]]}],
        dry_run=True,
    )
    assert result["status"] == "preview"
    assert result["requires_operator_approval"] is True
    assert admin.pending.summaries() == []
    assert admin._store.current().generation == 1


def test_relaxed_execution_policy_previews_as_capability_expansion(tmp_path: Path) -> None:
    admin = _admin(tmp_path)

    result = admin.repo_policy_apply(
        "demo",
        execution_mode="relaxed",
        adhoc_runners=["pnpm", "node"],
        adhoc_timeout_seconds=600,
        dry_run=True,
    )

    assert result["status"] == "preview"
    assert result["capability_delta"] == "expansion"
    assert result["requires_operator_approval"] is True
    assert {change["path"] for change in result["changes"]} >= {
        "repositories.demo.execution_mode",
        "repositories.demo.adhoc_runners",
        "repositories.demo.adhoc_timeout_seconds",
    }
    assert admin.pending.summaries() == []
    assert admin._store.current().generation == 1


def test_apply_requires_at_least_one_change_and_known_repo(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    with pytest.raises(ConfigError, match="at least one change"):
        admin.repo_policy_apply("demo")
    with pytest.raises(ConfigError, match="Unknown repository id"):
        admin.repo_policy_apply("missing", remove_profiles=["quick"])
    with pytest.raises(ConfigError, match="Invalid policy patch"):
        admin.repo_policy_apply("demo", set_profiles=[{"name": "bad name!", "commands": [["x"]]}])


def test_invalid_candidate_fails_closed_before_accept(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    # A verification profile removal that leaves a broken diagnostic reference is caught by
    # load_config validation on the rendered candidate: use an invalid selector kind.
    with pytest.raises(ConfigError):
        admin.repo_policy_apply(
            "demo",
            set_diagnostics={"dx": {"argv": ["echo"], "selector_kind": "not-a-kind"}},
        )
    assert admin._store.current().generation == 1
    assert admin.pending.summaries() == []


def test_runtime_logs_read_bounds_sources_and_filters(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    audit_path = tmp_path / "state" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"action": "workspace_commit", "success": True, "details": {"duration_ms": 5}},
        {"action": "workspace_run_profile", "success": False, "details": {"duration_ms": 900}},
    ]
    audit_path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    (tmp_path / "state" / "managed-runtime.log").write_text("one\ntwo\n", encoding="utf-8")

    failed = admin.runtime_logs_read("audit", limit=10, only_failed=True)
    assert [item["action"] for item in failed["events"]] == ["workspace_run_profile"]
    runtime = admin.runtime_logs_read("runtime", limit=1)
    assert runtime["lines"] == ["two"]
    assert runtime["path"] == "managed-runtime.log"
    assert runtime["files"] == ["managed-runtime.log"]
    with pytest.raises(ConfigError, match="source"):
        admin.runtime_logs_read("secrets")
    with pytest.raises(ConfigError, match="limit"):
        admin.runtime_logs_read("audit", limit=0)


def test_runtime_logs_read_reports_legacy_and_malformed_without_epoch(tmp_path: Path) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    admin = _admin(tmp_path)
    log_path = tmp_path / "state" / "managed-runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('plain\n{"broken"\n', encoding="utf-8")

    result = admin.runtime_logs_read_v2(source="runtime", limit=10)

    V2_TOOL_SPECS["runtime_logs_read"].validate_output(result)
    assert all(entry["timestamp"] is None for entry in result["entries"])
    assert [entry["parse_state"] for entry in result["entries"]] == [
        "malformed_json",
        "legacy_plaintext",
    ]
    assert result["malformed_count"] == 1
    assert result["legacy_count"] == 1
    assert result["structured_count"] == 0
    assert result["correlated_count"] == 0
    assert result["timestamp_unavailable_count"] == 2
    assert result["source_truncated"] is False
    assert "1970-01-01" not in json.dumps(result, sort_keys=True)


def test_runtime_logs_read_preserves_structured_provenance_and_correlation(tmp_path: Path) -> None:
    from repoforge.domain.runtime_events import RuntimeEventV1, encode_runtime_event

    admin = _admin(tmp_path)
    log_path = tmp_path / "state" / "managed-runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        encode_runtime_event(
            RuntimeEventV1(
                observed_at="2026-07-21T12:00:00+00:00",
                component="tunnel_client",
                stream="stdout",
                level="ERROR",
                event_kind="response_failure",
                message="failed safely",
                action="workspace_push",
                duration_ms=12.5,
                correlation_id="corr-1",
                operation_id="op-1",
                receipt_id="receipt-1",
                trace_id="trace-1",
                workspace_hash="a" * 64,
                repository_hash="b" * 64,
            )
        )
        + "\nlegacy without time\n",
        encoding="utf-8",
    )

    result = admin.runtime_logs_read_v2(
        source="runtime",
        limit=10,
        only_failed=True,
        start_time="2026-07-21T00:00:00+00:00",
    )

    assert result["structured_count"] == 1
    assert result["legacy_count"] == 0
    assert result["correlated_count"] == 1
    assert result["timestamp_unavailable_count"] == 0
    assert result["source_truncated"] is False
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["timestamp_state"] == "observed"
    assert entry["parse_state"] == "structured_v1"
    assert entry["component"] == "tunnel_client"
    assert entry["stream"] == "stdout"
    assert entry["event_kind"] == "response_failure"
    assert entry["correlation_id"] == "corr-1"
    assert entry["operation_id"] == "op-1"
    assert entry["receipt_id"] == "receipt-1"
    assert entry["trace_id"] == "trace-1"
    assert entry["workspace_hash"] == "a" * 64
    assert entry["repository_hash"] == "b" * 64


def test_runtime_logs_read_bounds_untrusted_structured_metadata(tmp_path: Path) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    admin = _admin(tmp_path)
    log_path = tmp_path / "state" / "managed-runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "observed_at": "2026-07-21T12:00:00+00:00",
                "component": "c" * 500,
                "stream": "s" * 500,
                "level": "INFO",
                "event_kind": "e" * 500,
                "message": "bounded",
                "action": "a" * 500,
                "correlation_id": "x" * 500,
                "operation_id": "o" * 500,
                "receipt_id": "r" * 500,
                "trace_id": "t" * 500,
                "workspace_hash": "not-a-hash",
                "repository_hash": "also-not-a-hash",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = admin.runtime_logs_read_v2(source="runtime", limit=10)

    V2_TOOL_SPECS["runtime_logs_read"].validate_output(result)
    entry = result["entries"][0]
    assert len(entry["component"]) == 160
    assert len(entry["stream"]) == 80
    assert len(entry["event_kind"]) == 160
    assert len(entry["action"]) == 160
    assert len(entry["correlation_id"]) == 160
    assert entry["workspace_hash"] is None
    assert entry["repository_hash"] is None


def test_runtime_logs_read_reports_bounded_source_truncation(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    log_path = tmp_path / "state" / "managed-runtime.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("".join(f"line-{index}\n" for index in range(1_000)), encoding="utf-8")

    exact = admin.runtime_logs_read_v2(source="runtime", limit=10)

    assert exact["source_truncated"] is False
    assert exact["truncated"] is True  # more filtered entries remain for pagination

    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("line-1000\n")
    truncated = admin.runtime_logs_read_v2(source="runtime", limit=10)

    assert len(truncated["entries"]) == 10
    assert truncated["source_truncated"] is True
    assert truncated["truncated"] is True
    assert truncated["legacy_count"] == 10
    assert truncated["timestamp_unavailable_count"] == 10


def test_v2_config_inspect_is_compact_typed_and_redacts_host_paths(tmp_path: Path) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    admin = _admin(tmp_path, runtime_status={"socket_path": str(tmp_path / "secret.sock")})
    result = admin.config_inspect_v2(repo_id="demo", include_pending=True)

    V2_TOOL_SPECS["config_inspect"].validate_output(result)
    rendered = json.dumps(result, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert "source_path" not in rendered
    assert result["accepted"]["state"] == "accepted"
    assert result["accepted"]["digest"] == admin._store.current().resolved_sha256
    assert result["active"] is None
    assert result["restart_required"] is True
    assert result["repository_projections"][0]["drift_reason"] == "intentionally_disabled"
    assert result["repository_projections"][0]["capability_projection_status"] == "disabled"
    assert {item["key"] for item in result["repo_facts"]} >= {
        "repo_id",
        "read_only",
        "publish_enabled",
        "profile_count",
    }


def test_v2_config_inspect_reports_ticket_graph_projection_drift(
    tmp_path: Path,
) -> None:
    admin = _admin(
        tmp_path,
        ticket_graph=SourceTicketGraph(root_issue=232, repository="owner/demo"),
    )

    result = admin.config_inspect_v2(repo_id="demo")

    projection = result["repository_projections"][0]
    assert projection["repo_id"] == "demo"
    assert projection["source_ticket_graph"] == {
        "enabled": True,
        "root_issue": 232,
        "repository": "owner/demo",
    }
    assert projection["accepted_ticket_graph"]["enabled"] is False
    assert projection["active_ticket_graph"]["enabled"] is False
    assert projection["drift_reason"] == "projection_loss"
    assert projection["capability_projection_status"] == "unavailable"
    assert projection["safe_reconciliation_action"] == (
        "Regenerate the accepted configuration from source so repositories.demo.ticket_graph "
        "exactly matches the reviewed source declaration before activation."
    )


def test_v2_config_inspect_tracks_accepted_and_active_ticket_graph(
    tmp_path: Path,
) -> None:
    graph = SourceTicketGraph(root_issue=232, repository="owner/demo")
    admin = _admin(
        tmp_path,
        ticket_graph=graph,
        preserve_ticket_graph_in_resolved=True,
    )

    accepted = admin.config_inspect_v2(repo_id="demo")
    projection = accepted["repository_projections"][0]
    assert projection["accepted_ticket_graph"] == projection["source_ticket_graph"]
    assert projection["drift_reason"] == "accepted_not_active"
    assert projection["capability_projection_status"] == "pending"

    admin._store.stage_activation(1)
    admin._store.activate(1)
    active = admin.config_inspect_v2(repo_id="demo")
    active_projection = active["repository_projections"][0]
    assert active_projection["active_ticket_graph"] == active_projection["accepted_ticket_graph"]
    assert active_projection["drift_reason"] == "none"
    assert active_projection["capability_projection_status"] == "active"


def test_v2_config_inspect_exposes_contract_identity_and_projection_state(
    tmp_path: Path,
) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    identity = RuntimeContractIdentity(
        server_build_sha="a" * 64,
        server_version="2.2.0",
        active_generation=1,
        tool_surface_hash="b" * 64,
        input_contract_digest="c" * 64,
        output_contract_digest="d" * 64,
        runtime_protocol_version=1,
        process_start_identity="e" * 64,
    )
    admin = _admin(tmp_path, contract_identity=identity)

    initial = admin.config_inspect_v2(repo_id="demo")
    V2_TOOL_SPECS["config_inspect"].validate_output(initial)
    assert initial["contract_identity"] == identity.as_dict()
    assert (
        initial["config_projection"]["source_digest"]
        == initial["config_projection"]["accepted_source_digest"]
    )
    assert initial["config_projection"]["drift_state"] == "activation_required"
    assert initial["config_projection"]["safe_reconciliation_action"] == (
        "Activate accepted configuration generation 1."
    )

    admin._store.source_path.write_text(
        admin._store.read_source_text() + "\n# operator edit after acceptance\n",
        encoding="utf-8",
    )
    drifted = admin.config_inspect_v2(repo_id="demo")

    V2_TOOL_SPECS["config_inspect"].validate_output(drifted)
    assert drifted["config_projection"]["drift_state"] == "source_changed"
    assert drifted["repository_projections"][0]["drift_reason"] == "source_not_refreshed"
    assert drifted["repository_projections"][0]["capability_projection_status"] == "pending"
    assert (
        drifted["config_projection"]["source_digest"]
        != drifted["config_projection"]["accepted_source_digest"]
    )
    assert drifted["config_projection"]["safe_reconciliation_action"] == (
        "Review and accept a new configuration generation before activation."
    )
    assert str(tmp_path) not in json.dumps(drifted, sort_keys=True)


def test_v2_runtime_logs_support_time_range_cursor_and_no_host_paths(tmp_path: Path) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    admin = _admin(tmp_path)
    audit_path = tmp_path / "state" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "timestamp": "2026-07-16T00:00:00+00:00",
            "action": "workspace_status",
            "success": True,
            "details": {"duration_ms": 2, "path": str(tmp_path / "private")},
        },
        {
            "timestamp": "2026-07-16T00:01:00+00:00",
            "action": "workspace_verify",
            "success": False,
            "details": {"duration_ms": 20, "error_code": "COMMAND_FAILED"},
        },
        {
            "timestamp": "2026-07-16T00:02:00+00:00",
            "action": "workspace_commit",
            "success": True,
            "details": {"duration_ms": 10},
        },
    ]
    audit_path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")

    first = admin.runtime_logs_read_v2(
        source="audit",
        limit=1,
        start_time="2026-07-16T00:01:00+00:00",
        end_time="2026-07-16T00:02:00+00:00",
    )
    V2_TOOL_SPECS["runtime_logs_read"].validate_output(first)
    assert [item["action"] for item in first["entries"]] == ["workspace_commit"]
    assert first["next_cursor"] is not None
    second = admin.runtime_logs_read_v2(
        source="audit",
        limit=1,
        start_time="2026-07-16T00:01:00+00:00",
        end_time="2026-07-16T00:02:00+00:00",
        cursor=first["next_cursor"],
    )
    assert [item["action"] for item in second["entries"]] == ["workspace_verify"]
    assert str(tmp_path) not in json.dumps((first, second), sort_keys=True)

    runtime_path = tmp_path / "state" / "managed-runtime.log"
    runtime_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-16T00:03:00+00:00",
                "level": "INFO",
                "message": f"ready at {tmp_path / 'private.sock'} and C:\\Users\\alice\\token.txt",
            }
        )
        + "\n"
        + f"plain runtime at {tmp_path / 'plain.sock'}\n",
        encoding="utf-8",
    )
    runtime = admin.runtime_logs_read_v2(source="runtime", limit=10)
    V2_TOOL_SPECS["runtime_logs_read"].validate_output(runtime)
    messages = [item["message"] for item in runtime["entries"]]
    assert all("<redacted:host_path>" in message for message in messages)
    assert str(tmp_path) not in json.dumps(runtime, sort_keys=True)
    assert all("C:\\Users\\alice" not in message for message in messages)
    # A plain-text line has no readable timestamp, so it must report none rather than a
    # fabricated 1970 (which is indistinguishable from a real epoch-zero record).
    plain = next(item for item in runtime["entries"] if "plain runtime" in item["message"])
    assert plain["timestamp"] is None


def test_runtime_logs_read_understands_go_slog_records(tmp_path: Path) -> None:
    """The tunnel-client writes slog's `time`/`msg`; reading only `timestamp`/`message`
    turned every managed-runtime record into a 1970 timestamp with an empty message even
    though the log file was well-formed JSONL. Observed live: 100 such entries."""
    from repoforge.contracts.registry import V2_TOOL_SPECS

    admin = _admin(tmp_path)
    runtime_path = tmp_path / "state" / "managed-runtime.log"
    runtime_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.write_text(
        json.dumps(
            {
                "time": "2026-07-26T20:50:19.896816+07:00",
                "level": "INFO",
                "msg": "tunnel metadata fetched",
            }
        )
        + "\n"
        + json.dumps({"level": "WARN", "msg": "no clock in this record"})
        + "\n",
        encoding="utf-8",
    )

    result = admin.runtime_logs_read_v2(source="runtime", limit=10)

    V2_TOOL_SPECS["runtime_logs_read"].validate_output(result)
    entries = {item["message"]: item for item in result["entries"]}
    fetched = entries["tunnel metadata fetched"]
    assert fetched["timestamp"] == "2026-07-26T20:50:19.896816+07:00"
    assert fetched["level"] == "INFO"
    # A record genuinely missing its clock stays unknown instead of being invented.
    assert entries["no clock in this record"]["timestamp"] is None
    assert entries["no clock in this record"]["level"] == "WARN"
    # No entry may carry the old fabricated stamp.
    assert all(item["timestamp"] != "1970-01-01T00:00:00+00:00" for item in result["entries"])


def test_v2_runtime_logs_third_source_page(tmp_path: Path) -> None:
    from repoforge.contracts.registry import V2_TOOL_SPECS

    admin = _admin(tmp_path)
    artifact = persist_failure_output(
        tmp_path / "state",
        "first failure line\nsecond failure line\nthird failure line\n",
    )
    assert artifact.reference is not None

    first = admin.runtime_logs_read_v2(
        source="failure_artifact",
        artifact_reference=artifact.reference,
        limit=2,
    )
    V2_TOOL_SPECS["runtime_logs_read"].validate_output(first)
    assert first["source"] == "failure_artifact"
    assert [item["message"] for item in first["entries"]] == [
        "first failure line",
        "second failure line",
    ]
    assert first["truncated"] is True
    assert first["next_cursor"] is not None

    second = admin.runtime_logs_read_v2(
        source="failure_artifact",
        artifact_reference=artifact.reference,
        limit=2,
        cursor=first["next_cursor"],
    )
    assert [item["message"] for item in second["entries"]] == ["third failure line"]
    assert second["truncated"] is False
    assert second["next_cursor"] is None

    with pytest.raises(ConfigError, match="artifact_reference"):
        admin.runtime_logs_read_v2(source="failure_artifact")


def test_existing_artifact_identity(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    original = persist_failure_output(state_root, "immutable failure evidence\n")
    assert original.reference is not None
    digest = original.reference.removeprefix("failure-output:")
    target = state_root / "failure-output-artifacts" / f"{digest}.blob"
    target.write_text("tampered\n", encoding="utf-8")

    repeated = persist_failure_output(state_root, "immutable failure evidence\n")

    assert repeated.reference is None
    assert repeated.status == "persistence_failed"
    assert target.read_text(encoding="utf-8") == "tampered\n"


def test_v2_runtime_log_cursor_fails_closed_when_audit_snapshot_changes(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    audit_path = tmp_path / "state" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    initial = [
        {
            "timestamp": "2026-07-16T00:01:00+00:00",
            "action": "workspace_verify",
            "success": True,
            "details": {"duration_ms": 20},
        },
        {
            "timestamp": "2026-07-16T00:02:00+00:00",
            "action": "workspace_commit",
            "success": True,
            "details": {"duration_ms": 10},
        },
    ]
    audit_path.write_text(
        "\n".join(json.dumps(item) for item in initial) + "\n",
        encoding="utf-8",
    )
    first = admin.runtime_logs_read_v2(source="audit", limit=1)
    assert first["next_cursor"] is not None

    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-16T00:03:00+00:00",
                    "action": "workspace_push",
                    "success": True,
                    "details": {"duration_ms": 5},
                }
            )
            + "\n"
        )

    with pytest.raises(ConfigError, match=r"cursor.*stale"):
        admin.runtime_logs_read_v2(source="audit", limit=1, cursor=first["next_cursor"])


# ---------------------------------------------------------------------------
# CLI approve / reject / pending against the same store
# ---------------------------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # `*_` so the stub does not pin an exact arity: `_state_root` takes the config path
    # it is resolving for, and a stub that only accepts today's signature turns a
    # correct change to that resolution into a red test here.
    monkeypatch.setattr(cli, "_state_root", lambda *_: tmp_path / "state")
    monkeypatch.setattr(cli, "_locks", lambda *_: FcntlLockManager(tmp_path / "locks"))


def test_cli_approve_accepts_pending_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _admin(tmp_path)
    result = admin.repo_policy_apply(
        "demo",
        set_profiles=[{"name": "debug", "commands": [["pnpm", "run", "debug:server"]]}],
    )
    change_id = result["change_id"]
    _cli_env(monkeypatch, tmp_path)
    code = cli._config_approve(
        tmp_path / "config.toml",
        argparse.Namespace(change_id=change_id, activate="never"),
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "approved"
    assert payload["generation"]["generation"] == 2
    assert payload["generation"]["approval"]["actor"]
    assert admin.pending.summaries() == []
    inspected = admin.config_inspect("demo")
    assert "debug" in inspected["repositories"]["demo"]["profiles"]
    persisted = parse_source(admin._store.read_source_text())
    assert {profile.name for profile in persisted.repositories[0].policy_patch.profiles} == {
        "debug"
    }


def test_cli_approve_discards_stale_pending_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _admin(tmp_path)
    pending = admin.repo_policy_apply(
        "demo",
        set_profiles=[{"name": "debug", "commands": [["pnpm", "run", "debug:server"]]}],
    )
    # A concurrent restriction moves the accepted generation forward first.
    applied = admin.repo_policy_apply("demo", remove_profiles=["quick"])
    assert applied["status"] == "applied"
    _cli_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="stale"):
        cli._config_approve(
            tmp_path / "config.toml",
            argparse.Namespace(change_id=pending["change_id"], activate="never"),
        )
    assert admin.pending.summaries() == []


def test_cli_pending_and_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    admin = _admin(tmp_path)
    result = admin.repo_policy_apply(
        "demo",
        set_profiles=[{"name": "debug", "commands": [["pnpm", "run", "debug:server"]]}],
    )
    _cli_env(monkeypatch, tmp_path)
    assert cli._config_pending(tmp_path / "config.toml") == 0
    listing = json.loads(capsys.readouterr().out)
    assert [item["change_id"] for item in listing["pending_changes"]] == [result["change_id"]]
    assert cli._config_reject(tmp_path / "config.toml", result["change_id"]) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert admin.pending.summaries() == []
    assert admin._store.current().generation == 1
    with pytest.raises(ConfigError, match="Unknown pending policy change"):
        cli._config_reject(tmp_path / "config.toml", result["change_id"])


# ---------------------------------------------------------------------------
# MCP protocol surface
# ---------------------------------------------------------------------------


class _FakeCodingService:
    def __getattr__(self, name: str) -> Any:
        if name.startswith(("repo_", "workspace_", "operation_")):
            return lambda *args, **kwargs: {"name": name}
        raise AttributeError(name)


@pytest.mark.anyio
async def test_config_admin_tools_are_registered_and_fail_closed_without_admin() -> None:
    server = create_server(service=_FakeCodingService())  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"config_inspect", "runtime_logs_read", "repo_policy"} <= tools
        result = await session.call_tool("config_inspect", {})
    assert result.isError is True
    rendered = "\n".join(
        item.text for item in result.content if getattr(item, "type", None) == "text"
    )
    assert "CONFIG_ADMIN_UNAVAILABLE" in rendered


@pytest.mark.anyio
async def test_config_admin_tools_round_trip_through_protocol(tmp_path: Path) -> None:
    admin = _admin(tmp_path)
    server = create_server(service=_FakeCodingService(), admin=admin)  # type: ignore[arg-type]
    async with create_connected_server_and_client_session(server) as session:
        inspected = await session.call_tool("config_inspect", {"repo_id": "demo"})
        assert inspected.isError is False
        assert inspected.structuredContent is not None
        assert inspected.structuredContent["accepted"]["generation"] == 1
        assert inspected.structuredContent["repo_facts"]

        mutation = {"section": "profile", "name": "quick", "operation": "remove"}
        preview = await session.call_tool(
            "repo_policy",
            {"repo_id": "demo", "action": "preview", "mutations": [mutation]},
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        assert preview.structuredContent["result"] == "preview"
        preview_token = preview.structuredContent["preview_token"]
        assert isinstance(preview_token, str)

        applied = await session.call_tool(
            "repo_policy",
            {
                "repo_id": "demo",
                "action": "apply",
                "preview_token": preview_token,
            },
        )
        applied_error = json.loads(applied.content[0].text) if applied.isError else None
        assert applied.isError is False, applied_error["error"]["message"]
        assert applied.structuredContent is not None
        assert applied.structuredContent["result"] == "applied"

        logs = await session.call_tool("runtime_logs_read", {"source": "runtime", "limit": 5})
        assert logs.isError is False


def _identity() -> RuntimeContractIdentity:
    return RuntimeContractIdentity(
        server_build_sha="a" * 64,
        server_version="2.2.0",
        active_generation=1,
        tool_surface_hash="b" * 64,
        input_contract_digest="c" * 64,
        output_contract_digest="d" * 64,
        runtime_protocol_version=1,
        process_start_identity="e" * 64,
    )


def _activation_receipt(
    *,
    generation: int,
    tool_surface_hash: str,
    process_identity: str | None,
    runtime_generation: int | None = -1,
):
    from repoforge.domain.runtime_activation import (
        RuntimeActivationClassification,
        RuntimeActivationIdentity,
        RuntimeActivationReceipt,
    )

    identity = RuntimeActivationIdentity(
        config_generation=generation,
        source_sha256="a" * 64,
        resolved_sha256="b" * 64,
        runtime_active_generation=(generation if runtime_generation == -1 else runtime_generation),
        process_identity=process_identity,
        tool_surface_hash=tool_surface_hash,
        runtime_phase="healthy",
    )
    return RuntimeActivationReceipt(
        receipt_id="receipt-" + "1" * 24,
        operation_id="op-" + "2" * 24,
        classification=RuntimeActivationClassification.HOT_RELOAD,
        target_generation=generation,
        accepted_identity=identity,
        previous_identity=None,
        active_identity=identity,
        continuation_reference=None,
        correlation_id="c" * 24,
        effect_boundary_crossed=True,
        accepted_at=NOW,
        updated_at=NOW,
    )


def test_config_inspect_corroborates_activation_against_the_live_identity(
    tmp_path: Path,
) -> None:
    """An activation command's own report cannot corroborate itself.

    A release has already been observed reporting `converged: true` minutes
    before its runtime turned out to be unusable, so `config_inspect` reads the
    persisted receipt and compares it against the identity the live process
    advertises.
    """
    identity = _identity()
    admin = _admin(
        tmp_path,
        contract_identity=identity,
        latest_activation_receipt=_activation_receipt(
            generation=identity.active_generation,
            tool_surface_hash=identity.tool_surface_hash,
            process_identity=identity.process_start_identity,
        ),
    )

    evidence = admin.config_inspect_v2()["activation_evidence"]

    assert evidence["agreement"] == "matches"
    assert evidence["receipt_id"] == "receipt-" + "1" * 24
    assert evidence["activated_tool_surface_hash"] == identity.tool_surface_hash
    assert evidence["effect_boundary_crossed"] is True


def test_config_inspect_reports_an_activation_that_did_not_end_where_it_claimed(
    tmp_path: Path,
) -> None:
    """The case worth catching: the runtime serves a different generation than the
    receipt activated. Not a different process instance, and not a different release --
    both of those change legitimately (#314)."""
    identity = _identity()
    admin = _admin(
        tmp_path,
        contract_identity=identity,
        latest_activation_receipt=_activation_receipt(
            generation=identity.active_generation,
            tool_surface_hash=identity.tool_surface_hash,
            process_identity=identity.process_start_identity,
            runtime_generation=identity.active_generation + 1,
        ),
    )

    evidence = admin.config_inspect_v2()["activation_evidence"]

    assert evidence["agreement"] == "diverged"


def test_a_restart_since_activation_is_a_fact_not_a_divergence(tmp_path: Path) -> None:
    """#314: the live installation reported `diverged` while perfectly healthy.

    The receipt records the identity of one process instance and one release. A watchdog
    restart changes the first and a later release activation changes the second, so
    comparing them called a converged runtime a failed activation -- and it cost real time
    while triaging an unrelated fault. Both changes are reported, as facts.
    """
    identity = _identity()
    admin = _admin(
        tmp_path,
        contract_identity=identity,
        latest_activation_receipt=_activation_receipt(
            generation=identity.active_generation,
            # What the receipt captured before the release upgrade and the restart;
            # the live identity is `_identity()`'s surface "b"*64 / process "e"*64.
            tool_surface_hash="f" * 64,
            process_identity="9" * 64,
        ),
    )

    evidence = admin.config_inspect_v2()["activation_evidence"]

    assert evidence["agreement"] == "matches", "same generation is still the same activation"
    assert evidence["process_restarted_since_activation"] is True
    assert evidence["tool_surface_changed_since_activation"] is True


def test_an_untouched_runtime_reports_no_restart_and_no_surface_change(
    tmp_path: Path,
) -> None:
    identity = _identity()
    admin = _admin(
        tmp_path,
        contract_identity=identity,
        latest_activation_receipt=_activation_receipt(
            generation=identity.active_generation,
            tool_surface_hash=identity.tool_surface_hash,
            process_identity=identity.process_start_identity,
        ),
    )

    evidence = admin.config_inspect_v2()["activation_evidence"]

    assert evidence["agreement"] == "matches"
    assert evidence["process_restarted_since_activation"] is False
    assert evidence["tool_surface_changed_since_activation"] is False


def test_config_inspect_says_unverifiable_rather_than_assuming_success(
    tmp_path: Path,
) -> None:
    """A receipt that never recorded an activated identity proves nothing.

    Reporting `matches` here would manufacture agreement out of missing data,
    which is the failure mode this field exists to prevent.
    """
    identity = _identity()
    admin = _admin(
        tmp_path,
        contract_identity=identity,
        latest_activation_receipt=_activation_receipt(
            generation=identity.active_generation,
            tool_surface_hash=identity.tool_surface_hash,
            process_identity=None,
            runtime_generation=None,
        ),
    )

    evidence = admin.config_inspect_v2()["activation_evidence"]
    assert evidence["agreement"] == "unverifiable"
    # An unknown process identity is reported as unknown, never as "did not change".
    assert evidence["process_restarted_since_activation"] is None
    # No receipt store wired at all is reported as absent, not as agreement.
    assert (
        _admin(tmp_path, contract_identity=identity).config_inspect_v2()["activation_evidence"]
        is None
    )


# ------------------- what an agent can see about runtime health (config_inspect_v2)


def _health_record(**overrides: object):
    from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

    defaults: dict[str, object] = {
        "protocol_version": 1,
        "phase": RuntimePhase.HEALTHY,
        "pid": 100,
        "process_identity": "a" * 64,
        "active_generation": 1,
        "accepted_generation": 1,
        "tunnel_profile": "repoforge",
        "tunnel_profile_fingerprint": "b" * 64,
        "tool_surface_hash": "c" * 64,
        "started_at": NOW,
        "updated_at": NOW,
        "correlation_id": "d" * 24,
        "child_pid": 200,
        "child_process_identity": "e" * 64,
        "health": (("control_plane", True, "control socket is accepting requests"),),
        "health_observed_at": NOW,
    }
    defaults.update(overrides)
    return RuntimeRecord(**defaults)  # type: ignore[arg-type]


def test_agent_facing_inspect_reports_live_runtime_health(tmp_path: Path) -> None:
    """The CLI saw runtime health and the agent did not.

    `config_inspect_v2` -- what the MCP tool calls -- carried only
    `activation_evidence.runtime_phase`, a word captured in a receipt when a release was
    activated. It said `healthy` throughout the 2026-07-28 incident, hours after the fact,
    while the connector was torn down twice.
    """
    admin = _admin(tmp_path, runtime_record=_health_record())

    health = admin.config_inspect_v2()["runtime_health"]

    assert health["phase"] == "healthy"
    assert health["checks"] == [
        {"name": "control_plane", "ok": True, "detail": "control socket is accepting requests"}
    ]
    assert health["observed_age_seconds"] == 0.0


def test_agent_facing_inspect_ages_the_health_observation(tmp_path: Path) -> None:
    """The field that separates `healthy and quiet` from `the watchdog stopped`.

    The watchdog writes only on change, so the phase alone cannot express the difference.
    """
    stale = "2026-07-15T00:00:00+00:00"  # a day before the fixed clock
    admin = _admin(tmp_path, runtime_record=_health_record(health_observed_at=stale))

    health = admin.config_inspect_v2()["runtime_health"]

    assert health["observed_at"] == stale
    assert health["observed_age_seconds"] == 86_400.0


def test_agent_facing_inspect_reports_restart_evidence(tmp_path: Path) -> None:
    """`restart_count` resets after a stable interval; these do not."""
    admin = _admin(
        tmp_path,
        runtime_record=_health_record(
            restart_count=0, restarts_total=2, last_restart_at="2026-07-28T17:15:55+00:00"
        ),
    )

    health = admin.config_inspect_v2()["runtime_health"]

    assert health["restarts_total"] == 2
    assert health["last_restart_at"] == "2026-07-28T17:15:55+00:00"


def test_agent_facing_inspect_omits_health_when_no_runtime_record_exists(tmp_path: Path) -> None:
    assert _admin(tmp_path).config_inspect_v2()["runtime_health"] is None


# ---------------------------------------------------------------------------
# Ad-hoc execution policy through the reviewed generation pipeline
# ---------------------------------------------------------------------------


def test_repo_policy_can_propose_an_adhoc_runner(tmp_path: Path) -> None:
    """An agent that needs a runner asks for it here rather than working around it.

    Every layer under this already supported execution policy; only the v2 surface had
    no field reaching it, so the one reviewed path to a new runner was an operator
    hand-editing config.
    """
    admin = _admin(tmp_path)

    preview = admin.repo_policy(
        "demo",
        action="preview",
        execution={
            "execution_mode": "relaxed",
            "adhoc_runners": ["uv", "bash"],
            "adhoc_timeout_seconds": 900,
        },
    )

    assert preview["result"] == "preview"
    assert preview["execution"] == {
        "execution_mode": "relaxed",
        "adhoc_runners": ["uv", "bash"],
        "adhoc_timeout_seconds": 900,
    }

    applied = admin.repo_policy(
        "demo",
        action="apply",
        preview_token=preview["preview_token"],
    )

    # Widening execution policy is an EXPANSION, so it is never applied on the model's
    # authority: it waits for `rf config approve`.
    assert applied["result"] == "pending_approval"
    assert applied["execution"]["adhoc_runners"] == ["uv", "bash"]


def test_repo_policy_can_propose_an_adhoc_shell_runner(tmp_path: Path) -> None:
    """Review finding: #377 threaded adhoc_shell_runners through config.py,
    policy_patch.py, config-generation classification, and config_admin's own
    canonicalization -- but never added it to the public v2 ExecutionPolicyDeclaration,
    so the one reviewed path to enabling the shell form was still an operator
    hand-editing config, exactly the gap adhoc_runners itself already had and was
    already fixed for."""
    admin = _admin(tmp_path)

    preview = admin.repo_policy(
        "demo",
        action="preview",
        execution={
            "execution_mode": "relaxed",
            "adhoc_runners": ["uv"],
            "adhoc_shell_runners": ["sh", "bash"],
        },
    )

    assert preview["result"] == "preview"
    assert preview["execution"]["adhoc_shell_runners"] == ["sh", "bash"]

    applied = admin.repo_policy(
        "demo",
        action="apply",
        preview_token=preview["preview_token"],
    )

    assert applied["result"] == "pending_approval"
    assert applied["execution"]["adhoc_shell_runners"] == ["sh", "bash"]


def test_repo_policy_refuses_a_shell_runner_the_config_loader_would_reject(
    tmp_path: Path,
) -> None:
    admin = _admin(tmp_path)

    with pytest.raises(ConfigError, match="basename"):
        admin.repo_policy(
            "demo",
            action="preview",
            execution={"adhoc_shell_runners": ["/bin/sh"]},
        )


def test_repo_policy_refuses_a_runner_the_config_loader_would_reject(tmp_path: Path) -> None:
    """The surface must not accept a runner that `domain.adhoc` refuses later."""
    admin = _admin(tmp_path)

    with pytest.raises(ConfigError, match="basename"):
        admin.repo_policy(
            "demo",
            action="preview",
            execution={"adhoc_runners": ["/usr/bin/python3"]},
        )


def test_repo_policy_refuses_an_empty_execution_declaration(tmp_path: Path) -> None:
    admin = _admin(tmp_path)

    with pytest.raises(ConfigError, match="at least one field"):
        admin.repo_policy("demo", action="preview", execution={})


def test_repo_policy_apply_refuses_execution_alongside_a_token(tmp_path: Path) -> None:
    """Apply acts on the exact previewed proposal, never on a re-sent declaration."""
    admin = _admin(tmp_path)
    preview = admin.repo_policy(
        "demo",
        action="preview",
        execution={"adhoc_timeout_seconds": 600},
    )

    with pytest.raises(ConfigError, match="only the exact preview_token"):
        admin.repo_policy(
            "demo",
            action="apply",
            preview_token=preview["preview_token"],
            execution={"adhoc_timeout_seconds": 30},
        )
