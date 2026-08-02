"""Retention must not delete a release a live process is executing from (#306).

Prune decided what to remove from `current`, `previous`, and recency alone -- none of which
says anything about what is *running*. On a real installation it removed two release trees
while three MCP workers were still executing from them, each orphaned to PID 1 after its
`tunnel-client` parent exited. A process running code that no longer exists on disk cannot
be restarted, cannot be resolved back to a release by any path check, and can still write
shared durable state from a version the installation no longer has.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repoforge.adapters.activation.release_processes import SystemReleaseProcessInspector
from repoforge.adapters.activation.release_store import RuntimeReleaseStore
from repoforge.application.activation.upgrade import UpgradeService
from repoforge.domain.activation import ReleaseManifest
from repoforge.ports.activation import (
    BuildArtifact,
    ObservedRuntime,
    ReleaseProcess,
    RestartOutcome,
    SmokeResult,
    WorktreeState,
)

_FINGERPRINT = "a" * 64
_SURFACE = "b" * 64


class _Inspector:
    def __init__(self, head: str) -> None:
        self._head = head

    def inspect(self, worktree: Path) -> WorktreeState:
        return WorktreeState(head_sha=self._head, clean=True)


class _Builder:
    def build(self, worktree: Path, *, commit_sha: str) -> BuildArtifact:
        del commit_sha
        return BuildArtifact(
            wheel_path=worktree / "dist" / "wheel.whl",
            build_fingerprint=_FINGERPRINT,
            package_version="2.2.0",
            source_digest="c" * 64,
        )


class _Installer:
    def install(self, wheel: Path, destination: Path) -> None:
        (destination / "venv" / "bin").mkdir(parents=True, exist_ok=True)


class _Smoke:
    def smoke(self, release_path: Path) -> SmokeResult:
        return SmokeResult(ok=True, tool_surface_hash=_SURFACE, detail="fake")


class _Restarter:
    def preflight_reclaim(self, departing_release: str | None = None):
        del departing_release
        return True, "", None

    def restart(self, *, departing_release: str | None = None) -> RestartOutcome:
        return RestartOutcome(ok=True, detail="fake restart", pid=99)


class _Observer:
    def __init__(self, store: RuntimeReleaseStore) -> None:
        self._store = store

    def observe(self) -> ObservedRuntime:
        return ObservedRuntime(
            running_release_sha=self._store.current_sha(), phase="healthy", pid=99
        )


class _Clock:
    """Advances per call: releases must have distinct `built_at` values or retention's
    "newest keep" is a tie whose winner is arbitrary."""

    def __init__(self) -> None:
        self._minute = 0

    def now_iso(self) -> str:
        self._minute += 1
        return f"2026-07-28T10:{self._minute:02d}:00+00:00"


class _Processes:
    """Report a fixed set of release processes, or fail like an unreadable table."""

    def __init__(self, processes: tuple[ReleaseProcess, ...] = (), *, unreadable: bool = False):
        self._processes = processes
        self._unreadable = unreadable

    def list_processes(self) -> tuple[ReleaseProcess, ...]:
        if self._unreadable:
            raise OSError("process table unavailable")
        return self._processes


def _install(store: RuntimeReleaseStore, sha: str, *, built_at: str) -> None:
    store.release_path(sha).mkdir(parents=True, exist_ok=True)
    store.write_manifest(
        ReleaseManifest(
            commit_sha=sha,
            package_version="2.2.0",
            build_fingerprint=_FINGERPRINT,
            tool_surface_hash=_SURFACE,
            source_worktree="/src",
            built_at=built_at,
        )
    )


def _held(sha: str, store: RuntimeReleaseStore, *, ppid: int = 1) -> ReleaseProcess:
    return ReleaseProcess(
        pid=4242,
        ppid=ppid,
        commit_sha=sha,
        executable=str(store.release_path(sha) / "venv" / "bin" / "python"),
        release_installed=True,
        ancestor_pids=(ppid,),
    )


def _service(
    store: RuntimeReleaseStore,
    *,
    head: str,
    processes: _Processes | None,
    clock: _Clock | None = None,
) -> UpgradeService:
    # One clock must be SHARED across the activations of a test: a per-service clock would
    # stamp every release with the same `built_at`, making retention's "newest keep" a tie.
    return UpgradeService(
        store=store,
        inspector=_Inspector(head),
        builder=_Builder(),
        installer=_Installer(),
        smoke=_Smoke(),
        restarter=_Restarter(),
        observer=_Observer(store),
        clock=clock or _Clock(),
        converge_attempts=1,
        converge_interval_seconds=0,
        release_processes=processes,
    )


# ------------------------------------------------------------------ retention policy


def test_retention_candidates_report_without_deleting(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    for index, sha in enumerate(("1111aaa", "2222bbb", "3333ccc")):
        _install(store, sha, built_at=f"2026-07-2{index + 1}T00:00:00+00:00")
    store.swap_current("2222bbb")
    store.swap_current("3333ccc")  # 2222bbb becomes `previous`

    candidates = store.retention_candidates(keep=1)

    assert candidates == ["1111aaa"]  # 3333ccc is current, 2222bbb is previous
    assert store.release_path("1111aaa").is_dir(), "a query must not delete anything"


def test_prune_keeps_a_release_a_live_process_is_executing_from(tmp_path: Path) -> None:
    store = RuntimeReleaseStore(tmp_path / "release-root")
    for index, sha in enumerate(("1111aaa", "2222bbb", "3333ccc")):
        _install(store, sha, built_at=f"2026-07-2{index + 1}T00:00:00+00:00")
    store.swap_current("2222bbb")
    store.swap_current("3333ccc")
    assert store.retention_candidates(keep=1) == ["1111aaa"]

    removed = store.prune(keep=1, protect=frozenset({"1111aaa"}))

    assert removed == []
    assert store.release_path("1111aaa").is_dir()


def test_an_activation_does_not_prune_a_release_still_being_executed(tmp_path: Path) -> None:
    """The end-to-end path: activating a new release must not evict live code."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    clock = _Clock()
    for head in ("1111aaa", "2222bbb", "3333ccc"):
        _service(store, head=head, processes=_Processes(), clock=clock).upgrade(
            tmp_path, activate=True
        )
    # 1111aaa is now the oldest release and neither `current` nor `previous`, so retention
    # at keep=1 wants it gone.
    assert store.retention_candidates(keep=1) == ["1111aaa"]

    # ... but a worker is still executing from it.
    result = _service(
        store,
        head="4444ddd",
        processes=_Processes((_held("1111aaa", store),)),
        clock=clock,
    ).upgrade(tmp_path, activate=True, keep_releases=1)

    assert result.status == "activated"
    assert "1111aaa" not in result.pruned
    assert result.retained_for_live_process == ("1111aaa",)
    assert store.release_path("1111aaa").is_dir()
    assert result.as_dict()["retained_for_live_process"] == ["1111aaa"]


