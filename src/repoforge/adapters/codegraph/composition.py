"""Per-repository composition for optional CodeGraph augmentation."""

from __future__ import annotations

from ...config import AppConfig
from ...ports.code_intelligence import CodeIntelligenceProvider
from ...ports.command import CommandExecutor
from ...ports.locking import LockManager
from ...ports.provider_registry import ProviderRegistry
from .augment import (
    CodeGraphAugmentedProvider,
    RepositoryCodeIntelligenceRouter,
    SemanticGraphProvider,
    UnavailableSemanticGraphProvider,
)
from .command import CodeGraphCommandRunner
from .projection import CodeGraphProjection
from .provider import ManagedCodeGraphProvider


def _graph_provider(
    provider_id: str,
    registry: ProviderRegistry,
    command: CommandExecutor,
    projection: CodeGraphProjection,
) -> SemanticGraphProvider:
    manifest = registry.get_provider(provider_id)
    if manifest is None or manifest.provider_id != provider_id or manifest.codegraph is None:
        return UnavailableSemanticGraphProvider(
            provider_id,
            "0",
            "Configured semantic graph enrollment is unavailable or invalid.",
        )
    try:
        return ManagedCodeGraphProvider(
            manifest,
            projection,
            CodeGraphCommandRunner(manifest, registry, command),
        )
    except Exception as exc:
        return UnavailableSemanticGraphProvider(
            provider_id,
            manifest.version,
            "Configured semantic graph enrollment could not be constructed at a reviewed "
            f"boundary ({type(exc).__name__}).",
        )


def build_repository_code_intelligence(
    config: AppConfig,
    baseline: CodeIntelligenceProvider,
    registry: ProviderRegistry,
    command: CommandExecutor,
    locks: LockManager,
) -> CodeIntelligenceProvider:
    enrolled = {
        repo_id: repository.code_intelligence_provider_id
        for repo_id, repository in config.repositories.items()
        if repository.code_intelligence_provider_id
    }
    if not enrolled:
        return baseline

    try:
        projection = CodeGraphProjection(config.server.state_root, locks)
    except Exception as exc:
        providers = {
            repo_id: CodeGraphAugmentedProvider(
                baseline,
                UnavailableSemanticGraphProvider(
                    provider_id,
                    "0",
                    "Managed semantic graph state could not be initialized at a reviewed "
                    f"boundary ({type(exc).__name__}).",
                ),
            )
            for repo_id, provider_id in sorted(enrolled.items())
        }
    else:
        providers = {
            repo_id: CodeGraphAugmentedProvider(
                baseline,
                _graph_provider(provider_id, registry, command, projection),
            )
            for repo_id, provider_id in sorted(enrolled.items())
        }
    return RepositoryCodeIntelligenceRouter(
        default_provider=baseline,
        providers=providers,
    )


__all__ = ["build_repository_code_intelligence"]
