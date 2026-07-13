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