def test_a_release_nothing_is_running_is_still_pruned(tmp_path: Path) -> None:
    """The guard must not turn retention off: an unused release is still removed."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    clock = _Clock()
    for head in ("1111aaa", "2222bbb", "3333ccc"):
        _service(store, head=head, processes=_Processes(), clock=clock).upgrade(
            tmp_path, activate=True
        )
    assert store.retention_candidates(keep=1) == ["1111aaa"]

    result = _service(
        store,
        head="4444ddd",
        # Only the release the live runtime serves is held, and that is protected anyway.
        processes=_Processes((_held("3333ccc", store, ppid=500),)),
        clock=clock,
    ).upgrade(tmp_path, activate=True, keep_releases=1)

    assert "1111aaa" in result.pruned
    assert result.retained_for_live_process == ()
    assert not store.release_path("1111aaa").exists()


def test_an_unreadable_process_table_prunes_nothing(tmp_path: Path) -> None:
    """Fail closed: "no processes found" and "cannot tell" must not be the same answer."""
    store = RuntimeReleaseStore(tmp_path / "release-root")
    clock = _Clock()
    for head in ("1111aaa", "2222bbb", "3333ccc"):
        _service(store, head=head, processes=_Processes(), clock=clock).upgrade(
            tmp_path, activate=True
        )
    assert store.retention_candidates(keep=1) == ["1111aaa"]

    result = _service(
        store, head="4444ddd", processes=_Processes(unreadable=True), clock=clock
    ).upgrade(tmp_path, activate=True, keep_releases=1)

    # The activation still succeeded -- retention is housekeeping, not part of the release.
    assert result.status == "activated"
    assert result.pruned == ()
    assert store.release_path("1111aaa").is_dir()


# ------------------------------------------------------- the real process inspector


def test_the_inspector_finds_a_real_process_running_from_a_release() -> None:
    """A REAL child process, the real process table, and real ancestry.

    The release root is the interpreter's OWN directory, so the "release" is the
    interpreter's filename. Deliberately not a realistic layout: what is under test here is
    prefix matching and first-segment extraction against a live process table, and the
    layout-shaped cases are covered deterministically by `_release_of` below. Anchoring on
    the real interpreter is what makes this portable -- three earlier attempts each failed
    for a platform reason unrelated to the code: a copied system binary is SIGKILLed on
    macOS (broken code signature), a relocated interpreter cannot find its stdlib on Linux,
    and assuming the interpreter sits three directories deep resolved the root to `/` on CI.
    """
    executable = Path(sys.executable).resolve()
    releases = executable.parent
    expected_release = executable.name

    child = subprocess.Popen(
        [str(executable), "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        inspector = SystemReleaseProcessInspector(releases)
        deadline = time.monotonic() + 15.0
        mine: ReleaseProcess | None = None
        while time.monotonic() < deadline and mine is None:
            mine = next(
                (process for process in inspector.list_processes() if process.pid == child.pid),
                None,
            )
            if mine is None:
                time.sleep(0.1)

        if mine is None:
            # Some interpreters re-exec through a different binary -- a macOS framework
            # Python reports the framework path for a venv copy -- so the premise of this
            # test does not hold there. Say so instead of failing on a platform quirk; the
            # parsing and policy assertions elsewhere do not depend on it.
            pytest.skip(
                f"this platform does not report {executable} as the executable of its own "
                f"child (pid {child.pid}, exit={child.poll()})"
            )
        assert mine.commit_sha == expected_release
        assert mine.executable.startswith(str(releases))
        # False by construction here: the "release" is the interpreter FILE, and
        # `release_installed` asks whether a release DIRECTORY exists. Its real semantics
        # are asserted in the deterministic tests above.
        assert mine.release_installed is False
        # This test process spawned it, so it IS supervised by us -- and the chain reaches
        # further up, which is what makes transitive supervision checkable.
        assert mine.supervised_by(os.getpid()) is True
        assert mine.ancestor_pids[0] == os.getpid()
        assert mine.supervised_by(999_999) is False
    finally:
        child.terminate()
        child.wait(timeout=15)


def test_the_inspector_never_reports_itself() -> None:
    """The scanning process is excluded, or every prune would protect its own release."""
    executable = Path(sys.executable).resolve()
    found = SystemReleaseProcessInspector(executable.parent).list_processes()

    assert os.getpid() not in [process.pid for process in found]


def test_the_inspector_reports_a_process_whose_release_was_removed(tmp_path: Path) -> None:
    """This is the state observed in production: running code that is gone from disk."""
    releases = tmp_path / "releases"
    inspector = SystemReleaseProcessInspector(releases)
    releases.mkdir(parents=True)

    process = ReleaseProcess(
        pid=4242,
        ppid=1,
        commit_sha="1111aaa",
        executable=str(releases / "1111aaa" / "venv" / "bin" / "python"),
        release_installed=(releases / "1111aaa").is_dir(),
        ancestor_pids=(1,),
    )

    assert process.release_installed is False
    assert process.supervised_by(7777) is False
    assert process.as_dict()["release_installed"] is False
    # And a real scan of an empty release root simply finds nothing.
    assert inspector.list_processes() == ()


def test_processes_outside_the_release_root_are_ignored(tmp_path: Path) -> None:
    """This test's own interpreter is not under the release root, so it must not appear."""
    releases = tmp_path / "releases"
    releases.mkdir(parents=True)

    assert SystemReleaseProcessInspector(releases).list_processes() == ()


