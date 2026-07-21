from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git

from repoforge.adapters.filesystem.transaction import JournaledFileTransaction
from repoforge.application.service import CodingService
from repoforge.application.workspace.refresh_v2 import WorkspaceRefreshV2
from repoforge.domain.errors import SecurityError, WorkspaceError
from repoforge.domain.generated_paths import GeneratedPathRule, valid_regenerated_paths


def _clone_publisher(env: ForgeEnvironment, name: str = "publisher") -> Path:
    publisher = env.root / name
    git("clone", "--branch", "main", str(env.remote), str(publisher), cwd=env.root)
    git("config", "user.name", "Upstream Maintainer", cwd=publisher)
    git("config", "user.email", "upstream@example.test", cwd=publisher)
    return publisher


def _push_upstream_file(
    env: ForgeEnvironment,
    relative_path: str,
    content: str,
    message: str,
    *,
    publisher_name: str = "publisher",
) -> str:
    publisher = _clone_publisher(env, publisher_name)
    target = publisher / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git("add", relative_path, cwd=publisher)
    git("commit", "-m", message, cwd=publisher)
    git("push", "origin", "main", cwd=publisher)
    return git("rev-parse", "HEAD", cwd=publisher)


def _commit_workspace_hello(env: ForgeEnvironment, workspace_id: str, content: str) -> str:
    current = env.service.workspace_read_file(workspace_id, "hello.txt")
    env.service.workspace_write_file(
        workspace_id,
        "hello.txt",
        content,
        str(current["sha256"]),
    )
    env.service.workspace_run_profile(workspace_id)
    return str(env.service.workspace_commit(workspace_id, "change workspace hello")["head_sha"])


def _install_hello_generator(
    env: ForgeEnvironment,
    *,
    side_effect: bool = False,
    nondeterministic: bool = False,
    source_content: str = "generated from source\n",
) -> None:
    scripts = env.source / "scripts"
    scripts.mkdir(exist_ok=True)
    (env.source / "hello.source").write_text(source_content, encoding="utf-8")
    if nondeterministic:
        script = (
            "from pathlib import Path\n"
            "from time import time_ns\n"
            "Path('hello.txt').write_text(str(time_ns()) + '\\n', encoding='utf-8')\n"
        )
    else:
        script = (
            "from pathlib import Path\n"
            "Path('hello.txt').write_text(Path('hello.source').read_text(encoding='utf-8'), "
            "encoding='utf-8')\n"
        )
    if side_effect:
        script += "Path('README.md').write_text('unexpected side effect\\n', encoding='utf-8')\n"
    (scripts / "render_hello.py").write_text(script, encoding="utf-8")
    git("add", "hello.source", "scripts/render_hello.py", cwd=env.source)
    git("commit", "-m", "add reviewed hello generator", cwd=env.source)
    git("push", "origin", "main", cwd=env.source)


def test_base_status_distinguishes_current_remote_local_and_diverged_states(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "base states")["workspace_id"])
    initial = service.workspace_base_status(workspace_id)
    initial_sha = str(initial["workspace_base_sha"])

    assert initial["staleness"] == "current"
    assert initial["local_base_sha"] == initial_sha
    assert initial["remote_base_sha"] == initial_sha
    assert initial["remote_available"] is True

    remote_sha = _push_upstream_file(
        forge_env,
        "upstream.txt",
        "remote change\n",
        "advance remote",
    )
    remote_stale = service.workspace_base_status(workspace_id)

    assert remote_stale["staleness"] == "remote_base_stale"
    assert remote_stale["local_base_sha"] == initial_sha
    assert remote_stale["remote_base_sha"] == remote_sha
    assert remote_stale["behind_base"] == 1
    assert remote_stale["upstream_changed_paths"] == ["upstream.txt"]

    git("merge", "--ff-only", "origin/main", cwd=forge_env.source)
    local_stale = service.workspace_base_status(workspace_id)
    assert local_stale["staleness"] == "local_base_stale"
    assert local_stale["local_base_sha"] == remote_sha
    assert local_stale["remote_base_sha"] == remote_sha

    divergent_root = forge_env.root / "divergent"
    divergent_root.mkdir()
    divergent_env = create_forge_environment(divergent_root)
    divergent_workspace = str(
        divergent_env.service.workspace_create("demo", "diverged base")["workspace_id"]
    )
    (divergent_env.source / "local-only.txt").write_text("local\n", encoding="utf-8")
    git("add", "local-only.txt", cwd=divergent_env.source)
    git("commit", "-m", "local base commit", cwd=divergent_env.source)
    _push_upstream_file(
        divergent_env,
        "remote-only.txt",
        "remote\n",
        "remote base commit",
    )

    diverged = divergent_env.service.workspace_base_status(divergent_workspace)
    assert diverged["staleness"] == "diverged"
    assert diverged["local_base_sha"] != diverged["remote_base_sha"]


