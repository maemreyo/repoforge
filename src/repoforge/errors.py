"""Domain-specific exceptions."""


class RepoForgeError(RuntimeError):
    """Base exception for expected operational failures."""


# Backward-compatible internal alias used by the first source bundle.
PersonalCodingMCPError = RepoForgeError


class ConfigError(RepoForgeError):
    """Raised when configuration is missing or invalid."""


class SecurityError(RepoForgeError):
    """Raised when an operation violates a safety boundary."""


class CommandError(RepoForgeError):
    """Raised when a subprocess exits unsuccessfully or times out."""


class WorkspaceError(RepoForgeError):
    """Raised for workspace lifecycle or registry failures."""