def test_a_sibling_directory_is_not_mistaken_for_a_release(tmp_path: Path) -> None:
    """`releases-old/...` must not be read as a release inside `releases/`."""
    from repoforge.adapters.activation.release_processes import _release_of

    releases = tmp_path / "releases"
    assert _release_of(str(releases / "1111aaa" / "venv" / "bin" / "python"), releases) == "1111aaa"
    assert _release_of(str(tmp_path / "releases-old" / "1111aaa" / "python"), releases) is None
    assert _release_of(str(tmp_path / "other" / "python"), releases) is None
    # The root itself is not a release.
    assert _release_of(str(releases), releases) is None


# ------------------------------------------------------------ the read surface


def test_runtime_inventory_reports_orphans_and_names_the_operator_action() -> None:
    from repoforge.application.activation.inventory import build_runtime_inventory

    orphan = ReleaseProcess(
        pid=27530,
        ppid=1,
        commit_sha="34e9b07",
        executable="/releases/34e9b07/venv/bin/python",
        release_installed=False,
        ancestor_pids=(1,),
    )
    # The live supervisor itself: ppid 1 under launchd, and NOT an orphan.
    supervisor = ReleaseProcess(
        pid=79552,
        ppid=1,
        commit_sha="8eca20b",
        executable="/releases/8eca20b/venv/bin/python",
        release_installed=True,
        ancestor_pids=(1,),
    )
    # Its MCP worker, two hops away through `tunnel-client`.
    supervised = ReleaseProcess(
        pid=79615,
        ppid=79600,
        commit_sha="8eca20b",
        executable="/releases/8eca20b/venv/bin/python",
        release_installed=True,
        ancestor_pids=(79600, 79552, 1),
    )

    payload = build_runtime_inventory(
        releases=[],
        observed=ObservedRuntime(running_release_sha="8eca20b", phase="healthy", pid=79552),
        agent=None,
        agent_secret_usable=True,
        dev_runtimes=[],
        release_processes=(orphan, supervisor, supervised),
    )

    assert payload["counts"]["orphan_processes"] == 1
    assert payload["counts"]["orphan_processes_on_removed_releases"] == 1
    reported = payload["orphan_processes"]
    assert isinstance(reported, list)
    assert [entry["pid"] for entry in reported] == [27530]
    # Neither the live supervisor nor its descendant may be reported as abandoned.
    assert 79552 not in [entry["pid"] for entry in reported]
    assert 79615 not in [entry["pid"] for entry in reported]


