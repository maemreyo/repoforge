from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from repoforge.adapters.codegraph.command import CodeGraphCommandOutput
from repoforge.adapters.codegraph.manifest import (
    ProjectionEntry,
    ProjectionManifest,
    ProjectionResult,
)
from repoforge.domain.code_intelligence import (
    CodeIntelligenceMeasure,
    CodeIntelligenceRequest,
    CodeIntelligenceResult,
    CodeIntelligenceSnapshot,
    CodeIntelligenceStatus,
    CodeLanguage,
    CodeSymbolFact,
    CodeSymbolKind,
    new_code_intelligence_result,
)
from repoforge.domain.codegraph_config import CodeGraphOptions
from repoforge.domain.provider_manifest import (
    ProviderExecutableIdentity,
    ProviderFilesystemRequirement,
    ProviderKind,
    ProviderManifest,
    ProviderOutputBounds,
)


def snapshot() -> CodeIntelligenceSnapshot:
    return CodeIntelligenceSnapshot("demo", "workspace-1", "a" * 40, "b" * 64)


def request(
    tmp_path: Path,
    *,
    changed: tuple[str, ...] = ("src/service.py",),
) -> CodeIntelligenceRequest:
    return CodeIntelligenceRequest(
        workspace_root=tmp_path / "workspace",
        snapshot=snapshot(),
        paths=("src/helper.py", "src/service.py", "tests/test_service.py"),
        changed_paths=changed,
    )


def baseline(
    code_request: CodeIntelligenceRequest,
    *symbols: CodeSymbolFact,
) -> CodeIntelligenceResult:
    return new_code_intelligence_result(
        provider_id="tree-sitter",
        provider_version="1",
        snapshot=code_request.snapshot,
        status=CodeIntelligenceStatus.CURRENT,
        coverage=CodeIntelligenceMeasure(100, "baseline complete"),
        confidence=CodeIntelligenceMeasure(100, "baseline calibrated"),
        analyzed_paths=code_request.paths,
        symbols=tuple(symbols),
    )


def symbol(name: str, path: str = "src/service.py") -> CodeSymbolFact:
    return CodeSymbolFact(
        CodeLanguage.PYTHON,
        name,
        f"{path[:-3].replace('/', '.')}.{name}",
        CodeSymbolKind.FUNCTION,
        path,
        1,
    )


def manifest(options: CodeGraphOptions | None = None) -> ProviderManifest:
    return ProviderManifest(
        provider_id="codegraph",
        kind=ProviderKind.ANALYZER,
        version="1.5.0",
        runtime=ProviderExecutableIdentity("codegraph", "c" * 64),
        supported_capabilities=("semantic_graph",),
        network_policy="none",
        filesystem=ProviderFilesystemRequirement(capability="managed_state_write"),
        output_bounds=ProviderOutputBounds(20_000, 20_000, 1_000_000),
        codegraph=options or CodeGraphOptions(),
    )


def status(source: Path, *, file_count: int = 3) -> str:
    return json.dumps(
        {
            "initialized": True,
            "version": "1.5.0",
            "projectPath": str(source),
            "indexPath": str(source / ".index"),
            "lastIndexed": "2026-08-06T00:00:00.000Z",
            "fileCount": file_count,
            "nodeCount": 6,
            "edgeCount": 5,
            "dbSizeBytes": 4096,
            "backend": "native",
            "journalMode": "wal",
            "nodesByKind": {"file": file_count, "function": 3},
            "languages": ["python"],
            "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
            "worktreeMismatch": None,
            "index": {
                "builtWithVersion": "1.5.0",
                "builtWithExtractionVersion": 7,
                "currentExtractionVersion": 7,
                "reindexRecommended": False,
                "state": "complete",
                "pendingRefs": 0,
            },
        }
    )


def query(name: str, path: str) -> str:
    node = {
        "id": f"node-{name}",
        "kind": "function",
        "name": name,
        "qualifiedName": f"{path[:-3].replace('/', '.')}.{name}",
        "filePath": path,
        "language": "python",
        "startLine": 1,
        "endLine": 2,
        "startColumn": 0,
        "endColumn": len(name),
        "updatedAt": 1,
    }
    return json.dumps([{"node": node, "score": 10.0}])


