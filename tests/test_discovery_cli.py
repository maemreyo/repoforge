from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, git

from repoforge.cli import main
from repoforge.discovery import (
    detect_repository,
    render_config,
    render_config_set,
    scan_repositories,
)


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
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
    assert "default_verification_profile = \"full\"" in rendered
    assert '[repositories.modern-app.profiles.full]' in rendered
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



def test_makefile_detection_prefers_canonical_targets(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repoforge")
    (repo / "pyproject.toml").write_text(
        "[build-system]\nrequires=[]\n[project]\nname='demo'\n[tool.ruff]\n[tool.mypy]\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "Makefile").write_text(
        "setup:\n\tuv sync --extra dev\n"
        "lint:\n\tuv run ruff check .\n"
        "typecheck:\n\tuv run mypy .\n"
        "test:\n\tuv run pytest\n"
        "build:\n\tuv build\n"
        "check: lint typecheck test build\n",
        encoding="utf-8",
    )
    detection = detect_repository(repo)
    profiles = {profile.name: profile for profile in detection.profiles}
    assert detection.package_manager == "uv"
    assert set(profiles) == {"setup", "quick", "test", "build", "full"}
    assert profiles["quick"].commands == (("make", "lint"), ("make", "typecheck"))
    assert profiles["full"].commands == (("make", "check"),)


def test_bounded_repository_scan_and_multi_config(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    first = init_repo(root / "alpha")
    second = init_repo(root / "group" / "alpha")
    hidden = init_repo(root / ".hidden")
    dependency = init_repo(root / "node_modules" / "dependency")
    (first / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (second / "go.mod").write_text("module example.com/alpha\n", encoding="utf-8")

    detections = scan_repositories([root], max_depth=3)
    assert [item.path for item in detections] == [first.resolve(), second.resolve()]
    assert [item.repo_id for item in detections] == ["alpha", "alpha-2"]
    assert hidden.resolve() not in {item.path for item in detections}
    assert dependency.resolve() not in {item.path for item in detections}

    rendered = render_config_set(detections)
    assert "[repositories.alpha]" in rendered
    assert "[repositories.alpha-2]" in rendered
    assert str(first.resolve()) in rendered
    assert str(second.resolve()) in rendered


def test_cli_scan_preview_and_multi_repo_init(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "projects"
    init_repo(root / "one")
    init_repo(root / "two")

    assert main(["scan-repos", str(root), "--max-depth", "1"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["repository_count"] == 2
    assert preview["review_required"] is True

    config = tmp_path / "multi.toml"
    assert (
        main(
            [
                "--config",
                str(config),
                "init",
                "--scan-root",
                str(root),
                "--max-depth",
                "1",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "scan"
    assert result["repository_count"] == 2
    text = config.read_text(encoding="utf-8")
    assert "[repositories.one]" in text
    assert "[repositories.two]" in text

    invalid = main(
        [
            "--config",
            str(tmp_path / "invalid.toml"),
            "init",
            "--scan-root",
            str(root),
            "--repo-id",
            "not-allowed",
        ]
    )
    assert invalid == 2
    assert "only valid" in capsys.readouterr().err

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
    assert "demo-init" in generated.read_text(encoding="utf-8")
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
    existing.write_text("existing\n", encoding="utf-8")
    code = main(
        [
            "--config",
            str(existing),
            "init",
            "--repo",
            str(forge_env.source),
        ]
    )
    assert code == 2
    assert "Refusing to overwrite" in capsys.readouterr().err

    code = main(["--config", str(tmp_path / "missing.toml"), "show-config"])
    assert code == 2
    assert "Configuration file not found" in capsys.readouterr().err
