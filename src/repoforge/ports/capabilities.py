from typing import Protocol


class ExecutableLocator(Protocol):
    def which(self, executable: str, *, path: str | None = None) -> str | None: ...