def test_base_status_reports_remote_outage_without_losing_last_known_state(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "remote outage")["workspace_id"])
    initial = service.workspace_base_status(workspace_id)
    git("remote", "set-url", "origin", str(forge_env.root / "missing.git"), cwd=forge_env.source)

    unavailable = service.workspace_base_status(workspace_id)

    assert unavailable["staleness"] == "unavailable_remote"
    assert unavailable["remote_available"] is False
    assert unavailable["remote_base_sha"] == initial["remote_base_sha"]
    assert unavailable["remote_error_code"] == "REMOTE_BASE_UNAVAILABLE"


def test_preview_is_read_only_and_refresh_creates_a_controlled_merge_commit(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "safe refresh")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    service.workspace_push(workspace_id)
    target_sha = _push_upstream_file(
        forge_env,
        "upstream.txt",
        "upstream\n",
        "add upstream file",
    )
    before = service.workspace_status(workspace_id)

    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    after_preview = service.workspace_status(workspace_id)
    assert after_preview["head_sha"] == before["head_sha"]
    assert after_preview["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert preview["strategy"] == "merge_no_ff"
    assert preview["target_base_sha"] == target_sha
    assert preview["predicted_conflict_paths"] == []
    assert preview["refreshable"] is True

    refreshed = service.workspace_refresh(
        workspace_id,
        str(preview["preview_id"]),
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    assert refreshed["status"] == "refreshed"
    assert refreshed["head_sha"] != before["head_sha"]
    assert refreshed["workspace_base_sha"] == target_sha
    assert refreshed["conflict_paths"] == []
    assert refreshed["force_push_required"] is False
    assert (workspace_path / "upstream.txt").read_text(encoding="utf-8") == "upstream\n"

    with pytest.raises(WorkspaceError, match="verified commit gate"):
        service.workspace_push(workspace_id)

    verified = service.workspace_run_profile(workspace_id)
    assert verified["satisfies_commit_gate"] is True
    adopted = service.workspace_commit(workspace_id, "approve refreshed merge")
    assert adopted["head_sha"] == refreshed["head_sha"]
    pushed = service.workspace_push(workspace_id)
    assert pushed["head_sha"] == refreshed["head_sha"]


def test_v2_refresh_integrates_trusted_upstream_template_without_resolution(
    forge_env: ForgeEnvironment,
) -> None:
    base_repo = forge_env.service.config.repositories["demo"]
    guarded_repo = replace(
        base_repo,
        denied_paths=(*base_repo.denied_paths, ".env.*", "**/.env.*"),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": guarded_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "trusted denied-path refresh")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    target_sha = _push_upstream_file(
        forge_env,
        ".env.example",
        "APP_MODE=example\n",
        "add reviewed environment template",
    )
    status = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
    )

    assert preview["conflicts"] == []
    applied = service.workspace_refresh_v2(
        workspace_id,
        action="apply",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
        plan_token=str(preview["plan_token"]),
        resolutions=[],
    )

    assert applied["result"] == "applied"
    assert applied["target_base_sha"] == target_sha
    assert (workspace_path / ".env.example").read_text(encoding="utf-8") == ("APP_MODE=example\n")
    with pytest.raises(SecurityError, match="Path is denied"):
        service.workspace_read_file(workspace_id, ".env.example")


def test_v2_refresh_preview_rejects_policy_blocked_conflict(
    forge_env: ForgeEnvironment,
) -> None:
    base_repo = forge_env.service.config.repositories["demo"]
    guarded_repo = replace(
        base_repo,
        denied_paths=(*base_repo.denied_paths, ".env.*", "**/.env.*"),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": guarded_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "denied conflict preview")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    (workspace_path / ".env.example").write_text("WORKSPACE=1\n", encoding="utf-8")
    git("add", ".env.example", cwd=workspace_path)
    git("commit", "-m", "workspace environment template", cwd=workspace_path)
    _push_upstream_file(
        forge_env,
        ".env.example",
        "UPSTREAM=1\n",
        "upstream environment template",
    )
    status = service.workspace_status(workspace_id)

    with pytest.raises(SecurityError, match="denied repository paths"):
        service.workspace_refresh_v2(
            workspace_id,
            action="preview",
            expected_head_sha=str(status["head_sha"]),
            expected_fingerprint=str(status["workspace_fingerprint"]),
        )


def test_refresh_preview_becomes_stale_after_workspace_or_remote_base_change(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "stale preview")["workspace_id"])
    _push_upstream_file(forge_env, "one.txt", "one\n", "first upstream")
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "workspace changed\n",
        str(hello["sha256"]),
    )
    with pytest.raises(WorkspaceError, match="STALE_REFRESH_PREVIEW"):
        service.workspace_refresh(
            workspace_id,
            str(preview["preview_id"]),
            str(before["head_sha"]),
            str(before["workspace_fingerprint"]),
        )

    changed_root = forge_env.root / "remote-change"
    changed_root.mkdir()
    changed_env = create_forge_environment(changed_root)
    changed_workspace = str(
        changed_env.service.workspace_create("demo", "remote changed")["workspace_id"]
    )
    _push_upstream_file(changed_env, "one.txt", "one\n", "first upstream")
    stable = changed_env.service.workspace_status(changed_workspace)
    remote_preview = changed_env.service.workspace_refresh_preview(
        changed_workspace,
        str(stable["head_sha"]),
        str(stable["workspace_fingerprint"]),
    )
    _push_upstream_file(
        changed_env,
        "two.txt",
        "two\n",
        "second upstream",
        publisher_name="publisher-two",
    )

    with pytest.raises(WorkspaceError, match="STALE_REFRESH_PREVIEW"):
        changed_env.service.workspace_refresh(
            changed_workspace,
            str(remote_preview["preview_id"]),
            str(stable["head_sha"]),
            str(stable["workspace_fingerprint"]),
        )


