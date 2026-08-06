"""Managed CodeGraph provider adapters."""

from .command import CodeGraphCommandOutput, CodeGraphCommandRunner
from .config import CodeGraphOptions, codegraph_options_from_config
from .manifest import ProjectionEntry, ProjectionManifest, ProjectionResult
from .normalize import (
    NormalizedAffected,
    NormalizedQuery,
    NormalizedQueryNode,
    NormalizedRelationships,
    NormalizedStatus,
    normalize_affected,
    normalize_impact,
    normalize_query,
    normalize_relationships,
    normalize_status,
)
from .projection import CodeGraphProjection
from .provider import ManagedCodeGraphProvider

__all__ = [
    "CodeGraphCommandOutput",
    "CodeGraphCommandRunner",
    "CodeGraphOptions",
    "CodeGraphProjection",
    "ManagedCodeGraphProvider",
    "NormalizedAffected",
    "NormalizedQuery",
    "NormalizedQueryNode",
    "NormalizedRelationships",
    "NormalizedStatus",
    "ProjectionEntry",
    "ProjectionManifest",
    "ProjectionResult",
    "codegraph_options_from_config",
    "normalize_affected",
    "normalize_impact",
    "normalize_query",
    "normalize_relationships",
    "normalize_status",
]
