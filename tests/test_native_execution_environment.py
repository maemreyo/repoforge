from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from repoforge.adapters.execution.native import NativeReviewedAdapter
from repoforge.domain.errors import CommandError, SecurityError
from repoforge.domain.execution_environment import EnvironmentIdentityRequest
from repoforge.ports.command import CommandResult
from repoforge.ports.execution_environment import ApprovedExecution


class RecordingExecutor:
    def __init__(self, *, missing: frozenset[str] = frozenset()) -> None:
        self.missing = missing
        self.calls: list[tuple[str, ...]] = []

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"PATH": "/private/bin", "LANG": "en_US.UTF-8", **dict(extra or {})}

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        extra_env: Mapping[str, str] | None = None,
        output_limit: int | None = None,
    ) -> CommandResult:
        del input_text, timeout, check, extra_env, output_limit
        command = tuple(argv)
        self.calls.append(command)
        if command[0] in self.missing:
            raise CommandError(f"Executable not found: {command[0]}")
        stdout = f"{command[0]} 1.0\n" if command[1:] == ("--version",) else "ok"
        return CommandResult(command, str(cwd), 0, stdout, "")

    def run_bytes(
        self, argv: Sequence[str], *, cwd: Path, timeout: int | None = None, max_bytes: int
    ) -> bytes:
        del argv, cwd, timeout, max_bytes
        return b""


def request(root: Path, *commands: tuple[str, ...]) -> EnvironmentIdentityRequest:
    return EnvironmentIdentityRequest(root, root, commands, ".")


def test_identity_inspects_only_profile_tools_and_hashes_reviewed_inputs(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("locked", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]", encoding="utf-8")
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)

    identity = adapter.identity(request(tmp_path, ("python", "-m", "pytest")))

    assert executor.calls == [("python", "--version")]
    assert identity.tools[0].version == "python 1.0"
    assert identity.lockfile_digests[0][0] == "uv.lock"
    assert identity.manifest_digests[0][0] == "pyproject.toml"
    assert identity.approved_env_var_names == ("LANG", "PATH")
    assert "/private/bin" not in repr(identity)


def test_missing_tool_produces_partial_identity_and_warning(tmp_path: Path) -> None:
    adapter = NativeReviewedAdapter(RecordingExecutor(missing=frozenset({"missing"})))
    identity_request = request(tmp_path, ("missing", "check"))

    identity = adapter.identity(identity_request)

    assert identity.tools == (identity.tools[0],)
    assert identity.tools[0].version is None
    assert identity.cache_eligible is False
    assert "missing" in adapter.doctor(identity_request)[0]


def test_execute_preserves_profile_command_contract(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)
    identity_request = request(tmp_path, ("python", "-m", "pytest"))
    identity = adapter.identity(identity_request)

    receipt = adapter.execute(
        ApprovedExecution(("python", "-m", "pytest"), identity_request, identity, 30)
    )

    assert receipt.argv == ("python", "-m", "pytest")
    assert receipt.identity_hash == identity.identity_hash
    assert receipt.result.stdout == "ok"


def test_unknown_profile_executable_is_not_probed(tmp_path: Path) -> None:
    executor = RecordingExecutor()
    adapter = NativeReviewedAdapter(executor)

    identity = adapter.identity(request(tmp_path, ("custom-build", "verify")))

    assert executor.calls == []
    assert identity.tools[0].version is None


def test_artifact_collection_rejects_escape_symlink_and_oversize(tmp_path: Path) -> None:
    adapter = NativeReviewedAdapter(RecordingExecutor(), max_artifact_bytes=3)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    (tmp_path / "large.txt").write_text("large", encoding="utf-8")

    with pytest.raises(SecurityError, match="escapes workspace"):
        adapter.collect_artifacts(("../outside.txt",), workspace_root=tmp_path)
    with pytest.raises(SecurityError, match="symlink"):
        adapter.collect_artifacts(("link.txt",), workspace_root=tmp_path)
    with pytest.raises(SecurityError, match="byte limit"):
        adapter.collect_artifacts(("large.txt",), workspace_root=tmp_path)


def test_collects_bounded_regular_artifact(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_text("ok", encoding="utf-8")
    adapter = NativeReviewedAdapter(RecordingExecutor(), max_artifact_bytes=3)

    artifacts = adapter.collect_artifacts(("result.txt", "missing.txt"), workspace_root=tmp_path)

    assert len(artifacts) == 1
    assert artifacts[0].path == "result.txt"
    assert artifacts[0].size_bytes == 2
