"""Tests for execution environment identity, port, and native adapter."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from repoforge.domain.execution_environment import (
    EnvironmentAdapterKind,
    EnvironmentIdentity,
    FilesystemCapability,
    NetworkPolicy,
    ToolVersion,
    normalize_tool_name,
)
from repoforge.ports.command import CommandResult


class TestToolVersion:
    def test_valid_tool(self) -> None:
        tv = ToolVersion(name="python", version="3.10.0")
        assert tv.name == "python"
        assert tv.version == "3.10.0"
        assert tv.digest is None

    def test_valid_tool_with_digest(self) -> None:
        sha = "a" * 64
        tv = ToolVersion(name="python", digest=sha)
        assert tv.digest == sha

    def test_invalid_tool_name_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid tool name"):
            ToolVersion(name="")

    def test_invalid_tool_name_too_long(self) -> None:
        with pytest.raises(ValueError, match="Invalid tool name"):
            ToolVersion(name="x" * 65)

    def test_invalid_tool_name_special_chars(self) -> None:
        with pytest.raises(ValueError, match="Invalid tool name"):
            ToolVersion(name="../python")

    def test_invalid_digest_length(self) -> None:
        with pytest.raises(ValueError, match="Invalid tool digest"):
            ToolVersion(name="python", digest="short")

    def test_invalid_digest_hex(self) -> None:
        with pytest.raises(ValueError, match="Invalid tool digest"):
            ToolVersion(name="python", digest="z" + "a" * 63)

    def test_empty_version_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid tool version"):
            ToolVersion(name="python", version="")

    def test_valid_version_with_plus(self) -> None:
        tv = ToolVersion(name="node", version="v18.2.0+20220101")
        assert tv.version == "v18.2.0+20220101"

    def test_valid_name_with_dot(self) -> None:
        tv = ToolVersion(name="python3.10", version="3.10.0")
        assert tv.name == "python3.10"


class TestEnvironmentIdentity:
    def test_default_identity(self) -> None:
        ident = EnvironmentIdentity()
        assert ident.adapter_kind is EnvironmentAdapterKind.NATIVE_REVIEWED
        assert ident.adapter_version == "1"
        assert ident.platform == ""
        assert ident.architecture == ""

    def test_unsupported_schema_version(self) -> None:
        with pytest.raises(ValueError, match="Unsupported schema version"):
            EnvironmentIdentity(schema_version=999)

    def test_full_identity(self) -> None:
        ident = EnvironmentIdentity(
            adapter_kind=EnvironmentAdapterKind.NATIVE_REVIEWED,
            adapter_version="1",
            platform="linux",
            architecture="x86_64",
            python_version="3.10.12",
            runtime_version="uv/0.4.5",
            tools=(
                ToolVersion(name="python", version="3.10.12"),
                ToolVersion(name="git", version="2.40.0"),
            ),
            lockfile_digests=(
                ("uv.lock", hashlib.sha256(b"lock").hexdigest()),
            ),
            manifest_digests=(("pyproject.toml", hashlib.sha256(b"manifest").hexdigest()),),
            approved_env_var_names=("PATH", "HOME"),
            network_policy=NetworkPolicy.RESTRICTED,
            filesystem_capability=FilesystemCapability.WORKSPACE_WRITE,
            working_directory_policy_hash=hashlib.sha256(b"/workspace").hexdigest(),
        )
        assert ident.identity_hash
        assert len(ident.identity_hash) == 64
        assert ident.cache_eligible

    def test_identity_deterministic(self) -> None:
        ident1 = EnvironmentIdentity(
            platform="darwin", architecture="arm64", python_version="3.11.0"
        )
        ident2 = EnvironmentIdentity(
            platform="darwin", architecture="arm64", python_version="3.11.0"
        )
        assert ident1.identity_hash == ident2.identity_hash

    def test_identity_changes_with_platform(self) -> None:
        ident1 = EnvironmentIdentity(platform="darwin", python_version="3.11.0")
        ident2 = EnvironmentIdentity(platform="linux", python_version="3.11.0")
        assert ident1.identity_hash != ident2.identity_hash

    def test_identity_changes_with_tool_version(self) -> None:
        ident1 = EnvironmentIdentity(
            platform="linux",
            python_version="3.11.0",
            tools=(ToolVersion(name="git", version="2.40.0"),),
        )
        ident2 = EnvironmentIdentity(
            platform="linux",
            python_version="3.11.0",
            tools=(ToolVersion(name="git", version="2.41.0"),),
        )
        assert ident1.identity_hash != ident2.identity_hash

    def test_identity_order_independent(self) -> None:
        """Tools and lockfiles are sorted by name, so order doesn't matter."""
        ident1 = EnvironmentIdentity(
            tools=(
                ToolVersion(name="git", version="1"),
                ToolVersion(name="python", version="1"),
            ),
        )
        ident2 = EnvironmentIdentity(
            tools=(
                ToolVersion(name="python", version="1"),
                ToolVersion(name="git", version="1"),
            ),
        )
        assert ident1.identity_hash == ident2.identity_hash

    def test_cache_eligible_false_when_platform_missing(self) -> None:
        ident = EnvironmentIdentity(python_version="3.11.0", runtime_version="uv/0.4.5")
        assert not ident.cache_eligible

    def test_cache_eligible_false_when_tools_missing(self) -> None:
        ident = EnvironmentIdentity(
            platform="linux", python_version="3.11.0", runtime_version="uv/0.4.5"
        )
        assert not ident.cache_eligible

    def test_cache_eligible_false_when_tool_version_missing(self) -> None:
        ident = EnvironmentIdentity(
            platform="linux",
            python_version="3.11.0",
            runtime_version="uv/0.4.5",
            tools=(ToolVersion(name="git"),),
        )
        assert not ident.cache_eligible

    def test_cache_eligible_true(self) -> None:
        ident = EnvironmentIdentity(
            platform="linux",
            architecture="x86_64",
            python_version="3.11.0",
            runtime_version="uv/0.4.5",
            tools=(ToolVersion(name="git", version="2.40.0"),),
        )
        assert ident.cache_eligible

    def test_invalid_lockfile_name(self) -> None:
        with pytest.raises(ValueError, match="Invalid lockfile name"):
            EnvironmentIdentity(lockfile_digests=(("", "a" * 64),))

    def test_invalid_lockfile_digest(self) -> None:
        with pytest.raises(ValueError, match="Invalid lockfile digest"):
            EnvironmentIdentity(lockfile_digests=(("uv.lock", "bad"),))

    def test_invalid_manifest_name(self) -> None:
        with pytest.raises(ValueError, match="Invalid manifest name"):
            EnvironmentIdentity(manifest_digests=(("", "a" * 64),))

    def test_invalid_env_var_name(self) -> None:
        with pytest.raises(ValueError, match="Invalid env var name"):
            EnvironmentIdentity(approved_env_var_names=("",))

    def test_invalid_network_policy(self) -> None:
        with pytest.raises(ValueError, match="network_policy must be a NetworkPolicy"):
            EnvironmentIdentity(network_policy="invalid")  # type: ignore[arg-type]

    def test_invalid_filesystem_capability(self) -> None:
        with pytest.raises(ValueError, match="filesystem_capability must be a FilesystemCapability"):
            EnvironmentIdentity(filesystem_capability="invalid")  # type: ignore[arg-type]

    def test_invalid_working_directory_policy_hash(self) -> None:
        with pytest.raises(ValueError, match="Invalid working_directory_policy_hash"):
            EnvironmentIdentity(working_directory_policy_hash="short")

    def test_identity_excludes_secrets(self) -> None:
        """Verify identity serialization never contains secret-like fields."""
        ident = EnvironmentIdentity(
            platform="darwin",
            architecture="arm64",
            python_version="3.11.0",
        )
        raw = json.dumps(
            {
                "schema_version": ident.schema_version,
                "adapter_kind": ident.adapter_kind.value,
                "adapter_version": ident.adapter_version,
                "platform": ident.platform,
                "architecture": ident.architecture,
                "python_version": ident.python_version,
                "runtime_version": ident.runtime_version,
                "tools": list(ident.tools),
                "lockfile_digests": list(ident.lockfile_digests),
                "manifest_digests": list(ident.manifest_digests),
                "approved_env_var_names": list(ident.approved_env_var_names),
                "network_policy": ident.network_policy.value,
                "filesystem_capability": ident.filesystem_capability.value,
                "working_directory_policy_hash": ident.working_directory_policy_hash,
            },
            sort_keys=True,
        )
        # No full environment, no absolute paths, no secrets
        assert "HOME=" not in raw
        assert "/Users/" not in raw
        assert "secret" not in raw.lower()