def test_v2_preview_uses_committed_head_even_when_worktree_is_dirty(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "dirty preview parity")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    clean = service.workspace_status(workspace_id)
    clean_preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(clean["head_sha"]),
        expected_fingerprint=str(clean["workspace_fingerprint"]),
    )

    workspace_path = Path(str(service.workspace_status(workspace_id)["path"]))
    (workspace_path / "scratch.txt").write_text("dirty but irrelevant\n", encoding="utf-8")
    dirty = service.workspace_status(workspace_id)
    dirty_preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(dirty["head_sha"]),
        expected_fingerprint=str(dirty["workspace_fingerprint"]),
    )

    assert clean_preview["prediction_scope"] == "committed_head"
    assert dirty_preview["prediction_scope"] == "committed_head"
    assert dirty_preview["plan_hash"] == clean_preview["plan_hash"]
    assert dirty_preview["conflicts"] == clean_preview["conflicts"]
    assert dirty_preview["apply_blockers"] == ["working_tree_not_clean"]


def test_v2_preview_returns_typed_three_way_conflict_evidence(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "typed conflict evidence")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    status = service.workspace_status(workspace_id)

    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
    )

    assert preview["result"] == "preview"
    assert preview["plan_token"].startswith("refresh-v2:")
    assert preview["conflicts"] == [
        {
            "path": "hello.txt",
            "kind": "content",
            "base": "hello\n",
            "ours": "changed locally\n",
            "theirs": "changed remotely\n",
            "content_truncated": False,
            "next_action": "Provide one reviewed resolution for this path.",
            "regeneration_command": [],
        }
    ]


