from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import pytest

import repoforge.onboarding as onboarding
from repoforge.errors import ConfigError
from repoforge.onboarding import (
    _repo_add,
    _repo_inspect,
    _repo_list,
    _repo_refresh,
    _repo_remove,
    _setup,
    _start,
    handle_onboarding_command,
)
from repoforge.runtime import ManagedRuntime
from repoforge.user_config import TunnelSettings, UserConfig, UserRepository, render_user_config


def write_minimal_config(tmp_path: Path, *, repo_ids: tuple[str, ...] = ("demo",)) -> Path:
    config_path = tmp_path / "config.toml"
    repositories = []
    for repo_id in repo_ids:
        repo = tmp_path / repo_id
        repo.mkdir(exist_ok=True)
        repositories.append(UserRepository(repo_id, repo))
    config = UserConfig(
        source_path=config_path,
        tunnel=TunnelSettings("tunnel_test"),
        repositories=tuple(repositories),
    )
    config_path.write_text(render_user_config(config), encoding="utf-8")
    return config_path


class FakeService:
    def __init__(self, *, doctor_ok: bool = True) -> None:
        self.doctor_ok = doctor_ok
        self.calls: list[tuple[str, Any]] = []

    def doctor(self) -> dict[str, Any]:
        return {
            "ok": self.doctor_ok,
            "summary": {"passed": 1, "errors": 0 if self.doctor_ok else 1, "warnings": 0},
        }

    def repo_status(self, repo_id: str) -> dict[str, Any]:
        self.calls.append(("repo_status", repo_id))
        return {}

    def repo_context(self, repo_id: str) -> dict[str, Any]:
        self.calls.append(("repo_context", repo_id))
        return {}

    def workspace_create(
        self, repo_id: str, task_slug: str, base: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("workspace_create", repo_id))
        return {"workspace_id": f"{repo_id}-workspace"}

    def workspace_status(self, workspace_id: str) -> dict[str, Any]:
        self.calls.append(("workspace_status", workspace_id))
        return {}

    def workspace_tree(self, workspace_id: str, max_entries: int = 2000) -> dict[str, Any]:
        self.calls.append(("workspace_tree", workspace_id))
        return {}

    def workspace_diff(self, workspace_id: str, staged: bool = False) -> dict[str, Any]:
        self.calls.append(("workspace_diff", workspace_id))
        return {}

    def workspace_remove(
        self, workspace_id: str, delete_local_branch: bool = False
    ) -> dict[str, Any]:
        self.calls.append(("workspace_remove", workspace_id))
        return {}


def test_setup_writes_minimal_config_runs_doctor_and_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    config_path = tmp_path / "config.toml"
    captured: dict[str, UserConfig] = {}

    monkeypatch.setattr(onboarding, "detect_repository_for_setup", lambda path, repo_id: object())

    def write(config: UserConfig, **_: Any) -> tuple[Path, list[Any]]:
        captured["config"] = config
        config.source_path.write_text(render_user_config(config), encoding="utf-8")
        return tmp_path / "resolved.toml", []

    service = FakeService()
    monkeypatch.setattr(onboarding, "write_user_and_lock", write)
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda config: service)
    monkeypatch.setattr(onboarding, "profile_summary", lambda detections: {})
    monkeypatch.setattr(
        onboarding,
        "_smoke_repository",
        lambda current, repo_id: {"repo_id": repo_id, "ok": True},
    )

    code = _setup(
        argparse.Namespace(
            config=str(config_path),
            force=False,
            repos=[str(repo_a), str(repo_b)],
            tunnel_id="tunnel_test",
            profile="repoforge",
            skip_smoke=False,
        )
    )
    assert code == 0
    assert [repo.repo_id for repo in captured["config"].repositories] == ["repo-a", "repo-b"]
    output = json.loads(capsys.readouterr().out)
    assert output["next"] == "Run `rf start`."
    assert len(output["smoke"]) == 2


