"""Generation-scoped service containers and atomic in-process hot reload."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from ...domain.errors import ConfigError
from ...ports.operation_gate import OperationGate


@dataclass(frozen=True, slots=True)
class GenerationServiceContainer:
    """One complete immutable application graph for a reviewed generation."""

    generation: int
    service: Any
    gate: OperationGate
    repository_ids: frozenset[str]
    dispose: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if self.generation <= 0:
            raise ValueError("Service container generation must be positive")


class AtomicServiceRouter:
    """Pin each request to one complete generation and retire old graphs safely."""

    def __init__(self, initial: GenerationServiceContainer) -> None:
        self._condition = threading.Condition()
        self._active = initial
        self._active_requests: dict[int, int] = {initial.generation: 0}
        self._retired: dict[int, GenerationServiceContainer] = {}
        self._closed = False

    @property
    def active_generation(self) -> int:
        with self._condition:
            return self._active.generation

    def active_container(self) -> GenerationServiceContainer:
        with self._condition:
            return self._active

    @contextmanager
    def acquire(self) -> Iterator[GenerationServiceContainer]:
        with self._condition:
            if self._closed:
                raise ConfigError("RUNTIME_STOPPING: service router is closed")
            selected = self._active
            generation = selected.generation
            self._active_requests[generation] = self._active_requests.get(generation, 0) + 1
        try:
            yield selected
        finally:
            dispose: GenerationServiceContainer | None = None
            with self._condition:
                remaining = self._active_requests[generation] - 1
                self._active_requests[generation] = remaining
                if remaining == 0 and generation in self._retired:
                    dispose = self._retired.pop(generation)
                    self._active_requests.pop(generation, None)
                self._condition.notify_all()
            self.dispose(dispose)

    def _validate_swap_locked(self, candidate: GenerationServiceContainer) -> None:
        if self._closed:
            raise ConfigError("RUNTIME_STOPPING: cannot hot reload a closed service router")
        previous = self._active
        if candidate.generation == previous.generation:
            raise ConfigError(
                f"HOT_RELOAD_SAME_GENERATION: generation {candidate.generation} is already active"
            )
        if candidate.generation in self._retired:
            raise ConfigError(
                f"HOT_RELOAD_GENERATION_RETIRED: generation {candidate.generation} is draining"
            )

    def _install_locked(
        self, candidate: GenerationServiceContainer
    ) -> tuple[GenerationServiceContainer, GenerationServiceContainer | None]:
        previous = self._active
        self._active = candidate
        self._active_requests.setdefault(candidate.generation, 0)
        dispose: GenerationServiceContainer | None = None
        if self._active_requests.get(previous.generation, 0) == 0:
            self._active_requests.pop(previous.generation, None)
            dispose = previous
        else:
            self._retired[previous.generation] = previous
        self._condition.notify_all()
        return previous, dispose

    def commit_swap(
        self,
        candidate: GenerationServiceContainer,
        commit: Callable[[], object],
    ) -> GenerationServiceContainer:
        """Serialize durable activation and pointer swap against new request acquisition.

        Existing requests continue on their pinned container. New acquisitions wait while the
        durable active-generation pointer is committed, then observe only the complete candidate.
        """
        with self._condition:
            self._validate_swap_locked(candidate)
            commit()
            previous, dispose = self._install_locked(candidate)
        self.dispose(dispose)
        return previous

    def swap(self, candidate: GenerationServiceContainer) -> GenerationServiceContainer:
        """Atomically expose candidate when no external durable commit is required."""
        return self.commit_swap(candidate, lambda: None)

    def snapshot(self) -> dict[str, object]:
        with self._condition:
            return {
                "active_generation": self._active.generation,
                "active_requests": dict(sorted(self._active_requests.items())),
                "retired_generations": sorted(self._retired),
                "closed": self._closed,
            }

    def wait_for_retired(self, generation: int, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while generation in self._retired:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return generation != self._active.generation

    def close(self, *, timeout_seconds: float = 30.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        dispose: list[GenerationServiceContainer] = []
        with self._condition:
            self._closed = True
            while any(self._active_requests.values()):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            dispose.extend(self._retired.values())
            self._retired.clear()
            dispose.append(self._active)
            self._active_requests.clear()
            self._condition.notify_all()
        for container in dispose:
            self.dispose(container)
        return True

    @staticmethod
    def dispose(container: GenerationServiceContainer | None) -> None:
        if container is not None and container.dispose is not None:
            with suppress(Exception):
                container.dispose()


@dataclass(frozen=True, slots=True)
class HotReloadResult:
    status: str
    previous_generation: int
    active_generation: int
    correlation_id: str
    retired_generation: int | None
    repository_ids: tuple[str, ...]


class HotReloadCoordinator:
    """Build and validate a candidate before committing one atomic router swap."""

    def __init__(
        self,
        *,
        router: AtomicServiceRouter,
        build_candidate: Callable[[int], GenerationServiceContainer],
        commit_activation: Callable[[int, int | None], object],
    ) -> None:
        self._router = router
        self._build_candidate = build_candidate
        self._commit_activation = commit_activation
        self._lock = threading.Lock()

    def reload(
        self,
        generation: int,
        *,
        expected_active: int | None,
        correlation_id: str,
    ) -> HotReloadResult:
        with self._lock:
            current = self._router.active_generation
            if expected_active is not None and current != expected_active:
                raise ConfigError(
                    f"STALE_ACTIVE_GENERATION: expected {expected_active}, found {current}"
                )
            candidate: GenerationServiceContainer | None = None
            try:
                candidate = self._build_candidate(generation)
                if candidate.generation != generation:
                    raise ConfigError(
                        f"Candidate generation mismatch: expected {generation}, "
                        f"built {candidate.generation}"
                    )
                repositories = candidate.service.repo_list(synthetic=True).get("repositories", [])
                observed_ids = frozenset(
                    str(item["repo_id"])
                    for item in repositories
                    if isinstance(item, dict) and "repo_id" in item
                )
                if observed_ids != candidate.repository_ids:
                    raise ConfigError(
                        "Candidate self-check repository set does not match its immutable metadata"
                    )
            except Exception as exc:
                if candidate is not None:
                    self._router.dispose(candidate)
                if isinstance(exc, ConfigError) and str(exc).startswith(
                    "HOT_RELOAD_RESTART_REQUIRED"
                ):
                    raise
                raise ConfigError(
                    f"HOT_RELOAD_CANDIDATE_FAILED: {type(exc).__name__}: {exc}"
                ) from exc
            try:
                previous = self._router.commit_swap(
                    candidate,
                    lambda: self._commit_activation(generation, current),
                )
            except Exception as exc:
                self._router.dispose(candidate)
                raise ConfigError(f"HOT_RELOAD_COMMIT_FAILED: {type(exc).__name__}: {exc}") from exc
            return HotReloadResult(
                "hot_reloaded",
                previous.generation,
                candidate.generation,
                correlation_id,
                previous.generation,
                tuple(sorted(candidate.repository_ids)),
            )
