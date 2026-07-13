"""Private optimistic onboarding session persistence boundary."""

from typing import Protocol

from ..domain.onboarding import OnboardingSession


class OnboardingStore(Protocol):
    def create(self, session: OnboardingSession) -> OnboardingSession: ...
    def read(self, session_id: str) -> OnboardingSession | None: ...
    def save(self, session: OnboardingSession, *, expected_revision: int) -> OnboardingSession: ...
    def cancel(
        self, session_id: str, *, expected_revision: int, updated_at: str
    ) -> OnboardingSession: ...
