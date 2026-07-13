"""Application-level environment preflight service."""

from pathlib import Path

from ...ports.onboarding_environment import EnvironmentPreflight, OnboardingEnvironment


class OnboardingPreflightService:
    def __init__(self, environment: OnboardingEnvironment):
        self._environment = environment

    def inspect(self, config_path: Path) -> EnvironmentPreflight:
        return self._environment.inspect(config_path)
