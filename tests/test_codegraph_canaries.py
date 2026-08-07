from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from codegraph_provider_support import baseline, manifest, request

from repoforge.adapters.code_intelligence.calibration import calibrated_confidence
from repoforge.adapters.codegraph.canaries import (
    DENIED_CANARY_PATHS,
    FORBIDDEN_CANARY_EDGES,
    FORBIDDEN_CANARY_TESTS,
    REQUIRED_CANARY_EDGES,
    REQUIRED_CANARY_TESTS,
    CanaryObservation,
    CodeGraphCanaryRunner,
    CodeGraphPromotionError,
    PromotedCodeGraphProvider,
    canary_corpus_digest,
)
from repoforge.adapters.codegraph.canary_probe import ManagedCodeGraphCanaryProbe
from repoforge.adapters.codegraph.receipts import (
    PromotionGateOutcome,
    PromotionIdentity,
    PromotionReceipt,
    PromotionReceiptStore,
    promotion_identity,
)
from repoforge.adapters.locking import FcntlLockManager
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceStatus,
    CodeLanguage,
    SemanticGraphEvidence,
)


class Clock:
    def now_iso(self) -> str:
        return "2026-08-06T00:00:00+00:00"


class Probe:
    def __init__(self, observations: tuple[CanaryObservation, CanaryObservation]) -> None:
        self.observations = observations
        self.calls: list[tuple[PromotionIdentity, int]] = []

    def run(
        self,
        identity: PromotionIdentity,
        timeout_seconds: int,
    ) -> tuple[CanaryObservation, CanaryObservation]:
        self.calls.append((identity, timeout_seconds))
        return self.observations


def _identity(**changes: object) -> PromotionIdentity:
    identity = PromotionIdentity(
        executable_digest="a" * 64,
        provider_version="1.5.0",
        platform="darwin",
        architecture="arm64",
        manifest_hash="b" * 64,
        options_digest="c" * 64,
        adapter_schema_version=1,
        corpus_digest="d" * 64,
    )
    return replace(identity, **changes)


def _observation(**changes: object) -> CanaryObservation:
    observation = CanaryObservation(
        canonical_digest="e" * 64,
        relationships=REQUIRED_CANARY_EDGES,
        affected_paths=REQUIRED_CANARY_TESTS,
        projected_paths=(
            "src/alpha.py",
            "src/beta.py",
            "tests/test_alpha.py",
            "web/leaf.ts",
            "web/root.test.ts",
            "web/root.ts",
        ),
        limitations=("Unsupported canary input was explicitly omitted.",),
        schema_valid=True,
        unsupported_explicit=True,
        incremental_deletion_clean=True,
        cleanup_confirmed=True,
        source_digest_before="f" * 64,
        source_digest_after="f" * 64,
    )
    return replace(observation, **changes)


def _runner(tmp_path: Path, probe: Probe) -> CodeGraphCanaryRunner:
    return CodeGraphCanaryRunner(
        _identity(),
        PromotionReceiptStore(tmp_path, FcntlLockManager(tmp_path / "locks")),
        probe,
        Clock(),
        timeout_seconds=37,
    )


def test_corpus_digest_is_deterministic_and_content_addressed(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "codegraph_canary"
    first = canary_corpus_digest(source)
    second = canary_corpus_digest(source)
    copied = tmp_path / "corpus"
    copied.mkdir()
    for path in source.rglob("*"):
        if path.is_file():
            target = copied / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())

    assert first == second == canary_corpus_digest(copied)
    (copied / "src" / "beta.py").write_text("def beta(): return 2\n", encoding="utf-8")
    assert canary_corpus_digest(copied) != first


def test_codegraph_calibration_matches_canary_recall() -> None:
    python_score, python_reason = calibrated_confidence(
        "codegraph",
        frozenset({CodeLanguage.PYTHON}),
    )
    assert python_score == 100
    assert "codegraph" in python_reason
    assert "python" in python_reason
    assert calibrated_confidence("codegraph", frozenset({CodeLanguage.TYPESCRIPT}))[0] == 100


