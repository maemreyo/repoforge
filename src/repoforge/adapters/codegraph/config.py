"""Public adapter-facing configuration surface for managed CodeGraph."""

from ...domain.codegraph_config import CodeGraphOptions, codegraph_options_from_config

__all__ = ["CodeGraphOptions", "codegraph_options_from_config"]
