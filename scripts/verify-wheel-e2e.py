#!/usr/bin/env python3
"""Exercise a real Git/worktree lifecycle using only the installed RepoForge wheel."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from repoforge.application.service import CodingService
from repoforge.config import load_config


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _toml(value: str) -> str:
    return json.dumps(value)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="repoforge-wheel-e2e-") as raw_root:
        root = Path(raw_root)
        remote = root / "remote.git"
        source = root / "source"
        _git("init", "--bare", str(remote), cwd=root)
        _git("clone", str(remote), str(source), cwd=root)
        _git("config", "user.name", "RepoForge E2E", cwd=source)
        _git("config", "user.email", "repoforge-e2e@example.invalid", cwd=source)
        (source / "hello.txt").write_text("hello\n", encoding="utf-8")
        (source / "README.md").write_text("# Wheel E2E\n", encoding="utf-8")
        _git("add", ".", cwd=source)
        _git("commit", "-m", "initial", cwd=source)
        _git("branch", "-M", "main", cwd=source)
        _git("push", "-u", "origin", "main", cwd=source)

        config_path = root / "resolved.toml"
        config_path.write_text(
            f"""[server]
workspace_root = {_toml(str(root / "workspaces"))}
state_root = {_toml(str(root / "state"))}
path_prefixes = [{_toml(str(Path(sys.executable).parent))}, "/usr/local/bin", "/usr/bin", "/bin"]

[repositories.demo]
path = {_toml(str(source))}
display_name = "Wheel E2E"
remote = "origin"
default_base = "main"
allowed_base_branches = ["main"]
branch_prefix = "e2e/"
protected_branches = ["main", "master"]
require_verification_before_commit = true
fetch_before_workspace = false
default_verification_profile = "full"
max_changed_files = 20
max_diff_lines = 1000
max_total_changed_bytes = 1000000
denied_paths = [".git", ".git/**", ".env", ".github/workflows/**", "**/*.pem"]

[repositories.demo.profiles.full]
description = "Wheel-installed verification"
verification = true
commands = [[{_toml(sys.executable)}, "-c", "from pathlib import Path; assert Path('hello.txt').read_text().startswith('changed')"]]
""",
            encoding="utf-8",
        )

        service = CodingService(load_config(config_path))
        repositories = service.repo_list()["repositories"]
        assert [item["repo_id"] for item in repositories] == ["demo"]

        created = service.workspace_create(
            "demo",
            "wheel installed lifecycle",
            idempotency_key="wheel-e2e-create-0001",
        )
        workspace_id = str(created["workspace_id"])
        workspace_path = Path(str(created["path"]))
        original = service.workspace_read_file(workspace_id, "hello.txt")
        service.workspace_replace_text(
            workspace_id,
            "hello.txt",
            "hello",
            "changed by installed wheel",
            str(original["sha256"]),
        )
        verification = service.workspace_verify(workspace_id)
        assert verification["satisfies_commit_gate"] is True
        committed = service.workspace_commit(workspace_id, "Verify installed wheel lifecycle")
        pushed = service.workspace_push(
            workspace_id,
            idempotency_key="wheel-e2e-push-0001",
        )
        assert pushed["head_sha"] == committed["head_sha"]
        assert _git("ls-remote", "--heads", "origin", str(created["branch"]), cwd=source)

        removed = service.workspace_remove(workspace_id, delete_local_branch=True)
        assert removed["removed"] is True
        assert not workspace_path.exists()
        assert not tuple(root.rglob("*.tmp"))

        print(
            json.dumps(
                {
                    "status": "ok",
                    "repository": "demo",
                    "workspace_id": workspace_id,
                    "head_sha": committed["head_sha"],
                    "remote_branch": created["branch"],
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