def test_promotion_identity_changes_for_every_identity_field() -> None:
    base = _identity()
    replacements = {
        "executable_digest": "1" * 64,
        "provider_version": "1.5.1",
        "platform": "linux",
        "architecture": "x86_64",
        "manifest_hash": "2" * 64,
        "options_digest": "3" * 64,
        "adapter_schema_version": 2,
        "corpus_digest": "4" * 64,
    }

    assert len(base.digest) == 64
    for field, value in replacements.items():
        assert replace(base, **{field: value}).digest != base.digest


def test_promotion_identity_uses_reviewed_manifest_fields() -> None:
    provider = manifest()
    identity = promotion_identity(
        provider,
        "d" * 64,
        platform_name="darwin",
        architecture="arm64",
    )

    assert identity.executable_digest == "c" * 64
    assert identity.provider_version == provider.version
    assert identity.manifest_hash == provider.manifest_hash
    assert identity.options_digest == provider.codegraph.options_digest  # type: ignore[union-attr]


def test_receipt_store_round_trips_only_successful_canonical_receipts(tmp_path: Path) -> None:
    store = PromotionReceiptStore(tmp_path, FcntlLockManager(tmp_path / "locks"))
    receipt = PromotionReceipt(
        _identity(),
        (PromotionGateOutcome("required_edges", True, 2),),
        (("relationship_count", 2),),
        "2026-08-06T00:00:00+00:00",
    )

    store.save(receipt)

    assert store.load(receipt.identity) == receipt
    assert store.load(_identity(provider_version="1.5.1")) is None
    failed = replace(receipt, gates=(PromotionGateOutcome("required_edges", False, 0),))
    with pytest.raises(ValueError, match="successful"):
        store.save(failed)


def test_canary_probe_rejects_symlink_corpus_directory(tmp_path: Path) -> None:
    managed = tmp_path / "providers" / "codegraph"
    outside = tmp_path / "outside"
    managed.mkdir(parents=True)
    outside.mkdir()
    (managed / "canary-corpus").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        ManagedCodeGraphCanaryProbe(
            manifest(),
            tmp_path,
            FcntlLockManager(tmp_path / "locks"),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )


def test_receipt_store_rejects_symlink_promotion_directory(tmp_path: Path) -> None:
    managed = tmp_path / "providers" / "codegraph"
    outside = tmp_path / "outside"
    managed.mkdir(parents=True)
    outside.mkdir()
    (managed / "promotion").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        PromotionReceiptStore(tmp_path, FcntlLockManager(tmp_path / "locks"))


def test_receipt_store_rejects_corruption_and_symlink(tmp_path: Path) -> None:
    store = PromotionReceiptStore(tmp_path, FcntlLockManager(tmp_path / "locks"))
    receipt = PromotionReceipt(
        _identity(),
        (PromotionGateOutcome("required_edges", True, 2),),
        (),
        "2026-08-06T00:00:00+00:00",
    )
    store.save(receipt)
    receipt_path = next((tmp_path / "providers" / "codegraph" / "promotion").glob("*.json"))
    receipt_path.write_text('{"raw":"provider-private-detail"}', encoding="utf-8")
    assert store.load(receipt.identity) is None
    receipt_path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    receipt_path.symlink_to(target)
    assert store.load(receipt.identity) is None


def test_successful_canary_is_cached_and_receipted(tmp_path: Path) -> None:
    probe = Probe((_observation(), _observation()))
    runner = _runner(tmp_path, probe)

    first = runner.ensure_promoted()
    second = runner.ensure_promoted()

    assert first == second
    assert first.passed is True
    assert len(probe.calls) == 1
    assert probe.calls[0][1] == 37
    assert {gate.name for gate in first.gates} >= {
        "required_edges",
        "forbidden_edges",
        "affected_test_recall",
        "deterministic_clean_rerun",
        "schema_valid",
        "unsupported_explicit",
        "incremental_deletion",
        "cleanup_confirmed",
        "excluded_paths_absent",
        "source_unchanged",
    }


