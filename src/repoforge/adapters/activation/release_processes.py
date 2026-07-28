"""Which live processes are executing from a release directory.

Retention decided what to delete from symlinks and recency alone, so it happily removed a
release tree while processes were still running out of it. Observed on a real installation
after an activation: three MCP workers alive from two pruned releases, each left behind
when its `tunnel-client` parent exited and no longer descended from any live supervisor. A
process executing code that no longer exists on disk cannot be restarted, cannot be
resolved back to a release by any path check, and can still write shared durable state from
a version the installation no longer has.

Supervision is reported as an ancestry chain rather than a parent id, because `ppid == 1`
proves nothing: under launchd -- which is pid 1 on macOS -- the healthy production
supervisor has ppid 1. Judging orphanhood from that reported the live runtime as abandoned.

The reaping policy is deliberately REPORT-ONLY. These processes are not ours to kill: they
may be mid-write, and an installation-wide sweep that terminates anything matching a path
pattern is a worse failure mode than the leak. So they are surfaced -- in `rf runtime ls`
and as a prune refusal that names the pid -- and the operator decides.

Identity comes from the executable path, per platform:

* Linux: `/proc/<pid>/exe`, which is the kernel's own answer.
* macOS/BSD: `ps -A -o comm=`, which reports the executable path rather than the short
  name it reports on Linux.

Neither is resolved through symlinks: `uv venv --relocatable` points `venv/bin/python` OUT
of the release tree, so resolving would land in the shared interpreter and lose exactly the
identity we are looking for.
"""

from __future__ import annotations

import os
from pathlib import Path

from ...ports.activation import ReleaseProcess
from ..subprocess.process_tree import bounded_ps

_MAX_ROWS = 4_096


def _release_of(executable: str, releases_root: Path) -> str | None:
    """Return the release directory name ``executable`` lives under, if any."""
    if not executable:
        return None
    root = str(releases_root)
    if not executable.startswith(root + os.sep):
        return None
    remainder = executable[len(root) + 1 :]
    head = remainder.split(os.sep, 1)[0]
    return head or None


def _ancestors(pid: int, parents: dict[int, int], *, depth_limit: int = 64) -> tuple[int, ...]:
    """Walk the parent chain from one snapshot, guarding against cycles and depth.

    The chain matters because supervision is transitive: the MCP worker's parent is
    `tunnel-client`, whose parent is the supervisor. A direct-parent check would report a
    perfectly supervised worker as abandoned.
    """
    chain: list[int] = []
    seen = {pid}
    current = parents.get(pid)
    while current is not None and current not in seen and len(chain) < depth_limit:
        chain.append(current)
        seen.add(current)
        current = parents.get(current)
    return tuple(chain)


def _linux_executable(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _linux_ppid(pid: int) -> int | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    name_end = value.rfind(")")
    if name_end <= 0:
        return None
    fields = value[name_end + 1 :].split()
    try:
        if fields[0] == "Z":
            return None
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def _linux_rows() -> list[tuple[int, int, str]] | None:
    try:
        entries = sorted(name for name in os.listdir("/proc") if name.isdigit())
    except OSError:
        return None
    rows: list[tuple[int, int, str]] = []
    for name in entries[:_MAX_ROWS]:
        pid = int(name)
        ppid = _linux_ppid(pid)
        if ppid is None:
            continue
        # A row is kept even when `exe` is unreadable (another user's process): it carries
        # no release identity, but it may be a LINK in the parent chain of one that does,
        # and a broken chain would report a supervised process as abandoned.
        rows.append((pid, ppid, _linux_executable(pid) or ""))
    return rows


def _ps_rows() -> list[tuple[int, int, str]] | None:
    # `-A` is required: a bare `ps` lists only this terminal's processes, which would
    # report a detached orphan as absent -- the exact process this exists to find.
    output = bounded_ps(["ps", "-A", "-o", "state=,pid=,ppid=,comm="])
    if output is None:
        return None
    rows: list[tuple[int, int, str]] = []
    for line in output.splitlines()[:_MAX_ROWS]:
        parts = line.strip().split(maxsplit=3)
        if len(parts) != 4 or parts[0].startswith("Z"):
            continue
        try:
            pid = int(parts[1])
            ppid = int(parts[2])
        except ValueError:
            continue
        if pid <= 0 or ppid < 0:
            continue
        rows.append((pid, ppid, parts[3]))
    return rows


class SystemReleaseProcessInspector:
    """List live processes whose executable lives inside the release root."""

    def __init__(self, releases_root: Path, *, self_pid: int | None = None) -> None:
        self._releases_root = releases_root
        self._self_pid = os.getpid() if self_pid is None else self_pid

    def list_processes(self) -> tuple[ReleaseProcess, ...]:
        rows = _linux_rows() if os.path.isdir("/proc") else _ps_rows()
        if rows is None:
            # An unreadable process table must not be reported as "nothing is running":
            # every caller treats an empty result as permission to delete a release.
            raise OSError("could not read the process table to check for release processes")
        parents = {pid: ppid for pid, ppid, _ in rows}
        found: list[ReleaseProcess] = []
        for pid, ppid, executable in rows:
            if pid == self._self_pid:
                continue
            commit_sha = _release_of(executable, self._releases_root)
            if commit_sha is None:
                continue
            found.append(
                ReleaseProcess(
                    pid=pid,
                    ppid=ppid,
                    commit_sha=commit_sha,
                    executable=executable,
                    release_installed=(self._releases_root / commit_sha).is_dir(),
                    ancestor_pids=_ancestors(pid, parents),
                )
            )
        return tuple(sorted(found, key=lambda process: (process.commit_sha, process.pid)))
