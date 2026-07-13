from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

cli = importlib.import_module("repoforge.interfaces.cli.main")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_guided_onboarding_real_git_initial_config(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    remote = tmp_path / "remote.git"
    source = tmp_path / "repos" / "demo"
    source.mkdir(parents=True)
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("init", "-q", "-b", "main", cwd=source)
    git("config", "user.email", "x@y", cwd=source)
    git("config", "user.name", "x", cwd=source)
    (source / "README.md").write_text("demo\n")
    git("add", ".", cwd=source)
    git("commit", "-qm", "init", cwd=source)
    git("remote", "add", "origin", str(remote), cwd=source)
    git("push", "-q", "-u", "origin", "main", cwd=source)
    config = home / ".config/repoforge/config.toml"
    code = cli.main(
        [
            "--config",
            str(config),
            "onboard",
            str(source.parent),
            "--non-interactive",
            "--tunnel-id",
            "tunnel_test",
            "--activate",
            "never",
            "--plan-only",
        ]
    )
    assert code == 3
    first = json.loads(capsys.readouterr().out)
    session_id = first["session_id"]
    proposal = first["session"]["repositories"][0]["proposal_id"]
    assert (
        cli.main(
            [
                "--config",
                str(config),
                "onboard",
                "resume",
                session_id,
                "--non-interactive",
                "--tunnel-id",
                "tunnel_test",
                "--activate",
                "never",
                "--approve",
                f"approve:{proposal}",
            ]
        )
        == 0
    )
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed" and config.is_file()
    persisted = (home / ".local/state/repoforge/onboarding" / f"{session_id}.json").read_text()
    assert "approve:" not in persisted and "CONTROL_PLANE_API_KEY" not in persisted
