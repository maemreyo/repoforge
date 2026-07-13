from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import repoforge.user_config as user_config
from repoforge.config import load_config
from repoforge.errors import ConfigError
from repoforge.onboarding import _normalize_argv
from repoforge.user_config import (
    TunnelSettings,
    UserConfig,
    UserRepository,
    build_lock_text,
    config_history,
    detect_repository_for_setup,
    generation_snapshot_path,
    render_user_config,
    resolve_runtime_config_path,
    resolved_config_path,
    rollback_generation,
    write_user_and_lock,
)


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def init_repo(path: Path, makefile: str) -> Path:
    path.mkdir()
    git("init", cwd=path)
    git("config", "user.name", "Test User", cwd=path)
    git("config", "user.email", "test@example.com", cwd=path)
    (path / "README.md").write_text("# Test\n", encoding="utf-8")
    (path / "Makefile").write_text(makefile, encoding="utf-8")
    git("add", ".", cwd=path)
    git("commit", "-m", "initial", cwd=path)
    git("branch", "-M", "main", cwd=path)
    return path


def test_minimal_config_resolves_multiple_repositories_and_make_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_config, "DEFAULT_STATE_ROOT", str(tmp_path / "state"))
    first = init_repo(
        tmp_path / "repoforge",
        "setup:\n\t@true\n\nlint:\n\t@true\n\ntypecheck:\n\t@true\n\ntest:\n\t@true\n\ncheck:\n\t@true\n",
    )
    second = init_repo(
        tmp_path / "work-frontier",
        "bootstrap:\n\t@true\n\nfix:\n\t@true\n\ncheck:\n\t@true\n\ntest:\n\t@true\n\nverify:\n\t@true\n",
    )
    config_path = tmp_path / "config.toml"
    user = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=(
            UserRepository("repoforge", first),
            UserRepository("work-frontier", second),
        ),
    )
    source = render_user_config(user)
    config_path.write_text(source, encoding="utf-8")
    lock, detections = build_lock_text(user, source)
    lock_path = resolved_config_path(config_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(lock, encoding="utf-8")

    assert {item.package_manager for item in detections} == {"make"}
    runtime = resolve_runtime_config_path(config_path)
    loaded = load_config(runtime)
    assert set(loaded.repositories) == {"repoforge", "work-frontier"}
    assert loaded.repositories["repoforge"].profiles["quick"].commands == (
        ("make", "lint"),
        ("make", "typecheck"),
    )
    assert loaded.repositories["repoforge"].profiles["full"].commands == (("make", "check"),)
    assert loaded.repositories["work-frontier"].profiles["full"].commands == (("make", "verify"),)


def test_manifest_change_invalidates_resolved_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_config, "DEFAULT_STATE_ROOT", str(tmp_path / "state"))
    repo = init_repo(tmp_path / "demo", "check:\n\t@true\n")
    config_path = tmp_path / "config.toml"
    user = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=(UserRepository("demo", repo),),
    )
    source = render_user_config(user)
    config_path.write_text(source, encoding="utf-8")
    lock, _ = build_lock_text(user, source)
    lock_path = resolved_config_path(config_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(lock, encoding="utf-8")

    assert resolve_runtime_config_path(config_path) == lock_path
    (repo / "Makefile").write_text("verify:\n\t@true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="refresh"):
        resolve_runtime_config_path(config_path)


def test_make_detection_exposes_actions_checks_and_full_gate(tmp_path: Path) -> None:
    repo = init_repo(
        tmp_path / "demo",
        "bootstrap:\n\t@true\n\nfix:\n\t@true\n\ncheck-architecture:\n\t@true\n\nverify:\n\t@true\n",
    )
    detection = detect_repository_for_setup(repo)
    profiles = {profile.name: profile for profile in detection.profiles}
    assert profiles["setup"].verification is False
    assert profiles["fix"].verification is False
    assert profiles["architecture"].commands == (("make", "check-architecture"),)
    assert profiles["full"].commands == (("make", "verify"),)


def test_legacy_config_is_used_directly(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "legacy", "check:\n\t@true\n")
    config = tmp_path / "legacy.toml"
    config.write_text(f'[repositories.legacy]\npath = "{repo}"\n', encoding="utf-8")
    assert resolve_runtime_config_path(config) == config.resolve()


def test_minimal_config_allows_path_only_repository_entries(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "My_Project", "check:\n\t@true\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'''version = 1

[tunnel]
id = "tunnel_test"

[[repo]]
path = "{repo}"
''',
        encoding="utf-8",
    )
    loaded = user_config.load_user_config(config)
    assert loaded.repositories[0].repo_id == "my-project"


def test_resolved_lock_rendering_is_deterministic(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo", "check:\n\t@true\n")
    config_path = tmp_path / "config.toml"
    user = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=(UserRepository("demo", repo),),
    )
    source = render_user_config(user)
    first, _ = build_lock_text(user, source)
    second, _ = build_lock_text(user, source)
    assert first == second


def test_global_config_argument_is_normalized_for_new_commands() -> None:
    assert _normalize_argv(["--config", "/tmp/config.toml", "start", "--dry-run"]) == [
        "start",
        "--config",
        "/tmp/config.toml",
        "--dry-run",
    ]
    assert _normalize_argv(["--config=/tmp/config.toml", "repo", "list"]) == [
        "repo",
        "--config=/tmp/config.toml",
        "list",
    ]


def test_modified_resolved_lock_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_config, "DEFAULT_STATE_ROOT", str(tmp_path / "state"))
    repo = init_repo(tmp_path / "demo", "check:\n\t@true\n")
    config_path = tmp_path / "config.toml"
    user = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=(UserRepository("demo", repo),),
    )
    source = render_user_config(user)
    config_path.write_text(source, encoding="utf-8")
    lock, _ = build_lock_text(user, source)
    lock_path = resolved_config_path(config_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(lock + "# tampered\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="modified"):
        resolve_runtime_config_path(config_path)


def test_boolean_config_version_is_rejected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "demo", "check:\n\t@true\n")
    config = tmp_path / "config.toml"
    config.write_text(
        f'''version = true

[tunnel]
id = "tunnel_test"

[[repo]]
path = "{repo}"
''',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="version"):
        user_config.load_user_config(config)


def test_mixed_minimal_and_legacy_formats_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """version = 1
[tunnel]
id = "tunnel_test"
[[repo]]
path = "/tmp/demo"
[repositories.demo]
path = "/tmp/demo"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Do not mix"):
        user_config.load_user_config(config)


@pytest.mark.parametrize(
    "content, message",
    [
        ('version = 1\n[tunnel]\n[[repo]]\npath = "/tmp/x"\n', "tunnel.id"),
        ('version = 1\n[tunnel]\nid = "bad tunnel"\n[[repo]]\npath = "/tmp/x"\n', "tunnel.id"),
        (
            'version = 1\n[tunnel]\nid = "tunnel_x"\nprofile = "bad/profile"\n[[repo]]\npath = "/tmp/x"\n',
            "tunnel.profile",
        ),
        ('version = 1\n[tunnel]\nid = "tunnel_x"\n', "At least one"),
        (
            'version = 1\n[tunnel]\nid = "tunnel_x"\n[[repo]]\nid = 42\npath = "/tmp/x"\n',
            "id must be a string",
        ),
    ],
)
def test_invalid_minimal_config_is_rejected(tmp_path: Path, content: str, message: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        user_config.load_user_config(config)


def test_duplicate_repository_ids_and_paths_are_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'''version = 1
[tunnel]
id = "tunnel_x"
[[repo]]
id = "same"
path = "{repo}"
[[repo]]
id = "same"
path = "{tmp_path / "other"}"
''',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Duplicate repository id"):
        user_config.load_user_config(config)

    config.write_text(
        f'''version = 1
[tunnel]
id = "tunnel_x"
[[repo]]
id = "first"
path = "{repo}"
[[repo]]
id = "second"
path = "{repo}"
''',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Duplicate repository path"):
        user_config.load_user_config(config)


def test_concurrent_user_config_update_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_config, "DEFAULT_STATE_ROOT", str(tmp_path / "state"))
    repo = init_repo(tmp_path / "demo", "check:\n\t@true\n")
    config_path = tmp_path / "config.toml"
    user = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=(UserRepository("demo", repo),),
    )
    config_path.write_text(render_user_config(user), encoding="utf-8")
    with pytest.raises(ConfigError, match="concurrently"):
        user_config.write_user_and_lock(user, expected_source_sha256="0" * 64)


def test_config_history_retains_complete_generations_and_rollback_restores_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_config, "DEFAULT_STATE_ROOT", str(tmp_path / "state"))
    repo = init_repo(tmp_path / "demo", "check:\n\t@true\n")
    config_path = tmp_path / "config.toml"
    first = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=(UserRepository("demo", repo),),
    )
    config_path.write_text(render_user_config(first), encoding="utf-8")
    write_user_and_lock(first)
    second = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_next"),
        repositories=(UserRepository("demo", repo),),
    )
    write_user_and_lock(second, expected_source_sha256=user_config.sha256_file(config_path))

    assert config_history(config_path) == [2, 1]

    rollback_generation(config_path, 1)

    assert user_config.load_user_config(config_path).tunnel.tunnel_id == "tunnel_test"
    assert user_config.lock_generation(resolved_config_path(config_path)) == 1
    snapshot = generation_snapshot_path(config_path, 1)
    assert snapshot.joinpath("config.toml").is_file()
    assert snapshot.joinpath("resolved.toml").is_file()


def test_rollback_rejects_incomplete_generation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_config, "DEFAULT_STATE_ROOT", str(tmp_path / "state"))
    config = tmp_path / "config.toml"
    config.write_text("version = 1\n", encoding="utf-8")
    generation_snapshot_path(config, 7).mkdir(parents=True)

    with pytest.raises(ConfigError, match="Unknown complete"):
        rollback_generation(config, 7)


def test_missing_and_invalid_toml_are_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        user_config.read_toml(tmp_path / "missing.toml")
    invalid = tmp_path / "invalid.toml"
    invalid.write_text("[broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="Cannot load"):
        user_config.read_toml(invalid)
