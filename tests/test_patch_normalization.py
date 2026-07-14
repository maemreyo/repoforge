from __future__ import annotations

import pytest

from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.patches import inspect_patch, normalize_patch


def _reader(files: dict[str, str]):
    return lambda path: files.get(path)


def test_openai_envelope_add_update_delete_and_move_becomes_unified_diff() -> None:
    files = {
        "edit.txt": "alpha\nbeta\ngamma\n",
        "delete.txt": "remove me\n",
        "old.txt": "move me\n",
    }
    patch = """*** Begin Patch
*** Add File: added.txt
+new file
*** Update File: edit.txt
@@
 alpha
-beta
+BETA
 gamma
*** Delete File: delete.txt
*** Update File: old.txt
*** Move to: moved.txt
@@
 move me
*** End Patch
"""

    inspection = inspect_patch(patch)
    assert inspection.input_format == "openai_apply_patch"
    assert inspection.paths == ("added.txt", "delete.txt", "edit.txt", "moved.txt", "old.txt")

    result = normalize_patch(patch, _reader(files))
    assert result.input_format == "openai_apply_patch"
    assert result.input_sha256 != result.normalized_sha256
    assert result.paths == inspection.paths
    assert "diff --git a/edit.txt b/edit.txt" in result.patch
    assert "-beta" in result.patch and "+BETA" in result.patch
    assert "--- /dev/null" in result.patch
    assert "+++ /dev/null" in result.patch
    assert "moved.txt" in result.patch


def test_unified_diff_recounts_and_relocates_unique_hunk() -> None:
    files = {"demo.txt": "zero\none\ntwo\nthree\n"}
    patch = """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -99,9 +99,12 @@
 one
-two
+TWO
 three
"""
    result = normalize_patch(patch, _reader(files))
    assert result.input_format == "unified_diff"
    assert "recounted_hunks" in result.repair_actions
    assert "relocated_hunks" in result.repair_actions
    assert "@@ -2,3 +2,3 @@" in result.patch


def test_unified_diff_uses_unique_whitespace_normalized_context() -> None:
    files = {"demo.txt": "before\nvalue    one\nafter\n"}
    patch = """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1,3 +1,3 @@
 before
-value one
+value two
 after
"""
    result = normalize_patch(patch, _reader(files))
    assert "whitespace_normalized_context" in result.repair_actions
    assert "-value    one" in result.patch
    assert "+value two" in result.patch


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (
            """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1 +1 @@
-missing
+changed
""",
            ErrorCode.PATCH_CONTEXT_NOT_FOUND,
        ),
        (
            """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1 +1 @@
-same
+changed
""",
            ErrorCode.PATCH_CONTEXT_AMBIGUOUS,
        ),
    ],
)
def test_missing_or_ambiguous_context_fails_closed(text: str, code: ErrorCode) -> None:
    files = {"demo.txt": "same\nother\nsame\n"}
    with pytest.raises(RepoForgeError) as failure:
        normalize_patch(text, _reader(files))
    assert failure.value.code is code
    assert failure.value.details["target_path"] == "demo.txt"
    assert failure.value.details["hunk_ordinal"] == 1


def test_unknown_format_returns_actionable_structured_error() -> None:
    with pytest.raises(RepoForgeError) as failure:
        inspect_patch("replace foo with bar")
    assert failure.value.code is ErrorCode.PATCH_FORMAT_UNSUPPORTED
    assert "workspace_write_file" in failure.value.safe_next_action
    assert failure.value.details["accepted_formats"] == [
        "unified_diff",
        "openai_apply_patch",
    ]


def test_normalization_is_deterministic() -> None:
    files = {"demo.txt": "one\ntwo\n"}
    patch = """*** Begin Patch
*** Update File: demo.txt
@@
-one
+ONE
 two
*** End Patch
"""
    first = normalize_patch(patch, _reader(files))
    second = normalize_patch(patch, _reader(files))
    assert first == second
