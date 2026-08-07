"""Managed graph-only CodeGraph provider with bounded one-shot queries."""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable

from ...domain.code_intelligence import (
    AffectedPathCandidate,
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceStatus,
    CodeRelationshipFact,
    CodeRelationshipKind,
    CodeSymbolFact,
    SemanticGraphEvidence,
)
from ...domain.provider_manifest import ProviderManifest
from .normalize import (
    normalize_affected,
    normalize_impact,
    normalize_query,
    normalize_relationships,
    normalize_status,
)
from .provider_contract import (
    DEFAULT_MAX_SEED_SYMBOLS,
    DEFAULT_MAX_TOTAL_OUTPUT_BYTES,
    DEFAULT_MAX_WALL_SECONDS,
    QUERY_RESULT_LIMIT,
    RELATIONSHIP_COMMAND_LIMIT,
    AnalysisBudget,
    BoundaryReached,
    ProjectionBoundary,
    RunnerBoundary,
)


class ManagedCodeGraphProvider:
    """Return only normalized semantic graph evidence for an existing baseline result."""

    def __init__(
        self,
        manifest: ProviderManifest,
        projection: ProjectionBoundary,
        runner: RunnerBoundary,
        *,
        max_seed_symbols: int = DEFAULT_MAX_SEED_SYMBOLS,
        max_total_output_bytes: int | None = None,
        max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if manifest.codegraph is None:
            raise ValueError("Managed CodeGraph provider requires reviewed CodeGraph options")
        if (
            not isinstance(max_seed_symbols, int)
            or isinstance(max_seed_symbols, bool)
            or max_seed_symbols < 1
        ):
            raise ValueError("max_seed_symbols must be a positive integer")
        actual_bytes = (
            min(manifest.output_bounds.max_artifact_bytes, DEFAULT_MAX_TOTAL_OUTPUT_BYTES)
            if max_total_output_bytes is None
            else max_total_output_bytes
        )
        if not isinstance(actual_bytes, int) or isinstance(actual_bytes, bool) or actual_bytes < 1:
            raise ValueError("max_total_output_bytes must be a positive integer")
        if not isinstance(max_wall_seconds, (int, float)) or isinstance(max_wall_seconds, bool):
            raise ValueError("max_wall_seconds must be a positive finite number")
        if not math.isfinite(max_wall_seconds) or max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be a positive finite number")
        self._manifest = manifest
        self._options = manifest.codegraph
        self._projection = projection
        self._runner = runner
        self._max_seed_symbols = min(max_seed_symbols, self._options.max_relationships)
        self._max_total_output_bytes = actual_bytes
        self._max_wall_seconds = float(max_wall_seconds)
        self._monotonic = monotonic

    @property
    def provider_id(self) -> str:
        return self._manifest.provider_id

    @property
    def provider_version(self) -> str:
        return self._manifest.version

    def analyze(
        self,
        request: CodeIntelligenceRequest,
        baseline: CodeIntelligenceResult,
    ) -> SemanticGraphEvidence:
        with self._projection.operation(request.snapshot.workspace_id):
            return self._analyze_locked(request, baseline)

    def _analyze_locked(
        self,
        request: CodeIntelligenceRequest,
        baseline: CodeIntelligenceResult,
    ) -> SemanticGraphEvidence:
        if baseline.snapshot != request.snapshot:
            return self._unavailable("Baseline and CodeGraph snapshot identities do not match.")

        relationships: set[CodeRelationshipFact] = set()
        affected_paths: set[AffectedPathCandidate] = set()
        limitations: set[str] = set()
        truncated = False
        index_ready = False
        workspace_id = request.snapshot.workspace_id
        workspace_state = self._projection.workspace_root(workspace_id)
        source_before = workspace_state / "source"
        index_before = source_before / ".index"
        incomplete = workspace_state / "INCOMPLETE"
        had_complete_index = index_before.is_dir() and not index_before.is_symlink()
        projected_count = 0
        budget = AnalysisBudget(
            self._monotonic(),
            self._max_total_output_bytes,
            self._max_wall_seconds,
            self._monotonic,
        )

        try:
            invalid_index = index_before.is_symlink() or (
                index_before.exists() and not index_before.is_dir()
            )
            if incomplete.exists() or incomplete.is_symlink() or invalid_index:
                self._projection.invalidate(workspace_id)
                had_complete_index = False
            prepared = self._projection.prepare(request, self._options)
            source = prepared.source_root
            home = source.parent / "home"
            allowed_paths = frozenset(entry.path for entry in prepared.manifest.entries)
            projected_count = len(allowed_paths)
            limitations.update(prepared.manifest.limitations)
            if prepared.manifest.truncated:
                limitations.add("CodeGraph projection reached a reviewed resource bound.")
                truncated = True

            budget.check_time()
            update = (
                self._runner.sync(source, home)
                if had_complete_index
                else self._runner.init(source, home)
            )
            budget.consume(update)
            status_output = self._runner.status(source, home)
            status_text = budget.consume(status_output)
            status = normalize_status(
                status_text,
                expected_version=self.provider_version,
                projection_root=source,
            )
            if status.file_count != projected_count:
                raise ValueError(
                    "CodeGraph status file count does not match the projection manifest"
                )
            self._projection.mark_complete(workspace_id, prepared.manifest.manifest_digest)
            index_ready = True

            projected_changed_paths = tuple(
                path for path in request.changed_paths if path in allowed_paths
            )
            if len(projected_changed_paths) != len(request.changed_paths):
                limitations.add(
                    "Changed paths outside the managed CodeGraph projection were excluded."
                )
            changed_paths = projected_changed_paths[: self._options.max_changed_paths]
            if len(projected_changed_paths) > len(changed_paths):
                limitations.add("CodeGraph changed-path selection reached its reviewed bound.")
                truncated = True
            if changed_paths:
                affected_output = self._runner.affected(
                    source,
                    home,
                    changed_paths,
                    depth=self._options.max_depth,
                )
                affected = normalize_affected(
                    budget.consume(affected_output),
                    expected_changed_paths=changed_paths,
                    limit=self._options.max_affected_paths,
                )
                affected_paths.update(affected.candidates)
                if affected.truncated:
                    limitations.add("CodeGraph affected-path output reached its reviewed bound.")
                    truncated = True

            seeds, seed_limitations, seed_truncated = self._select_seeds(
                baseline,
                request,
                allowed_paths,
            )
            limitations.update(seed_limitations)
            truncated = truncated or seed_truncated
            for seed in seeds:
                budget.check_time()
                query_output = self._runner.query(
                    source,
                    home,
                    seed.name,
                    limit=QUERY_RESULT_LIMIT,
                )
                query = normalize_query(
                    budget.consume(query_output),
                    expected_symbol=seed.name,
                    expected_path=seed.path,
                    allowed_paths=allowed_paths,
                    limit=QUERY_RESULT_LIMIT,
                )
                if query.truncated:
                    limitations.add("CodeGraph symbol query reached its reviewed fan-out bound.")
                    truncated = True
                if query.ambiguous:
                    limitations.add(
                        "Ambiguous cross-path CodeGraph symbol matches were excluded from traversal."
                    )
                    continue
                if not query.nodes:
                    limitations.add(
                        "CodeGraph could not resolve one reviewed baseline symbol safely."
                    )
                    continue

                for command, call in (
                    ("callers", self._runner.callers),
                    ("callees", self._runner.callees),
                ):
                    remaining = self._options.max_relationships - len(relationships)
                    if remaining <= 0:
                        limitations.add(
                            "CodeGraph relationship collection reached its reviewed bound."
                        )
                        truncated = True
                        break
                    output = call(
                        source,
                        home,
                        seed.name,
                        limit=min(RELATIONSHIP_COMMAND_LIMIT, remaining),
                    )
                    normalized = normalize_relationships(
                        budget.consume(output),
                        command=command,
                        relationship_kind=CodeRelationshipKind.CALLS,
                        expected_symbol=seed.name,
                        seed_path=seed.path,
                        seed_symbol=seed.qualified_name,
                        allowed_paths=allowed_paths,
                        limit=remaining,
                    )
                    relationships.update(normalized.relationships)
                    if normalized.truncated:
                        limitations.add("CodeGraph relationship output reached its reviewed bound.")
                        truncated = True

                remaining_paths = self._options.max_affected_paths - len(affected_paths)
                if remaining_paths <= 0:
                    limitations.add(
                        "CodeGraph affected-path collection reached its reviewed bound."
                    )
                    truncated = True
                    break
                impact_output = self._runner.impact(
                    source,
                    home,
                    seed.name,
                    depth=self._options.max_depth,
                )
                impact = normalize_impact(
                    budget.consume(impact_output),
                    expected_symbol=seed.name,
                    allowed_paths=allowed_paths,
                    limit=remaining_paths,
                    max_depth=self._options.max_depth,
                )
                affected_paths.update(impact.candidates)
                if impact.truncated:
                    limitations.add("CodeGraph impact output reached its reviewed bound.")
                    truncated = True
        except BoundaryReached as exc:
            limitations.add(exc.reason)
            truncated = True
        except Exception as exc:
            limitations.add(
                f"CodeGraph semantic analysis stopped at a reviewed boundary ({type(exc).__name__})."
            )
        finally:
            if not index_ready:
                try:
                    self._projection.invalidate(workspace_id)
                except Exception:
                    limitations.add(
                        "CodeGraph incomplete index invalidation could not be confirmed."
                    )

        if not index_ready:
            return self._unavailable(*limitations)
        return self._evidence(
            request,
            projected_count,
            relationships,
            affected_paths,
            limitations,
            truncated,
        )

    def _select_seeds(
        self,
        baseline: CodeIntelligenceResult,
        request: CodeIntelligenceRequest,
        allowed_paths: frozenset[str],
    ) -> tuple[tuple[CodeSymbolFact, ...], set[str], bool]:
        selected_paths = frozenset((*request.changed_paths, *request.paths))
        eligible = tuple(
            symbol
            for symbol in baseline.symbols
            if symbol.path in selected_paths and symbol.path in allowed_paths
        )
        name_counts = Counter(symbol.name for symbol in baseline.symbols)
        ambiguous = {name for name, count in name_counts.items() if count > 1}
        limitations: set[str] = set()
        if any(symbol.name in ambiguous for symbol in eligible):
            limitations.add(
                "Ambiguous baseline symbol names were excluded from CodeGraph traversal."
            )
        ordered = tuple(
            sorted(
                (symbol for symbol in eligible if symbol.name not in ambiguous),
                key=lambda symbol: (
                    symbol.path not in request.changed_paths,
                    symbol.path,
                    symbol.line,
                    symbol.qualified_name,
                ),
            )
        )
        truncated = len(ordered) > self._max_seed_symbols
        if truncated:
            limitations.add("CodeGraph symbol selection reached its reviewed bound.")
        return ordered[: self._max_seed_symbols], limitations, truncated

    def _evidence(
        self,
        request: CodeIntelligenceRequest,
        projected_count: int,
        relationships: set[CodeRelationshipFact],
        affected_paths: set[AffectedPathCandidate],
        limitations: set[str],
        truncated: bool,
    ) -> SemanticGraphEvidence:
        coverage_value = min(
            100,
            round(100 * projected_count / max(1, len(request.paths))),
        )
        status = (
            CodeIntelligenceStatus.PARTIAL
            if limitations or truncated
            else CodeIntelligenceStatus.CURRENT
        )
        confidence_value = 70 if status is CodeIntelligenceStatus.PARTIAL else 90
        coverage_reason = "Managed CodeGraph projection coverage."
        confidence_reason = "Pinned CodeGraph CLI evidence normalized by the managed adapter."
        return SemanticGraphEvidence(
            self.provider_id,
            self.provider_version,
            status,
            CodeIntelligenceMeasure(coverage_value, coverage_reason),
            CodeIntelligenceMeasure(confidence_value, confidence_reason),
            tuple(relationships),
            tuple(affected_paths),
            tuple(sorted(limitations)),
            truncated,
        )

    def _unavailable(self, *reasons: str) -> SemanticGraphEvidence:
        limitations = tuple(sorted(set(reasons))) or ("Managed CodeGraph evidence is unavailable.",)
        reason = "Managed CodeGraph evidence is unavailable."
        return SemanticGraphEvidence(
            self.provider_id,
            self.provider_version,
            CodeIntelligenceStatus.UNAVAILABLE,
            CodeIntelligenceMeasure(0, reason),
            CodeIntelligenceMeasure(0, reason),
            limitations=limitations,
        )


__all__ = ["ManagedCodeGraphProvider"]
