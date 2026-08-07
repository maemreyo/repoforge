"""Semantic canary promotion gates for managed CodeGraph providers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ...domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceStatus,
    SemanticGraphEvidence,
)
from ...ports.clock import Clock
from .augment import SemanticGraphProvider
from .receipts import (
    PromotionGateOutcome,
    PromotionIdentity,
    PromotionReceipt,
    PromotionReceiptStore,
)


@dataclass(frozen=True, slots=True, order=True)
class CanaryEdge:
    kind: str
    source_path: str
    source_symbol: str
    target_path: str
    target_symbol: str


@dataclass(frozen=True, slots=True)
class CanaryObservation:
    canonical_digest: str
    relationships: tuple[CanaryEdge, ...]
    affected_paths: tuple[str, ...]
    projected_paths: tuple[str, ...]
    limitations: tuple[str, ...]
    schema_valid: bool
    unsupported_explicit: bool
    incremental_deletion_clean: bool
    cleanup_confirmed: bool
    source_digest_before: str
    source_digest_after: str


class CanaryProbe(Protocol):
    def run(
        self,
        identity: PromotionIdentity,
        timeout_seconds: int,
    ) -> tuple[CanaryObservation, CanaryObservation]: ...


class CodeGraphPromotionError(ValueError):
    pass


REQUIRED_CANARY_EDGES = (
    CanaryEdge("calls", "src/alpha.py", "src.alpha.alpha", "src/beta.py", "beta"),
    CanaryEdge("calls", "web/root.ts", "web.root.root", "web/leaf.ts", "leaf"),
)
FORBIDDEN_CANARY_EDGES = (
    CanaryEdge(
        "calls",
        "src/duplicate_a.py",
        "src.duplicate_a.duplicate",
        "src/duplicate_b.py",
        "src.duplicate_b.duplicate",
    ),
)
REQUIRED_CANARY_TESTS = ("tests/test_alpha.py", "web/root.test.ts")
FORBIDDEN_CANARY_TESTS = ("tests/test_unrelated.py",)
DENIED_CANARY_PATHS = ("excluded/blocked_input.py",)


def _gate(name: str, passed: bool, observed: int) -> PromotionGateOutcome:
    return PromotionGateOutcome(name, passed, max(0, observed))


def _gates(first: CanaryObservation, second: CanaryObservation) -> tuple[PromotionGateOutcome, ...]:
    required_edges = set(REQUIRED_CANARY_EDGES)
    forbidden_edges = set(FORBIDDEN_CANARY_EDGES)
    required_tests = set(REQUIRED_CANARY_TESTS)
    forbidden_tests = set(FORBIDDEN_CANARY_TESTS)
    excluded_paths = set(DENIED_CANARY_PATHS)
    observations = (first, second)
    edge_sets = tuple(set(item.relationships) for item in observations)
    test_sets = tuple(set(item.affected_paths) for item in observations)
    projected_sets = tuple(set(item.projected_paths) for item in observations)
    return (
        _gate(
            "required_edges",
            all(required_edges.issubset(edges) for edges in edge_sets),
            min(len(required_edges & edges) for edges in edge_sets),
        ),
        _gate(
            "forbidden_edges",
            all(not (forbidden_edges & edges) for edges in edge_sets),
            sum(len(forbidden_edges & edges) for edges in edge_sets),
        ),
        _gate(
            "affected_test_recall",
            all(required_tests.issubset(tests) for tests in test_sets),
            min(len(required_tests & tests) for tests in test_sets),
        ),
        _gate(
            "forbidden_affected_tests",
            all(not (forbidden_tests & tests) for tests in test_sets),
            sum(len(forbidden_tests & tests) for tests in test_sets),
        ),
        _gate(
            "deterministic_clean_rerun",
            first.canonical_digest == second.canonical_digest,
            int(first.canonical_digest == second.canonical_digest),
        ),
        _gate("schema_valid", all(item.schema_valid for item in observations), 2),
        _gate(
            "unsupported_explicit",
            all(item.unsupported_explicit for item in observations),
            sum(item.unsupported_explicit for item in observations),
        ),
        _gate(
            "incremental_deletion",
            all(item.incremental_deletion_clean for item in observations),
            sum(item.incremental_deletion_clean for item in observations),
        ),
        _gate(
            "cleanup_confirmed",
            all(item.cleanup_confirmed for item in observations),
            sum(item.cleanup_confirmed for item in observations),
        ),
        _gate(
            "excluded_paths_absent",
            all(not (excluded_paths & paths) for paths in projected_sets),
            sum(len(excluded_paths & paths) for paths in projected_sets),
        ),
        _gate(
            "source_unchanged",
            all(item.source_digest_before == item.source_digest_after for item in observations),
            sum(item.source_digest_before == item.source_digest_after for item in observations),
        ),
    )


class CodeGraphCanaryRunner:
    def __init__(
        self,
        identity: PromotionIdentity,
        store: PromotionReceiptStore,
        probe: CanaryProbe,
        clock: Clock,
        *,
        timeout_seconds: int,
    ) -> None:
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds < 1
        ):
            raise ValueError("canary timeout must be a positive integer")
        self.identity = identity
        self._store = store
        self._probe = probe
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    def ensure_promoted(self) -> PromotionReceipt:
        cached = self._store.load(self.identity)
        if cached is not None:
            return cached
        try:
            first, second = self._probe.run(self.identity, self._timeout_seconds)
        except Exception as exc:
            raise CodeGraphPromotionError(f"canary_probe_{type(exc).__name__} failed") from exc
        gates = _gates(first, second)
        failed = tuple(gate.name for gate in gates if not gate.passed)
        if failed:
            raise CodeGraphPromotionError("CodeGraph promotion gates failed: " + ", ".join(failed))
        metrics = (
            ("affected_path_count", len(first.affected_paths)),
            ("projected_path_count", len(first.projected_paths)),
            ("relationship_count", len(first.relationships)),
        )
        receipt = PromotionReceipt(
            self.identity,
            gates,
            metrics,
            self._clock.now_iso(),
        )
        self._store.save(receipt)
        return receipt


class PromotedCodeGraphProvider:
    def __init__(
        self,
        delegate: SemanticGraphProvider,
        promotion: CodeGraphCanaryRunner,
    ) -> None:
        self._delegate = delegate
        self._promotion = promotion

    @property
    def provider_id(self) -> str:
        return self._delegate.provider_id

    @property
    def provider_version(self) -> str:
        return self._delegate.provider_version

    def analyze(
        self,
        request: CodeIntelligenceRequest,
        baseline: CodeIntelligenceResult,
    ) -> SemanticGraphEvidence:
        try:
            receipt = self._promotion.ensure_promoted()
        except Exception as exc:
            reason = "Managed CodeGraph promotion evidence is unavailable."
            return SemanticGraphEvidence(
                self.provider_id,
                self.provider_version,
                CodeIntelligenceStatus.UNAVAILABLE,
                CodeIntelligenceMeasure(0, reason),
                CodeIntelligenceMeasure(0, reason),
                limitations=(
                    "Managed CodeGraph promotion stopped at a reviewed boundary "
                    f"({type(exc).__name__}).",
                ),
            )
        graph = self._delegate.analyze(request, baseline)
        if graph.status is not CodeIntelligenceStatus.CURRENT:
            return graph
        return SemanticGraphEvidence(
            graph.provider_id,
            graph.provider_version,
            graph.status,
            graph.coverage,
            CodeIntelligenceMeasure(
                100,
                "Semantic confidence is backed by a matching successful promotion receipt "
                f"{receipt.identity.digest[:12]}.",
            ),
            graph.relationships,
            graph.affected_paths,
            graph.limitations,
            graph.truncated,
        )


def canary_corpus_digest(root: Path) -> str:
    corpus = root.expanduser().resolve()
    if not corpus.is_dir():
        raise ValueError("CodeGraph canary corpus must be a directory")
    digest = hashlib.sha256()
    files = tuple(sorted(path for path in corpus.rglob("*") if path.is_file()))
    if not files:
        raise ValueError("CodeGraph canary corpus must contain files")
    for path in files:
        if path.is_symlink():
            raise ValueError("CodeGraph canary corpus must not contain symlinks")
        relative = path.relative_to(corpus).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


__all__ = [
    "DENIED_CANARY_PATHS",
    "FORBIDDEN_CANARY_EDGES",
    "FORBIDDEN_CANARY_TESTS",
    "REQUIRED_CANARY_EDGES",
    "REQUIRED_CANARY_TESTS",
    "CanaryEdge",
    "CanaryObservation",
    "CanaryProbe",
    "CodeGraphCanaryRunner",
    "CodeGraphPromotionError",
    "PromotedCodeGraphProvider",
    "canary_corpus_digest",
]
