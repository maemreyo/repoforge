"""Backward-compatible command adapter imports."""

from .adapters.subprocess import CommandRunner, SubprocessCommandExecutor
from .ports.command import CommandResult

__all__ = ["CommandResult", "CommandRunner", "SubprocessCommandExecutor"]