def test_v2_generated_conflict_is_regenerated_without_hand_resolution(
    forge_env: ForgeEnvironment,
) -> None:
    _install_hello_generator(forge_env)
    configured = forge_env.service.config.repositories["demo"]
    generated_repo = replace(
        configured,
        generated_paths=(
            GeneratedPathRule(
                "hello.txt",
                ("python3", "scripts/render_hello.py"),
                "Generated hello fixture",
            ),
        ),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": generated_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "generated conflict")
    workspace_id = str(created["workspace_id"])
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed locally\n",
        str(current["sha256"]),
    )
    service.workspace_run_profile(workspace_id)
    service.workspace_commit(workspace_id, "change generated output locally")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "change generated output upstream",
    )
    status = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
    )

    conflict = preview["conflicts"][0]
    assert conflict["kind"] == "generated"
    assert conflict["regeneration_command"] == ["python3", "scripts/render_hello.py"]
    assert preview["conflict_scope"] == "generated"
    assert preview["semantic_conflict_count"] == 0
    assert preview["generated_conflict_count"] == 1

    applied = service.workspace_refresh_v2(
        workspace_id,
        action="apply",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
        plan_token=str(preview["plan_token"]),
        resolutions=[],
    )

    workspace_path = Path(str(created["path"]))
    assert applied["result"] == "applied"
    assert applied["warnings"] == []
    assert applied["verify_selector"] == ["hello.txt"]
    receipt = applied["regeneration_receipts"][0]
    assert receipt["commands"] == [["python3", "scripts/render_hello.py"]]
    assert receipt["generated_paths"] == ["hello.txt"]
    assert receipt["deterministic"] is True
    assert len(receipt["source_identity"]) == 64
    assert len(receipt["output_identity"]) == 64
    assert applied["source_change_metrics"]["changed_files"] == 0
    assert applied["generated_change_metrics"]["changed_files"] == 1
    assert applied["generated_change_metrics"]["binary_files"] == 0
    durable_receipts = service.state.load(workspace_id).metadata["generated_path_receipts_v1"]
    assert durable_receipts[-1]["refresh_commit_sha"] == applied["head_sha"]
    assert durable_receipts[-1]["target_base_sha"] == applied["target_base_sha"]
    assert durable_receipts[-1]["plan_hash"] == applied["plan_hash"]
    assert durable_receipts[-1]["output_identity"] == receipt["output_identity"]
    assert valid_regenerated_paths(
        workspace_path,
        generated_repo.generated_paths,
        durable_receipts,
    ) == frozenset({"hello.txt"})
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "generated from source\n"
    (workspace_path / "hello.txt").write_text("manual edit\n", encoding="utf-8")
    assert (
        valid_regenerated_paths(
            workspace_path,
            generated_repo.generated_paths,
            durable_receipts,
        )
        == frozenset()
    )


def test_v2_refresh_resolves_semantic_source_before_regenerating_output(
    forge_env: ForgeEnvironment,
) -> None:
    _install_hello_generator(forge_env)
    configured = forge_env.service.config.repositories["demo"]
    generated_repo = replace(
        configured,
        generated_paths=(
            GeneratedPathRule(
                "hello.txt",
                ("python3", "scripts/render_hello.py"),
                "Generated hello fixture",
            ),
        ),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": generated_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "mixed generated conflict")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    (workspace_path / "hello.txt").write_text("changed locally\n", encoding="utf-8")
    (workspace_path / "hello.source").write_text("local source\n", encoding="utf-8")
    git("add", "hello.txt", "hello.source", cwd=workspace_path)
    git("commit", "-m", "change generated output and source locally", cwd=workspace_path)
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "change generated output upstream",
    )
    _push_upstream_file(
        forge_env,
        "hello.source",
        "remote source\n",
        "change source input upstream",
        publisher_name="publisher-source",
    )
    status = service.workspace_status(workspace_id)

    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
    )

    assert preview["conflict_scope"] == "mixed"
    assert preview["semantic_conflict_paths"] == ["hello.source"]
    assert preview["generated_conflict_paths"] == ["hello.txt"]

    applied = service.workspace_refresh_v2(
        workspace_id,
        action="apply",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
        plan_token=str(preview["plan_token"]),
        resolutions=[{"path": "hello.source", "content": "resolved source\n"}],
    )

    assert applied["result"] == "applied"
    assert applied["warnings"] == []
    assert applied["verify_selector"] == ["hello.source", "hello.txt"]
    assert applied["source_change_metrics"]["changed_files"] == 1
    assert applied["generated_change_metrics"]["changed_files"] == 1
    assert (workspace_path / "hello.source").read_text(encoding="utf-8") == "resolved source\n"
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "resolved source\n"


