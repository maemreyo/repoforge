"""Boundary for registering the runtime supervisor with an OS process manager.

The supervisor process already exists (worker -> tunnel-client -> serve child); this
port is only about making it OS-resident so it survives reboot/crash independently of
whoever ran ``rf start``. launchd is the darwin implementation; systemd would be a
future adapter behind the same port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RegistrarResult:
    status: str
    detail: str
    unit_path: str


@dataclass(frozen=True, slots=True)
class RegistrarStatus:
    registered: bool
    loaded: bool
    detail: str
    unit_path: str


class ProcessSupervisorRegistrar(Protocol):
    def install(self) -> RegistrarResult: ...
    def uninstall(self) -> RegistrarResult: ...
    def status(self) -> RegistrarStatus: ...
