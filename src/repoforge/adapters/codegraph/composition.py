"""Per-repository composition for optional CodeGraph augmentation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ...config import AppConfig
from ...ports.clock import Clock
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
from .canaries import CodeGraphCanaryRunner, PromotedCodeGraphProvider
from .canary_corpus import embedded_canary_digest
from .canary_probe import ManagedCodeGraphCanaryProbe
from .command import CodeGraphCommandRunner
from .lifecycle import CodeGraphLifecycle
from .projection import CodeGraphProjection
from .provider import ManagedCodeGraphProvider
from .receipts import PromotionReceiptStore, promotion_identity


class _SystemClock:
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class CodeGraphRuntime:
    provider: CodeIntelligenceProvider
    lifecycle: CodeGraphLifecycle | None


def _graph_provider(
    provider_id: str,
    config: AppConfig,
    baseline: CodeIntelligenceProvider,
    registry: ProviderRegistry,
    command: CommandExecutor,
    locks: LockManager,
    clock: Clock,
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
        runner = CodeGraphCommandRunner(manifest, registry, command)
        delegate = ManagedCodeGraphProvider(manifest, projection, runner)
        identity = promotion_identity(manifest, embedded_canary_digest())
        promotion = CodeGraphCanaryRunner(
            identity,
            PromotionReceiptStore(config.server.state_root, locks),
            ManagedCodeGraphCanaryProbe(
                manifest,
                config.server.state_root,
                locks,
                projection,
                runner,
                baseline,
            ),
            clock,
            timeout_seconds=manifest.codegraph.canary_timeout_seconds,
        )
        return PromotedCodeGraphProvider(delegate, promotion)
    except Exception as exc:
        return UnavailableSemanticGraphProvider(
            provider_id,
            manifest.version,
            "Configured semantic graph enrollment could not be constructed at a reviewed "
            f"boundary ({type(exc).__name__}).",
        )


def build_codegraph_runtime(
    config: AppConfig,
    baseline: CodeIntelligenceProvider,
    registry: ProviderRegistry,
    command: CommandExecutor,
    locks: LockManager,
    clock: Clock,
) -> CodeGraphRuntime:
    enrolled = {
        repo_id: repository.code_intelligence_provider_id
        for repo_id, repository in config.repositories.items()
        if repository.code_intelligence_provider_id
    }
    if not enrolled:
        return CodeGraphRuntime(baseline, None)

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
        lifecycle = None
    else:
        providers = {
            repo_id: CodeGraphAugmentedProvider(
                baseline,
                _graph_provider(
                    provider_id,
                    config,
                    baseline,
                    registry,
                    command,
                    locks,
                    clock,
                    projection,
                ),
            )
            for repo_id, provider_id in sorted(enrolled.items())
        }
        lifecycle = CodeGraphLifecycle(projection)
    return CodeGraphRuntime(
        RepositoryCodeIntelligenceRouter(
            default_provider=baseline,
            providers=providers,
        ),
        lifecycle,
    )


def build_repository_code_intelligence(
    config: AppConfig,
    baseline: CodeIntelligenceProvider,
    registry: ProviderRegistry,
    command: CommandExecutor,
    locks: LockManager,
    clock: Clock | None = None,
) -> CodeIntelligenceProvider:
    return build_codegraph_runtime(
        config,
        baseline,
        registry,
        command,
        locks,
        clock or _SystemClock(),
    ).provider


__all__ = [
    "CodeGraphRuntime",
    "build_codegraph_runtime",
    "build_repository_code_intelligence",
]