def test_v2_refresh_recovers_crash_before_regeneration(
    forge_env: ForgeEnvironment,
) -> None:
    _install_hello_generator(forge_env)
    configured = forge_env.service.config.repositories["demo"]
    generated_repo = replace(
        configured,
        generated_paths=(
            GeneratedPathRule(
                "hello.txt",
                ("python3", "scripts/render_hello.py"),
                "Generated hello fixture",
            ),
        ),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": generated_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "crash before regeneration")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    (workspace_path / "hello.txt").write_text("changed locally\n", encoding="utf-8")
    (workspace_path / "hello.source").write_text("local source\n", encoding="utf-8")
    git("add", "hello.txt", "hello.source", cwd=workspace_path)
    git("commit", "-m", "change generated output and source locally", cwd=workspace_path)
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "change generated output upstream",
    )
    _push_upstream_file(
        forge_env,
        "hello.source",
        "remote source\n",
        "change source input upstream",
        publisher_name="publisher-crash-source",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )

    def crash(point: str) -> None:
        if point == "after_semantic_resolutions":
            raise KeyboardInterrupt("simulated crash before regeneration")

    service._refresh_v2 = WorkspaceRefreshV2(
        service.application.context,
        fault_injector=crash,
    )
    with pytest.raises(KeyboardInterrupt, match="before regeneration"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[{"path": "hello.source", "content": "resolved source\n"}],
        )

    service._refresh_v2 = WorkspaceRefreshV2(service.application.context)
    recovered = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )
    restored = service.workspace_status(workspace_id)
    assert recovered["plan_hash"] == preview["plan_hash"]
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True
    assert (workspace_path / "hello.source").read_text(encoding="utf-8") == "local source\n"
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "changed locally\n"


def test_v2_generated_output_does_not_consume_source_budget(
    forge_env: ForgeEnvironment,
) -> None:
    _install_hello_generator(forge_env, source_content="x" * 4096 + "\n")
    configured = forge_env.service.config.repositories["demo"]
    generated_repo = replace(
        configured,
        max_total_changed_bytes=32,
        generated_paths=(
            GeneratedPathRule(
                "hello.txt",
                ("python3", "scripts/render_hello.py"),
                "Generated hello fixture",
            ),
        ),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": generated_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "large generated output")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    (workspace_path / "hello.txt").write_text("changed locally\n", encoding="utf-8")
    git("add", "hello.txt", cwd=workspace_path)
    git("commit", "-m", "change generated output locally", cwd=workspace_path)
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "change generated output upstream",
    )
    status = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
    )

    applied = service.workspace_refresh_v2(
        workspace_id,
        action="apply",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
        plan_token=str(preview["plan_token"]),
        resolutions=[],
    )

    assert applied["source_change_metrics"]["total_current_bytes"] == 0
    assert applied["generated_change_metrics"]["total_current_bytes"] > 32
    assert (workspace_path / "hello.txt").stat().st_size > 32


def test_v2_refresh_rejects_generator_side_effects_and_rolls_back(
    forge_env: ForgeEnvironment,
) -> None:
    _install_hello_generator(forge_env, side_effect=True)
    configured = forge_env.service.config.repositories["demo"]
    generated_repo = replace(
        configured,
        generated_paths=(
            GeneratedPathRule(
                "hello.txt",
                ("python3", "scripts/render_hello.py"),
                "Generated hello fixture",
            ),
        ),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": generated_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "generator side effect")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    (workspace_path / "hello.txt").write_text("changed locally\n", encoding="utf-8")
    git("add", "hello.txt", cwd=workspace_path)
    git("commit", "-m", "change generated output locally", cwd=workspace_path)
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "change generated output upstream",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )

    with pytest.raises(SecurityError, match="undeclared or unstaged changes"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[],
        )

    restored = service.workspace_status(workspace_id)
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "changed locally\n"
    assert (workspace_path / "README.md").read_text(encoding="utf-8").startswith("# Demo")


