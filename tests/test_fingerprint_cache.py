from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.ports.command import CommandExecutor, CommandResult

from conftest import create_forge_environment


class CountingExecutor:
    def __init__(self, delegate: CommandExecutor) -> None:
        self._delegate = delegate
        self.full_fingerprint_scans = 0

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return self._delegate.environment(extra)

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
        if tuple(argv) == ("git", "diff", "--binary", "HEAD", "--"):
            self.full_fingerprint_scans += 1
        return self._delegate.run(
            argv,
            cwd=cwd,
            input_text=input_text,
            timeout=timeout,
            check=check,
            extra_env=extra_env,
            output_limit=output_limit,
        )

    def run_bytes(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: int | None = None,
        max_bytes: int,
    ) -> bytes:
        if tuple(argv) == ("git", "diff", "--binary", "HEAD", "--"):
            self.full_fingerprint_scans += 1
        return self._delegate.run_bytes(argv, cwd=cwd, timeout=timeout, max_bytes=max_bytes)


def _service_with_counting_executor(tmp_path: Path) -> tuple[CodingService, CountingExecutor]:
    environment = create_forge_environment(tmp_path)
    executor = CountingExecutor(environment.service.runner)
    return CodingService(load_config(environment.config_path), runner=executor), executor


def test_workspace_status_reuses_fingerprint_after_matching_validity_token(tmp_path: Path) -> None:
    service, executor = _service_with_counting_executor(tmp_path)
    workspace_id = service.workspace_create("demo", "fingerprint-cache-status")["workspace_id"]

    first = service.workspace_status(workspace_id)
    second = service.workspace_status(workspace_id)

    assert first["workspace_fingerprint"] == second["workspace_fingerprint"]
    assert executor.full_fingerprint_scans == 1


def test_workspace_status_recomputes_after_out_of_band_workspace_mutation(tmp_path: Path) -> None:
    service, executor = _service_with_counting_executor(tmp_path)
    created = service.workspace_create("demo", "fingerprint-cache-external")
    workspace_id = created["workspace_id"]

    before = service.workspace_status(workspace_id)
    Path(created["path"]).joinpath("external.txt").write_text("changed outside RepoForge\n")
    after = service.workspace_status(workspace_id)

    assert after["workspace_fingerprint"] != before["workspace_fingerprint"]
    assert executor.full_fingerprint_scans == 2


def test_workspace_status_recomputes_when_a_dirty_tracked_file_changes_outside_repoforge(
    tmp_path: Path,
) -> None:
    service, executor = _service_with_counting_executor(tmp_path)
    created = service.workspace_create("demo", "fingerprint-cache-dirty-tracked")
    workspace_id = created["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    service.workspace_write_file(workspace_id, "hello.txt", "first change\n", hello["sha256"])
    before = service.workspace_status(workspace_id)
    Path(created["path"]).joinpath("hello.txt").write_text("second change\n")
    after = service.workspace_status(workspace_id)

    assert after["workspace_fingerprint"] != before["workspace_fingerprint"]
    assert executor.full_fingerprint_scans == 2


def test_workspace_status_recomputes_when_spaced_untracked_file_changes_outside_repoforge(
    tmp_path: Path,
) -> None:
    service, executor = _service_with_counting_executor(tmp_path)
    created = service.workspace_create("demo", "fingerprint-cache-spaced-untracked")
    workspace_id = created["workspace_id"]
    external = Path(created["path"]).joinpath("external file.txt")
    external.write_text("first\n")
    before = service.workspace_status(workspace_id)

    external.write_text("other\n")
    after = service.workspace_status(workspace_id)

    assert after["workspace_fingerprint"] != before["workspace_fingerprint"]
    assert executor.full_fingerprint_scans == 2


def test_apply_patch_primes_cached_post_mutation_fingerprint(tmp_path: Path) -> None:
    service, executor = _service_with_counting_executor(tmp_path)
    workspace_id = service.workspace_create("demo", "fingerprint-cache-patch")["workspace_id"]
    status = service.workspace_status(workspace_id)
    executor.full_fingerprint_scans = 0
    patch = "\n".join(
        (
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "@@ -1,3 +1,4 @@",
            " # Demo",
            " ",
            " Repository instructions.",
            "+Cached fingerprint.",
            "",
        )
    )

    applied = service.workspace_apply_patch(
        workspace_id,
        patch,
        status["head_sha"],
        status["workspace_fingerprint"],
    )
    status_after = service.workspace_status(workspace_id)

    assert status_after["workspace_fingerprint"] == applied["workspace_fingerprint"]
    assert executor.full_fingerprint_scans == 1
