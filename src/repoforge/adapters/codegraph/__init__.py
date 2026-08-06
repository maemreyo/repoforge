"""Managed CodeGraph provider adapters."""

from .command import CodeGraphCommandOutput, CodeGraphCommandRunner
from .config import CodeGraphOptions, codegraph_options_from_config
from .manifest import ProjectionEntry, ProjectionManifest, ProjectionResult
from .projection import CodeGraphProjection

__all__ = [
    "CodeGraphCommandOutput",
    "CodeGraphCommandRunner",
    "CodeGraphOptions",
    "CodeGraphProjection",
    "ProjectionEntry",
    "ProjectionManifest",
    "ProjectionResult",
    "codegraph_options_from_config",
]
