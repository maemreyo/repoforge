"""Bounded environment/tool preflight for guided onboarding."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..ports.onboarding_environment import EnvironmentPreflight


class SystemOnboardingEnvironment:
    @staticmethod
    def _version(argv: list[str]) -> str | None:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        text = (result.stdout or result.stderr).strip()
        return text.splitlines()[0][:300] if result.returncode == 0 and text else None

    @staticmethod
    def _uv_tool_rf() -> str | None:
        uv = shutil.which("uv")
        if uv:
            try:
                result = subprocess.run(
                    [uv, "tool", "dir", "--bin"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if result.returncode == 0:
                    candidate = Path(result.stdout.strip()) / "rf"
                    if candidate.is_file():
                        return str(candidate.resolve())
            except (OSError, subprocess.SubprocessError):
                pass
        fallback = Path.home() / ".local" / "bin" / "rf"
        return str(fallback.resolve()) if fallback.is_file() else None

    def inspect(self, config_path: Path) -> EnvironmentPreflight:
        current = shutil.which("rf") or sys.argv[0]
        current_path = str(Path(current).expanduser().resolve()) if current else "rf"
        virtual_env = os.environ.get("VIRTUAL_ENV")
        uv_tool_rf = self._uv_tool_rf()
        warnings: list[str] = []
        if virtual_env and uv_tool_rf:
            try:
                shadowed = Path(current_path).is_relative_to(
                    Path(virtual_env).expanduser().resolve()
                )
            except ValueError:
                shadowed = False
            if shadowed and Path(current_path) != Path(uv_tool_rf):
                warnings.append("EXECUTABLE_SHADOWED")
        gh = shutil.which("gh")
        gh_authenticated = False
        if gh:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                gh_authenticated = (
                    subprocess.run(
                        [gh, "auth", "status"], capture_output=True, check=False, timeout=10
                    ).returncode
                    == 0
                )
        return EnvironmentPreflight(
            current_rf=current_path,
            python=str(Path(sys.executable).resolve()),
            virtual_env=virtual_env,
            uv_tool_rf=uv_tool_rf,
            git_version=self._version([shutil.which("git") or "git", "--version"]),
            gh_version=self._version([gh, "--version"]) if gh else None,
            gh_authenticated=gh_authenticated,
            tunnel_version=self._version(
                [shutil.which("tunnel-client") or "tunnel-client", "--version"]
            )
            if shutil.which("tunnel-client")
            else None,
            config_exists=config_path.expanduser().is_file(),
            api_key_available=bool(os.environ.get("CONTROL_PLANE_API_KEY")),
            warnings=tuple(warnings),
        )
