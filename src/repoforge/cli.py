"""Compatibility entry point for the production CLI interface."""

from .interfaces.cli.main import build_parser, main

__all__ = ["build_parser", "main"]
