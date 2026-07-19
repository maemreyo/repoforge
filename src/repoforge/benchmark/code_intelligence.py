"""Deterministic seeded-corpus measurement for code-intelligence providers."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from ..domain.code_intelligence import CodeIntelligenceRequest, CodeIntelligenceSnapshot
from ..ports.code_intelligence import CodeIntelligenceProvider
from .harness import CorpusCase, ProviderRecallObservation


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return tuple(value)


def _files(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(path, str) and isinstance(content, str) for path, content in value.items()
    ):
        raise ValueError(f"{field_name} must map relative paths to UTF-8 text")
    return dict(value)


def _write_fixture(root: Path, fixture: dict[str, str]) -> None:
    for relative_path, content in fixture.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def measure_provider_recall(
    provider: CodeIntelligenceProvider,
    cases: tuple[CorpusCase, ...],
) -> tuple[ProviderRecallObservation, ...]:
    """Run one provider over every corpus case explicitly marked for recall measurement."""

    observations: list[ProviderRecallObservation] = []
    for case in cases:
        if case.corpus != "seeded_bugs" or case.metadata.get("provider_recall") is not True:
            continue
        language = case.metadata.get("language")
        if not isinstance(language, str) or not language:
            raise ValueError(f"{case.case_id}.metadata.language must be non-empty text")
        fixture = _files(case.input.get("files"), f"{case.case_id}.input.files")
        changed_paths = _string_tuple(
            case.input.get("changed_paths"),
            f"{case.case_id}.input.changed_paths",
        )
        expected_tests = _string_tuple(
            case.expected.get("candidate_tests"),
            f"{case.case_id}.expected.candidate_tests",
        )
        with TemporaryDirectory(prefix="repoforge-code-intelligence-") as temp_dir:
            root = Path(temp_dir)
            _write_fixture(root, fixture)
            result = provider.analyze(
                CodeIntelligenceRequest(
                    workspace_root=root.resolve(),
                    snapshot=CodeIntelligenceSnapshot(
                        repo_id="benchmark",
                        workspace_id="seeded-corpus",
                        head_sha="a" * 40,
                        workspace_fingerprint="b" * 64,
                    ),
                    paths=tuple(sorted(fixture)),
                    changed_paths=changed_paths,
                    diagnostic_ids=("pytest-target",),
                )
            )
        observations.append(
            ProviderRecallObservation(
                provider_id=provider.provider_id,
                language=language,
                case_id=case.case_id,
                expected_tests=expected_tests,
                routed_tests=tuple(candidate.test_path for candidate in result.affected_tests),
            )
        )
    return tuple(observations)


__all__ = ["measure_provider_recall"]
