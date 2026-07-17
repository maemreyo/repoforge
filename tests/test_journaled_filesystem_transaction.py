from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path


def _transaction():
    from repoforge.adapters.filesystem import transaction
    from repoforge.domain import filesystem_transaction

    return transaction, filesystem_transaction


def test_commit_supports_write_create_delete_and_move(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    existing = root / "existing.txt"
    existing.write_text("old\n", encoding="utf-8")
    existing.chmod(0o755)
    (root / "delete.txt").write_text("delete\n", encoding="utf-8")
    (root / "move.txt").write_text("move\n", encoding="utf-8")

    engine = transaction.JournaledFileTransaction(root)
    receipt = engine.commit(
        domain.TransactionPlan(
            actions=(
                domain.WriteFile("existing.txt", b"new\n"),
                domain.CreateFile("created.txt", b"created\n"),
                domain.DeleteFile("delete.txt"),
                domain.MoveFile("move.txt", "moved.txt"),
            )
        )
    )

    assert existing.read_bytes() == b"new\n"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert (root / "created.txt").read_bytes() == b"created\n"
    assert not (root / "delete.txt").exists()
    assert not (root / "move.txt").exists()
    assert (root / "moved.txt").read_bytes() == b"move\n"
    assert receipt.changed_paths == (
        "created.txt",
        "delete.txt",
        "existing.txt",
        "move.txt",
        "moved.txt",
    )
    assert engine.pending_transactions() == ()


def test_ordered_plan_supports_create_move_write_and_delete_recreate(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "replace.txt").write_text("original\n", encoding="utf-8")

    engine = transaction.JournaledFileTransaction(root)
    engine.commit(
        domain.TransactionPlan(
            actions=(
                domain.CreateFile("temporary.txt", b"first\n"),
                domain.MoveFile("temporary.txt", "final.txt"),
                domain.WriteFile("final.txt", b"second\n"),
                domain.DeleteFile("replace.txt"),
                domain.CreateFile("replace.txt", b"replacement\n"),
            )
        )
    )

    assert (root / "final.txt").read_bytes() == b"second\n"
    assert (root / "replace.txt").read_bytes() == b"replacement\n"


def test_staging_is_on_same_filesystem_and_fsync_precedes_apply(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("old\n", encoding="utf-8")
    checkpoints: list[str] = []

    engine = transaction.JournaledFileTransaction(root, fault_injector=checkpoints.append)
    engine.commit(domain.TransactionPlan(actions=(domain.WriteFile("file.txt", b"new\n"),)))

    assert checkpoints.index("after_stage_fsync:0") < checkpoints.index("before_apply:0")
    assert checkpoints.index("after_apply:0") < checkpoints.index("before_commit_marker")
    assert engine.last_stage_device == root.stat().st_dev


def test_validation_failure_creates_no_journal_and_changes_nothing(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("old\n", encoding="utf-8")
    engine = transaction.JournaledFileTransaction(root)

    try:
        engine.commit(domain.TransactionPlan(actions=(domain.CreateFile("file.txt", b"new\n"),)))
    except domain.TransactionValidationError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("create over an existing path must fail")

    assert (root / "file.txt").read_text(encoding="utf-8") == "old\n"
    assert engine.pending_transactions() == ()


def test_io_failure_rolls_back_all_applied_actions(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "one.txt").write_text("one\n", encoding="utf-8")
    (root / "two.txt").write_text("two\n", encoding="utf-8")

    def fail(point: str) -> None:
        if point == "after_apply:0":
            raise OSError("injected I/O failure")

    engine = transaction.JournaledFileTransaction(root, fault_injector=fail)
    try:
        engine.commit(
            domain.TransactionPlan(
                actions=(
                    domain.WriteFile("one.txt", b"ONE\n"),
                    domain.WriteFile("two.txt", b"TWO\n"),
                )
            )
        )
    except OSError as exc:
        assert "injected" in str(exc)
    else:
        raise AssertionError("injected failure must escape")

    assert (root / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert (root / "two.txt").read_text(encoding="utf-8") == "two\n"
    assert engine.pending_transactions() == ()


def test_simulated_crash_between_renames_is_recovered_on_restart(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "one.txt").write_text("one\n", encoding="utf-8")
    (root / "two.txt").write_text("two\n", encoding="utf-8")

    def crash(point: str) -> None:
        if point == "after_backup:1":
            raise domain.SimulatedTransactionCrash(point)

    engine = transaction.JournaledFileTransaction(root, fault_injector=crash)
    try:
        engine.commit(
            domain.TransactionPlan(
                actions=(
                    domain.WriteFile("one.txt", b"ONE\n"),
                    domain.WriteFile("two.txt", b"TWO\n"),
                )
            )
        )
    except domain.SimulatedTransactionCrash:
        pass
    else:
        raise AssertionError("simulated crash must interrupt without in-process rollback")

    assert engine.pending_transactions()
    recovered = transaction.JournaledFileTransaction(root).recover_pending()

    assert recovered.rolled_back == 1
    assert recovered.finalized == 0
    assert (root / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert (root / "two.txt").read_text(encoding="utf-8") == "two\n"
    assert engine.pending_transactions() == ()


def test_crash_after_commit_marker_keeps_committed_state_and_purges_journal(
    tmp_path: Path,
) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("old\n", encoding="utf-8")

    def crash(point: str) -> None:
        if point == "after_commit_marker":
            raise domain.SimulatedTransactionCrash(point)

    engine = transaction.JournaledFileTransaction(root, fault_injector=crash)
    try:
        engine.commit(domain.TransactionPlan(actions=(domain.WriteFile("file.txt", b"new\n"),)))
    except domain.SimulatedTransactionCrash:
        pass
    else:
        raise AssertionError("simulated crash must interrupt finalization")

    recovered = transaction.JournaledFileTransaction(root).recover_pending()
    assert recovered.finalized == 1
    assert recovered.rolled_back == 0
    assert (root / "file.txt").read_text(encoding="utf-8") == "new\n"
    assert engine.pending_transactions() == ()


def test_rollback_failure_is_not_suppressed_and_journal_remains(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("old\n", encoding="utf-8")

    def fail(point: str) -> None:
        if point == "after_apply:0":
            raise OSError("primary failure")
        if point == "before_rollback:0":
            raise OSError("rollback failure")

    engine = transaction.JournaledFileTransaction(root, fault_injector=fail)
    try:
        engine.commit(domain.TransactionPlan(actions=(domain.WriteFile("file.txt", b"new\n"),)))
    except domain.TransactionRecoveryError as exc:
        assert "primary failure" in str(exc)
        assert "rollback failure" in str(exc)
    else:
        raise AssertionError("rollback failure must be surfaced")

    assert engine.pending_transactions()
    recovered = transaction.JournaledFileTransaction(root).recover_pending()
    assert recovered.rolled_back == 1
    assert (root / "file.txt").read_text(encoding="utf-8") == "old\n"


def test_real_process_kill_mid_transaction_recovers_original_tree(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "one.txt").write_text("one\n", encoding="utf-8")
    (root / "two.txt").write_text("two\n", encoding="utf-8")
    marker = tmp_path / "killed.marker"
    script = f"""
import os
from pathlib import Path
from repoforge.adapters.filesystem.transaction import JournaledFileTransaction
from repoforge.domain.filesystem_transaction import TransactionPlan, WriteFile
root = Path({str(root)!r})
marker = Path({str(marker)!r})
def kill(point):
    if point == 'after_apply:0':
        marker.write_text('ready')
        os.kill(os.getpid(), 9)
JournaledFileTransaction(root, fault_injector=kill).commit(
    TransactionPlan(actions=(WriteFile('one.txt', b'ONE\\n'), WriteFile('two.txt', b'TWO\\n')))
)
"""
    completed = subprocess.run([sys.executable, "-c", script], check=False)

    assert completed.returncode != 0
    assert marker.is_file()
    transaction, _ = _transaction()
    result = transaction.JournaledFileTransaction(root).recover_pending()
    assert result.rolled_back == 1
    assert (root / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert (root / "two.txt").read_text(encoding="utf-8") == "two\n"


def test_paths_cannot_escape_workspace_root(tmp_path: Path) -> None:
    transaction, domain = _transaction()
    root = tmp_path / "workspace"
    root.mkdir()
    engine = transaction.JournaledFileTransaction(root)

    for action in (
        domain.CreateFile("../outside.txt", b"bad"),
        domain.MoveFile("missing.txt", "../outside.txt"),
    ):
        try:
            engine.commit(domain.TransactionPlan(actions=(action,)))
        except domain.TransactionValidationError:
            pass
        else:
            raise AssertionError("transaction paths must stay under the workspace root")

    assert not (tmp_path / "outside.txt").exists()
