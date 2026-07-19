"""Cross-process and fault-injection coverage for keyed local workspace mutations."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

import repoforge.application.workspace.file_write as file_write_module
from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence import JsonExternalMutationLedger
from repoforge.domain.errors import ConfigError, ErrorCode


def _audit_events(root: Path, action: str) -> list[dict[str, object]]:
    path = root / "state" / "audit.jsonl"
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [event for event in events if event["action"] == action]


def test_external_mutation_ledger_reserves_once_and_enforces_window(tmp_path: Path) -> None:
    ledger = JsonExternalMutationLedger(tmp_path / "state", FcntlLockManager(tmp_path / "locks"))

    first = ledger.reserve(
        "demo",
        "effect-a",
        count=1,
        now_epoch=100.0,
        max_in_window=2,
        window_seconds=60,
    )
    replay = ledger.reserve(
        "demo",
        "effect-a",
        count=1,
        now_epoch=101.0,
        max_in_window=2,
        window_seconds=60,
    )
    second = ledger.reserve(
        "demo",
        "effect-b",
        count=1,
        now_epoch=102.0,
        max_in_window=2,
        window_seconds=60,
    )

    assert first == 1
    assert replay == 1
    assert second == 2
    with pytest.raises(ConfigError, match="external mutation window limit"):
        ledger.reserve(
            "demo",
            "effect-c",
            count=1,
            now_epoch=103.0,
            max_in_window=2,
            window_seconds=60,
        )
    assert (
        ledger.reserve(
            "demo",
            "effect-c",
            count=1,
            now_epoch=200.0,
            max_in_window=2,
            window_seconds=60,
        )
        == 1
    )


def test_cross_process_keyed_write_executes_effect_once(
    forge_env: ForgeEnvironment,
    tmp_path: Path,
) -> None:
    workspace_id = forge_env.service.workspace_create("demo", "cross-process-write")["workspace_id"]
    worker = tmp_path / "mutation_worker.py"
    start = tmp_path / "start"
    first_result = tmp_path / "first.json"
    second_result = tmp_path / "second.json"
    worker.write_text(
        f"""import json
import sys
import time
from pathlib import Path
from repoforge.application.service import CodingService
from repoforge.config import load_config

start = Path({str(start)!r})
while not start.exists():
    time.sleep(0.01)
service = CodingService(load_config(Path({str(forge_env.config_path)!r})))
result = service.workspace_write_file(
    {workspace_id!r},
    "concurrent.txt",
    "effect once\\n",
    "<new>",
    idempotency_key="cross-process-write-key-0001",
)
Path(sys.argv[1]).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
""",
        encoding="utf-8",
    )

    first = subprocess.Popen([sys.executable, str(worker), str(first_result)])
    second = subprocess.Popen([sys.executable, str(worker), str(second_result)])
    try:
        time.sleep(0.1)
        start.write_text("go\n", encoding="utf-8")
        assert first.wait(timeout=15) == 0
        assert second.wait(timeout=15) == 0
    finally:
        if first.poll() is None:
            first.kill()
        if second.poll() is None:
            second.kill()

    assert json.loads(first_result.read_text(encoding="utf-8")) == json.loads(
        second_result.read_text(encoding="utf-8")
    )
    workspace = Path(forge_env.service.workspace_status(workspace_id)["path"])
    assert (workspace / "concurrent.txt").read_text(encoding="utf-8") == "effect once\n"
    events = _audit_events(forge_env.root, "workspace_write_file")
    keyed = [event for event in events if "idempotency_key_hash" in event["details"]]
    assert len(keyed) == 2
    assert sum(bool(event["details"].get("idempotent_replay")) for event in keyed) == 1


def test_corrupt_completed_write_receipt_fails_before_mutation(
    forge_env: ForgeEnvironment,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "corrupt-write-receipt")["workspace_id"]
    first = service.workspace_write_file(
        workspace_id,
        "corrupt.txt",
        "stable\n",
        "<new>",
        idempotency_key="corrupt-write-key-0001",
    )
    assert service.idempotency is not None
    record = next(service.idempotency.root.glob("workspace_write_file-*.json"))
    record.write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(ConfigError) as corrupt:
        service.workspace_write_file(
            workspace_id,
            "corrupt.txt",
            "stable\n",
            "<new>",
            idempotency_key="corrupt-write-key-0001",
        )

    assert corrupt.value.code is ErrorCode.STATE_PERSISTENCE_FAILED
    workspace = Path(service.workspace_status(workspace_id)["path"])
    assert (workspace / "corrupt.txt").read_text(encoding="utf-8") == "stable\n"
    assert first["sha256"]


def test_lost_write_response_marks_key_uncertain_without_reapplying(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "lost-write-response")["workspace_id"]
    original_to_data = file_write_module.to_data

    def fail_result_serialization(value: object) -> object:
        del value
        raise RuntimeError("simulated lost response")

    monkeypatch.setattr(file_write_module, "to_data", fail_result_serialization)
    with pytest.raises(ConfigError) as lost:
        service.workspace_write_file(
            workspace_id,
            "lost.txt",
            "written once\n",
            "<new>",
            idempotency_key="lost-write-key-0001",
        )
    assert lost.value.code is ErrorCode.IDEMPOTENCY_UNCERTAIN
    assert lost.value.retryable is False

    monkeypatch.setattr(file_write_module, "to_data", original_to_data)
    workspace = Path(service.workspace_status(workspace_id)["path"])
    assert (workspace / "lost.txt").read_text(encoding="utf-8") == "written once\n"
    with pytest.raises(ConfigError) as replay:
        service.workspace_write_file(
            workspace_id,
            "lost.txt",
            "written once\n",
            "<new>",
            idempotency_key="lost-write-key-0001",
        )
    assert replay.value.code is ErrorCode.IDEMPOTENCY_UNCERTAIN
    assert (workspace / "lost.txt").read_text(encoding="utf-8") == "written once\n"
