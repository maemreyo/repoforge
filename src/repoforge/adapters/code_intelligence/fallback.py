"""Explicit primary/fallback orchestration for code-intelligence providers."""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.code_intelligence import (
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceStatus,
    new_code_intelligence_result,
)
from ...ports.code_intelligence import CodeIntelligenceProvider


def _with_limitation(result: CodeIntelligenceResult, limitation: str) -> CodeIntelligenceResult:
    return new_code_intelligence_result(
        provider_id=result.provider_id,
        provider_version=result.provider_version,
        snapshot=result.snapshot,
        status=result.status,
        coverage=result.coverage,
        confidence=result.confidence,
        analyzed_paths=result.analyzed_paths,
        symbols=result.symbols,
        imports=result.imports,
        references=result.references,
        affected_tests=result.affected_tests,
        unsupported_paths=result.unsupported_paths,
        malformed_paths=result.malformed_paths,
        generated_paths=result.generated_paths,
        denied_paths=result.denied_paths,
        limitations=tuple((*result.limitations, limitation)),
        truncated=result.truncated,
    )


@dataclass(frozen=True, slots=True)
class FallbackCodeIntelligenceProvider:
    """Use the secondary provider only when primary evidence is unusable or bounded away."""

    primary: CodeIntelligenceProvider
    fallback: CodeIntelligenceProvider

    @property
    def provider_id(self) -> str:
        return f"{self.primary.provider_id}+fallback"

    @property
    def provider_version(self) -> str:
        return self.primary.provider_version

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceResult:
        fallback_reason: str | None = None
        primary_result: CodeIntelligenceResult | None = None
        try:
            primary_result = self.primary.analyze(request)
        except Exception as exc:
            fallback_reason = (
                f"Primary provider {self.primary.provider_id} raised {type(exc).__name__}; "
                f"provider {self.fallback.provider_id} supplied the returned evidence."
            )
        else:
            if primary_result.status is CodeIntelligenceStatus.UNAVAILABLE:
                fallback_reason = (
                    f"Primary provider {self.primary.provider_id} returned unavailable evidence; "
                    f"provider {self.fallback.provider_id} supplied the returned evidence."
                )
            elif primary_result.truncated:
                fallback_reason = (
                    f"Primary provider {self.primary.provider_id} reached a reviewed bound; "
                    f"provider {self.fallback.provider_id} was evaluated as a fallback."
                )
            else:
                return primary_result

        fallback_result = self.fallback.analyze(request)
        if (
            primary_result is not None
            and primary_result.status is not CodeIntelligenceStatus.UNAVAILABLE
            and len(primary_result.analyzed_paths) > len(fallback_result.analyzed_paths)
        ):
            assert fallback_reason is not None
            return _with_limitation(
                primary_result,
                f"{fallback_reason} Primary evidence covered more paths and was retained.",
            )
        assert fallback_reason is not None
        return _with_limitation(fallback_result, fallback_reason)


__all__ = ["FallbackCodeIntelligenceProvider"]
