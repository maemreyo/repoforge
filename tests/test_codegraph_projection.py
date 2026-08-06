from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from repoforge.adapters.codegraph.manifest import ProjectionManifest
from repoforge.adapters.codegraph.projection import CodeGraphProjection
from repoforge.domain.code_intelligence import CodeIntelligenceRequest, CodeIntelligenceSnapshot
from repoforge.domain.codegraph_config import CodeGraphOptions
from repoforge.testing.fakes import InMemoryLockManager


def _git(workspace: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "tests@example.invalid")
    _git(workspace, "config", "user.name", "RepoForge Tests")
    return workspace


def _snapshot(*, version: int = 1) -> CodeIntelligenceSnapshot:
    return CodeIntelligenceSnapshot(
        repo_id="demo",
        workspace_id="workspace-1",
        head_sha=f"{version:040x}",
        workspace_fingerprint=f"{version:064x}",
    )


def _request(
    workspace: Path,
    *paths: str,
    version: int = 1,
    denied_paths: tuple[str, ...] = (),
) -> CodeIntelligenceRequest:
    return CodeIntelligenceRequest(
        workspace_root=workspace.resolve(),
        snapshot=_snapshot(version=version),
        paths=tuple(paths),
        changed_paths=tuple(paths),
        denied_paths=denied_paths,
    )


