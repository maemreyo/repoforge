"""Every mutating workspace operation returns fresh optimistic-lock tokens.

A client chaining write -> patch -> replace -> restore must be able to use
only the tokens returned by each previous call; workspace_status should
never be required in between.
"""

from __future__ import annotations

from pathlib import Path

from conftest import create_forge_environment

from repoforge.application.service import CodingService
from repoforge.config import load_config

_README_PATCH = "\n".join(
    (
        "diff --git a/README.md b/README.md",
        "--- a/README.md",
        "+++ b/README.md",
        "@@ -1,3 +1,4 @@",
        " # Demo",
        " ",
        " Repository instructions.",
        "+Chained mutation.",
        "",
    )
)


def _service(tmp_path: Path) -> CodingService:
    environment = create_forge_environment(tmp_path)
    return CodingService(load_config(environment.config_path))


def test_write_apply_replace_restore_chain_uses_only_returned_tokens(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace_id = service.workspace_create("demo", "optimistic-lock-chain")["workspace_id"]

    write = service.workspace_write_file(workspace_id, "notes.txt", "draft\n", "<new>")
    assert "workspace_fingerprint" in write
    assert "head_sha" in write

    applied = service.workspace_apply_patch(
        workspace_id,
        _README_PATCH,
        write["head_sha"],
        write["workspace_fingerprint"],
    )
    assert "head_sha" in applied
    assert applied["workspace_fingerprint"] != write["workspace_fingerprint"]

    hello = service.workspace_read_file(workspace_id, "hello.txt")
    replaced = service.workspace_replace_text(
        workspace_id, "hello.txt", "hello", "hello, chained", hello["sha256"]
    )
    assert "workspace_fingerprint" in replaced
    assert "head_sha" in replaced
    assert replaced["workspace_fingerprint"] != applied["workspace_fingerprint"]

    restored = service.workspace_restore_paths(
        workspace_id, ["notes.txt"], replaced["workspace_fingerprint"]
    )
    assert "head_sha" in restored
    assert "notes.txt" in restored["removed_untracked"]

    final_status = service.workspace_status(workspace_id)
    assert final_status["workspace_fingerprint"] == restored["workspace_fingerprint"]
    assert final_status["head_sha"] == restored["head_sha"] == applied["head_sha"]


def test_run_profile_and_run_diagnostic_return_head_sha(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace_id = service.workspace_create("demo", "optimistic-lock-profile")["workspace_id"]

    profile_result = service.workspace_run_profile(workspace_id, "quick")
    assert "head_sha" in profile_result

    diagnostic_result = service.workspace_run_diagnostic(
        workspace_id, "pytest-target", selector="hello.txt::test_example"
    )
    assert "head_sha" in diagnostic_result
