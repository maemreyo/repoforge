from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.errors import CommandError


def run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def test_workspace_edit_verify_and_commit(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", str(remote), cwd=tmp_path)

    source = tmp_path / "source"
    run("git", "clone", str(remote), str(source), cwd=tmp_path)
    run("git", "config", "user.name", "Test User", cwd=source)
    run("git", "config", "user.email", "test@example.com", cwd=source)
    (source / "hello.txt").write_text("hello\n", encoding="utf-8")
    (source / "hello.txt").chmod(0o755)
    run("git", "add", "hello.txt", cwd=source)
    run("git", "commit", "-m", "initial", cwd=source)
    run("git", "branch", "-M", "main", cwd=source)
    run("git", "push", "-u", "origin", "main", cwd=source)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{source}"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main"]
require_verification_before_commit = true
fetch_before_workspace = true
allowed_paths = []
denied_paths = [".git/**", ".env"]

[repositories.demo.profiles.full]
description = "Simple verification"
verification = true
commands = [["python", "-c", "from pathlib import Path; assert Path('hello.txt').read_text().strip() == 'changed'"]]
""",
        encoding="utf-8",
    )

    service = CodingService(load_config(config_path))
    created = service.workspace_create("demo", "change hello")
    workspace_id = created["workspace_id"]
    current = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "changed\n", current["sha256"])
    workspace_path = Path(created["path"])
    assert workspace_path.joinpath("hello.txt").stat().st_mode & 0o777 == 0o755
    verified = service.workspace_run_profile(workspace_id, "full")
    assert verified["satisfies_commit_gate"] is True
    receipt = service.state.load(workspace_id).last_verification
    assert receipt is not None
    assert receipt.environment_identity_hash is not None
    assert len(receipt.environment_identity_hash) == 64
    committed = service.workspace_commit(workspace_id, "Change greeting")
    assert committed["head_sha"] != created["head_sha"]
    service.workspace_push(workspace_id)


def test_untracked_diff_and_patch_fingerprint(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", str(remote), cwd=tmp_path)
    source = tmp_path / "source"
    run("git", "clone", str(remote), str(source), cwd=tmp_path)
    run("git", "config", "user.name", "Test User", cwd=source)
    run("git", "config", "user.email", "test@example.com", cwd=source)
    (source / "a.txt").write_text("one\n", encoding="utf-8")
    run("git", "add", "a.txt", cwd=source)
    run("git", "commit", "-m", "initial", cwd=source)
    run("git", "branch", "-M", "main", cwd=source)
    run("git", "push", "-u", "origin", "main", cwd=source)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{source}"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main"]
require_verification_before_commit = false
fetch_before_workspace = true
allowed_paths = []
denied_paths = [".git/**", ".env"]
""",
        encoding="utf-8",
    )
    service = CodingService(load_config(config_path))
    created = service.workspace_create("demo", "patch test")
    workspace_id = created["workspace_id"]
    service.workspace_write_file(workspace_id, "new.txt", "new file\n", "<new>")
    diff = service.workspace_diff(workspace_id)
    assert "new.txt" in diff["untracked_paths"]
    assert "+new file" in diff["diff"]

    status = service.workspace_status(workspace_id)
    patch = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-one
+two
"""
    applied = service.workspace_apply_patch(
        workspace_id,
        patch,
        status["head_sha"],
        status["workspace_fingerprint"],
    )
    assert "a.txt" in applied["changed_paths"]
    assert applied["input_format"] == "unified_diff"
    assert len(applied["normalized_patch_sha256"]) == 64

    refreshed = service.workspace_status(workspace_id)
    envelope = (
        "*** Begin Patch\n"
        "*** Add File: envelope.txt\n"
        "+created through envelope\n"
        "*** Update File: a.txt\n"
        "@@\n"
        "-two\n"
        "+three   \n"
        "*** End Patch\n"
    )
    applied_envelope = service.workspace_apply_patch(
        workspace_id,
        envelope,
        refreshed["head_sha"],
        refreshed["workspace_fingerprint"],
    )
    assert applied_envelope["input_format"] == "openai_apply_patch"
    assert "converted_openai_envelope" in applied_envelope["repair_actions"]
    assert (Path(created["path"]) / "a.txt").read_text(encoding="utf-8") == "three\n"
    assert (Path(created["path"]) / "envelope.txt").read_text(encoding="utf-8") == (
        "created through envelope\n"
    )

    audit_text = (tmp_path / "state" / "audit.jsonl").read_text(encoding="utf-8")
    assert "created through envelope" not in audit_text
    assert "input_patch_sha256" in audit_text
    assert "normalized_patch_sha256" in audit_text


def test_run_profile_failure_records_diagnostic_audit_details(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    run("git", "init", "--bare", str(remote), cwd=tmp_path)

    source = tmp_path / "source"
    run("git", "clone", str(remote), str(source), cwd=tmp_path)
    run("git", "config", "user.name", "Test User", cwd=source)
    run("git", "config", "user.email", "test@example.com", cwd=source)
    (source / "hello.txt").write_text("hello\n", encoding="utf-8")
    run("git", "add", "hello.txt", cwd=source)
    run("git", "commit", "-m", "initial", cwd=source)
    run("git", "branch", "-M", "main", cwd=source)
    run("git", "push", "-u", "origin", "main", cwd=source)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"

[repositories.demo]
path = "{source}"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main"]
require_verification_before_commit = true
fetch_before_workspace = true
allowed_paths = []
denied_paths = [".git/**", ".env"]

[repositories.demo.profiles.broken]
description = "First command passes, second fails"
verification = true
commands = [
    ["python", "-c", "print('super-secret-marker-output')"],
    ["python", "-c", "import sys; sys.exit(7)"],
]
""",
        encoding="utf-8",
    )

    service = CodingService(load_config(config_path))
    created = service.workspace_create("demo", "run broken profile")
    workspace_id = created["workspace_id"]

    with pytest.raises(CommandError):
        service.workspace_run_profile(workspace_id, "broken")

    audit_path = tmp_path / "state" / "audit.jsonl"
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failure_events = [
        event
        for event in events
        if event["action"] == "workspace_run_profile" and not event["success"]
    ]
    assert len(failure_events) == 1
    details = failure_events[0]["details"]

    assert details["failed_command"] == "python"
    assert details["exit_code"] == 7
    assert details["steps_completed"] == 1

    audit_text = audit_path.read_text(encoding="utf-8")
    assert "super-secret-marker-output" not in audit_text
