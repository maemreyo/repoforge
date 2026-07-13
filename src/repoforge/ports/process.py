"""Process identity boundary used by runtime supervision."""

from typing import Protocol


class ProcessInspector(Protocol):
    def identity(self, pid: int) -> str | None: ...
