from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.code_intelligence.syntax import SyntaxCodeIntelligenceProvider
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    new_code_intelligence_result,
)
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.evidence import (
    EvidenceSourceKind,
    EvidenceStatus,
    new_code_intelligence_evidence,
)


def _snapshot() -> CodeIntelligenceSnapshot:
    return CodeIntelligenceSnapshot(
        repo_id="demo",
        workspace_id="workspace-1",
        head_sha="a" * 40,
        workspace_fingerprint="b" * 64,
    )


def _write(root: Path, relative_path: str, content: str | bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def test_provider_reports_malformed_unsupported_generated_and_denied_paths(tmp_path: Path) -> None:
    _write(tmp_path, "src/valid.py", "def valid():\n    return True\n")
    _write(tmp_path, "src/broken.py", "def broken(:\n")
    _write(tmp_path, "src/binary.py", b"\xff\xfe\x00")
    _write(tmp_path, "README.md", "# Unsupported\n")
    _write(tmp_path, "generated/client.ts", "export function generated() {}\n")
    _write(tmp_path, ".env", "SECRET=do-not-read\n")

    request = CodeIntelligenceRequest(
        workspace_root=tmp_path.resolve(),
        snapshot=_snapshot(),
        paths=(
            "src/valid.py",
            "src/broken.py",
            "src/binary.py",
            "README.md",
            "generated/client.ts",
        ),
        changed_paths=("src/valid.py",),
        denied_paths=(".env",),
    )
    result = SyntaxCodeIntelligenceProvider().analyze(request)

    assert result.status is CodeIntelligenceStatus.PARTIAL
    assert result.analyzed_paths == ("src/valid.py",)
    assert result.malformed_paths == ("src/binary.py", "src/broken.py")
    assert result.unsupported_paths == ("README.md",)
    assert result.generated_paths == ("generated/client.ts",)
    assert result.denied_paths == (".env",)
    assert result.coverage.value < 100
    assert result.limitations
    assert "SECRET" not in repr(result)


def test_transitive_import_graph_produces_bounded_affected_test_candidate(tmp_path: Path) -> None:
    files = {
        "src/core.py": "def value():\n    return 1\n",
        "src/service.py": "from src.core import value\n\ndef service():\n    return value()\n",
        "tests/test_service.py": (
            "from src.service import service\n\ndef test_service():\n    assert service() == 1\n"
        ),
    }
    for path, content in files.items():
        _write(tmp_path, path, content)
    result = SyntaxCodeIntelligenceProvider().analyze(
        CodeIntelligenceRequest(
            workspace_root=tmp_path.resolve(),
            snapshot=_snapshot(),
            paths=tuple(files),
            changed_paths=("src/core.py",),
            diagnostic_ids=("pytest-target",),
        )
    )

    candidate = result.affected_tests[0]
    assert candidate.test_path == "tests/test_service.py"
    assert candidate.diagnostic_id == "pytest-target"
    assert candidate.selector == "tests/test_service.py"
    assert "import hops" in candidate.reason
    assert candidate.confidence >= 70


def test_no_supported_files_returns_explicit_unavailable_result(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# Docs only\n")
    result = SyntaxCodeIntelligenceProvider().analyze(
        CodeIntelligenceRequest(
            workspace_root=tmp_path.resolve(),
            snapshot=_snapshot(),
            paths=("README.md",),
            changed_paths=("README.md",),
        )
    )
    assert result.status is CodeIntelligenceStatus.UNAVAILABLE
    assert result.coverage.value == 0
    assert result.confidence.value == 0
    assert result.limitations


def test_code_intelligence_evidence_is_snapshot_bound_and_provider_neutral() -> None:
    result = new_code_intelligence_result(
        provider_id="syntax",
        provider_version="1",
        snapshot=_snapshot(),
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "All supported paths were analyzed."),
        confidence=CodeIntelligenceMeasure(90, "Syntactic imports resolved."),
        analyzed_paths=("src/service.py",),
        limitations=("No runtime dispatch analysis.",),
    )
    evidence = new_code_intelligence_evidence(
        result,
        created_at="2026-07-17T00:00:00+00:00",
    )
    assert evidence is not None
    assert evidence.source_kind is EvidenceSourceKind.CODE_INTELLIGENCE
    assert evidence.status is EvidenceStatus.CURRENT
    assert evidence.snapshot.snapshot_id == result.snapshot.snapshot_id
    assert evidence.scope.paths == ("src/service.py",)
    assert evidence.provider_id == "syntax"


def test_domain_rejects_unavailable_result_that_claims_analyzed_paths() -> None:
    with pytest.raises(RepoForgeError) as invalid:
        new_code_intelligence_result(
            provider_id="syntax",
            provider_version="1",
            snapshot=_snapshot(),
            status=CodeIntelligenceStatus.UNAVAILABLE,
            coverage=CodeIntelligenceMeasure(0, "Unavailable."),
            confidence=CodeIntelligenceMeasure(0, "Unavailable."),
            analyzed_paths=("src/service.py",),
            limitations=("Provider unavailable.",),
        )
    assert invalid.value.code is ErrorCode.CODE_INTELLIGENCE_INVALID
