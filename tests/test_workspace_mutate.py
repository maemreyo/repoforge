from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import ForgeEnvironment, create_forge_environment

import repoforge.application.workspace.mutate_enhanced as mutate_enhanced_module
from repoforge.adapters.filesystem.receipt_transaction import JournaledFileTransaction
from repoforge.application.workspace.mutate import (
    ApplyPatchMutation,
    CreateMutation,
    DeleteMutation,
    MoveMutation,
    ReplaceTextMutation,
    RestoreMutation,
    TextReplacement,
    WriteMutation,
)
from repoforge.domain.errors import ConfigError, ErrorCode, SecurityError, WorkspaceError
from repoforge.domain.filesystem_transaction import SimulatedTransactionCrash


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mixed_mutations_commit_as_one_journaled_transaction(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 mixed mutate")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    agents = service.workspace_read_file(workspace_id, "AGENTS.md")
    before = service.workspace_status(workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [
            ReplaceTextMutation(
                path="hello.txt",
                expected_sha256=hello["sha256"],
                edits=(TextReplacement("hello", "changed hello"),),
            ),
            CreateMutation(path="temporary.txt", content="temporary\n"),
            MoveMutation(
                source="temporary.txt",
                destination="moved.txt",
                expected_source_sha256=hashlib.sha256(b"temporary\n").hexdigest(),
            ),
            DeleteMutation(path="AGENTS.md", expected_sha256=agents["sha256"]),
        ],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
    )

    assert result["changed"] is True
    assert result["dry_run"] is False
    assert result["operation_count"] == 4
    assert result["workspace_fingerprint"] != before["workspace_fingerprint"]
    assert result["head_sha"] == before["head_sha"]
    assert (root / "hello.txt").read_text(encoding="utf-8") == "changed hello\n"
    assert not (root / "temporary.txt").exists()
    assert (root / "moved.txt").read_text(encoding="utf-8") == "temporary\n"
    assert not (root / "AGENTS.md").exists()
    assert [item["status"] for item in result["operations"]] == [
        "ready",
        "ready",
        "ready",
        "ready",
    ]


def test_no_op_write_returns_changed_false_and_preserves_fingerprint(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 no op")["workspace_id"]
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    status = service.workspace_status(workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [
            WriteMutation(
                path="hello.txt",
                content="hello\n",
                expected_sha256=hello["sha256"],
            )
        ],
        expected_workspace_fingerprint=status["workspace_fingerprint"],
    )

    assert result["changed"] is False
    assert result["workspace_fingerprint"] == status["workspace_fingerprint"]
    assert result["operations"][0]["status"] == "no_op"


def test_dry_run_reports_candidates_and_failure_without_mutating(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 dry run")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    before = service.workspace_status(workspace_id)

    result = service.workspace_mutate(
        workspace_id,
        [
            ReplaceTextMutation(
                path="hello.txt",
                expected_sha256=hello["sha256"],
                edits=(TextReplacement("missing", "replacement"),),
            ),
            CreateMutation(path="would-create.txt", content="candidate\n"),
        ],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["changed"] is False
    assert result["would_change"] is True
    assert result["ready"] is False
    assert result["operations"][0]["status"] == "failed"
    assert "expected 1 occurrences, found 0" in result["operations"][0]["failure_reason"]
    assert result["operations"][1]["status"] == "ready"
    assert result["operations"][1]["after_sha256"] == hashlib.sha256(b"candidate\n").hexdigest()
    assert (root / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (root / "would-create.txt").exists()
    assert (
        service.workspace_status(workspace_id)["workspace_fingerprint"]
        == before["workspace_fingerprint"]
    )


def test_stale_global_fingerprint_and_file_sha_leave_tree_untouched(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 stale mutate")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    hello = service.workspace_read_file(workspace_id, "hello.txt")
    stale = service.workspace_status(workspace_id)["workspace_fingerprint"]
    service.workspace_write_file(
        workspace_id,
        "hello.txt",
        "changed outside mutate\n",
        hello["sha256"],
    )

    with pytest.raises(WorkspaceError, match="Workspace changed since"):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="new.txt", content="new\n")],
            expected_workspace_fingerprint=stale,
        )
    assert not (root / "new.txt").exists()

    current = service.workspace_status(workspace_id)
    with pytest.raises(WorkspaceError, match="expected_sha256"):
        service.workspace_mutate(
            workspace_id,
            [
                WriteMutation(
                    path="hello.txt",
                    content="another\n",
                    expected_sha256="0" * 64,
                ),
                CreateMutation(path="also-new.txt", content="new\n"),
            ],
            expected_workspace_fingerprint=current["workspace_fingerprint"],
        )
    assert (root / "hello.txt").read_text(encoding="utf-8") == "changed outside mutate\n"
    assert not (root / "also-new.txt").exists()


def test_patch_and_restore_are_planned_into_the_same_transaction_model(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 patch restore")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    status = service.workspace_status(workspace_id)
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,3 +1,4 @@
 # Demo
 
 Repository instructions.
+Journaled patch.
"""

    patched = service.workspace_mutate(
        workspace_id,
        [ApplyPatchMutation(patch=patch)],
        expected_workspace_fingerprint=status["workspace_fingerprint"],
    )
    assert patched["changed"] is True
    assert "Journaled patch." in (root / "README.md").read_text(encoding="utf-8")

    restored = service.workspace_mutate(
        workspace_id,
        [RestoreMutation(paths=("README.md",))],
        expected_workspace_fingerprint=patched["workspace_fingerprint"],
    )
    assert restored["changed"] is True
    assert (root / "README.md").read_text(encoding="utf-8") == (
        "# Demo\n\nRepository instructions.\n"
    )


def test_change_budget_failure_rolls_back_the_entire_transaction(tmp_path: Path) -> None:
    env = create_forge_environment(tmp_path, max_changed_files=1)
    service = env.service
    workspace_id = service.workspace_create("demo", "v2 budget rollback")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    status = service.workspace_status(workspace_id)

    with pytest.raises(WorkspaceError, match="Change budget exceeded"):
        service.workspace_mutate(
            workspace_id,
            [
                CreateMutation(path="one.txt", content="one\n"),
                CreateMutation(path="two.txt", content="two\n"),
            ],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
        )

    assert not (root / "one.txt").exists()
    assert not (root / "two.txt").exists()
    assert (
        service.workspace_status(workspace_id)["workspace_fingerprint"]
        == status["workspace_fingerprint"]
    )


def test_per_op_policy_gating_is_loaded_from_repository_config(tmp_path: Path) -> None:
    env = create_forge_environment(
        tmp_path,
        allowed_mutation_ops=("replace_text", "write"),
    )
    service = env.service
    workspace_id = service.workspace_create("demo", "v2 op policy")["workspace_id"]
    status = service.workspace_status(workspace_id)

    with pytest.raises(SecurityError, match=r"create.*disabled"):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="blocked.txt", content="blocked\n")],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
        )


def test_denied_paths_and_binary_text_replacements_fail_closed(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 denied mutate")["workspace_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    status = service.workspace_status(workspace_id)

    with pytest.raises(SecurityError):
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path=".github/workflows/evil.yml", content="evil\n")],
            expected_workspace_fingerprint=status["workspace_fingerprint"],
        )

    (root / "binary.dat").write_bytes(b"before\x00after")
    binary_status = service.workspace_status(workspace_id)
    with pytest.raises(SecurityError, match="binary"):
        service.workspace_mutate(
            workspace_id,
            [
                ReplaceTextMutation(
                    path="binary.dat",
                    expected_sha256=_sha(root / "binary.dat"),
                    edits=(TextReplacement("before", "changed"),),
                )
            ],
            expected_workspace_fingerprint=binary_status["workspace_fingerprint"],
        )


def test_keyed_mutate_replays_same_transaction_and_rejects_conflicting_request(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 keyed mutate")["workspace_id"]
    before = service.workspace_status(workspace_id)
    operation = CreateMutation(path="keyed.txt", content="created once\n")

    first = service.workspace_mutate(
        workspace_id,
        [operation],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        idempotency_key="workspace-mutate-key-0001",
    )
    replay = service.workspace_mutate(
        workspace_id,
        [operation],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        idempotency_key="workspace-mutate-key-0001",
    )

    assert replay == first
    assert first["transaction_id"]
    root = Path(service.workspace_status(workspace_id)["path"])
    assert (root / "keyed.txt").read_text(encoding="utf-8") == "created once\n"
    receipt = next((root.parent / ".repoforge-transaction-receipts").rglob("*.json"))
    assert "created once" not in receipt.read_text(encoding="utf-8")

    with pytest.raises(ConfigError) as conflict:
        service.workspace_mutate(
            workspace_id,
            [CreateMutation(path="other.txt", content="different\n")],
            expected_workspace_fingerprint=before["workspace_fingerprint"],
            idempotency_key="workspace-mutate-key-0001",
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert not (root / "other.txt").exists()


def test_cross_process_keyed_mutate_executes_transaction_once(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 concurrent keyed mutate")["workspace_id"]
    before = service.workspace_status(workspace_id)
    start = tmp_path / "start"
    worker = tmp_path / "mutate_worker.py"
    first_result = tmp_path / "first.json"
    second_result = tmp_path / "second.json"
    worker.write_text(
        f"""import json
import sys
import time
from pathlib import Path
from repoforge.application.service import CodingService
from repoforge.application.workspace.mutate import CreateMutation
from repoforge.config import load_config

start = Path({str(start)!r})
while not start.exists():
    time.sleep(0.01)
service = CodingService(load_config(Path({str(forge_env.config_path)!r})))
result = service.workspace_mutate(
    {workspace_id!r},
    [CreateMutation(path='concurrent-mutate.txt', content='effect once\\n')],
    expected_workspace_fingerprint={before["workspace_fingerprint"]!r},
    idempotency_key='workspace-mutate-concurrent-key-0001',
)
Path(sys.argv[1]).write_text(json.dumps(result, sort_keys=True), encoding='utf-8')
""",
        encoding="utf-8",
    )

    first = subprocess.Popen([sys.executable, str(worker), str(first_result)])
    second = subprocess.Popen([sys.executable, str(worker), str(second_result)])
    try:
        time.sleep(0.1)
        start.write_text("go\n", encoding="utf-8")
        assert first.wait(timeout=20) == 0
        assert second.wait(timeout=20) == 0
    finally:
        if first.poll() is None:
            first.kill()
        if second.poll() is None:
            second.kill()

    assert json.loads(first_result.read_text(encoding="utf-8")) == json.loads(
        second_result.read_text(encoding="utf-8")
    )
    root = Path(service.workspace_status(workspace_id)["path"])
    assert (root / "concurrent-mutate.txt").read_text(encoding="utf-8") == "effect once\n"
    receipts = list((root.parent / ".repoforge-transaction-receipts").rglob("*.json"))
    assert len(receipts) == 1


def test_keyed_mutate_crash_after_commit_marker_replays_committed_receipt(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 keyed crash after commit")["workspace_id"]
    before = service.workspace_status(workspace_id)

    class CrashOnceTransaction(JournaledFileTransaction):
        crashed = False

        def _checkpoint(self, point: str) -> None:
            if point == "after_commit_marker" and not type(self).crashed:
                type(self).crashed = True
                raise SimulatedTransactionCrash(point)
            super()._checkpoint(point)

    def open_crashing_transaction(_ctx: object, workspace_root: Path) -> JournaledFileTransaction:
        return CrashOnceTransaction(workspace_root)

    monkeypatch.setattr(
        mutate_enhanced_module,
        "open_file_transaction",
        open_crashing_transaction,
    )
    operation = CreateMutation(path="committed-once.txt", content="one physical mutation\n")
    with pytest.raises(SimulatedTransactionCrash):
        service.workspace_mutate(
            workspace_id,
            [operation],
            expected_workspace_fingerprint=before["workspace_fingerprint"],
            idempotency_key="workspace-mutate-crash-key-0001",
        )

    replay = service.workspace_mutate(
        workspace_id,
        [operation],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        idempotency_key="workspace-mutate-crash-key-0001",
    )

    root = Path(service.workspace_status(workspace_id)["path"])
    assert (root / "committed-once.txt").read_text(encoding="utf-8") == ("one physical mutation\n")
    assert replay["changed"] is True
    assert replay["transaction_id"]


def test_keyed_mutate_crash_before_commit_rolls_back_then_retries_safely(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 keyed crash before commit")["workspace_id"]
    before = service.workspace_status(workspace_id)

    class CrashOnceTransaction(JournaledFileTransaction):
        crashed = False

        def _checkpoint(self, point: str) -> None:
            if point == "after_receipt_apply" and not type(self).crashed:
                type(self).crashed = True
                raise SimulatedTransactionCrash(point)
            super()._checkpoint(point)

    def open_crashing_transaction(_ctx: object, workspace_root: Path) -> JournaledFileTransaction:
        return CrashOnceTransaction(workspace_root)

    monkeypatch.setattr(
        mutate_enhanced_module,
        "open_file_transaction",
        open_crashing_transaction,
    )
    operations = [
        CreateMutation(path="first.txt", content="first\n"),
        CreateMutation(path="second.txt", content="second\n"),
    ]
    with pytest.raises(SimulatedTransactionCrash):
        service.workspace_mutate(
            workspace_id,
            operations,
            expected_workspace_fingerprint=before["workspace_fingerprint"],
            idempotency_key="workspace-mutate-crash-key-0002",
        )

    result = service.workspace_mutate(
        workspace_id,
        operations,
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        idempotency_key="workspace-mutate-crash-key-0002",
    )

    root = Path(service.workspace_status(workspace_id)["path"])
    assert (root / "first.txt").read_text(encoding="utf-8") == "first\n"
    assert (root / "second.txt").read_text(encoding="utf-8") == "second\n"
    assert result["changed_paths"] == ["first.txt", "second.txt"]


def test_corrupt_keyed_mutate_receipt_fails_closed(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "v2 corrupt mutate receipt")["workspace_id"]
    before = service.workspace_status(workspace_id)
    operation = CreateMutation(path="corrupt-receipt.txt", content="stable\n")
    service.workspace_mutate(
        workspace_id,
        [operation],
        expected_workspace_fingerprint=before["workspace_fingerprint"],
        idempotency_key="workspace-mutate-corrupt-key-0001",
    )

    root = Path(service.workspace_status(workspace_id)["path"])
    receipts = root.parent / ".repoforge-transaction-receipts"
    receipt = next(receipts.rglob("workspace_mutate-*.json"))
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["result"]["changed"] = "not-a-boolean"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError) as corrupt:
        service.workspace_mutate(
            workspace_id,
            [operation],
            expected_workspace_fingerprint=before["workspace_fingerprint"],
            idempotency_key="workspace-mutate-corrupt-key-0001",
        )
    assert corrupt.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    assert (root / "corrupt-receipt.txt").read_text(encoding="utf-8") == "stable\n"