def test_orphan_reporting_does_not_claim_repoforge_will_kill_them() -> None:
    """The reaping policy is report-only: these processes are not RepoForge's to kill."""
    from repoforge.application.activation.inventory import build_runtime_inventory
    from repoforge.application.activation.selection import ReleaseChoice

    payload = build_runtime_inventory(
        releases=[
            ReleaseChoice(
                commit_sha="8eca20b",
                branch="main",
                subject="s",
                built_at="2026-07-28T10:00:00+00:00",
                is_current=True,
                is_previous=False,
            )
        ],
        observed=ObservedRuntime(running_release_sha="8eca20b", phase="healthy", pid=79552),
        agent=None,
        agent_secret_usable=True,
        dev_runtimes=[],
        release_processes=(
            ReleaseProcess(
                pid=2127,
                ppid=1,
                commit_sha="402b11b",
                executable="/releases/402b11b/venv/bin/python",
                release_installed=False,
                ancestor_pids=(1,),
            ),
        ),
    )

    action = str(payload["safe_next_action"])
    assert "2127" in action
    assert "no longer installed" in action
    assert "never terminates a process it does not own" in action


@pytest.mark.parametrize("supervisor_pid", [None, 7777, 1])
def test_supervision_is_ancestry_never_ppid_one(supervisor_pid: int | None) -> None:
    """Caught by running the read surface against a real machine: under launchd -- pid 1 on
    macOS -- the healthy production supervisor has ppid 1, so a `ppid == 1` rule reported
    the LIVE runtime as abandoned and advised killing it."""
    supervisor = ReleaseProcess(
        pid=1000,
        ppid=1,  # launchd started it: this is a perfectly healthy supervisor
        commit_sha="1111aaa",
        executable="/releases/1111aaa/venv/bin/python",
        release_installed=True,
        ancestor_pids=(1,),
    )
    # Two hops from the supervisor (worker -> tunnel-client -> supervisor).
    worker = ReleaseProcess(
        pid=1002,
        ppid=1001,
        commit_sha="1111aaa",
        executable="/releases/1111aaa/venv/bin/python",
        release_installed=True,
        ancestor_pids=(1001, 1000, 1),
    )

    assert supervisor.supervised_by(1000) is True
    assert worker.supervised_by(1000) is True
    # And nothing is "supervised" by an unrelated or unknown pid.
    assert supervisor.supervised_by(supervisor_pid) is (supervisor_pid == 1)
    assert worker.supervised_by(supervisor_pid) is (supervisor_pid == 1)


def test_a_stopped_runtime_makes_every_release_process_unsupervised() -> None:
    """With no live supervisor there is nothing for a process to descend FROM, so a
    survivor of a stopped runtime is exactly what the operator needs told."""
    from repoforge.application.activation.inventory import build_runtime_inventory

    payload = build_runtime_inventory(
        releases=[],
        observed=ObservedRuntime(running_release_sha=None, phase="stopped", pid=None),
        agent=None,
        agent_secret_usable=True,
        dev_runtimes=[],
        release_processes=(
            ReleaseProcess(
                pid=5150,
                ppid=1,
                commit_sha="1111aaa",
                executable="/releases/1111aaa/venv/bin/python",
                release_installed=True,
                ancestor_pids=(1,),
            ),
        ),
    )

    assert payload["counts"]["orphan_processes"] == 1