class TestVerificationReceiptCompatibility:
    def test_receipt_remains_compatible_without_environment_identity(self) -> None:
        from repoforge.domain.workspace import VerificationReceipt

        receipt = VerificationReceipt(
            profile="full",
            fingerprint="a" * 64,
            completed_at="2026-07-15T00:00:00+00:00",
            commands=[],
        )
        assert receipt.environment_identity_hash is None

    def test_receipt_can_bind_environment_identity(self) -> None:
        from repoforge.domain.workspace import VerificationReceipt

        receipt = VerificationReceipt(
            profile="full",
            fingerprint="a" * 64,
            completed_at="2026-07-15T00:00:00+00:00",
            commands=[],
            environment_identity_hash="b" * 64,
        )
        assert receipt.environment_identity_hash == "b" * 64


class TestNormalizeToolName:
    def test_basic_normalize(self) -> None:
        assert normalize_tool_name("Python") == "python"

    def test_strip_whitespace(self) -> None:
        assert normalize_tool_name("  git  ") == "git"

    def test_replace_spaces(self) -> None:
        assert normalize_tool_name("node js") == "node_js"

    def test_truncate_long(self) -> None:
        assert normalize_tool_name("x" * 100) == "x" * 64


class TestRealPlatformIdentity:
    """Verify identity works against the real running platform."""

    def test_real_platform_produces_valid_hash(self) -> None:
        ident = EnvironmentIdentity(
            platform=platform.system().lower(),
            architecture=platform.machine().lower(),
            python_version=sys.version.split()[0],
            runtime_version=f"python/{sys.version.split()[0]}",
            tools=(
                ToolVersion(name="python", version=sys.version.split()[0]),
            ),
        )
        h = ident.identity_hash
        assert isinstance(h, str)
        assert len(h) == 64
        assert ident.cache_eligible


