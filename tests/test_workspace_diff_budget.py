"""`workspace_diff` defaults to the cheap shape, because reviewing diffs dominates budget.

Audit evidence from a real installation: `workspace_diff` ran 2563 times averaging 60_294
bytes -- routinely at the ceiling -- while single-file `workspace_read_file` averaged 7_754
across 3931 calls. Reading files was never the problem; shipping every hunk of up to 100
files on every review was. `repo_history` already made patch text opt-in (`include_patch`);
this gives the diff tool the same switch and defaults it off.
"""

from __future__ import annotations

import json

from conftest import ForgeEnvironment, git


def _workspace_with_changes(env: ForgeEnvironment, files: int = 3) -> str:
    created = env.service.workspace_create("demo", "diff budget")
    workspace = created["path"]
    from pathlib import Path

    for index in range(files):
        # Enough lines that hunks dwarf the metadata, so the size difference is real
        # rather than an artefact of a tiny fixture.
        body = "\n".join(f"line {index}-{number} of changed content" for number in range(200))
        Path(workspace, f"changed_{index}.txt").write_text(body + "\n", encoding="utf-8")
    git("add", "-A", cwd=Path(workspace))
    return str(created["workspace_id"])


def test_diff_omits_hunks_by_default_and_says_so(forge_env: ForgeEnvironment) -> None:
    workspace_id = _workspace_with_changes(forge_env)

    result = forge_env.service.workspace_diff_v2(workspace_id, staged=True)

    assert result["hunks_included"] is False
    assert result["files"], "the file list must still be returned"
    for entry in result["files"]:
        assert not entry["hunks"], "hunks must be omitted unless requested"
        # The per-file verdict a reviewer usually needs survives.
        assert entry["path"]
        assert entry["additions"] >= 0
    # Aggregate metrics are untouched, so "how big is this change" needs no patch text.
    assert result["change_metrics"]["changed_files"] >= 3


def test_requesting_hunks_returns_them(forge_env: ForgeEnvironment) -> None:
    workspace_id = _workspace_with_changes(forge_env)

    result = forge_env.service.workspace_diff_v2(workspace_id, staged=True, include_hunks=True)

    assert result["hunks_included"] is True
    assert any(entry["hunks"] for entry in result["files"])


def test_the_default_shape_is_dramatically_smaller(forge_env: ForgeEnvironment) -> None:
    """The whole point is bytes, so measure bytes instead of trusting the flag."""
    workspace_id = _workspace_with_changes(forge_env)

    light = forge_env.service.workspace_diff_v2(workspace_id, staged=True)
    heavy = forge_env.service.workspace_diff_v2(workspace_id, staged=True, include_hunks=True)

    light_bytes = len(json.dumps(light, default=str))
    heavy_bytes = len(json.dumps(heavy, default=str))
    assert light_bytes * 4 < heavy_bytes, (
        f"expected the default to be far smaller: light={light_bytes} heavy={heavy_bytes}"
    )


def test_omitting_hunks_is_not_reported_as_truncation(forge_env: ForgeEnvironment) -> None:
    """Nothing was cut for want of budget, so `truncated` must stay false.

    Conflating "you did not ask for patch text" with "your results were cut" would teach a
    caller to go hunting for a cursor that does not exist.
    """
    workspace_id = _workspace_with_changes(forge_env)

    result = forge_env.service.workspace_diff_v2(workspace_id, staged=True)

    assert result["truncated"] is False
    assert result["next_cursor"] is None
    assert result["omitted_count"] == 0
