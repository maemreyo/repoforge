"""Backward-compatible command adapter imports through the composition root."""

from .bootstrap import SubprocessCommandExecutor
from .ports.command import CommandResult

CommandRunner = SubprocessCommandExecutor

__all__ = ["CommandResult", "CommandRunner", "SubprocessCommandExecutor"]
