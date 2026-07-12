from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.cli import main
from repoforge.discovery import detect_repository, render_config


def init_repo(path: Path) -> Path:
    path.mkdir()
    git("init", cwd=path)
    git("config", "user.name", "Test User", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    (path / "README.md").write_text("# Project\n", encoding="utf-8")
    git("add", ".", cwd=path)
    git("commit", "-m", "initial", cwd=path)
    git("branch", "-M", "main", cwd=path)
    return path


def test_javascript_detection_and_render_config(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "modern-app")
    (repo / "AGENTS.md").write_text("Test first.\n", encoding="utf-8")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.20.0",
                "scripts": {
                    "check": "biome check .",
                    "fix": "biome check --write .",
                    "test": "vitest run",
                    "test:preflight": "node preflight.mjs",
                    "build": "tsc",
                },
            }
        ),
        encoding="utf-8",
    )
    detection = detect_repository(repo)
    assert detection.repo_id == "modern-app"
    assert detection.ecosystem == "javascript"
    assert detection.package_manager == "pnpm"
    assert detection.package_manager_version == "10.20.0"
    assert {profile.name for profile in detection.profiles} == {
        "setup",
        "fix",
        "quick",
        "test",
        "preflight",
        "full",
    }
    rendered = render_config(detection)
    assert "[server]" not in rendered
    assert '[repositories.modern-app.actions]' in rendered
    assert '[repositories.modern-app.checks]' in rendered
    assert '[repositories.modern-app.profiles.full]' not in rendered
    assert str(repo) in rendered


@pytest.mark.parametrize(
    ("manifest", "content", "ecosystem", "manager", "profile"),
    [
        ("pyproject.toml", "[tool.ruff]\n[tool.mypy]\n", "python", "python", "full"),
        ("Cargo.toml", "[package]\nname='demo'\n", "rust", "cargo", "quick"),
        ("go.mod", "module example.com/demo\n", "go", "go", "test"),
    ],
)
def test_non_javascript_detection(
    tmp_path: Path,
    manifest: str,
    content: str,
    ecosystem: str,
    manager: str,
    profile: str,
) -> None:
    repo = init_repo(tmp_path / ecosystem)
    (repo / manifest).write_text(content, encoding="utf-8")
    detection = detect_repository(repo)
    assert detection.ecosystem == ecosystem
    assert detection.package_manager == manager
    assert profile in {item.name for item in detection.profiles}


def test_generic_and_invalid_detection(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "generic")
    detection = detect_repository(repo)
    assert detection.ecosystem == "generic"
    assert detection.warnings
    with pytest.raises(ValueError, match="does not exist"):
        detect_repository(tmp_path / "missing")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a Git working tree"):
        detect_repository(plain)


def test_cli_setup_diagnostics_and_workspace_commands(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(
        "PATH", f"{forge_env.fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    )
    generated = tmp_path / "generated.toml"
    assert (
        main(
            [
                "--config",
                str(generated),
                "init",
                "--repo",
                str(forge_env.source),
                "--repo-id",
                "demo-init",
            ]
        )
        == 0
    )
    assert generated.exists()
    generated_text = generated.read_text(encoding="utf-8")
    assert "[server]" not in generated_text
    assert "[repositories.demo-init]" in generated_text
    assert (
        main(
            [
                "--config",
                str(generated),
                "init",
                "--repo",
                str(forge_env.source),
                "--repo-id",
                "demo-second",
            ]
        )
        == 0
    )
    generated_text = generated.read_text(encoding="utf-8")
    assert "[repositories.demo-init]" in generated_text
    assert "[repositories.demo-second]" in generated_text
    capsys.readouterr()

    assert main(["inspect-repo", str(forge_env.source)]) == 0
    inspect_output = json.loads(capsys.readouterr().out)
    assert inspect_output["ecosystem"] == "javascript"
    assert main(["inspect-repo", str(forge_env.source), "--render-config"]) == 0
    assert "[repositories.source]" in capsys.readouterr().out

    config = str(forge_env.config_path)
    assert main(["--config", config, "show-config"]) == 0
    assert json.loads(capsys.readouterr().out)["repositories"][0]["repo_id"] == "demo"
    assert main(["--config", config, "list-workspaces"]) == 0
    assert json.loads(capsys.readouterr().out)["workspaces"] == []

    doctor_code = main(["--config", config, "doctor", "--fix"])
    assert doctor_code in (0, 1)
    doctor_output = json.loads(capsys.readouterr().out)
    assert "checks" in doctor_output
    assert doctor_output["fixes"]["actions"][0]["ok"] is True

    assert main(["--config", config, "smoke-test", "--repo-id", "demo"]) == 0
    smoke = json.loads(capsys.readouterr().out)
    assert smoke["ok"] is True
    assert {step["step"] for step in smoke["steps"]} >= {
        "workspace_create",
        "workspace_remove",
    }

    assert main(["--config", config, "audit", "--tail", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["events"]
    assert (
        main(
            [
                "--config",
                config,
                "tunnel-command",
                "--tunnel-id",
                "tunnel_test",
            ]
        )
        == 0
    )
    tunnel = json.loads(capsys.readouterr().out)
    assert "sample_mcp_stdio_local" in tunnel["init"]

    workspace_id = forge_env.service.workspace_create("demo", "cli remove")["workspace_id"]
    assert (
        main(
            [
                "--config",
                config,
                "remove-workspace",
                workspace_id,
                "--delete-local-branch",
            ]
        )
        == 0
    )
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] is True


def test_cli_errors_and_existing_config(
    forge_env: ForgeEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "existing.toml"
    existing.write_text(
        f'[repositories.seed]\npath = "{forge_env.source}"\n',
        encoding="utf-8",
    )
    code = main(
        [
            "--config",
            str(existing),
            "init",
            "--repo",
            str(forge_env.source),
            "--repo-id",
            "added",
        ]
    )
    assert code == 0
    updated = existing.read_text(encoding="utf-8")
    assert "[repositories.seed]" in updated
    assert "[repositories.added]" in updated
    capsys.readouterr()

    code = main(
        [
            "--config",
            str(existing),
            "init",
            "--repo",
            str(forge_env.source),
            "--repo-id",
            "added",
        ]
    )
    assert code == 2
    assert "already configured" in capsys.readouterr().err

    code = main(
        [
            "--config",
            str(existing),
            "init",
            "--repo",
            str(forge_env.source),
            "--repo-id",
            "replacement",
            "--force",
        ]
    )
    assert code == 0
    replaced = existing.read_text(encoding="utf-8")
    assert "[repositories.replacement]" in replaced
    assert "[repositories.seed]" not in replaced
    capsys.readouterr()

    code = main(["--config", str(tmp_path / "missing.toml"), "show-config"])
    assert code == 2
    assert "Configuration file not found" in capsys.readouterr().err
