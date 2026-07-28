"""The conflict-marker gate itself (#308).

`CHANGELOG.md` reached `main` carrying `<<<<<<< HEAD` / `=======` / `>>>>>>> origin/main`
and passed ten green checks, because every gate reads Python or a contract and none reads
Markdown. These tests pin both halves of the gate that closes it: it must catch a real
marker in any tracked text file, and it must not invent findings in content that only
resembles one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_conflict_markers.py"
_REPO_ROOT = Path(__file__).parents[1]


def _run(cwd: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *paths],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A real Git repo with tracked files: the gate scans `git ls-files`, not a walk."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=root, check=True)
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def test_the_repository_itself_has_no_conflict_markers() -> None:
    """The live assertion: this is what would have failed the merge that shipped them."""
    completed = _run(_REPO_ROOT)
    assert completed.returncode == 0, completed.stderr


def test_a_committed_conflict_marker_fails_the_gate(tmp_path: Path) -> None:
    conflicted = "\n".join(
        [
            "# Changelog",
            "",
            "<<<<<<< HEAD",
            "- entry from this branch",
            "=======",
            "- entry from origin/main",
            ">>>>>>> origin/main",
            "",
        ]
    )
    root = _repo(tmp_path, {"CHANGELOG.md": conflicted})

    completed = _run(root)

    assert completed.returncode == 1
    assert "CHANGELOG.md:3" in completed.stderr
    # The middle marker is reported too, so the operator sees the whole block.
    assert "CHANGELOG.md:5" in completed.stderr
    assert "CHANGELOG.md:7" in completed.stderr


def test_markers_are_caught_in_any_tracked_text_file_not_only_python(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "docs/guide.md": "intro\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
            "config.toml": "[server]\n",
        },
    )

    completed = _run(root)

    assert completed.returncode == 1
    assert "docs/guide.md" in completed.stderr


def test_content_that_merely_resembles_a_marker_is_not_a_finding(tmp_path: Path) -> None:
    """A `=======` line is legitimate on its own -- Markdown setext headings and comment
    dividers use it -- so it is only a finding beside a real conflict side."""
    root = _repo(
        tmp_path,
        {
            "README.md": "Title\n=======\n\nbody\n",
            "Makefile": "# =============================================\nhelp:\n",
            "notes.md": "a << b and c >> d, plus <<<<<<<inline with no space\n",
            "shift.py": "value = 1 <<<<<<< 2\n",
        },
    )

    completed = _run(root)

    assert completed.returncode == 0, completed.stderr


def test_untracked_files_are_out_of_scope(tmp_path: Path) -> None:
    """Build output and sandboxes must not be able to fail the gate."""
    root = _repo(tmp_path, {"kept.md": "clean\n"})
    (root / "dist").mkdir()
    (root / "dist" / "generated.md").write_text("<<<<<<< HEAD\nx\n", encoding="utf-8")

    completed = _run(root)

    assert completed.returncode == 0, completed.stderr


def test_a_binary_file_is_never_reported(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"kept.md": "clean\n"})
    (root / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n<<<<<<< HEAD\x00\xff")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)

    completed = _run(root)

    assert completed.returncode == 0, completed.stderr