@pytest.mark.parametrize(
    ("first", "second", "gate"),
    [
        (replace(_observation(), relationships=()), _observation(), "required_edges"),
        (
            replace(
                _observation(),
                relationships=(*REQUIRED_CANARY_EDGES, *FORBIDDEN_CANARY_EDGES),
            ),
            _observation(),
            "forbidden_edges",
        ),
        (replace(_observation(), affected_paths=()), _observation(), "affected_test_recall"),
        (
            replace(
                _observation(),
                affected_paths=(*REQUIRED_CANARY_TESTS, *FORBIDDEN_CANARY_TESTS),
            ),
            _observation(),
            "forbidden_affected_tests",
        ),
        (_observation(), replace(_observation(), canonical_digest="1" * 64), "deterministic"),
        (replace(_observation(), schema_valid=False), _observation(), "schema_valid"),
        (
            replace(_observation(), unsupported_explicit=False),
            _observation(),
            "unsupported_explicit",
        ),
        (
            replace(_observation(), incremental_deletion_clean=False),
            _observation(),
            "incremental_deletion",
        ),
        (replace(_observation(), cleanup_confirmed=False), _observation(), "cleanup_confirmed"),
        (
            replace(
                _observation(),
                projected_paths=(*_observation().projected_paths, *DENIED_CANARY_PATHS),
            ),
            _observation(),
            "excluded_paths_absent",
        ),
        (
            replace(_observation(), source_digest_after="2" * 64),
            _observation(),
            "source_unchanged",
        ),
    ],
)
def test_canary_gate_failures_do_not_write_receipts(
    tmp_path: Path,
    first: CanaryObservation,
    second: CanaryObservation,
    gate: str,
) -> None:
    probe = Probe((first, second))
    runner = _runner(tmp_path, probe)

    with pytest.raises(CodeGraphPromotionError, match=gate):
        runner.ensure_promoted()

    assert (
        PromotionReceiptStore(
            tmp_path,
            FcntlLockManager(tmp_path / "locks"),
        ).load(_identity())
        is None
    )


class Delegate:
    provider_id = "codegraph"
    provider_version = "1.5.0"

    def __init__(self, graph: SemanticGraphEvidence) -> None:
        self.graph = graph
        self.calls = 0

    def analyze(self, request, baseline_result):  # type: ignore[no-untyped-def]
        del request, baseline_result
        self.calls += 1
        return self.graph


def _graph(status: CodeIntelligenceStatus) -> SemanticGraphEvidence:
    limitations = () if status is CodeIntelligenceStatus.CURRENT else ("bounded graph",)
    return SemanticGraphEvidence(
        "codegraph",
        "1.5.0",
        status,
        CodeIntelligenceMeasure(
            100 if status is not CodeIntelligenceStatus.UNAVAILABLE else 0,
            "coverage",
        ),
        CodeIntelligenceMeasure(
            90 if status is not CodeIntelligenceStatus.UNAVAILABLE else 0,
            "confidence",
        ),
        limitations=limitations,
    )


def test_promoted_provider_requires_receipt_and_calibrates_current_graph(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    baseline_result = baseline(code_request)
    delegate = Delegate(_graph(CodeIntelligenceStatus.CURRENT))
    promoted = PromotedCodeGraphProvider(
        delegate,
        _runner(tmp_path, Probe((_observation(), _observation()))),
    )

    graph = promoted.analyze(code_request, baseline_result)

    assert graph.status is CodeIntelligenceStatus.CURRENT
    assert graph.confidence.value == 100
    assert "promotion receipt" in graph.confidence.reason.lower()
    assert delegate.calls == 1


def test_failed_promotion_returns_unavailable_without_calling_delegate(tmp_path: Path) -> None:
    code_request = request(tmp_path)
    baseline_result = baseline(code_request)
    delegate = Delegate(_graph(CodeIntelligenceStatus.CURRENT))
    bad = replace(_observation(), schema_valid=False)
    promoted = PromotedCodeGraphProvider(delegate, _runner(tmp_path, Probe((bad, bad))))

    graph = promoted.analyze(code_request, baseline_result)

    assert graph.status is CodeIntelligenceStatus.UNAVAILABLE
    assert delegate.calls == 0
    assert all("provider-private-detail" not in item for item in graph.limitations)