def test_v2_refresh_rejects_nondeterministic_regeneration_and_rolls_back(
    forge_env: ForgeEnvironment,
) -> None:
    _install_hello_generator(forge_env, nondeterministic=True)
    configured = forge_env.service.config.repositories["demo"]
    generated_repo = replace(
        configured,
        generated_paths=(
            GeneratedPathRule(
                "hello.txt",
                ("python3", "scripts/render_hello.py"),
                "Generated hello fixture",
            ),
        ),
    )
    config = replace(
        forge_env.service.config,
        repositories={**forge_env.service.config.repositories, "demo": generated_repo},
    )
    service = CodingService(config)
    created = service.workspace_create("demo", "nondeterministic generator")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    (workspace_path / "hello.txt").write_text("changed locally\n", encoding="utf-8")
    git("add", "hello.txt", cwd=workspace_path)
    git("commit", "-m", "change generated output locally", cwd=workspace_path)
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "change generated output upstream",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )

    with pytest.raises(WorkspaceError, match="nondeterministic"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[],
        )

    restored = service.workspace_status(workspace_id)
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "changed locally\n"


def test_v2_refresh_apply_requires_exact_resolution_set(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "exact resolutions")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    status = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
    )

    with pytest.raises(WorkspaceError, match="exactly one resolution"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(status["head_sha"]),
            expected_fingerprint=str(status["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[],
        )

    applied = service.workspace_refresh_v2(
        workspace_id,
        action="apply",
        expected_head_sha=str(status["head_sha"]),
        expected_fingerprint=str(status["workspace_fingerprint"]),
        plan_token=str(preview["plan_token"]),
        resolutions=[{"path": "hello.txt", "content": "reviewed combined result\n"}],
    )
    workspace_path = Path(str(service.workspace_status(workspace_id)["path"]))

    assert applied["result"] == "applied"
    assert applied["changed_paths"] == ["hello.txt"]
    assert applied["verify_selector"] == ["hello.txt"]
    assert applied["transaction_id"]
    assert applied["workspace_fingerprint"] != status["workspace_fingerprint"]
    assert (workspace_path / "hello.txt").read_text(
        encoding="utf-8"
    ) == "reviewed combined result\n"
    assert (
        git("rev-list", "--parents", "-n", "1", "HEAD", cwd=workspace_path).split().__len__() == 3
    )


def test_v2_refresh_registry_failure_restores_exact_reviewed_state(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "v2 registry rollback")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )
    original_save = service.state.save

    def fail_refresh_save(record: object) -> None:
        metadata = getattr(record, "metadata", {})
        if isinstance(metadata, dict) and "last_refresh_target_sha" in metadata:
            raise OSError("simulated registry failure")
        original_save(record)  # type: ignore[arg-type]

    monkeypatch.setattr(service.state, "save", fail_refresh_save)
    with pytest.raises(WorkspaceError, match="reviewed Git and registry state was restored"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[{"path": "hello.txt", "content": "reviewed resolution\n"}],
        )

    restored = service.workspace_status(workspace_id)
    workspace_path = Path(service.state.load(workspace_id).path)
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "changed locally\n"
    assert not (forge_env.root / "state" / "workspace-refresh-transactions" / workspace_id).exists()


def test_v2_refresh_recovers_prepared_merge_after_process_crash(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "v2 crash rollback")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )

    def crash(point: str) -> None:
        if point == "after_merge_started":
            raise KeyboardInterrupt("simulated process death")

    service._refresh_v2 = WorkspaceRefreshV2(
        service.application.context,
        fault_injector=crash,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process death"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[{"path": "hello.txt", "content": "reviewed resolution\n"}],
        )

    journal = forge_env.root / "state" / "workspace-refresh-transactions" / workspace_id
    assert journal.exists()
    service._refresh_v2 = WorkspaceRefreshV2(service.application.context)
    recovered = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )
    restored = service.workspace_status(workspace_id)

    assert recovered["plan_hash"] == preview["plan_hash"]
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True
    assert not journal.exists()