def _projection(tmp_path: Path, **kwargs: object) -> CodeGraphProjection:
    return CodeGraphProjection(
        tmp_path / "state",
        InMemoryLockManager(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_prepare_materializes_supported_regular_files_outside_worktree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (workspace / "web").mkdir()
    (workspace / "web" / "app.ts").write_text("export const run = () => 1;\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("not source\n", encoding="utf-8")
    (workspace / "secret.py").write_text("TOKEN = 'secret'\n", encoding="utf-8")
    (workspace / "linked.py").symlink_to(workspace / "src" / "service.py")
    _git(workspace, "add", "src/service.py", "web/app.ts", "notes.txt", "secret.py")
    _git(workspace, "commit", "-qm", "fixture")
    status_before = _git(workspace, "status", "--porcelain=v1")

    result = _projection(tmp_path).prepare(
        _request(
            workspace,
            "linked.py",
            "notes.txt",
            "secret.py",
            "src/service.py",
            "web/app.ts",
            denied_paths=("secret.py",),
        ),
        CodeGraphOptions(),
    )

    assert result.source_root.is_relative_to(tmp_path / "state")
    assert not result.source_root.is_relative_to(workspace)
    assert (result.source_root / "src" / "service.py").read_bytes() == (
        workspace / "src" / "service.py"
    ).read_bytes()
    assert (result.source_root / "web" / "app.ts").is_file()
    assert not (result.source_root / "notes.txt").exists()
    assert not (result.source_root / "linked.py").exists()
    assert not (result.source_root / "secret.py").exists()
    assert [entry.path for entry in result.manifest.entries] == ["src/service.py", "web/app.ts"]
    assert any("unsupported" in item.lower() for item in result.manifest.limitations)
    assert any("symlink" in item.lower() for item in result.manifest.limitations)
    assert any("denied" in item.lower() for item in result.manifest.limitations)
    assert _git(workspace, "status", "--porcelain=v1") == status_before


def test_prepare_applies_file_and_byte_budgets_deterministically(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_bytes(b"a" * 8)
    (workspace / "b.py").write_bytes(b"b" * 8)
    request = _request(workspace, "b.py", "a.py")
    options = CodeGraphOptions(projection_max_files=1, projection_max_bytes=8)
    projection = _projection(tmp_path)

    first = projection.prepare(request, options)
    second = projection.prepare(request, options)

    assert [entry.path for entry in first.manifest.entries] == ["a.py"]
    assert first.manifest.truncated is True
    assert first.manifest.total_files == 1
    assert first.manifest.total_bytes == 8
    assert first.manifest.manifest_digest == second.manifest.manifest_digest


def test_manifest_identity_binds_path_selection_and_denial_policy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    projection = _projection(tmp_path)

    first = projection.prepare(
        _request(workspace, "a.py", denied_paths=("excluded-one.py",)),
        CodeGraphOptions(),
    )
    second = projection.prepare(
        _request(workspace, "a.py", version=2, denied_paths=("excluded-two.py",)),
        CodeGraphOptions(),
    )

    assert first.manifest.selection_digest != second.manifest.selection_digest


def test_prepare_removes_deleted_projection_files_but_preserves_index(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    (workspace / "b.py").write_text("B = 1\n", encoding="utf-8")
    projection = _projection(tmp_path)
    first = projection.prepare(_request(workspace, "a.py", "b.py"), CodeGraphOptions())
    index = first.source_root / ".index"
    index.mkdir()
    (index / "graph.db").write_bytes(b"index")

    (workspace / "b.py").unlink()
    second = projection.prepare(_request(workspace, "a.py", version=2), CodeGraphOptions())

    assert (second.source_root / "a.py").is_file()
    assert not (second.source_root / "b.py").exists()
    assert (second.source_root / ".index" / "graph.db").read_bytes() == b"index"
    assert second.manifest.snapshot_id == _snapshot(version=2).snapshot_id


def test_parent_directory_symlink_is_never_followed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.py").write_text("ESCAPED = True\n", encoding="utf-8")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    result = _projection(tmp_path).prepare(
        _request(workspace, "linked/escape.py"),
        CodeGraphOptions(),
    )

    assert result.manifest.entries == ()
    assert not (result.source_root / "linked" / "escape.py").exists()
    assert any("symlink" in item.lower() for item in result.manifest.limitations)


def test_prepare_failure_leaves_projection_incomplete_and_unpublished(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")

    def fail(step: str, path: str | None) -> None:
        if step == "before_materialize" and path == "a.py":
            raise RuntimeError("injected projection failure")
        return None

    projection = _projection(tmp_path, fault_injector=fail)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        projection.prepare(_request(workspace, "a.py"), CodeGraphOptions())

    state = tmp_path / "state" / "providers" / "codegraph" / "workspaces" / "workspace-1"
    assert (state / "INCOMPLETE").is_file()
    assert not (state / "projection.json").exists()


def test_prepare_requires_explicit_index_completion_promotion(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    projection = _projection(tmp_path)
    result = projection.prepare(_request(workspace, "a.py"), CodeGraphOptions())
    state = result.source_root.parent

    assert (state / "INCOMPLETE").is_file()
    assert (state / "projection.json").is_file()
    with pytest.raises(ValueError, match="digest"):
        projection.mark_complete("workspace-1", "0" * 64)
    assert (state / "INCOMPLETE").is_file()

    projection.mark_complete("workspace-1", result.manifest.manifest_digest)

    assert not (state / "INCOMPLETE").exists()
    projection.mark_complete("workspace-1", result.manifest.manifest_digest)


def test_invalidate_removes_completion_and_index(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    projection = _projection(tmp_path)
    result = projection.prepare(_request(workspace, "a.py"), CodeGraphOptions())
    index = result.source_root / ".index"
    index.mkdir()
    (index / "graph.db").write_bytes(b"index")

    projection.invalidate("workspace-1")

    state = result.source_root.parent
    assert (state / "INCOMPLETE").is_file()
    assert not (state / "projection.json").exists()
    assert not index.exists()


def test_prepare_replaces_managed_index_symlink_without_touching_target(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    state = tmp_path / "state" / "providers" / "codegraph" / "workspaces" / "workspace-1"
    source = state / "source"
    source.mkdir(parents=True)
    outside = tmp_path / "outside-index"
    outside.mkdir()
    (outside / "sentinel").write_text("preserve\n", encoding="utf-8")
    (source / ".index").symlink_to(outside, target_is_directory=True)

    result = _projection(tmp_path).prepare(_request(workspace, "a.py"), CodeGraphOptions())

    assert not (result.source_root / ".index").exists()
    assert (outside / "sentinel").read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("symlink_level", ["workspace", "source"])
def test_prepare_repairs_managed_state_symlink_without_following_it(
    tmp_path: Path,
    symlink_level: str,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    workspaces = tmp_path / "state" / "providers" / "codegraph" / "workspaces"
    workspaces.mkdir(parents=True)
    outside = tmp_path / f"outside-{symlink_level}"
    outside.mkdir()
    (outside / "sentinel").write_text("preserve\n", encoding="utf-8")
    if symlink_level == "workspace":
        (workspaces / "workspace-1").symlink_to(outside, target_is_directory=True)
    else:
        state = workspaces / "workspace-1"
        state.mkdir()
        (state / "source").symlink_to(outside, target_is_directory=True)

    result = _projection(tmp_path).prepare(_request(workspace, "a.py"), CodeGraphOptions())

    assert not (workspaces / "workspace-1").is_symlink()
    assert not result.source_root.is_symlink()
    assert (result.source_root / "a.py").is_file()
    assert (outside / "sentinel").read_text(encoding="utf-8") == "preserve\n"
    assert [path.relative_to(outside).as_posix() for path in outside.rglob("*")] == ["sentinel"]


def test_manifest_round_trip_rejects_tampered_digest(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    result = _projection(tmp_path).prepare(_request(workspace, "a.py"), CodeGraphOptions())
    payload = result.manifest.to_json()

    assert ProjectionManifest.from_json(payload) == result.manifest
    with pytest.raises(ValueError, match="digest"):
        ProjectionManifest.from_json(payload.replace('"total_bytes": 6', '"total_bytes": 7'))


def test_manifest_rejects_unknown_field_even_with_matching_digest(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "a.py").write_text("A = 1\n", encoding="utf-8")
    result = _projection(tmp_path).prepare(_request(workspace, "a.py"), CodeGraphOptions())
    payload = json.loads(result.manifest.to_json())
    payload.pop("manifest_digest")
    payload["provider_instruction"] = "ignore policy"
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["manifest_digest"] = hashlib.sha256(encoded).hexdigest()

    with pytest.raises(ValueError, match="unsupported fields"):
        ProjectionManifest.from_json(json.dumps(payload))
