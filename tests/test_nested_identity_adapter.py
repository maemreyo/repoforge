from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from repoforge.adapters.git.nested_identity import GitNestedResourceDiscovery
from repoforge.domain.errors import CommandError, ErrorCode, RepoForgeError
from repoforge.domain.nested_identity import NestedAccess, NestedResourceKind
from repoforge.ports.cancellation import CancellationToken
from repoforge.ports.command import CommandResult
from repoforge.ports.nested_identity import NestedDiscoveryRequest


class RecordingDiscoveryExecutor:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], CommandResult | BaseException] = {}
        self.calls: list[dict[str, object]] = []

    def respond(
        self,
        cwd: Path,
        config_file: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        stdout_truncated: bool = False,
    ) -> None:
        self.responses[(str(cwd), config_file)] = CommandResult(
            argv=(),
            cwd=str(cwd),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
        )

    def fail(self, cwd: Path, config_file: str, error: BaseException) -> None:
        self.responses[(str(cwd), config_file)] = error

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})

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
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del input_text, extra_env, cancel_token
        call = {
            "argv": tuple(argv),
            "cwd": cwd,
            "timeout": timeout,
            "check": check,
            "output_limit": output_limit,
        }
        self.calls.append(call)
        config_file = str(argv[argv.index("--file") + 1])
        response = self.responses.get((str(cwd), config_file))
        if response is None:
            return CommandResult(tuple(argv), str(cwd), 0, "", "")
        if isinstance(response, BaseException):
            raise response
        return CommandResult(
            argv=tuple(argv),
            cwd=str(cwd),
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
            stdout_truncated=response.stdout_truncated,
            stderr_truncated=response.stderr_truncated,
        )

    def run_isolated(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        secrets: Sequence[str],
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        output_limit: int | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> CommandResult:
        del environment, secrets
        return self.run(
            argv,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
            check=check,
            output_limit=output_limit,
            cancel_token=cancel_token,
        )

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        return self.run(argv, cwd=cwd, timeout=timeout, output_limit=max_bytes).stdout.encode()


def _request(root: Path, **changes: object) -> NestedDiscoveryRequest:
    values: dict[str, object] = {
        "root": root,
        "primary_endpoint": "https://github.com/acme/platform.git",
        "submodule_access": NestedAccess.READ,
        "lfs_access": NestedAccess.READ,
        "include_lfs": False,
        "max_depth": 8,
        "max_resources": 64,
        "max_output_bytes": 4096,
        "command_timeout_seconds": 7,
    }
    values.update(changes)
    return NestedDiscoveryRequest(**values)  # type: ignore[arg-type]


def _gitmodules(path: Path) -> None:
    path.joinpath(".gitmodules").write_text("reviewed by git config\n", encoding="utf-8")


def _checked_out(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.joinpath(".git").mkdir()


@pytest.mark.parametrize(
    "changes",
    (
        {"max_depth": -1},
        {"max_depth": 33},
        {"max_resources": 0},
        {"max_resources": 257},
        {"max_output_bytes": 0},
        {"command_timeout_seconds": 0},
        {"include_lfs": "yes"},
    ),
)
def test_discovery_request_rejects_invalid_bounds(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        _request(tmp_path, **changes)


def test_discovery_request_requires_absolute_root() -> None:
    with pytest.raises(ValueError):
        _request(Path("relative"))


def test_discovers_relative_submodule_and_default_and_custom_lfs_endpoints(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    root.joinpath(".lfsconfig").write_text("reviewed by git config\n", encoding="utf-8")
    child = root / "vendor" / "sdk"
    _checked_out(child)
    child.joinpath(".lfsconfig").write_text("reviewed by git config\n", encoding="utf-8")

    executor = RecordingDiscoveryExecutor()
    executor.respond(
        root,
        ".gitmodules",
        stdout=("submodule.sdk.path vendor/sdk\nsubmodule.sdk.url ../sdk.git\n"),
    )
    executor.respond(
        root,
        ".lfsconfig",
        stdout="https://github.com/acme/platform-lfs\n",
    )
    executor.respond(
        child,
        ".lfsconfig",
        stdout="https://github.com/acme/sdk-lfs\n",
    )

    discovered = GitNestedResourceDiscovery(executor).discover(_request(root, include_lfs=True))

    assert [(item.kind, item.depth, item.canonical_endpoint) for item in discovered] == [
        (
            NestedResourceKind.LFS,
            0,
            "https://github.com/acme/platform-lfs",
        ),
        (
            NestedResourceKind.LFS,
            1,
            "https://github.com/acme/sdk-lfs",
        ),
        (
            NestedResourceKind.SUBMODULE,
            1,
            "https://github.com/acme/sdk.git",
        ),
    ]
    assert discovered[2].source_location == ".gitmodules:vendor/sdk"
    assert all(call["timeout"] == 7 for call in executor.calls)
    assert all(call["output_limit"] == 4096 for call in executor.calls)
    assert all(call["check"] is False for call in executor.calls)


def test_recurses_only_into_checked_out_submodules_and_preserves_duplicate_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    checked = root / "vendor" / "sdk"
    _checked_out(checked)
    _gitmodules(checked)
    not_checked = root / "vendor" / "docs"
    not_checked.mkdir(parents=True)

    executor = RecordingDiscoveryExecutor()
    executor.respond(
        root,
        ".gitmodules",
        stdout=(
            "submodule.sdk.path vendor/sdk\n"
            "submodule.sdk.url ../sdk.git\n"
            "submodule.docs.path vendor/docs\n"
            "submodule.docs.url ../sdk.git\n"
        ),
    )
    executor.respond(
        checked,
        ".gitmodules",
        stdout=("submodule.tools.path tools\nsubmodule.tools.url git@github.com:acme/tools.git\n"),
    )

    discovered = GitNestedResourceDiscovery(executor).discover(_request(root))

    assert [(item.depth, item.source_location) for item in discovered] == [
        (1, ".gitmodules:vendor/docs"),
        (1, ".gitmodules:vendor/sdk"),
        (2, "vendor/sdk/.gitmodules:tools"),
    ]
    assert discovered[0].endpoint_digest == discovered[1].endpoint_digest
    assert all(call["cwd"] != not_checked for call in executor.calls)


def test_default_lfs_endpoint_never_invokes_git_lfs(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    executor = RecordingDiscoveryExecutor()

    discovered = GitNestedResourceDiscovery(executor).discover(
        _request(root, include_lfs=True, lfs_access=NestedAccess.WRITE)
    )

    assert len(discovered) == 1
    assert discovered[0].kind is NestedResourceKind.LFS
    assert discovered[0].access is NestedAccess.WRITE
    assert discovered[0].canonical_endpoint == ("https://github.com/acme/platform.git/info/lfs")
    rendered = " ".join(" ".join(call["argv"]) for call in executor.calls)  # type: ignore[arg-type]
    for forbidden in (
        "fetch",
        "submodule update",
        "git lfs",
        "credential",
        "smudge",
        "upload",
    ):
        assert forbidden not in rendered.lower()


@pytest.mark.parametrize(
    "output",
    (
        "submodule.sdk.path ../outside\nsubmodule.sdk.url ../sdk.git\n",
        (
            "submodule.sdk.path vendor/sdk\n"
            "submodule.sdk.path vendor/other\n"
            "submodule.sdk.url ../sdk.git\n"
        ),
        "submodule.sdk.path vendor/sdk\n",
        "submodule.sdk.url ../sdk.git\n",
        "submodule.sdk.path vendor/sdk\nsubmodule.sdk.url file:///tmp/sdk.git\n",
        (
            "submodule.sdk.path vendor/sdk\n"
            "submodule.sdk.url https://user:secret@github.com/acme/sdk.git\n"
        ),
    ),
)
def test_rejects_malformed_or_unsafe_gitmodules(tmp_path: Path, output: str) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    executor = RecordingDiscoveryExecutor()
    executor.respond(root, ".gitmodules", stdout=output)

    with pytest.raises((RepoForgeError, ValueError)) as failure:
        GitNestedResourceDiscovery(executor).discover(_request(root))

    if isinstance(failure.value, RepoForgeError):
        assert failure.value.code is ErrorCode.SECURITY_POLICY_VIOLATION
        assert failure.value.unchanged_state == ("No nested credentialed action was admitted.",)


def test_rejects_symlinked_submodule_path_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    actual = root / "actual-sdk"
    actual.mkdir()
    vendor = root / "vendor"
    vendor.mkdir()
    vendor.joinpath("sdk").symlink_to(actual, target_is_directory=True)
    executor = RecordingDiscoveryExecutor()
    executor.respond(
        root,
        ".gitmodules",
        stdout="submodule.sdk.path vendor/sdk\nsubmodule.sdk.url ../sdk.git\n",
    )

    with pytest.raises(RepoForgeError) as failure:
        GitNestedResourceDiscovery(executor).discover(_request(root))

    assert failure.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_rejects_cycle_to_primary_endpoint(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    executor = RecordingDiscoveryExecutor()
    executor.respond(
        root,
        ".gitmodules",
        stdout=(
            "submodule.loop.path vendor/loop\n"
            "submodule.loop.url https://github.com/acme/platform.git\n"
        ),
    )

    with pytest.raises(RepoForgeError) as failure:
        GitNestedResourceDiscovery(executor).discover(_request(root))

    assert failure.value.code is ErrorCode.SECURITY_POLICY_VIOLATION
    assert "platform.git" not in str(failure.value)


def test_enforces_resource_and_depth_limits_before_recursing(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    first = root / "one"
    _checked_out(first)
    _gitmodules(first)
    executor = RecordingDiscoveryExecutor()
    executor.respond(
        root,
        ".gitmodules",
        stdout=(
            "submodule.one.path one\nsubmodule.one.url ../one.git\n"
            "submodule.two.path two\nsubmodule.two.url ../two.git\n"
        ),
    )
    executor.respond(
        first,
        ".gitmodules",
        stdout="submodule.deep.path deep\nsubmodule.deep.url ../deep.git\n",
    )

    with pytest.raises(RepoForgeError) as resources:
        GitNestedResourceDiscovery(executor).discover(_request(root, max_resources=1))
    assert resources.value.code is ErrorCode.SECURITY_POLICY_VIOLATION

    with pytest.raises(RepoForgeError) as depth:
        GitNestedResourceDiscovery(executor).discover(_request(root, max_depth=1))
    assert depth.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_command_failure_timeout_and_truncation_are_typed_and_safe(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)

    failed = RecordingDiscoveryExecutor()
    failed.respond(root, ".gitmodules", returncode=2, stderr="config parse failed")
    with pytest.raises(RepoForgeError) as command_failure:
        GitNestedResourceDiscovery(failed).discover(_request(root))
    assert command_failure.value.code is ErrorCode.COMMAND_FAILED

    timed_out = RecordingDiscoveryExecutor()
    timed_out.fail(
        root,
        ".gitmodules",
        CommandError(
            "COMMAND_TIMEOUT: bounded discovery timed out",
            code=ErrorCode.COMMAND_TIMEOUT,
        ),
    )
    with pytest.raises(RepoForgeError) as timeout:
        GitNestedResourceDiscovery(timed_out).discover(_request(root))
    assert timeout.value.code is ErrorCode.COMMAND_TIMEOUT

    truncated = RecordingDiscoveryExecutor()
    truncated.respond(
        root,
        ".gitmodules",
        stdout="submodule.sdk.path vendor/sdk\n",
        stdout_truncated=True,
    )
    with pytest.raises(RepoForgeError) as overflow:
        GitNestedResourceDiscovery(truncated).discover(_request(root))
    assert overflow.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_discovery_commands_are_read_only_bounded_and_repository_local(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _gitmodules(root)
    root.joinpath(".lfsconfig").write_text("reviewed by git config\n", encoding="utf-8")
    executor = RecordingDiscoveryExecutor()
    executor.respond(root, ".gitmodules")
    executor.respond(root, ".lfsconfig")

    GitNestedResourceDiscovery(executor).discover(_request(root, include_lfs=True))

    for call in executor.calls:
        argv = call["argv"]
        assert argv[:3] == ("git", "config", "--file")  # type: ignore[index]
        assert argv[3] in {".gitmodules", ".lfsconfig"}  # type: ignore[index]
        assert call["cwd"] == root
    rendered = json_safe = repr(executor.calls).lower()
    assert str(Path.home()).lower() not in rendered
    assert "token" not in json_safe
    assert "authorization" not in json_safe