def test_v2_refresh_recovers_inner_resolution_journal_before_reset(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "v2 nested crash rollback")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )

    def crash(point: str) -> None:
        if point == "after_apply:0":
            raise KeyboardInterrupt("simulated nested transaction death")

    service._refresh_v2 = WorkspaceRefreshV2(
        service.application.context,
        file_fault_injector=crash,
    )
    with pytest.raises(KeyboardInterrupt, match="nested transaction death"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
            resolutions=[{"path": "hello.txt", "content": "reviewed resolution\n"}],
        )

    workspace_path = Path(service.state.load(workspace_id).path)
    service._refresh_v2 = WorkspaceRefreshV2(service.application.context)
    recovered = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )
    restored = service.workspace_status(workspace_id)

    assert recovered["plan_hash"] == preview["plan_hash"]
    assert JournaledFileTransaction(workspace_path).pending_transactions() == ()
    assert (workspace_path / "hello.txt").read_text(encoding="utf-8") == "changed locally\n"
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    audit_events = [
        json.loads(line)
        for line in (forge_env.root / "state" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    recovery_preview = [event for event in audit_events if event["action"] == "workspace_refresh"][
        -1
    ]
    assert recovery_preview["details"]["action"] == "preview"
    assert recovery_preview["details"]["recovery_pending"] is True
    assert recovery_preview["details"]["is_mutating"] is True


def test_v2_refresh_finalizes_committed_journal_after_process_crash(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "v2 crash finalize")["workspace_id"])
    _push_upstream_file(
        forge_env,
        "upstream.txt",
        "upstream\n",
        "non-conflicting upstream change",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(before["head_sha"]),
        expected_fingerprint=str(before["workspace_fingerprint"]),
    )

    def crash(point: str) -> None:
        if point == "after_commit_marker":
            raise KeyboardInterrupt("simulated post-commit process death")

    service._refresh_v2 = WorkspaceRefreshV2(
        service.application.context,
        fault_injector=crash,
    )
    with pytest.raises(KeyboardInterrupt, match="post-commit"):
        service.workspace_refresh_v2(
            workspace_id,
            action="apply",
            expected_head_sha=str(before["head_sha"]),
            expected_fingerprint=str(before["workspace_fingerprint"]),
            plan_token=str(preview["plan_token"]),
        )

    journal = forge_env.root / "state" / "workspace-refresh-transactions" / workspace_id
    committed = service.workspace_status(workspace_id)
    assert committed["head_sha"] != before["head_sha"]
    assert journal.exists()

    service._refresh_v2 = WorkspaceRefreshV2(service.application.context)
    finalized = service.workspace_refresh_v2(
        workspace_id,
        action="preview",
        expected_head_sha=str(committed["head_sha"]),
        expected_fingerprint=str(committed["workspace_fingerprint"]),
    )
    workspace_path = Path(service.state.load(workspace_id).path)

    assert finalized["result"] == "preview"
    assert (workspace_path / "upstream.txt").read_text(encoding="utf-8") == "upstream\n"
    assert not journal.exists()


def test_conflicting_refresh_returns_exact_paths_and_restores_original_state(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "conflict refresh")["workspace_id"])
    _commit_workspace_hello(forge_env, workspace_id, "changed locally\n")
    _push_upstream_file(
        forge_env,
        "hello.txt",
        "changed remotely\n",
        "conflicting upstream change",
    )
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    assert preview["predicted_conflict_paths"] == ["hello.txt"]
    result = service.workspace_refresh(
        workspace_id,
        str(preview["preview_id"]),
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )
    restored = service.workspace_status(workspace_id)

    assert result["status"] == "conflict"
    assert result["conflict_paths"] == ["hello.txt"]
    assert result["recovered"] is True
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True


