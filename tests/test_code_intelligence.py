from __future__ import annotations

from pathlib import Path

import pytest

from repoforge.adapters.code_intelligence import (
    FallbackCodeIntelligenceProvider,
    TreeSitterCodeIntelligenceProvider,
)
from repoforge.adapters.code_intelligence.calibration import calibrated_confidence
from repoforge.adapters.code_intelligence.syntax import SyntaxCodeIntelligenceProvider
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    CodeLanguage,
    new_code_intelligence_result,
    unavailable_code_intelligence,
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
    assert "calibration" in result.confidence.reason.lower()
    assert result.limitations
    assert "SECRET" not in repr(result)


def test_tree_sitter_reports_malformed_unsupported_generated_and_denied_paths(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/valid.py", "def valid():\n    return True\n")
    _write(tmp_path, "src/broken.py", "def broken(:\n")
    _write(tmp_path, "src/binary.py", b"\xff\xfe\x00")
    _write(tmp_path, "README.md", "# Unsupported\n")
    _write(tmp_path, "generated/client.ts", "export function generated() {}\n")

    result = TreeSitterCodeIntelligenceProvider().analyze(
        CodeIntelligenceRequest(
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
    )

    assert result.status is CodeIntelligenceStatus.PARTIAL
    assert result.analyzed_paths == ("src/valid.py",)
    assert result.malformed_paths == ("src/binary.py", "src/broken.py")
    assert result.unsupported_paths == ("README.md",)
    assert result.generated_paths == ("generated/client.ts",)
    assert result.denied_paths == (".env",)
    assert result.truncated is False


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


def test_tree_sitter_parses_python_symbols_and_import_graph(tmp_path: Path) -> None:
    files = {
        "src/package/core.py": "def value():\n    return 1\n",
        "src/package/service.py": (
            "from src.package.core import value\n\n"
            "class Service:\n"
            "    def run(self):\n"
            "        return value()\n"
        ),
        "tests/test_service.py": (
            "from src.package.service import Service\n\n"
            "def test_service():\n"
            "    assert Service().run() == 1\n"
        ),
    }
    for path, content in files.items():
        _write(tmp_path, path, content)

    result = TreeSitterCodeIntelligenceProvider().analyze(
        CodeIntelligenceRequest(
            workspace_root=tmp_path.resolve(),
            snapshot=_snapshot(),
            paths=tuple(files),
            changed_paths=("src/package/core.py",),
            diagnostic_ids=("pytest-target",),
        )
    )

    assert result.provider_id == "tree-sitter"
    assert result.status is CodeIntelligenceStatus.CURRENT
    assert {fact.name for fact in result.symbols} >= {"value", "Service", "run", "test_service"}
    assert any(
        fact.source_path == "src/package/service.py" and fact.resolved_path == "src/package/core.py"
        for fact in result.imports
    )
    assert result.affected_tests[0].test_path == "tests/test_service.py"
    assert result.affected_tests[0].selector == "tests/test_service.py"


def test_affected_test_confidence_is_provider_corpus_calibrated(tmp_path: Path) -> None:
    files = {
        "src/value.js": "export function value() { return 1; }\n",
        "tests/value.test.js": (
            "import { value } from '../src/value';\ntest('value', () => { value(); });\n"
        ),
    }
    for path, content in files.items():
        _write(tmp_path, path, content)
    request = CodeIntelligenceRequest(
        workspace_root=tmp_path.resolve(),
        snapshot=_snapshot(),
        paths=tuple(files),
        changed_paths=("src/value.js",),
    )

    syntax_candidate = SyntaxCodeIntelligenceProvider().analyze(request).affected_tests[0]
    tree_sitter_candidate = TreeSitterCodeIntelligenceProvider().analyze(request).affected_tests[0]

    assert syntax_candidate.confidence == 0
    assert tree_sitter_candidate.confidence == 100


def test_tree_sitter_handles_dynamic_import_reexports_and_jsx_references(tmp_path: Path) -> None:
    files = {
        "src/components/Button.tsx": (
            "export function Button() { return <button disabled={false}>Go</button>; }\n"
        ),
        "src/components/index.ts": "export { Button } from './Button';\n",
        "src/app.tsx": (
            "import { Button } from './components';\n"
            "export async function loadFeature() { return import('./components/Button'); }\n"
            "export function App() { return <Button />; }\n"
        ),
        "src/app.test.tsx": ("import { App } from './app';\ntest('app', () => { App(); });\n"),
    }
    for path, content in files.items():
        _write(tmp_path, path, content)

    result = TreeSitterCodeIntelligenceProvider().analyze(
        CodeIntelligenceRequest(
            workspace_root=tmp_path.resolve(),
            snapshot=_snapshot(),
            paths=tuple(files),
            changed_paths=("src/components/Button.tsx",),
        )
    )

    imports = {(fact.source_path, fact.target, fact.resolved_path) for fact in result.imports}
    assert ("src/components/index.ts", "./Button", "src/components/Button.tsx") in imports
    assert ("src/app.tsx", "./components/Button", "src/components/Button.tsx") in imports
    assert any(
        fact.source_path == "src/app.tsx" and fact.symbol == "Button" for fact in result.references
    )
    assert [candidate.test_path for candidate in result.affected_tests] == ["src/app.test.tsx"]


def test_fallback_provider_uses_secondary_provider_after_unavailable_or_error(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/value.py", "def value():\n    return 1\n")
    request = CodeIntelligenceRequest(
        workspace_root=tmp_path.resolve(),
        snapshot=_snapshot(),
        paths=("src/value.py",),
        changed_paths=("src/value.py",),
    )

    class UnavailableProvider:
        provider_id = "primary"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            return unavailable_code_intelligence(
                snapshot=request.snapshot,
                reason="Primary parser unavailable.",
                provider_id=self.provider_id,
                provider_version=self.provider_version,
            )

    class BrokenProvider:
        provider_id = "broken"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            raise RuntimeError("boom")

    for primary, marker in ((UnavailableProvider(), "primary"), (BrokenProvider(), "runtimeerror")):
        result = FallbackCodeIntelligenceProvider(
            primary=primary,
            fallback=TreeSitterCodeIntelligenceProvider(),
        ).analyze(request)
        assert result.provider_id == "tree-sitter"
        assert result.status is CodeIntelligenceStatus.CURRENT
        assert any(marker in limitation.lower() for limitation in result.limitations)


def test_fallback_provider_keeps_current_primary_without_evaluating_fallback(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/value.py", "def value():\n    return 1\n")
    request = CodeIntelligenceRequest(
        workspace_root=tmp_path.resolve(),
        snapshot=_snapshot(),
        paths=("src/value.py",),
        changed_paths=("src/value.py",),
    )

    class UnexpectedFallback:
        provider_id = "unexpected"
        provider_version = "1"

        def analyze(self, request: CodeIntelligenceRequest):
            raise AssertionError("fallback must not run for current primary evidence")

    result = FallbackCodeIntelligenceProvider(
        primary=TreeSitterCodeIntelligenceProvider(),
        fallback=UnexpectedFallback(),
    ).analyze(request)

    assert result.provider_id == "tree-sitter"
    assert result.status is CodeIntelligenceStatus.CURRENT


def test_fallback_provider_retains_broader_truncated_primary(tmp_path: Path) -> None:
    request = CodeIntelligenceRequest(
        workspace_root=tmp_path.resolve(),
        snapshot=_snapshot(),
        paths=("src/one.py", "src/two.py"),
        changed_paths=("src/one.py",),
    )
    primary_result = new_code_intelligence_result(
        provider_id="primary",
        provider_version="1",
        snapshot=request.snapshot,
        status=CodeIntelligenceStatus.PARTIAL,
        coverage=CodeIntelligenceMeasure(100, "Both paths were analyzed."),
        confidence=CodeIntelligenceMeasure(90, "Primary corpus calibration."),
        analyzed_paths=("src/one.py", "src/two.py"),
        limitations=("Primary fact bound was reached.",),
        truncated=True,
    )
    fallback_result = new_code_intelligence_result(
        provider_id="fallback",
        provider_version="1",
        snapshot=request.snapshot,
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(50, "One path was analyzed."),
        confidence=CodeIntelligenceMeasure(50, "Fallback corpus calibration."),
        analyzed_paths=("src/one.py",),
    )

    class FixedProvider:
        def __init__(self, result):
            self.result = result
            self.provider_id = result.provider_id
            self.provider_version = result.provider_version

        def analyze(self, request: CodeIntelligenceRequest):
            return self.result

    result = FallbackCodeIntelligenceProvider(
        primary=FixedProvider(primary_result),
        fallback=FixedProvider(fallback_result),
    ).analyze(request)

    assert result.provider_id == "primary"
    assert result.analyzed_paths == ("src/one.py", "src/two.py")
    assert any("covered more paths" in limitation for limitation in result.limitations)


def test_mixed_language_confidence_uses_weakest_corpus_recall() -> None:
    syntax_value, _ = calibrated_confidence(
        "syntax",
        frozenset({CodeLanguage.PYTHON, CodeLanguage.JAVASCRIPT}),
    )
    tree_sitter_value, _ = calibrated_confidence(
        "tree-sitter",
        frozenset({CodeLanguage.PYTHON, CodeLanguage.TYPESCRIPT}),
    )

    missing_value, missing_reason = calibrated_confidence(
        "missing-provider",
        frozenset({CodeLanguage.PYTHON}),
    )
    empty_value, empty_reason = calibrated_confidence("syntax", frozenset())

    assert syntax_value == 0
    assert tree_sitter_value == 100
    assert missing_value == 0
    assert "missing-provider" in missing_reason
    assert empty_value == 0
    assert "none" in empty_reason


def test_tree_sitter_confidence_is_loaded_from_versioned_calibration(tmp_path: Path) -> None:
    _write(tmp_path, "src/value.py", "def value():\n    return 1\n")
    result = TreeSitterCodeIntelligenceProvider().analyze(
        CodeIntelligenceRequest(
            workspace_root=tmp_path.resolve(),
            snapshot=_snapshot(),
            paths=("src/value.py",),
            changed_paths=("src/value.py",),
        )
    )

    assert "calibration" in result.confidence.reason.lower()
    assert "tree-sitter" in result.confidence.reason.lower()
    assert 0 < result.confidence.value <= 100