def test_setup_rejects_invalid_tunnel_before_writing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "config.toml"
    with pytest.raises(ConfigError, match=r"tunnel\.id"):
        _setup(
            argparse.Namespace(
                config=str(config),
                force=False,
                repos=[str(repo)],
                tunnel_id="bad tunnel",
                profile="repoforge",
                skip_smoke=True,
            )
        )
    assert not config.exists()


def test_setup_refuses_existing_config_without_force(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("existing", encoding="utf-8")
    with pytest.raises(ConfigError, match="already exists"):
        _setup(
            argparse.Namespace(
                config=str(config),
                force=False,
                repos=[str(tmp_path)],
                tunnel_id="tunnel_test",
                profile="repoforge",
                skip_smoke=True,
            )
        )


def test_repo_list_reports_current_and_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    resolved = tmp_path / "resolved.toml"
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: resolved)
    monkeypatch.setattr(onboarding, "resolve_runtime_config_path", lambda path: resolved)
    assert _repo_list(argparse.Namespace(config=str(config))) == 0
    assert json.loads(capsys.readouterr().out)["lock_status"] == "current"

    def stale(path: Path) -> Path:
        raise ConfigError("stale lock")

    monkeypatch.setattr(onboarding, "resolve_runtime_config_path", stale)
    assert _repo_list(argparse.Namespace(config=str(config))) == 0
    assert "stale lock" in json.loads(capsys.readouterr().out)["lock_status"]


def test_repo_add_and_remove_update_config_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    added_path = tmp_path / "second"
    added_path.mkdir()
    writes: list[UserConfig] = []
    monkeypatch.setattr(onboarding, "detect_repository_for_setup", lambda path, repo_id: object())

    def write(updated: UserConfig, **_: Any) -> tuple[Path, list[Any]]:
        writes.append(updated)
        updated.source_path.write_text(render_user_config(updated), encoding="utf-8")
        return tmp_path / "resolved.toml", []

    monkeypatch.setattr(onboarding, "write_user_and_lock", write)
    monkeypatch.setattr(onboarding, "profile_summary", lambda detections: {"second": {}})
    assert (
        _repo_add(argparse.Namespace(config=str(config), path=str(added_path), repo_id="second"))
        == 0
    )
    assert [repo.repo_id for repo in writes[-1].repositories] == ["demo", "second"]
    capsys.readouterr()

    assert _repo_remove(argparse.Namespace(config=str(config), repo_id="second")) == 0
    assert [repo.repo_id for repo in writes[-1].repositories] == ["demo"]
    capsys.readouterr()

    with pytest.raises(ConfigError, match="final repository"):
        _repo_remove(argparse.Namespace(config=str(config), repo_id="demo"))