def test_successful_refresh_invalidates_all_current_receipts(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "receipt invalidation")["workspace_id"])
    _push_upstream_file(forge_env, "upstream.txt", "upstream\n", "advance upstream")
    record = service.state.load(workspace_id)
    record.metadata.update(
        {
            "verified_commit_sha": "a" * 40,
            "verification_profile": "full",
            "verification_completed_at": "2026-07-14T00:00:00+00:00",
            "assessment_receipt": "assessment-1",
            "architecture_receipt": "architecture-1",
            "execution_plan_id": "plan-1",
        }
    )
    service.state.save(record)
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    result = service.workspace_refresh(
        workspace_id,
        str(preview["preview_id"]),
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )
    fresh = service.state.load(workspace_id)

    assert set(result["invalidated_receipts"]) >= {
        "verification",
        "assessment",
        "architecture",
        "execution_plan",
    }
    assert fresh.last_verification is None
    for key in (
        "verified_commit_sha",
        "verification_profile",
        "verification_completed_at",
        "assessment_receipt",
        "architecture_receipt",
        "execution_plan_id",
    ):
        assert key not in fresh.metadata


def test_rename_delete_conflict_is_predicted_and_aborted(forge_env: ForgeEnvironment) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "rename delete conflict")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    git("mv", "hello.txt", "local-hello.txt", cwd=workspace_path)
    git("commit", "-m", "rename hello locally", cwd=workspace_path)

    publisher = _clone_publisher(forge_env)
    git("rm", "hello.txt", cwd=publisher)
    git("commit", "-m", "delete hello upstream", cwd=publisher)
    git("push", "origin", "main", cwd=publisher)

    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )
    assert preview["predicted_conflict_paths"]

    result = service.workspace_refresh(
        workspace_id,
        str(preview["preview_id"]),
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )
    restored = service.workspace_status(workspace_id)

    assert result["status"] == "conflict"
    assert result["conflict_paths"] == preview["predicted_conflict_paths"]
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True


def test_refresh_rolls_back_when_registry_save_fails_and_audits_failure(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = str(service.workspace_create("demo", "refresh rollback")["workspace_id"])
    target_sha = _push_upstream_file(
        forge_env,
        "upstream.txt",
        "upstream\n",
        "advance upstream",
    )
    before = service.workspace_status(workspace_id)
    original_record = service.state.load(workspace_id)
    original_base_sha = original_record.metadata["workspace_base_sha"]
    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    def fail_save(_record: object) -> None:
        raise OSError("simulated registry failure")

    monkeypatch.setattr(service.state, "save", fail_save)
    with pytest.raises(WorkspaceError, match="registry update failed; Git state was restored"):
        service.workspace_refresh(
            workspace_id,
            str(preview["preview_id"]),
            str(before["head_sha"]),
            str(before["workspace_fingerprint"]),
        )

    restored = service.workspace_status(workspace_id)
    persisted = service.state.load(workspace_id)
    assert restored["head_sha"] == before["head_sha"]
    assert restored["workspace_fingerprint"] == before["workspace_fingerprint"]
    assert restored["clean"] is True
    assert persisted.metadata["workspace_base_sha"] == original_base_sha

    audit_path = forge_env.root / "state" / "audit.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    refresh_events = [event for event in events if event["action"] == "workspace_refresh"]
    assert refresh_events[-1]["success"] is False
    assert refresh_events[-1]["details"]["target_base_sha"] == target_sha


def test_refresh_rejects_a_workspace_registered_on_a_protected_branch(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    created = service.workspace_create("demo", "protected refresh")
    workspace_id = str(created["workspace_id"])
    workspace_path = Path(str(created["path"]))
    _push_upstream_file(forge_env, "upstream.txt", "upstream\n", "advance upstream")
    before = service.workspace_status(workspace_id)
    preview = service.workspace_refresh_preview(
        workspace_id,
        str(before["head_sha"]),
        str(before["workspace_fingerprint"]),
    )

    git("checkout", "--detach", cwd=forge_env.source)
    git("branch", "-D", "main", cwd=forge_env.source)
    git("branch", "-m", "main", cwd=workspace_path)
    record = service.state.load(workspace_id)
    record.branch = "main"
    service.state.save(record)

    with pytest.raises(SecurityError, match="Branch must start"):
        service.workspace_refresh(
            workspace_id,
            str(preview["preview_id"]),
            str(before["head_sha"]),
            str(before["workspace_fingerprint"]),
        )
    assert not (workspace_path / "upstream.txt").exists()
