#!/usr/bin/env python3
"""Fail when a tracked file carries unresolved merge-conflict markers.

`CHANGELOG.md` reached `main` with `<<<<<<< HEAD` / `=======` / `>>>>>>> origin/main` in
it and passed ten green checks, because every gate lints Python or verifies contracts and
nothing reads Markdown. A conflict marker is never intentional content in this repository,
so it is cheap to make it impossible to merge.

Scope is deliberately narrow to stay false-positive free:

* Only the ``<<<<<<<`` and ``>>>>>>>`` sides are matched, at the start of a line, followed
  by a space or end of line. ``=======`` alone is legitimate -- Markdown setext headings
  and comment dividers use it -- so it is reported only inside a file that already has one
  of the other two.
* Only files Git tracks are scanned, so build output and sandboxes are out of scope.
* This file exempts itself: it has to contain the patterns it looks for.

Usage: `python scripts/check_conflict_markers.py [path ...]`; exits 1 on any finding.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_START = re.compile(r"^<{7}( |$)")
_END = re.compile(r"^>{7}( |$)")
_MIDDLE = re.compile(r"^={7}$")
_SELF = "scripts/check_conflict_markers.py"
_MAX_BYTES = 8 * 1024 * 1024


def tracked_files(paths: list[str]) -> list[str]:
    command = ["git", "ls-files", "-z", "--", *paths] if paths else ["git", "ls-files", "-z"]
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    return [name for name in completed.stdout.split("\0") if name]


def findings_for(path: Path) -> list[tuple[int, str]]:
    """Return (line number, line) for every conflict marker in one file."""
    try:
        if path.stat().st_size > _MAX_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Binary or unreadable: a conflict marker in it is not something we can claim.
        return []
    lines = text.splitlines()
    sided = [
        (number, line)
        for number, line in enumerate(lines, start=1)
        if _START.match(line) or _END.match(line)
    ]
    if not sided:
        return []
    middles = [(number, line) for number, line in enumerate(lines, start=1) if _MIDDLE.match(line)]
    return sorted(sided + middles)


def main(argv: list[str]) -> int:
    findings: list[tuple[str, int, str]] = []
    for name in tracked_files(argv):
        if name == _SELF:
            continue
        for number, line in findings_for(Path(name)):
            findings.append((name, number, line))
    if not findings:
        return 0
    print("Unresolved merge-conflict markers are committed:", file=sys.stderr)
    for name, number, line in findings:
        print(f"  {name}:{number}: {line}", file=sys.stderr)
    print(
        "\nResolve the conflict and remove the marker lines before committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