class _FakeExecutor:
    def __init__(self) -> None:
        self.called: list[tuple] = []

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {"PATH": "/usr/bin"}

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
        self.called.append(("run", argv, cwd, check))
        return CommandResult(
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=0,
            stdout="ok",
            stderr="",
        )

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        return b"ok"


class TestNativeReviewedAdapter:
    def test_identity_default(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        ident = adapter.identity()
        assert ident.adapter_kind is EnvironmentAdapterKind.NATIVE_REVIEWED
        assert ident.platform == platform.system().lower()
        assert ident.architecture == platform.machine().lower()
        assert ident.python_version == sys.version.split()[0]
        assert ident.identity_hash
        assert len(ident.identity_hash) == 64

    def test_identity_cached(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        ident1 = adapter.identity()
        ident2 = adapter.identity()
        assert ident1 is ident2

    def test_doctor_healthy(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        warnings = adapter.doctor()
        python_warnings = [w for w in warnings if "python" in w]
        assert len(python_warnings) == 0

    def test_execute_delegates_to_executor(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        fake = _FakeExecutor()
        adapter = NativeReviewedAdapter(fake)
        receipt = adapter.execute(
            ["echo", "hello"],
            cwd=Path("/tmp"),
        )
        assert receipt.argv == ("echo", "hello")
        assert receipt.result.stdout == "ok"
        assert receipt.identity_hash
        assert len(fake.called) == 1

    def test_execute_receipt_contains_identity_hash(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        fake = _FakeExecutor()
        adapter = NativeReviewedAdapter(fake)
        receipt = adapter.execute(["echo", "hi"], cwd=Path("/tmp"))
        ident = adapter.identity()
        assert receipt.identity_hash == ident.identity_hash

    def test_prepare_is_idempotent(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        adapter.prepare(cwd=Path("/tmp"))

    def test_cleanup_is_idempotent(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        adapter.cleanup(cwd=Path("/tmp"))

    def test_collect_artifacts_with_existing_files(self, tmp_path: Path) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        fake = _FakeExecutor()
        adapter = NativeReviewedAdapter(fake)
        test_file = tmp_path / "output.txt"
        test_file.write_text("hello")
        artifacts = adapter.collect_artifacts(["output.txt"], cwd=tmp_path)
        assert len(artifacts) == 1
        assert artifacts[0].path == "output.txt"
        assert artifacts[0].size_bytes == 5
        assert artifacts[0].digest

    def test_collect_artifacts_missing_file(self, tmp_path: Path) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        artifacts = adapter.collect_artifacts(["nonexistent.txt"], cwd=tmp_path)
        assert len(artifacts) == 0

    def test_identity_excludes_secrets_and_paths(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        ident = adapter.identity()
        assert ident.working_directory_policy_hash == ""
        raw = str(ident)
        assert "secret" not in raw.lower()
        assert "os.environ" not in raw

    def test_identity_no_absolute_user_paths(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        adapter = NativeReviewedAdapter(_FakeExecutor())
        ident = adapter.identity()
        raw = str(ident)
        assert "/Users/" not in raw

    def test_lockfile_digests_discovered(self, tmp_path: Path) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("lock-contents")
        adapter = NativeReviewedAdapter(
            _FakeExecutor(),
            project_root=tmp_path,
        )
        ident = adapter.identity()
        assert len(ident.lockfile_digests) >= 1
        found = any(name == "uv.lock" for name, _ in ident.lockfile_digests)
        assert found

    def test_manifest_digests_discovered(self, tmp_path: Path) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        manifest = tmp_path / "pyproject.toml"
        manifest.write_text("[project]\nname = 'test'")
        adapter = NativeReviewedAdapter(
            _FakeExecutor(),
            project_root=tmp_path,
        )
        ident = adapter.identity()
        found = any(name == "pyproject.toml" for name, _ in ident.manifest_digests)
        assert found


class TestNativeAdapterProfileCompatibility:
    def test_simple_command_preserves_argv(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        fake = _FakeExecutor()
        adapter = NativeReviewedAdapter(fake)
        receipt = adapter.execute(["git", "status"], cwd=Path("/tmp"))
        assert receipt.argv == ("git", "status")
        assert list(fake.called[0][1]) == ["git", "status"]

    def test_timeout_passed_through(self) -> None:
        from repoforge.adapters.execution.native import NativeReviewedAdapter

        fake = _FakeExecutor()
        adapter = NativeReviewedAdapter(fake)
        adapter.execute(["sleep", "1"], cwd=Path("/tmp"), timeout=30)
        assert list(fake.called[0][1]) == ["sleep", "1"]

