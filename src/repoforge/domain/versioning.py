"""Shared positive version and revision primitives."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class SchemaVersion:
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("schema version must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class Revision:
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("revision must be a positive integer")

    def next(self) -> Revision:
        return Revision(self.value + 1)
