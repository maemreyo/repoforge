from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ForgeEnvironment

import repoforge.application.workspace.file_write as file_write_module
from repoforge.adapters.persistence import JsonEffectReceiptStore, JsonIdempotencyStore
from repoforge.application.operations import OperationManager
from repoforge.application.outcome_reconciliation import OutcomeReceiptReconciler
from repoforge.application.service import CodingService
from repoforge.config import load_config
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError
from repoforge.domain.execution_receipt import (
    EffectReceiptState,
    create_effect_receipt,
    transition_effect_receipt,
)
from repoforge.domain.operation_task import OperationState
from repoforge.domain.operations import IdempotencyState, hash_idempotency_key
from repoforge.domain.versioning import Revision
from repoforge.testing import InMemoryLockManager


def _accepted_receipt():
    return create_effect_receipt(
        receipt_id="receipt-" + "a" * 24,
        operation_id="op-" + "b" * 24,
        action="workspace_write_file",
        idempotency_key_hash="c" * 64,
        request_fingerprint="d" * 64,
        accepted_at="2026-07-20T00:00:00+00:00",
        correlation_id="correlation-0001",
        pre_identity={"head_sha": "1" * 40, "workspace_id": "workspace-1"},
    )


def test_outcome_state_machine_rejects_terminal_downgrade() -> None:
    accepted = _accepted_receipt()
    applying = transition_effect_receipt(
        accepted,
        EffectReceiptState.APPLYING,
        now="2026-07-20T00:00:01+00:00",
    )
    unvalidated = transition_effect_receipt(
        applying,
        EffectReceiptState.APPLIED_UNVALIDATED,
        now="2026-07-20T00:00:02+00:00",
        result_reference="operation-result:op-" + "b" * 24,
        effect_boundary_crossed=True,
        post_identity={"head_sha": "2" * 40, "workspace_id": "workspace-1"},
    )
    validated = transition_effect_receipt(
        unvalidated,
        EffectReceiptState.APPLIED_VALIDATED,
        now="2026-07-20T00:00:03+00:00",
        result_reference="operation-result:op-" + "b" * 24,
    )

    assert validated.effect_boundary_crossed is True
    assert dict(validated.pre_identity) == {
        "head_sha": "1" * 40,
        "workspace_id": "workspace-1",
    }
    assert dict(validated.post_identity) == {
        "head_sha": "2" * 40,
        "workspace_id": "workspace-1",
    }
    with pytest.raises(RepoForgeError) as invalid:
        transition_effect_receipt(
            validated,
            EffectReceiptState.FAILED_BEFORE_EFFECT,
            now="2026-07-20T00:00:04+00:00",
            error_code=ErrorCode.FAILED_BEFORE_EFFECT.value,
        )
    assert invalid.value.code is ErrorCode.STATE_INVALID


def test_outcome_store_roundtrips_with_cas_and_indexes(tmp_path: Path) -> None:
    store = JsonEffectReceiptStore(tmp_path, InMemoryLockManager())
    created = store.create(_accepted_receipt())
    applying = transition_effect_receipt(
        created.value,
        EffectReceiptState.APPLYING,
        now="2026-07-20T00:00:01+00:00",
    )
    saved = store.save(applying, expected_revision=created.revision)

    assert saved.revision == Revision(2)
    assert store.read(applying.receipt_id) == saved
    assert [item.value for item in store.list_for_operation(applying.operation_id).records] == [
        applying
    ]
    assert applying.idempotency_key_hash is not None
    assert [
        item.value
        for item in store.list_for_idempotency(
            applying.action,
            applying.idempotency_key_hash,
        ).records
    ] == [applying]
    with pytest.raises(RepoForgeError) as stale:
        store.save(applying, expected_revision=Revision(1))
    assert stale.value.code is ErrorCode.STATE_STALE


