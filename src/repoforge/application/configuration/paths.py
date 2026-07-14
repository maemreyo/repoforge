"""Application-facing access to the single RepoForge user-path authority."""

from ...domain.user_paths import RepoForgePaths, resolve_repoforge_paths

__all__ = ["RepoForgePaths", "resolve_repoforge_paths"]