def test_repo_add_preview_does_not_write_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setattr(onboarding, "detect_repository_for_setup", lambda path, repo_id: object())
    monkeypatch.setattr(
        onboarding, "profile_summary", lambda detections: {"candidate": {"test": {}}}
    )
    monkeypatch.setattr(
        onboarding, "write_user_and_lock", lambda *args, **kwargs: pytest.fail("wrote")
    )

    assert (
        _repo_add(
            argparse.Namespace(
                config=str(config), path=str(candidate), repo_id="candidate", preview=True
            )
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "pending_approval"
    assert output["capability_delta"] == "expansion"
    assert "candidate" not in config.read_text(encoding="utf-8")


def test_repo_inspect_returns_a_no_write_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    detection = type(
        "Detection",
        (),
        {
            "repo_id": "candidate",
            "path": candidate,
            "ecosystem": "python",
            "package_manager": "uv",
            "instruction_files": ("AGENTS.md",),
            "warnings": (),
        },
    )()
    monkeypatch.setattr(onboarding, "detect_repository_for_setup", lambda path, repo_id: detection)
    monkeypatch.setattr(
        onboarding, "profile_summary", lambda detections: {"candidate": {"test": {}}}
    )

    assert _repo_inspect(argparse.Namespace(path=str(candidate), repo_id=None)) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "pending_approval"
    assert output["instruction_files"] == ["AGENTS.md"]


def test_repo_refresh_previews_then_accepts_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    lock = tmp_path / "resolved.toml"
    lock.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: lock)
    monkeypatch.setattr(
        onboarding,
        "build_lock_text",
        lambda config, source, **_: ("[repoforge_lock]\ngeneration = 1\n", []),
    )
    monkeypatch.setattr(onboarding, "profile_summary", lambda detections: {})

    assert _repo_refresh(argparse.Namespace(config=str(config), accept=False)) == 2
    assert lock.read_text(encoding="utf-8") == "old\n"
    assert "No changes written" in capsys.readouterr().err

    assert _repo_refresh(argparse.Namespace(config=str(config), accept=True)) == 0
    assert lock.read_text(encoding="utf-8") == "[repoforge_lock]\ngeneration = 1\n"
    assert json.loads(capsys.readouterr().out)["accepted"] is True

    assert _repo_refresh(argparse.Namespace(config=str(config), accept=False)) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False


def test_start_dry_run_uses_minimal_config_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    resolved = tmp_path / "resolved.toml"
    service = FakeService()
    monkeypatch.setattr(onboarding, "resolve_runtime_config_path", lambda path: resolved)
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda value: service)
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        onboarding, "_repoforge_command", lambda path: ["rf", "--config", str(path), "serve"]
    )
    monkeypatch.setattr(onboarding, "_tunnel_state_path", lambda path: tmp_path / "state.json")

    assert (
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=False,
                dry_run=True,
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["would_initialize"] is True
    assert output["run"] == ["tunnel-client", "run", "--profile", "repoforge"]


def test_start_returns_when_doctor_has_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    monkeypatch.setattr(
        onboarding, "resolve_runtime_config_path", lambda path: tmp_path / "resolved"
    )
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda value: FakeService(doctor_ok=False))
    assert (
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=False,
                dry_run=True,
            )
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_start_initializes_doctors_and_executes_tunnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    state = tmp_path / "state.json"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        onboarding, "resolve_runtime_config_path", lambda path: tmp_path / "resolved"
    )
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda value: FakeService())
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(onboarding, "_repoforge_command", lambda path: ["rf", "serve"])
    monkeypatch.setattr(onboarding, "_tunnel_state_path", lambda path: state)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "runtime-secret")
    monkeypatch.setattr(
        onboarding,
        "_run_checked",
        lambda argv, env, timeout=60: calls.append(list(argv)),
    )

    class Executed(Exception):
        pass

    def execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        assert env["CONTROL_PLANE_API_KEY"] == "runtime-secret"
        calls.append(argv)
        raise Executed

    monkeypatch.setattr(onboarding.os, "execvpe", execute)
    with pytest.raises(Executed):
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=False,
                dry_run=False,
            )
        )
    assert calls[0][1] == "init"
    assert calls[1][1] == "doctor"
    assert calls[2][1] == "run"
    assert "runtime-secret" not in state.read_text(encoding="utf-8")


def test_start_repairs_profile_when_tunnel_doctor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    state = tmp_path / "state.json"
    monkeypatch.setattr(
        onboarding, "resolve_runtime_config_path", lambda path: tmp_path / "resolved"
    )
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda value: FakeService())
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(onboarding, "_repoforge_command", lambda path: ["rf", "serve"])
    monkeypatch.setattr(onboarding, "_tunnel_state_path", lambda path: state)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "runtime-secret")
    attempts = {"doctor": 0}

    def run(argv: list[str], env: dict[str, str], timeout: int = 60) -> None:
        if argv[1] == "doctor":
            attempts["doctor"] += 1
            if attempts["doctor"] == 1:
                raise ConfigError("profile missing")

    monkeypatch.setattr(onboarding, "_run_checked", run)
    monkeypatch.setattr(
        onboarding.os, "execvpe", lambda *args: (_ for _ in ()).throw(RuntimeError())
    )
    with pytest.raises(RuntimeError):
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=True,
                dry_run=False,
            )
        )
    assert attempts["doctor"] == 2


