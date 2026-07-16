from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment, git

from repoforge.domain.errors import SecurityError, WorkspaceError


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
