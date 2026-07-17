"""Bounded local code-intelligence adapters."""

from .fallback import FallbackCodeIntelligenceProvider
from .syntax import SyntaxCodeIntelligenceProvider
from .tree_sitter import TreeSitterCodeIntelligenceProvider

__all__ = [
    "FallbackCodeIntelligenceProvider",
    "SyntaxCodeIntelligenceProvider",
    "TreeSitterCodeIntelligenceProvider",
]
