"""Managed real-provider probe for the embedded CodeGraph canary corpus."""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

from ...domain.code_intelligence import (
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
)
from ...domain.provider_manifest import ProviderManifest
from ...ports.code_intelligence import CodeIntelligenceProvider
from ...ports.locking import LockManager
from ..filesystem.atomic import atomic_write_bytes, fsync_dir
from .canaries import DENIED_CANARY_PATHS, CanaryEdge, CanaryObservation, CanaryProbe
from .canary_corpus import CANARY_FILES
from .projection import CodeGraphProjection
from .provider import ManagedCodeGraphProvider
from .provider_contract import RunnerBoundary
from .receipts import PromotionIdentity


class ManagedCodeGraphCanaryProbe(CanaryProbe):
    def __init__(
        self,
        manifest: ProviderManifest,
        state_root: Path,
        locks: LockManager,
        projection: CodeGraphProjection,
        runner: RunnerBoundary,
        baseline: CodeIntelligenceProvider,
    ) -> None:
        self._manifest = manifest
        root = state_root.expanduser().resolve() / "providers" / "codegraph" / "canary-corpus"
        if root.is_symlink():
            raise ValueError("CodeGraph canary corpus directory must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("CodeGraph canary corpus directory must be a managed directory")
        self._root = root
        self._locks = locks
        self._projection = projection
        self._runner = runner
        self._baseline = baseline

    def run(
        self,
        identity: PromotionIdentity,
        timeout_seconds: int,
    ) -> tuple[CanaryObservation, CanaryObservation]:
        lock_name = f"codegraph-canary-{identity.digest}"
        with self._locks.lock(lock_name, timeout_seconds=timeout_seconds):
            source = self._materialize(identity)
            before = self._tree_digest(source)
            workspace_id = f"canary-{identity.digest[:24]}"
            provider = ManagedCodeGraphProvider(
                self._manifest,
                self._projection,
                self._runner,
                max_wall_seconds=float(timeout_seconds),
            )
            try:
                first = self._observe(provider, identity, source, workspace_id, version=1)
                second = self._observe(provider, identity, source, workspace_id, version=1)
                incremental_clean = self._incremental_deletion(
                    provider,
                    identity,
                    source,
                    workspace_id,
                )
                self._restore_file(source, "src/beta.py")
                after = self._tree_digest(source)
                cleanup_confirmed = self._cleanup(workspace_id)
                return (
                    replace(
                        first,
                        incremental_deletion_clean=incremental_clean,
                        cleanup_confirmed=cleanup_confirmed,
                        source_digest_before=before,
                        source_digest_after=after,
                    ),
                    replace(
                        second,
                        incremental_deletion_clean=incremental_clean,
                        cleanup_confirmed=cleanup_confirmed,
                        source_digest_before=before,
                        source_digest_after=after,
                    ),
                )
            finally:
                with contextlib.suppress(Exception):
                    self._projection.dispose_workspace(workspace_id)
                self._remove_managed(source.parent)

    def _materialize(self, identity: PromotionIdentity) -> Path:
        root = self._root / identity.digest
        self._remove_managed(root)
        source = root / "source"
        source.mkdir(parents=True, mode=0o700)
        for relative_path, data in CANARY_FILES:
            atomic_write_bytes(source / relative_path, data)
        fsync_dir(source)
        return source

    @staticmethod
    def _remove_managed(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)

    @staticmethod
    def _tree_digest(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
        return digest.hexdigest()

    @staticmethod
    def _snapshot(
        identity: PromotionIdentity, workspace_id: str, version: int
    ) -> CodeIntelligenceSnapshot:
        seed = f"{identity.digest}:{version}".encode()
        return CodeIntelligenceSnapshot(
            "codegraph-canary",
            workspace_id,
            hashlib.sha256(b"head:" + seed).hexdigest()[:40],
            hashlib.sha256(b"tree:" + seed).hexdigest(),
        )

    def _request(
        self,
        identity: PromotionIdentity,
        source: Path,
        workspace_id: str,
        *,
        version: int,
        deleted: frozenset[str] = frozenset(),
    ) -> CodeIntelligenceRequest:
        paths = tuple(path for path, _ in CANARY_FILES if path not in deleted)
        return CodeIntelligenceRequest(
            source,
            self._snapshot(identity, workspace_id, version),
            paths,
            ("src/alpha.py", "web/root.ts"),
            ("pytest-target",),
            DENIED_CANARY_PATHS,
        )

    def _observe(
        self,
        provider: ManagedCodeGraphProvider,
        identity: PromotionIdentity,
        source: Path,
        workspace_id: str,
        *,
        version: int,
    ) -> CanaryObservation:
        request = self._request(identity, source, workspace_id, version=version)
        baseline = self._baseline.analyze(request)
        graph = provider.analyze(request, baseline)
        manifest = self._projection.read_manifest(workspace_id)
        projected = tuple(entry.path for entry in manifest.entries) if manifest is not None else ()
        relationships = tuple(
            CanaryEdge(
                fact.kind.value,
                fact.source_path,
                fact.source_symbol,
                fact.target_path or fact.source_path,
                fact.target_symbol,
            )
            for fact in graph.relationships
        )
        payload = {
            "status": graph.status.value,
            "coverage": graph.coverage.value,
            "confidence": graph.confidence.value,
            "relationships": [
                [
                    edge.kind,
                    edge.source_path,
                    edge.source_symbol,
                    edge.target_path,
                    edge.target_symbol,
                ]
                for edge in relationships
            ],
            "affected_paths": [item.path for item in graph.affected_paths],
            "projected_paths": list(projected),
            "limitations": list(graph.limitations),
            "truncated": graph.truncated,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return CanaryObservation(
            hashlib.sha256(canonical).hexdigest(),
            relationships,
            tuple(item.path for item in graph.affected_paths),
            projected,
            graph.limitations,
            graph.status is not CodeIntelligenceStatus.UNAVAILABLE,
            any("unsupported" in item.lower() for item in graph.limitations),
            False,
            False,
            "0" * 64,
            "0" * 64,
        )

    def _incremental_deletion(
        self,
        provider: ManagedCodeGraphProvider,
        identity: PromotionIdentity,
        source: Path,
        workspace_id: str,
    ) -> bool:
        deleted_path = "src/beta.py"
        (source / deleted_path).unlink()
        request = self._request(
            identity,
            source,
            workspace_id,
            version=2,
            deleted=frozenset({deleted_path}),
        )
        baseline = self._baseline.analyze(request)
        graph = provider.analyze(request, baseline)
        manifest = self._projection.read_manifest(workspace_id)
        paths = (
            frozenset(entry.path for entry in manifest.entries)
            if manifest is not None
            else frozenset()
        )
        return deleted_path not in paths and all(
            fact.source_path != deleted_path and fact.target_path != deleted_path
            for fact in graph.relationships
        )

    @staticmethod
    def _restore_file(source: Path, relative_path: str) -> None:
        data = dict(CANARY_FILES)[relative_path]
        atomic_write_bytes(source / relative_path, data)

    def _cleanup(self, workspace_id: str) -> bool:
        try:
            self._projection.dispose_workspace(workspace_id)
        except Exception:
            return False
        return not self._projection.workspace_root(workspace_id).exists()


__all__ = ["ManagedCodeGraphCanaryProbe"]
