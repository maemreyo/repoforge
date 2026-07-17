"""Agent-facing configuration administration: patches, delta gating, approval flow."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from repoforge.adapters.configuration import ConfigGenerationStore
from repoforge.adapters.locking import FcntlLockManager
from repoforge.application.config_admin import ConfigAdminService
from repoforge.application.configuration.document import (
    apply_policy_patch,
    apply_proposal,
    apply_risk_policy,
    parse_resolved,
    render_resolved,
)
from repoforge.application.configuration.source import (
    SourceConfiguration,
    SourceRepository,
    parse_source,
    render_source,
)
from repoforge.application.repository_admin.proposals import RepositoryProposalService
from repoforge.bootstrap import (
    build_pending_policy_change_store,
    read_audit_events,
    read_runtime_log,
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
            ),
        ),
    )
    source_path.write_text(render_source(source), encoding="utf-8")
    store = ConfigGenerationStore(
        source_path, tmp_path / "state", FcntlLockManager(tmp_path / "locks")
    )
    proposal = _proposal(repo_root)
    document = apply_proposal(parse_resolved(None), proposal)
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
        return {"status": "hot_reloaded", "active_generation": generation}

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
        reload_runtime=reload_runtime,
        read_runtime_status=(lambda: dict(runtime_status)) if runtime_status is not None else None,
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
    # The durable source now carries the patch, so a later refresh preserves it.
    persisted = parse_source(admin._store.read_source_text())
    assert persisted.repositories[0].policy_patch.remove_profiles == ("quick",)


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
    # The unapproved patch is not persisted in the editable source.
    persisted = parse_source(admin._store.read_source_text())
    assert persisted.repositories[0].policy_patch.is_empty()


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


# ---------------------------------------------------------------------------
# CLI approve / reject / pending against the same store
# ---------------------------------------------------------------------------


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "_state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(cli, "_locks", lambda: FcntlLockManager(tmp_path / "locks"))


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
        assert {"config_inspect", "runtime_logs_read", "repo_policy_apply"} <= tools
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
        assert "client_capabilities" in inspected.structuredContent
        assert "features" in inspected.structuredContent["client_capabilities"]
        preview = await session.call_tool(
            "repo_policy_apply",
            {
                "repo_id": "demo",
                "set_profiles": [{"name": "debug", "commands": [["pnpm", "run", "debug:server"]]}],
                "dry_run": True,
            },
        )
        assert preview.isError is False
        assert preview.structuredContent is not None
        assert preview.structuredContent["status"] == "preview"
        assert preview.structuredContent["capability_delta"] == "expansion"
        execution_preview = await session.call_tool(
            "repo_policy_apply",
            {
                "repo_id": "demo",
                "execution_mode": "relaxed",
                "adhoc_runners": ["pnpm", "node"],
                "adhoc_timeout_seconds": 600,
                "dry_run": True,
            },
        )
        assert execution_preview.isError is False
        assert execution_preview.structuredContent is not None
        assert execution_preview.structuredContent["capability_delta"] == "expansion"
        applied = await session.call_tool(
            "repo_policy_apply",
            {"repo_id": "demo", "remove_profiles": ["quick"]},
        )
        assert applied.isError is False
        assert applied.structuredContent is not None
        assert applied.structuredContent["status"] == "applied"
        logs = await session.call_tool("runtime_logs_read", {"source": "runtime", "limit": 5})
        assert logs.isError is False
