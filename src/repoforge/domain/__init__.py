"""Pure RepoForge domain package with lazy compatibility exports.

Production modules import concrete domain submodules directly. Lazy exports preserve the
historical ``repoforge.domain`` API without creating config/policy import cycles.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CommandError": ("errors", "CommandError"),
    "ConfigError": ("errors", "ConfigError"),
    "ErrorCode": ("errors", "ErrorCode"),
    "OperationError": ("errors", "OperationError"),
    "PersonalCodingMCPError": ("errors", "PersonalCodingMCPError"),
    "RepoForgeError": ("errors", "RepoForgeError"),
    "SecurityError": ("errors", "SecurityError"),
    "WorkspaceError": ("errors", "WorkspaceError"),
    "VerificationReceipt": ("workspace", "VerificationReceipt"),
    "WorkspaceRecord": ("workspace", "WorkspaceRecord"),
    "EffectiveExecutionPolicy": ("execution_environment", "EffectiveExecutionPolicy"),
    "EnvironmentIdentity": ("execution_environment", "EnvironmentIdentity"),
    "ExecutionEvidence": ("execution_environment", "ExecutionEvidence"),
    "RequestedExecutionPolicy": ("execution_environment", "RequestedExecutionPolicy"),
    "GitHubCapabilityEvidenceState": (
        "github_capability_preflight",
        "GitHubCapabilityEvidenceState",
    ),
    "GitHubCapabilityPreflightReport": (
        "github_capability_preflight",
        "GitHubCapabilityPreflightReport",
    ),
    "GitHubCapabilityPreflightRequest": (
        "github_capability_preflight",
        "GitHubCapabilityPreflightRequest",
    ),
    "GitHubOperationCapability": (
        "github_capability_preflight",
        "GitHubOperationCapability",
    ),
    "ActorClass": ("repository_identity", "ActorClass"),
    "AuthLease": ("repository_identity", "AuthLease"),
    "CredentialProfile": ("repository_identity", "CredentialProfile"),
    "IdentityReceipt": ("repository_identity", "IdentityReceipt"),
    "OperationIdentityContext": ("repository_identity", "OperationIdentityContext"),
    "PublicationIntent": ("repository_identity", "PublicationIntent"),
    "PublicationEvidence": ("publication", "PublicationEvidence"),
    "RemoteTopology": ("publication", "RemoteTopology"),
    "RepositoryEndpoint": ("publication", "RepositoryEndpoint"),
    "ReviewedPublication": ("publication", "ReviewedPublication"),
    "RepositoryIdentityBinding": ("repository_identity", "RepositoryIdentityBinding"),
    "review_publication": ("publication", "review_publication"),
    "assert_path_allowed": ("policy", "assert_path_allowed"),
    "extract_patch_paths": ("policy", "extract_patch_paths"),
    "normalize_relative_path": ("policy", "normalize_relative_path"),
    "resolve_workspace_path": ("policy", "resolve_workspace_path"),
    "slugify": ("policy", "slugify"),
    "validate_branch": ("policy", "validate_branch"),
    "validate_patch": ("policy", "validate_patch"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(f"{__name__}.{module_name}"), attribute)


__all__ = sorted(_EXPORTS)
