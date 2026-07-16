from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from repoforge.application.service import CodingService
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import load_config
from repoforge.ports.clock import Clock


def git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


@dataclass(frozen=True)
class ForgeEnvironment:
    root: Path
    remote: Path
    source: Path
    fake_bin: Path
    gh_state: Path
    config_path: Path
    service: CodingService


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
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {{"prs": {{}}}}

def save(data):
    STATE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\\n")

def branch():
    return subprocess.run(
        ["git", "branch", "--show-current"], check=True, capture_output=True, text=True
    ).stdout.strip()

def head_sha():
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

def arg_value(args, flag, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default

args = sys.argv[1:]
if args == ["--version"]:
    print("gh version 2.80.0 (fake)")
    raise SystemExit(0)
if args[:2] == ["auth", "status"]:
    print("Logged in to github.com as test-user")
    raise SystemExit(0)
if args[:2] == ["auth", "setup-git"]:
    print("Configured git protocol")
    raise SystemExit(0)
if args[:2] == ["repo", "view"]:
    if "--jq" in args:
        print("owner/demo")
    else:
        print(json.dumps({{"nameWithOwner": "owner/demo"}}))
    raise SystemExit(0)
if args[:2] == ["issue", "view"]:
    number = int(args[2])
    data = load()
    override = (data.get("issues") or {{}}).get(str(number), {{}})
    payload = {{
        "number": number,
        "title": "Implement safer workflow",
        "body": "Issue body",
        "state": "OPEN",
        "author": {{"login": "test-user"}},
        "labels": [{{"name": "enhancement"}}],
        "assignees": [],
        "url": f"https://github.com/owner/demo/issues/{{number}}",
        "comments": [{{
            "body": (
                "context\\n\\nObjective: implement the ticket.\\n"
                "Acceptance criteria: behavior is verified.\\n"
                "Tests: run the production gate."
            ),
            "author": {{"login": "reviewer"}},
        }}],
    }}
    payload.update(override)
    print(json.dumps(payload))
    raise SystemExit(0)
if args[:2] == ["pr", "create"]:
    data = load()
    head = arg_value(args, "--head", branch())
    title = arg_value(args, "--title", "Draft PR")
    body = sys.stdin.read()
    pr = {{
        "number": 42,
        "title": title,
        "body": body,
        "url": "https://github.com/owner/demo/pull/42",
        "state": "OPEN",
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
        "statusCheckRollup": [],
        "headRefOid": head_sha(),
    }}
    data.setdefault("prs", {{}})[head] = pr
    save(data)
    print(pr["url"])
    raise SystemExit(0)
if args[:2] == ["pr", "edit"]:
    data = load()
    ref = args[2]
    pr = data.setdefault("prs", {{}}).get(ref)
    if not pr:
        print("no pull request found", file=sys.stderr)
        raise SystemExit(1)
    title = arg_value(args, "--title")
    if title is not None:
        pr["title"] = title
    if "--body-file" in args:
        pr["body"] = sys.stdin.read()
    save(data)
    raise SystemExit(0)
if args[:2] == ["pr", "checks"]:
    data = load()
    checks = data.get("checks") or [
        {{"name": "unit", "state": "SUCCESS", "bucket": "pass", "link": "https://github.com/owner/demo/actions/runs/1001/job/101", "workflow": "CI", "description": "ok", "startedAt": "", "completedAt": ""}},
        {{"name": "lint", "state": "SKIPPED", "bucket": "skipping", "link": "https://github.com/owner/demo/actions/runs/1002/job/102", "workflow": "CI", "description": "skipped", "startedAt": "", "completedAt": ""}},
    ]
    if "--required" in args:
        checks = [item for item in checks if item.get("required", True)]
    print(json.dumps(checks))
    raise SystemExit(0)
if args[:2] == ["pr", "view"]:
    ref = args[2]
    data = load()
    # Numeric reads represent an existing GitHub PR independent of the workspace-created PR.
    if ref.isdigit():
        number = int(ref)
        print(json.dumps({{
            "number": number,
            "title": "Existing PR",
            "body": "Existing body",
            "state": "OPEN",
            "isDraft": False,
            "author": {{"login": "test-user"}},
            "baseRefName": "main",
            "headRefName": "feature",
            "url": f"https://github.com/owner/demo/pull/{{number}}",
            "files": [{{"path": "hello.txt"}}],
            "commits": [],
            "statusCheckRollup": [],
            "reviews": [],
        }}))
        raise SystemExit(0)
    pr = data.setdefault("prs", {{}}).get(ref)
    if not pr:
        print("no pull request found", file=sys.stderr)
        raise SystemExit(1)
    if "--jq" in args and ".headRefOid" in args:
        print(pr.get("headRefOid", head_sha()))
    else:
        print(json.dumps(pr))
    raise SystemExit(0)

if args and args[0] == "api":
    data = load()
    endpoint = next((arg for arg in args[1:] if not arg.startswith("-") and arg not in {{"GET", "per_page=100", "filter=latest"}}), "")
    current_head = head_sha()
    if endpoint.endswith("/check-runs") and "/commits/" in endpoint:
        runs = data.get("check_runs") or {{
            "101": {{"id": 101, "name": "unit", "head_sha": current_head, "status": "completed", "conclusion": "success", "details_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "html_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
            "102": {{"id": 102, "name": "lint", "head_sha": current_head, "status": "completed", "conclusion": "skipped", "details_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "html_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
        }}
        print(json.dumps({{"total_count": len(runs), "check_runs": list(runs.values())}}))
        raise SystemExit(0)
    if "/check-runs/" in endpoint and endpoint.endswith("/annotations"):
        check_id = endpoint.split("/check-runs/", 1)[1].split("/", 1)[0]
        if data.get("annotations_permission_denied"):
            print("Resource not accessible by integration", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps((data.get("annotations") or {{}}).get(check_id, [])))
        raise SystemExit(0)
    if "/check-runs/" in endpoint:
        check_id = endpoint.rsplit("/", 1)[-1]
        runs = data.get("check_runs") or {{
            "101": {{"id": 101, "name": "unit", "head_sha": current_head, "status": "completed", "conclusion": "success", "details_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "html_url": "https://github.com/owner/demo/actions/runs/1001/job/101", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
            "102": {{"id": 102, "name": "lint", "head_sha": current_head, "status": "completed", "conclusion": "skipped", "details_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "html_url": "https://github.com/owner/demo/actions/runs/1002/job/102", "started_at": "", "completed_at": "", "output": {{"title": "", "summary": "", "text": "", "annotations_count": 0}}, "app": {{"name": "GitHub Actions"}}}},
        }}
        item = runs.get(check_id)
        if item is None:
            print("check run not found", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(item))
        raise SystemExit(0)
    if "/actions/jobs/" in endpoint and endpoint.endswith("/logs"):
        job_id = endpoint.split("/actions/jobs/", 1)[1].split("/", 1)[0]
        if data.get("logs_permission_denied"):
            print("Resource not accessible by integration", file=sys.stderr)
            raise SystemExit(1)
        log = (data.get("logs") or {{}}).get(job_id)
        if log is None:
            print("job log not found", file=sys.stderr)
            raise SystemExit(1)
        print(log, end="")
        raise SystemExit(0)
    if "/actions/jobs/" in endpoint:
        job_id = endpoint.rsplit("/", 1)[-1]
        job = (data.get("jobs") or {{}}).get(job_id)
        if job is None:
            print("job not found", file=sys.stderr)
            raise SystemExit(1)
        print(json.dumps(job))
        raise SystemExit(0)

print("unsupported fake gh invocation: " + " ".join(args), file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)


def create_forge_environment(
    tmp_path: Path,
    *,
    max_batch_files: int = 20,
    max_changed_files: int = 20,
    require_verification: bool = True,
    clock: Clock | None = None,
    execution_mode: str = "strict",
    adhoc_runners: tuple[str, ...] = (),
) -> ForgeEnvironment:
    remote = tmp_path / "remote.git"
    git("init", "--bare", str(remote), cwd=tmp_path)

    source = tmp_path / "source"
    git("clone", str(remote), str(source), cwd=tmp_path)
    git("config", "user.name", "Test User", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    (source / "hello.txt").write_text("hello\n", encoding="utf-8")
    (source / "README.md").write_text("# Demo\n\nRepository instructions.\n", encoding="utf-8")
    (source / "AGENTS.md").write_text("Always test changes.\n", encoding="utf-8")
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "packageManager": "pnpm@10.20.0",
                "engines": {"node": "22.23.1"},
                "scripts": {"test": "echo test", "check": "echo check"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    git("add", ".", cwd=source)
    git("commit", "-m", "initial", cwd=source)
    git("branch", "-M", "main", cwd=source)
    git("push", "-u", "origin", "main", cwd=source)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    gh_state = tmp_path / "fake-gh-state.json"
    _write_fake_gh(fake_bin, gh_state)

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''[server]
workspace_root = "{tmp_path / "workspaces"}"
state_root = "{tmp_path / "state"}"
max_batch_files = {max_batch_files}
path_prefixes = ["{fake_bin}", "/usr/local/bin", "/usr/bin", "/bin"]

[repositories.demo]
path = "{source}"
display_name = "Demo Repository"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "ai/"
protected_branches = ["main", "master"]
require_verification_before_commit = {str(require_verification).lower()}
fetch_before_workspace = true
default_verification_profile = "full"
max_changed_files = {max_changed_files}
max_diff_lines = 1000
max_total_changed_bytes = 1000000
allowed_paths = []
denied_paths = [".git", ".git/**", ".env", ".github/workflows/**", "**/*.pem"]
pr_labels = ["agent"]
pr_reviewers = ["reviewer"]
no_maintainer_edit = true
execution_mode = "{execution_mode}"
adhoc_runners = {json.dumps(list(adhoc_runners))}

[repositories.demo.profiles.quick]
description = "Fast non-gating check"
verification = false
commands = [["python3", "-c", "from pathlib import Path; assert Path('hello.txt').exists()"]]

[repositories.demo.profiles.full]
description = "Full verification"
verification = true
commands = [["python3", "-c", "from pathlib import Path; assert Path('hello.txt').read_text().startswith('changed')"]]

[repositories.demo.diagnostics.pytest-target]
summary = "Run one tracked pytest target"
argv = ["python3", "-c", "print('1 passed in 0.01s')", "{{selector}}"]
selector_kind = "pytest_node"
timeout_seconds = 30
network_policy = "local_only"
mutability = "read_only"
parser = "pytest"
output_limit = 2000
''',
        encoding="utf-8",
    )
    config = load_config(config_path)
    if clock is None:
        service = CodingService(config)
    else:
        application = build_application(config, overrides=AdapterOverrides(clock=clock))
        service = CodingService(config, application=application)
    return ForgeEnvironment(
        root=tmp_path,
        remote=remote,
        source=source,
        fake_bin=fake_bin,
        gh_state=gh_state,
        config_path=config_path,
        service=service,
    )


@pytest.fixture
def forge_env(tmp_path: Path) -> ForgeEnvironment:
    return create_forge_environment(tmp_path)
