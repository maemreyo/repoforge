"""Provider-neutral code-intelligence analysis boundary."""

from __future__ import annotations

from typing import Protocol

from ..domain.code_intelligence import CodeIntelligenceRequest, CodeIntelligenceResult


class CodeIntelligenceProvider(Protocol):
    """Return bounded facts for one exact workspace snapshot without mutating it."""

    @property
    def provider_id(self) -> str: ...

    @property
    def provider_version(self) -> str: ...

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceResult: ...


__all__ = ["CodeIntelligenceProvider"]
