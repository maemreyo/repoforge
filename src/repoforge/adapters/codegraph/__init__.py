"""Managed CodeGraph provider adapters."""

from .augment import (
    CodeGraphAugmentedProvider,
    RepositoryCodeIntelligenceRouter,
    UnavailableSemanticGraphProvider,
)
from .canaries import (
    CanaryEdge,
    CanaryObservation,
    CodeGraphCanaryRunner,
    CodeGraphPromotionError,
    PromotedCodeGraphProvider,
    canary_corpus_digest,
)
from .command import CodeGraphCommandOutput, CodeGraphCommandRunner
from .composition import (
    CodeGraphRuntime,
    build_codegraph_runtime,
    build_repository_code_intelligence,
)
from .config import CodeGraphOptions, codegraph_options_from_config
from .lifecycle import CodeGraphCleanupResult, CodeGraphLifecycle
from .manifest import ProjectionEntry, ProjectionManifest, ProjectionResult
from .normalize import (
    NormalizedAffected,
    NormalizedQuery,
    NormalizedQueryNode,
    NormalizedRelationships,
    NormalizedStatus,
    normalize_affected,
    normalize_impact,
    normalize_query,
    normalize_relationships,
    normalize_status,
)
from .projection import CodeGraphProjection
from .provider import ManagedCodeGraphProvider
from .receipts import (
    PromotionGateOutcome,
    PromotionIdentity,
    PromotionReceipt,
    PromotionReceiptStore,
    promotion_identity,
)

__all__ = [
    "CanaryEdge",
    "CanaryObservation",
    "CodeGraphAugmentedProvider",
    "CodeGraphCanaryRunner",
    "CodeGraphCleanupResult",
    "CodeGraphCommandOutput",
    "CodeGraphCommandRunner",
    "CodeGraphLifecycle",
    "CodeGraphOptions",
    "CodeGraphProjection",
    "CodeGraphPromotionError",
    "CodeGraphRuntime",
    "ManagedCodeGraphProvider",
    "NormalizedAffected",
    "NormalizedQuery",
    "NormalizedQueryNode",
    "NormalizedRelationships",
    "NormalizedStatus",
    "ProjectionEntry",
    "ProjectionManifest",
    "ProjectionResult",
    "PromotedCodeGraphProvider",
    "PromotionGateOutcome",
    "PromotionIdentity",
    "PromotionReceipt",
    "PromotionReceiptStore",
    "RepositoryCodeIntelligenceRouter",
    "UnavailableSemanticGraphProvider",
    "build_codegraph_runtime",
    "build_repository_code_intelligence",
    "canary_corpus_digest",
    "codegraph_options_from_config",
    "normalize_affected",
    "normalize_impact",
    "normalize_query",
    "normalize_relationships",
    "normalize_status",
    "promotion_identity",
]