class FakeProjection:
    def __init__(
        self,
        tmp_path: Path,
        code_request: CodeIntelligenceRequest,
        *,
        projected_paths: tuple[str, ...] | None = None,
        limitations: tuple[str, ...] = (),
        truncated: bool = False,
    ) -> None:
        self.root = tmp_path / "state" / code_request.snapshot.workspace_id
        self.source = self.root / "source"
        self.home = self.root / "home"
        self.source.mkdir(parents=True)
        self.home.mkdir()
        paths = projected_paths if projected_paths is not None else code_request.paths
        entries = tuple(
            ProjectionEntry(path, hashlib.sha256(path.encode()).hexdigest(), len(path))
            for path in paths
        )
        self.result = ProjectionResult(
            self.source,
            ProjectionManifest.from_snapshot(
                code_request.snapshot,
                options_digest=CodeGraphOptions().options_digest,
                selection_digest="d" * 64,
                entries=entries,
                limitations=limitations,
                truncated=truncated,
            ),
        )
        self.prepared = 0
        self.completed: list[str] = []
        self.invalidated = 0
        self.operation_entries = 0
        self.operation_active = False

    @contextmanager
    def operation(
        self,
        workspace_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator[None]:
        del timeout_seconds
        assert workspace_id == "workspace-1"
        assert not self.operation_active
        self.operation_entries += 1
        self.operation_active = True
        try:
            yield
        finally:
            self.operation_active = False

    def workspace_root(self, workspace_id: str) -> Path:
        assert workspace_id == "workspace-1"
        return self.root

    def prepare(
        self,
        code_request: CodeIntelligenceRequest,
        options: CodeGraphOptions,
    ) -> ProjectionResult:
        del code_request, options
        self.prepared += 1
        return self.result

    def mark_complete(self, workspace_id: str, manifest_digest: str) -> None:
        assert workspace_id == "workspace-1"
        self.completed.append(manifest_digest)

    def invalidate(self, workspace_id: str) -> None:
        assert workspace_id == "workspace-1"
        self.invalidated += 1
        for path in (self.root / "INCOMPLETE", self.source / ".index"):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()


class FakeRunner:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[tuple[str, object]] = []
        self.ambiguous_query = False
        self.fail_query = False
        self.status_file_count: int | None = None
        self.status_payload: str | None = None
        self.truncated_command: str | None = None

    def _output(self, command: str, stdout: str) -> CodeGraphCommandOutput:
        return CodeGraphCommandOutput(
            command,
            stdout,
            command == self.truncated_command,
        )

    def init(self, projection: Path, home: Path, **_: object) -> CodeGraphCommandOutput:
        del home
        self.calls.append(("init", projection))
        (projection / ".index").mkdir()
        return self._output("init", "initialized")

    def sync(self, projection: Path, home: Path, **_: object) -> CodeGraphCommandOutput:
        del home
        self.calls.append(("sync", projection))
        return self._output("sync", "synced")

    def status(self, projection: Path, home: Path, **_: object) -> CodeGraphCommandOutput:
        del home
        self.calls.append(("status", projection))
        payload = self.status_payload or status(
            self.source,
            file_count=self.status_file_count or 3,
        )
        return self._output("status", payload)

    def affected(
        self,
        projection: Path,
        home: Path,
        paths: tuple[str, ...],
        **_: object,
    ) -> CodeGraphCommandOutput:
        del projection, home
        self.calls.append(("affected", paths))
        payload = {
            "changedFiles": list(paths),
            "affectedTests": ["tests/test_service.py"],
            "totalDependentsTraversed": 2,
        }
        return self._output("affected", json.dumps(payload))

    def query(
        self,
        projection: Path,
        home: Path,
        search: str,
        **_: object,
    ) -> CodeGraphCommandOutput:
        del projection, home
        self.calls.append(("query", search))
        if self.fail_query:
            payload = '{"private":"provider-secret"} trailing'
        elif self.ambiguous_query:
            payload = json.dumps(
                [
                    *json.loads(query(search, "src/service.py")),
                    *json.loads(query(search, "src/helper.py")),
                ]
            )
        else:
            payload = query(search, "src/service.py")
        return self._output("query", payload)

    def callers(
        self,
        projection: Path,
        home: Path,
        symbol_name: str,
        **_: object,
    ) -> CodeGraphCommandOutput:
        del projection, home
        self.calls.append(("callers", symbol_name))
        payload = {
            "symbol": symbol_name,
            "callers": [
                {
                    "name": "helper",
                    "kind": "function",
                    "filePath": "src/helper.py",
                    "startLine": 1,
                }
            ],
        }
        return self._output("callers", json.dumps(payload))

    def callees(
        self,
        projection: Path,
        home: Path,
        symbol_name: str,
        **_: object,
    ) -> CodeGraphCommandOutput:
        del projection, home
        self.calls.append(("callees", symbol_name))
        payload = {
            "symbol": symbol_name,
            "callees": [
                {
                    "name": "helper",
                    "kind": "function",
                    "filePath": "src/helper.py",
                    "startLine": 1,
                }
            ],
        }
        return self._output("callees", json.dumps(payload))

    def impact(
        self,
        projection: Path,
        home: Path,
        symbol_name: str,
        **_: object,
    ) -> CodeGraphCommandOutput:
        del projection, home
        self.calls.append(("impact", symbol_name))
        payload = {
            "symbol": symbol_name,
            "depth": 5,
            "nodeCount": 1,
            "edgeCount": 1,
            "affected": [
                {
                    "name": "test_service",
                    "kind": "function",
                    "filePath": "tests/test_service.py",
                    "startLine": 1,
                }
            ],
        }
        return self._output("impact", json.dumps(payload))


class SequenceClock:
    def __init__(self, values: Iterable[float]) -> None:
        self._values = iter(values)
        self._last = 0.0

    def __call__(self) -> float:
        self._last = next(self._values, self._last)
        return self._last
