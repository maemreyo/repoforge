import subprocess
from pathlib import Path

from repoforge.adapters.onboarding_environment import SystemOnboardingEnvironment


def test_preflight_warns_when_virtualenv_rf_shadows_uv_tool(tmp_path: Path, monkeypatch) -> None:
    venv = tmp_path / "venv"
    current = venv / "bin" / "rf"
    current.parent.mkdir(parents=True)
    current.write_text("")
    tool = tmp_path / "tool-rf"
    tool.write_text("")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.setattr("shutil.which", lambda name: str(current) if name == "rf" else None)
    monkeypatch.setattr(SystemOnboardingEnvironment, "_uv_tool_rf", staticmethod(lambda: str(tool)))
    result = SystemOnboardingEnvironment().inspect(tmp_path / "config.toml")
    assert "EXECUTABLE_SHADOWED" in result.warnings


def test_preflight_warns_when_gh_is_not_installed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = SystemOnboardingEnvironment().inspect(tmp_path / "config.toml")

    assert result.gh_version is None
    assert result.gh_authenticated is False
    assert "GH_NOT_INSTALLED" in result.warnings
    assert "GH_NOT_AUTHENTICATED" not in result.warnings


def test_preflight_warns_when_gh_is_installed_but_not_authenticated(
    tmp_path: Path, monkeypatch
) -> None:
    fake_gh = "/usr/local/bin/gh"
    monkeypatch.setattr("shutil.which", lambda name: fake_gh if name == "gh" else None)

    def fake_run(argv, **kwargs):
        if argv[:2] == [fake_gh, "auth"]:
            return subprocess.CompletedProcess(argv, 1)
        return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.0.0\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SystemOnboardingEnvironment().inspect(tmp_path / "config.toml")

    assert result.gh_authenticated is False
    assert "GH_NOT_AUTHENTICATED" in result.warnings
    assert "GH_NOT_INSTALLED" not in result.warnings