def test_legacy_idempotency_record_remains_readable_without_receipt_claim(tmp_path: Path) -> None:
    store = JsonIdempotencyStore(tmp_path)
    key_hash = hash_idempotency_key("legacy-key-0001")
    path = store.root / f"workspace_write_file-{key_hash}.json"
    path.write_text(
        json.dumps(
            {
                "action": "workspace_write_file",
                "correlation_id": "legacy-correlation",
                "key_hash": key_hash,
                "request_fingerprint": "e" * 64,
                "result": {"ok": True},
                "state": "completed",
                "updated_at": "2026-07-20T00:00:00+00:00",
                "updated_at_epoch": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = store.load("workspace_write_file", key_hash)

    assert loaded is not None
    assert loaded.state is IdempotencyState.COMPLETED
    assert loaded.receipt_id is None
    assert loaded.operation_id is None


def test_response_failure_replays_authoritative_outcome_after_service_restart(
    forge_env: ForgeEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = forge_env.service
    workspace_id = service.workspace_create("demo", "receipt-restart-replay")["workspace_id"]
    original_to_data = file_write_module.to_data

    def fail_result_serialization(value: object) -> object:
        del value
        raise RuntimeError("simulated response loss")

    monkeypatch.setattr(file_write_module, "to_data", fail_result_serialization)
    with pytest.raises(ConfigError) as failed:
        service.workspace_write_file(
            workspace_id,
            "restart.txt",
            "written once\n",
            "<new>",
            idempotency_key="receipt-restart-key-0001",
        )
    assert failed.value.code is ErrorCode.FAILED_AFTER_EFFECT

    monkeypatch.setattr(file_write_module, "to_data", original_to_data)
    restarted = CodingService(load_config(forge_env.config_path))
    replay = restarted.workspace_write_file(
        workspace_id,
        "restart.txt",
        "written once\n",
        "<new>",
        idempotency_key="receipt-restart-key-0001",
    )

    workspace = Path(restarted.workspace_status(workspace_id)["path"])
    assert replay["path"] == "restart.txt"
    assert replay["sha256"]
    assert workspace.joinpath("restart.txt").read_text(encoding="utf-8") == "written once\n"
    key_hash = hash_idempotency_key("receipt-restart-key-0001")
    receipts = restarted.application.context.effect_receipts
    assert receipts is not None
    matched = receipts.list_for_idempotency("workspace_write_file", key_hash).records
    assert len(matched) == 1
    assert matched[0].value.state is EffectReceiptState.FAILED_AFTER_EFFECT
    result_store = restarted.application.context.operation_result_store
    assert result_store is not None
    assert result_store.read(matched[0].value.operation_id)["path"] == "restart.txt"


def test_startup_reconciler_classifies_all_nonterminal_receipts(
    forge_env: ForgeEnvironment,
) -> None:
    ctx = forge_env.service.application.context
    receipts = ctx.effect_receipts
    results = ctx.operation_result_store
    operations = ctx.operation_store
    assert receipts is not None
    assert results is not None
    assert operations is not None
    manager = OperationManager(ctx)

    def create(state: EffectReceiptState, *, with_result: bool = False) -> tuple[str, str]:
        now = ctx.clock.now_iso()
        task = manager.create(
            kind="workspace_write_file",
            phase="accepted",
            cancel_supported=False,
            workspace_id=None,
            now=now,
        )
        envelope = receipts.create(
            create_effect_receipt(
                receipt_id=f"receipt-{ctx.ids.new_hex(24)}",
                operation_id=task.operation_id,
                action="workspace_write_file",
                idempotency_key_hash=None,
                request_fingerprint="f" * 64,
                accepted_at=now,
                correlation_id=ctx.ids.new_hex(24),
            )
        )
        if state is not EffectReceiptState.ACCEPTED:
            manager.start(task.operation_id, now=ctx.clock.now_iso())
            envelope = receipts.save(
                transition_effect_receipt(
                    envelope.value,
                    EffectReceiptState.APPLYING,
                    now=ctx.clock.now_iso(),
                ),
                expected_revision=envelope.revision,
            )
        if state is EffectReceiptState.APPLIED_UNVALIDATED:
            result_reference = f"operation-result:{task.operation_id}"
            results.save(task.operation_id, {"ok": True})
            envelope = receipts.save(
                transition_effect_receipt(
                    envelope.value,
                    state,
                    now=ctx.clock.now_iso(),
                    result_reference=result_reference,
                    effect_boundary_crossed=True,
                ),
                expected_revision=envelope.revision,
            )
        assert with_result is (state is EffectReceiptState.APPLIED_UNVALIDATED)
        return envelope.value.receipt_id, task.operation_id

    accepted_receipt, accepted_operation = create(EffectReceiptState.ACCEPTED)
    applying_receipt, applying_operation = create(EffectReceiptState.APPLYING)
    unvalidated_receipt, unvalidated_operation = create(
        EffectReceiptState.APPLIED_UNVALIDATED,
        with_result=True,
    )

    report = OutcomeReceiptReconciler(ctx).reconcile(stale_after_seconds=0)

    assert report.failed_before_effect == 1
    assert report.unknown == 1
    assert report.validated == 1
    assert report.deferred_active == 0
    assert receipts.read(accepted_receipt).value.state is EffectReceiptState.FAILED_BEFORE_EFFECT
    assert receipts.read(applying_receipt).value.state is EffectReceiptState.UNKNOWN
    assert receipts.read(unvalidated_receipt).value.state is EffectReceiptState.APPLIED_VALIDATED
    assert operations.read(accepted_operation).state is OperationState.FAILED
    assert operations.read(applying_operation).state is OperationState.FAILED
    assert operations.read(unvalidated_operation).state is OperationState.SUCCEEDED