def test_legacy_start_requires_tunnel_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "legacy.toml"
    config.write_text("[repositories.demo]\npath = '/tmp/demo'\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolve_runtime_config_path", lambda path: config)
    with pytest.raises(ConfigError, match="Legacy config"):
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=True,
                dry_run=True,
            )
        )


def test_onboarding_router_handles_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.toml"
    code = handle_onboarding_command(["repo", "--config", str(missing), "list"])
    assert code == 2
    assert "Configuration file not found" in capsys.readouterr().err
    assert handle_onboarding_command(["doctor"]) is None


def test_runtime_status_accepts_config_after_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    lock = tmp_path / "resolved.toml"
    lock.write_text("[repoforge_lock]\ngeneration = 1\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: lock)

    assert handle_onboarding_command(["runtime", "status", "--config", str(config)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "stopped"
    assert output["config_generation"] == 1


def test_runtime_status_reports_starting_for_managed_tunnel_without_mcp_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    lock = tmp_path / "resolved.toml"
    lock.write_text("[repoforge_lock]\ngeneration = 1\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: lock)
    monkeypatch.setattr(
        onboarding,
        "read_managed_runtime",
        lambda path: ManagedRuntime(os.getpid(), 1, "repoforge", "tunnel-client", "now"),
    )

    assert handle_onboarding_command(["runtime", "status", "--config", str(config)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "starting"
    assert output["managed_pid"] == os.getpid()


def test_runtime_start_routes_to_managed_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    captured: dict[str, Any] = {}

    def start(args: argparse.Namespace) -> int:
        captured["managed"] = args.managed
        captured["dry_run"] = args.dry_run
        return 0

    monkeypatch.setattr(onboarding, "_start", start)

    assert handle_onboarding_command(["runtime", "start", "--config", str(config)]) == 0
    assert captured == {"managed": True, "dry_run": False}


def test_runtime_stop_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_minimal_config(tmp_path)

    assert handle_onboarding_command(["runtime", "stop", "--config", str(config)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "stopped"
    assert output["pid"] is None


def test_repo_add_restarts_an_active_managed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_minimal_config(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    lock = tmp_path / "resolved.toml"
    lock.write_text("[repoforge_lock]\ngeneration = 1\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: lock)
    monkeypatch.setattr(onboarding, "detect_repository_for_setup", lambda path, repo_id: object())
    monkeypatch.setattr(onboarding, "profile_summary", lambda detections: {"candidate": {}})
    monkeypatch.setattr(
        onboarding,
        "read_managed_runtime",
        lambda path: ManagedRuntime(os.getpid(), 1, "repoforge", "tunnel-client", "now"),
    )
    monkeypatch.setattr(onboarding, "stop_managed_runtime", lambda path: None)
    starts: list[bool] = []

    def start(args: argparse.Namespace, *, emit: bool = True) -> int:
        starts.append(args.managed)
        return 0

    monkeypatch.setattr(onboarding, "_start", start)

    def write(updated: UserConfig, **_: Any) -> tuple[Path, list[Any]]:
        updated.source_path.write_text(render_user_config(updated), encoding="utf-8")
        lock.write_text("[repoforge_lock]\ngeneration = 2\n", encoding="utf-8")
        return lock, []

    monkeypatch.setattr(onboarding, "write_user_and_lock", write)

    assert (
        _repo_add(
            argparse.Namespace(
                config=str(config), path=str(candidate), repo_id="candidate", preview=False
            )
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert starts == [True]
    assert output["status"] == "active"
    assert output["config_generation"] == 2


def test_managed_activation_rolls_back_when_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    lock = tmp_path / "resolved.toml"
    lock.write_text("[repoforge_lock]\ngeneration = 2\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: lock)
    monkeypatch.setattr(
        onboarding,
        "read_managed_runtime",
        lambda path: ManagedRuntime(os.getpid(), 1, "repoforge", "tunnel-client", "now"),
    )
    monkeypatch.setattr(onboarding, "stop_managed_runtime", lambda path: None)
    monkeypatch.setattr(onboarding, "rollback_generation", lambda path, generation: lock)
    starts = iter([1, 0])
    monkeypatch.setattr(onboarding, "_start", lambda args, *, emit=True: next(starts))

    result = onboarding._activate_managed_runtime(config, 1, rollback_allowed=True)

    assert result["status"] == "rolled_back"
    assert result["config_generation"] == 1


def test_restrictive_activation_failure_does_not_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    lock = tmp_path / "resolved.toml"
    lock.write_text("[repoforge_lock]\ngeneration = 2\n", encoding="utf-8")
    monkeypatch.setattr(onboarding, "resolved_config_path", lambda path: lock)
    monkeypatch.setattr(
        onboarding,
        "read_managed_runtime",
        lambda path: ManagedRuntime(os.getpid(), 1, "repoforge", "tunnel-client", "now"),
    )
    monkeypatch.setattr(onboarding, "stop_managed_runtime", lambda path: None)
    monkeypatch.setattr(onboarding, "_start", lambda args, *, emit=True: 1)
    monkeypatch.setattr(
        onboarding, "rollback_generation", lambda path, generation: pytest.fail("rolled back")
    )

    with pytest.raises(ConfigError, match="restrictive configuration"):
        onboarding._activate_managed_runtime(config, 1, rollback_allowed=False)


def test_smoke_repository_always_removes_workspace() -> None:
    service = FakeService()
    result = onboarding._smoke_repository(service, "demo")
    assert result == {"repo_id": "demo", "ok": True}
    assert service.calls[-1] == ("workspace_remove", "demo-workspace")


def test_repoforge_command_prefers_installed_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        onboarding.shutil, "which", lambda name: "/bin/rf" if name == "rf" else None
    )
    assert onboarding._repoforge_command(tmp_path / "config.toml") == [
        "/bin/rf",
        "--config",
        str(tmp_path / "config.toml"),
        "serve",
    ]
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: None)
    command = onboarding._repoforge_command(tmp_path / "config.toml")
    assert command[1:3] == ["-m", "repoforge"]


def test_run_checked_reports_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 1
        stdout = "out"
        stderr = "err"

    monkeypatch.setattr(onboarding.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(ConfigError, match="Command failed"):
        onboarding._run_checked(["tool", "doctor"], env={})


def test_start_requires_tunnel_client_outside_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    monkeypatch.setattr(
        onboarding, "resolve_runtime_config_path", lambda path: tmp_path / "resolved"
    )
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda value: FakeService())
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: None)
    with pytest.raises(ConfigError, match="tunnel-client"):
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=True,
                dry_run=False,
            )
        )


def test_start_prompts_for_missing_runtime_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_minimal_config(tmp_path)
    monkeypatch.setattr(
        onboarding, "resolve_runtime_config_path", lambda path: tmp_path / "resolved"
    )
    monkeypatch.setattr(onboarding, "load_config", lambda path: path)
    monkeypatch.setattr(onboarding, "CodingService", lambda value: FakeService())
    monkeypatch.setattr(onboarding.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(onboarding, "_repoforge_command", lambda path: ["rf", "serve"])
    monkeypatch.setattr(onboarding, "_tunnel_state_path", lambda path: tmp_path / "state.json")
    monkeypatch.delenv("CONTROL_PLANE_API_KEY", raising=False)

    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(onboarding.sys, "stdin", TtyInput())
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda prompt: "prompt-secret")
    monkeypatch.setattr(onboarding, "_run_checked", lambda *args, **kwargs: None)

    class Executed(Exception):
        pass

    def execute(file: str, argv: list[str], env: dict[str, str]) -> None:
        assert env["CONTROL_PLANE_API_KEY"] == "prompt-secret"
        raise Executed

    monkeypatch.setattr(onboarding.os, "execvpe", execute)
    with pytest.raises(Executed):
        _start(
            argparse.Namespace(
                config=str(config),
                tunnel_id=None,
                profile=None,
                skip_doctor=True,
                dry_run=False,
            )
        )
