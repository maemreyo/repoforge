"""Identifier generation boundary for deterministic application tests."""

from typing import Protocol


class IdGenerator(Protocol):
    def new_hex(self, length: int = 10) -> str: ...
