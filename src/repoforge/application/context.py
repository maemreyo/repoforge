"""Stable public facade for the application context during reviewed base integration."""

from .context_merge_impl import (
    ApplicationContext,
    orphaned_repository_config,
    repository_policy_snapshot,
)

__all__ = [
    "ApplicationContext",
    "orphaned_repository_config",
    "repository_policy_snapshot",
]
