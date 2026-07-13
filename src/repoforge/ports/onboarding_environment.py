"""Environment preflight boundary for guided onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EnvironmentPreflight:
    current_rf: str
    python: str
    virtual_env: str | None
    uv_tool_rf: str | None
    git_version: str | None
    gh_version: str | None
    gh_authenticated: bool
    tunnel_version: str | None
    config_exists: bool
    api_key_available: bool
    warnings: tuple[str, ...]


class OnboardingEnvironment(Protocol):
    def inspect(self, config_path: Path) -> EnvironmentPreflight: ...
