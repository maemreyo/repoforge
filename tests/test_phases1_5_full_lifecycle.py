from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repoforge.application.service import CodingService
from repoforge.application.workspace.edit import FileEdit, TextEdit
from repoforge.config import load_config


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _write_fake_gh(fake_bin: Path, state_path: Path) -> None:
    script = fake_bin / "gh"
    script.write_text(
        f"""#!/usr/bin/env python3
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
STATE = Path({str(state_path)!r})
def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {{"prs": {{}}}}
def save(data):
    STATE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\\n")
def branch():
    return subprocess.run(["git", "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip()
def arg_value(args, flag, default=None):
    try: return args[args.index(flag) + 1]
    except (ValueError, IndexError): return default
args = sys.argv[1:]
if args == ["--version"]:
    print("gh version 2.80.0 (fake)"); raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print("Logged in to github.com as test-user"); raise SystemExit(0)
if args[:2] == ["auth", "setup-git"]:
    print("Configured git protocol"); raise SystemExit(0)
if args[:2] == ["repo", "view"]:
    print("owner/demo" if "--jq" in args else json.dumps({{"nameWithOwner": "owner/demo"}})); raise SystemExit(0)
if args[:2] == ["issue", "view"]:
    number = int(args[2]); print(json.dumps({{
      "number": number, "title": "Implement safer workflow", "body": "Issue body", "state": "OPEN",
      "author": {{"login": "test-user"}}, "labels": [{{"name": "enhancement"}}], "assignees": [],
      "url": f"https://github.com/owner/demo/issues/{{number}}",
      "comments": [{{"body": "context", "author": {{"login": "reviewer"}}}}],
    }})); raise SystemExit(0)
if args[:2] == ["pr", "create"]:
    data = load(); head = arg_value(args, "--head", branch()); title = arg_value(args, "--title", "Draft PR")
    pr = {{"number": 42, "title": title, "body": sys.stdin.read(), "url": "https://github.com/owner/demo/pull/42",
          "state": "OPEN", "isDraft": True, "mergeable": "MERGEABLE", "reviewDecision": "", "statusCheckRollup": []}}
    data.setdefault("prs", {{}})[head] = pr; save(data); print(pr["url"]); raise SystemExit(0)
if args[:2] == ["pr", "edit"]:
    data = load(); ref = args[2]; pr = data.setdefault("prs", {{}}).get(ref)
    if not pr: print("no pull request found", file=sys.stderr); raise SystemExit(1)
    title = arg_value(args, "--title")
    if title is not None: pr["title"] = title
    if "--body-file" in args: pr["body"] = sys.stdin.read()
    save(data); raise SystemExit(0)
if args[:2] == ["pr", "checks"]:
    print(json.dumps([
      {{"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": "https://ci/unit", "workflow": "CI", "description": "ok", "startedAt": "", "completedAt": ""}},
      {{"name": "lint", "state": "SKIPPED", "bucket": "skipping", "link": "https://ci/lint", "workflow": "CI", "description": "skipped", "startedAt": "", "completedAt": ""}},
    ])); raise SystemExit(0)
if args[:2] == ["pr", "view"]:
    ref = args[2]; data = load()
    if ref.isdigit():
        number = int(ref); print(json.dumps({{
          "number": number, "title": "Existing PR", "body": "Existing body", "state": "OPEN", "isDraft": False,
          "author": {{"login": "test-user"}}, "baseRefName": "main", "headRefName": "feature",
          "url": f"https://github.com/owner/demo/pull/{{number}}", "files": [{{"path": "hello.txt"}}],
          "commits": [], "statusCheckRollup": [], "reviews": []
        }})); raise SystemExit(0)
    pr = data.setdefault("prs", {{}}).get(ref)
    if not pr: print("no pull request found", file=sys.stderr); raise SystemExit(1)
    print(json.dumps(pr)); raise SystemExit(0)
print("unsupported fake gh invocation: " + " ".join(args), file=sys.stderr); raise SystemExit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


@dataclass(frozen=True)
class Environment:
    remote: Path
    source: Path
    config_path: Path
    service: CodingService


def _environment(tmp_path: Path) -> Environment:
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    source = tmp_path / "source"
    _git("clone", str(remote), str(source), cwd=tmp_path)
    _git("config", "user.name", "Test User", cwd=source)
    _git("config", "user.email", "test@example.com", cwd=source)
    (source / "hello.txt").write_text("hello\n", encoding="utf-8")
    (source / "README.md").write_text("# Demo\n\nRepository instructions.\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("Always test changes.\n", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "packageManager": "pnpm@10.20.0",
                "engines": {"node": "22.23.1"},
                "scripts": {"test": "echo test"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _git("add", ".", cwd=source)
    _git("commit", "-m", "initial", cwd=source)
    _git("branch", "-M", "main", cwd=source)
    _git("push", "-u", "origin", "main", cwd=source)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_fake_gh(fake_bin, tmp_path / "gh.json")
    config_path = tmp_path / "resolved.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
max_batch_files = 20
path_prefixes = ["{fake_bin}", "/usr/local/bin", "/usr/bin", "/bin"]

[repositories.demo]
path = "{source}"
display_name = "Demo Repository"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main", "master"]
require_verification_before_commit = true
fetch_before_workspace = true
default_verification_profile = "full"
max_changed_files = 20
max_diff_lines = 1000
max_total_changed_bytes = 1000000
denied_paths = [".git", ".git/**", ".env", ".github/workflows/**", "**/*.pem"]
pr_labels = ["agent"]
pr_reviewers = ["reviewer"]
no_maintainer_edit = true

[repositories.demo.profiles.quick]
description = "Fast non-gating check"
verification = false
commands = [["python3", "-c", "from pathlib import Path; assert Path('hello.txt').exists()"]]

[repositories.demo.profiles.full]
description = "Full verification"
verification = true
commands = [["python3", "-c", "from pathlib import Path; assert Path('hello.txt').read_text().startswith('changed')"]]
''',
        encoding="utf-8",
    )
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{old_path}"
    return Environment(remote, source, config_path, CodingService(load_config(config_path)))


def test_complete_service_lifecycle_and_adapters(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    service = env.service
    assert service.repo_list()["repositories"][0]["display_name"] == "Demo Repository"
    assert service.repo_status("demo")["gh_authenticated"] is True
    context = service.repo_context("demo")
    assert context["package_manager"] == "pnpm@10.20.0"
    assert service.repo_recent_commits("demo", 3)["commits"][0]["subject"] == "initial"
    assert service.repo_issue_read("demo", 7)["number"] == 7
    assert service.repo_pr_read("demo", 8)["number"] == 8
    doctor = service.doctor()
    assert "checks" in doctor and doctor["summary"]["total"] >= 1
    assert any(check["name"] == "executable:tunnel-client" for check in doctor["checks"])

    created = service.workspace_create(
        "demo", "Improve developer experience", idempotency_key="create-workspace-0001"
    )
    replayed_create = service.workspace_create(
        "demo", "Improve developer experience", idempotency_key="create-workspace-0001"
    )
    assert replayed_create == created
    workspace_id = created["workspace_id"]
    workspace_path = Path(created["path"])
    assert service.workspace_list()["workspaces"][0]["workspace_id"] == workspace_id
    status = service.workspace_status(workspace_id)
    assert status["clean"] is True
    assert "hello.txt" in service.workspace_tree(workspace_id, 100)["entries"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    assert hello["content"] == "1: hello"
    assert len(service.workspace_read_files(workspace_id, ["hello.txt", "README.md"])["files"]) == 2
    assert service.workspace_search(workspace_id, "Repository")["matches"]
    replaced = service.workspace_edit(
        workspace_id,
        [FileEdit("hello.txt", hello["sha256"], (TextEdit("hello", "changed hello"),))],
    )
    assert replaced["files"][0]["replacements"] == 1
    created_file = service.workspace_write_file(workspace_id, "notes.txt", "temporary\n", "<new>")
    assert created_file["path"] == "notes.txt"
    current_status = service.workspace_status(workspace_id)
    restored = service.workspace_restore_paths(
        workspace_id, ["notes.txt"], current_status["workspace_fingerprint"]
    )
    assert (
        "notes.txt" in restored["removed_untracked"]
        and not workspace_path.joinpath("notes.txt").exists()
    )
    patch_status = service.workspace_status(workspace_id)
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,5 @@
 # Demo

 Repository instructions.
+
+Developer experience improved.
"""
    assert (
        "README.md"
        in service.workspace_apply_patch(
            workspace_id, patch, patch_status["head_sha"], patch_status["workspace_fingerprint"]
        )["changed_paths"]
    )
    assert "Developer experience improved" in service.workspace_diff(workspace_id)["diff"]
    assert service.workspace_run_profile(workspace_id, "quick")["satisfies_commit_gate"] is False
    assert service.workspace_run_profile(workspace_id)["satisfies_commit_gate"] is True
    committed = service.workspace_commit(workspace_id, "Improve developer experience")
    pushed = service.workspace_push(workspace_id, idempotency_key="push-workspace-0001")
    assert pushed["head_sha"] == committed["head_sha"]
    assert service.workspace_push(workspace_id, idempotency_key="push-workspace-0001") == pushed
    pr = service.workspace_create_draft_pr(
        workspace_id,
        "Improve developer experience",
        "## Summary\n\nSafer workflow.",
        idempotency_key="create-pr-00000001",
    )
    assert (
        service.workspace_create_draft_pr(
            workspace_id,
            "Improve developer experience",
            "## Summary\n\nSafer workflow.",
            idempotency_key="create-pr-00000001",
        )
        == pr
    )
    assert pr["draft"] is True and pr["labels"] == ["agent"]
    assert (
        service.workspace_update_draft_pr(
            workspace_id,
            title="Improve DX safely",
            idempotency_key="update-pr-00000001",
        )["title"]
        == "Improve DX safely"
    )
    assert service.workspace_pr_status(workspace_id)["number"] == 42
    checks = service.workspace_pr_checks(workspace_id, required_only=True)
    assert checks["all_passed"] is True and checks["summary"] == {"pass": 1, "skipping": 1}
    removed = service.workspace_remove(workspace_id, delete_local_branch=True)
    assert removed["removed"] is True and removed["remote_branch_untouched"] is True
