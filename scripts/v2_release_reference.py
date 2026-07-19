"""Source-tree composition for the deterministic Forge v2 release-corpus executor."""

from repoforge.adapters.code_intelligence import TreeSitterCodeIntelligenceProvider
from repoforge.benchmark.reference import ReferenceExecutor

execute_case = ReferenceExecutor(TreeSitterCodeIntelligenceProvider())

__all__ = ["execute_case"]
