"""Persistence boundary for durable pull-request check watches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.pr_check_watch import PrCheckWatch


@dataclass(frozen=True, slots=True)
class PrCheckWatchPage:
    records: tuple[PrCheckWatch, ...]
    scan_truncated: bool


class PrCheckWatchStore(Protocol):
    def create(self, watch: PrCheckWatch) -> PrCheckWatch: ...

    def read(self, operation_id: str) -> PrCheckWatch | None: ...

    def save(
        self,
        watch: PrCheckWatch,
        *,
        expected_updated_at: str,
    ) -> PrCheckWatch: ...

    def list_records(self, *, max_records: int) -> PrCheckWatchPage: ...

    def delete(self, operation_id: str) -> None: ...
