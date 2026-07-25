"""Round-6 finding 8: CLI-level regressions for the durable agent secret (F3)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.interfaces.cli.main import main

_SECRET = "sandbox-only-never-networked"


def _usable_release(root: Path) -> None:
    """A release the agent could actually launch, so only the secret is under test."""
    release = root / "releases" / "abc1234"
    (release / "venv" / "bin").mkdir(parents=True)
    (release / "venv" / "bin" / "python").touch()
    (release / ".manifest.json").write_text(
        json.dumps(
            {
                "commit_sha": "abc1234",
                "package_version": "2.2.0",
                "build_fingerprint": "a" * 64,
                "tool_surface_hash": "b" * 64,
                "source_worktree": "/src",
                "built_at": "2026-07-25T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (root / "current").symlink_to(Path("releases") / "abc1234")


def _stub_launchctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never run the real `launchctl` from a test: it would touch the user's session."""
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir(exist_ok=True)
    launchctl = shim_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")


def _install_agent(root: Path, *, persist: bool = False) -> int:
    argv = ["runtime", "install-agent", "--release-root", str(root)]
    if persist:
        argv.append("--persist-api-key")
    return main(argv)


def test_install_agent_refuses_without_a_durable_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "release-root"
    root.mkdir()
    _usable_release(root)
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _stub_launchctl(monkeypatch, tmp_path)

    assert _install_agent(root) != 0

    payload = capsys.readouterr().out
    assert "AGENT_SECRET_UNUSABLE" in payload
    # No LaunchAgent may be registered when the agent could not start.
    assert not (tmp_path / "home" / "Library" / "LaunchAgents").exists()
    # The refusal must not leak the operator's home directory.
    assert str(Path.home()) not in payload


def test_persist_api_key_refuses_when_the_environment_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "release-root"
    root.mkdir()
    _usable_release(root)
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _stub_launchctl(monkeypatch, tmp_path)

    assert _install_agent(root, persist=True) != 0
    assert "CONTROL_PLANE_API_KEY_ABSENT" in capsys.readouterr().out


def test_persist_api_key_stores_it_owner_only_and_never_prints_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "release-root"
    root.mkdir()
    _usable_release(root)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", _SECRET)
    # Never touch the operator's real LaunchAgents directory from a test.
    # `~` is expanded from $HOME, so patching Path.home() would NOT redirect it: an
    # earlier version of this test therefore installed a real LaunchAgent on the
    # developer's machine. Redirect HOME, and stub launchctl so nothing is bootstrapped.
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _stub_launchctl(monkeypatch, tmp_path)

    _install_agent(root, persist=True)

    store = RuntimeReleaseStore(root)
    assert oct(store.agent_env_path().stat().st_mode & 0o777) == "0o600"
    assert store.agent_secret_status().usable is True
    # The secret value must never appear in command output.
    assert _SECRET not in capsys.readouterr().out


def test_agent_status_reports_metadata_but_never_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "release-root"
    root.mkdir()
    store = RuntimeReleaseStore(root)
    store.write_agent_env({"CONTROL_PLANE_API_KEY": _SECRET})
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _stub_launchctl(monkeypatch, tmp_path)

    main(["runtime", "agent-status", "--release-root", str(root)])

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["durable_secret_usable"] is True
    assert payload["agent_env_keys"] == ["CONTROL_PLANE_API_KEY"]
    assert payload["durable_secret_checks"]["mode_secure"] is True
    assert _SECRET not in out
