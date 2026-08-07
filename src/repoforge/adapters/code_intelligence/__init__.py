"""Bounded local code-intelligence adapters."""

from ..codegraph.augment import (
    CodeGraphAugmentedProvider,
    RepositoryCodeIntelligenceRouter,
)
from .fallback import FallbackCodeIntelligenceProvider
from .syntax import SyntaxCodeIntelligenceProvider
from .tree_sitter import TreeSitterCodeIntelligenceProvider

__all__ = [
    "CodeGraphAugmentedProvider",
    "FallbackCodeIntelligenceProvider",
    "RepositoryCodeIntelligenceRouter",
    "SyntaxCodeIntelligenceProvider",
    "TreeSitterCodeIntelligenceProvider",
]
