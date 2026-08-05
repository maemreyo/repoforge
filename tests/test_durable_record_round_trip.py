"""Writer/reader drift gate: what a durable store writes, it must read back.

`tests/test_upgrade_compatibility.py` covers records this release *cannot decode*.
The 2026-07-29 outage was the other half: a record that decoded fine and then failed
its own `__post_init__`. `restarts_total` and `last_restart_at` were added to
`RuntimeRecord` and to the supervisor's writer but not to the decoder, so every read
defaulted `restarts_total` to 0 while `restart_count` came back as written, and the
invariant relating them rejected the record. The runtime would not start, `rf` ran the
same broken decoder, and no release could be rolled back to because every release since
the field landed carried it.

The rule here is generic, and deliberately knows nothing about any specific invariant:

    for a fully populated record, ``read(write(record)) == record``

A field added to the dataclass and the writer but not the decoder silently becomes a
default on read, and that is what this catches -- before it reaches the invariant that
turns it fatal. Every case populates *every* optional field, enforced mechanically by
`_assert_every_optional_field_populated`, because a fixture that leaves an optional
field at its default cannot tell a decoder that drops it from one that reads it.

`test_every_durable_record_type_has_a_round_trip_case` discovers the durable record
types from the codebase rather than from a list maintained by hand, so a new store
cannot join the state root without joining this property.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import os
import pkgutil
import typing
from collections.abc import Callable
from pathlib import Path

import pytest

from repoforge.testing.fakes import InMemoryLockManager

_SHA = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_HEAD_SHA = "9a96afa68f9ca682f674456a47f6962372e5ab17"
_HEAD_SHA_B = "aa53cda0e8f7c1b2d3a4956871f0e2c3b4d5a6f7"
_IDENTITY_CONTEXT_ID = "identity-" + "d" * 24
_IDENTITY_CONTEXT_DIGEST = "e" * 64


def _h(marker: str) -> str:
    """A distinct 64-hex value. Distinct so a decoder that swaps two fields is caught."""
    return (marker * 64)[:64]


# -- the property ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RoundTripCase:
    """One durable record type, written and read back through its real store."""

    #: The record type this case covers. Matched against the discovered set, so a
    #: renamed or newly added record type surfaces as a coverage failure.
    record_type: type
    #: Writes a fully populated record and reads it back. Returns both, rather than
    #: asserting, so the equality failure names the case and shows both sides.
    round_trip: Callable[[Path], tuple[object, object]]
    #: Set when one record type needs several cases because a discriminator selects
    #: which of its fields are persisted at all.
    variant: str = ""
    #: `Type.field` paths this variant's discriminator makes meaningless, exempt from
    #: the populated check; `Type.*` exempts a whole nested type. Only for a field the
    #: encoder is *supposed* to drop for this variant, or a nested type the domain
    #: derives rather than the fixture supplying -- anything else here hides the very
    #: drift this file exists to catch.
    inapplicable: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        if not self.variant:
            return self.record_type.__name__
        return f"{self.record_type.__name__}-{self.variant}"


#: Fields whose default is the only value a valid record may carry, so "populate it with
#: something else" is not an instruction a fixture can follow. A codec pins the schema
#: version it writes and rejects any other on decode, and that check is the store's own;
#: it does not need this property to cover it too.
_PINNED_TO_THEIR_DEFAULT = frozenset({"schema_version", "plan_format_version"})


def _assert_every_optional_field_populated(
    record: object, *, inapplicable: frozenset[str] = frozenset(), path: str = ""
) -> None:
    """Fail when a field with a default was left at that default.

    A round-trip over a record whose optional fields are all defaults proves nothing:
    a decoder that drops those fields returns the same defaults and the assertion still
    passes. Recurses into nested records, because the drift that took the runtime down
    is just as available one level in.
    """
    for field in dataclasses.fields(record):  # type: ignore[arg-type]
        value = getattr(record, field.name)
        qualified = f"{type(record).__name__}.{field.name}"
        where = f"{path}{qualified}"
        has_default = field.default is not dataclasses.MISSING
        skip = (
            field.name in _PINNED_TO_THEIR_DEFAULT
            or qualified in inapplicable
            or f"{type(record).__name__}.*" in inapplicable
        )
        if has_default and not skip and value == field.default:
            raise AssertionError(
                f"{where} is still its default ({value!r}). Populate it: a default here "
                "makes this round trip unable to detect a decoder that drops the field."
            )
        for nested in value if isinstance(value, tuple) else (value,):
            if dataclasses.is_dataclass(nested) and not isinstance(nested, type):
                _assert_every_optional_field_populated(
                    nested, inapplicable=inapplicable, path=f"{where}."
                )


# -- fixtures, one per durable record type -----------------------------------


def _value(envelope: object) -> object:
    """Unwrap a `StateEnvelope`, keeping `None` distinguishable from a decoded record."""
    return getattr(envelope, "value", None)


def _workspace_identity(head_sha: str) -> object:
    from repoforge.domain.failure_intelligence import WorkspaceIdentity

    return WorkspaceIdentity(
        head_sha=head_sha,
        workspace_fingerprint=_h("1"),
        config_generation=_h("2"),
        policy_hash=_h("3"),
    )


def _runtime_record(tmp_path: Path) -> tuple[object, object]:
    """The record from the incident. Written by the supervisor, read by everything."""
    from repoforge.adapters.runtime.state_store import process_identity
    from repoforge.bootstrap import build_runtime_store
    from repoforge.domain.runtime import RuntimePhase, RuntimeRecord

    # `read` re-verifies the recorded pids against live processes and rewrites or
    # discards the record when they no longer match, so the round trip has to name a
    # process that is genuinely running: this one.
    pid = os.getpid()
    identity = process_identity(pid)
    assert identity is not None, "the test process must be inspectable by `ps`"

    store = build_runtime_store(tmp_path / "managed-runtime-v3.json")
    written = RuntimeRecord(
        protocol_version=1,
        phase=RuntimePhase.HEALTHY,
        pid=pid,
        process_identity=identity,
        active_generation=15,
        accepted_generation=15,
        tunnel_profile="repoforge",
        tunnel_profile_fingerprint=_SHA_B,
        tool_surface_hash=_SHA_C,
        started_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:39:48+00:00",
        correlation_id="d" * 24,
        child_pid=pid,
        child_process_identity=identity,
        restart_count=1,
        last_error_code="CHILD_IDENTITY_MISMATCH",
        last_error="Recorded tunnel child is no longer the owned process",
        health=(("tunnel_admin", True, "admin endpoint returned HTTP 200"),),
        package_version="2.2.0",
        executable="/opt/repoforge/venv/bin/python",
        install_origin="wheel",
        running_release_sha="9a96afa68f9c",
        health_observed_at="2026-07-29T09:39:48+00:00",
        consecutive_health_failures=2,
        restarts_total=3,
        last_restart_at="2026-07-29T09:26:21+00:00",
        fail_closed_since="2026-07-29T09:26:21+00:00",
    )
    store.write(written)
    return written, store.read()


def _worker_binding(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonWorkerBindingStore
    from repoforge.domain.operation_worker import OperationWorkerBinding

    store = JsonWorkerBindingStore(tmp_path, InMemoryLockManager())
    written = OperationWorkerBinding(
        operation_id="op-" + "1" * 24,
        child_pid=4321,
        child_pgid=4321,
        child_start_token="child-token-1",
        server_pid=1234,
        server_start_token="server-token-1",
        created_at="2026-07-29T09:26:21+00:00",
        owner_generation=15,
        owner_id="worker-1",
        attempt=2,
        identity_context_id=_IDENTITY_CONTEXT_ID,
        identity_context_digest=_IDENTITY_CONTEXT_DIGEST,
    )
    store.put(written)
    return written, store.get(written.operation_id)


def _execution_worker_binding(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonExecutionWorkerBindingStore
    from repoforge.domain.execution_worker import ExecutionWorkerBinding

    store = JsonExecutionWorkerBindingStore(tmp_path, InMemoryLockManager())
    written = ExecutionWorkerBinding(
        worker_id="worker-0123456789ab",
        pid=4321,
        pgid=4321,
        process_start_token="2026-07-29 09:26:21 +0000",
        generation=15,
        release_sha="9a96afa68f9c",
        supervisor_pid=1234,
        supervisor_process_identity=_SHA,
        correlation_id="c" * 24,
        started_at="2026-07-29T09:26:21+00:00",
        state="running",
    )
    store.put(written)
    return written, store.get(written.worker_id)


def _execution_worker_archive(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonExecutionWorkerBindingStore
    from repoforge.domain.execution_worker import (
        ExecutionWorkerArchiveEntry,
        ExecutionWorkerBinding,
    )

    store = JsonExecutionWorkerBindingStore(tmp_path, InMemoryLockManager())
    binding = ExecutionWorkerBinding(
        worker_id="worker-0123456789ab",
        pid=4321,
        pgid=4321,
        process_start_token="2026-07-29 09:26:21 +0000",
        generation=15,
        release_sha="9a96afa68f9c",
        supervisor_pid=1234,
        supervisor_process_identity=_SHA,
        correlation_id="c" * 24,
        started_at="2026-07-29T09:26:21+00:00",
        state="running",
    )
    store.put(binding)
    terminal = store.update_state(binding.worker_id, "reclaimed")
    archived = store.list_archive()
    assert len(archived) == 1
    assert terminal is not None
    # The archive write is terminal-state plus a timestamp; compare the exact entry
    # the store must have written against what the codec read back.
    return (
        ExecutionWorkerArchiveEntry.from_binding(terminal, terminated_at=archived[0].terminated_at),
        archived[0],
    )


def _repository_identity_binding(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonRepositoryBindingStore
    from repoforge.domain.repository_identity import (
        RepositoryIdentityBinding,
        RepositoryProvider,
    )

    store = JsonRepositoryBindingStore(tmp_path, InMemoryLockManager())
    written = RepositoryIdentityBinding(
        provider=RepositoryProvider.GITHUB,
        provider_host="github.example.com",
        repository_id="987654",
        canonical_name="github.example.com/acme/repoforge",
        human_profile_id="personal",
        agent_profile_id="automation",
        config_revision=_SHA,
    )
    store.create(written)
    return written, _value(store.read(written.provider_host, written.repository_id))


def _operation_identity_record(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonOperationIdentityStore
    from repoforge.domain.operation_identity import (
        LeaseCapabilityRequest,
        OperationIdentityRecord,
        OperationIdentityReference,
        operation_identity_digest,
    )
    from repoforge.domain.repository_identity import (
        ActorClass,
        AuthLease,
        AuthLeaseState,
        AuthTargetKind,
        OpaqueCredentialReference,
        OperationIdentityContext,
        RepositoryProvider,
    )

    operation_id = "op-" + "f" * 24
    lease = AuthLease(
        lease_id="lease-primary",
        profile_id="personal",
        provider=RepositoryProvider.GITHUB,
        repository_id="987654",
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="987654",
        actor_id="4242",
        credential_ref=OpaqueCredentialReference("gh-account", "personal-account"),
        issued_at="2026-07-29T09:26:21+00:00",
        expires_at="2026-07-29T10:26:21+00:00",
        state=AuthLeaseState.ACTIVE,
        config_revision=_SHA,
        policy_revision=_SHA_B,
        material_digest=_SHA_C,
        provider_metadata=(("github_host", "github.com"),),
    )
    context = OperationIdentityContext(
        operation_id=operation_id,
        primary_repository_id="987654",
        actor_class=ActorClass.HUMAN_OPERATED,
        auth_leases=(lease,),
        selected_at="2026-07-29T09:26:21+00:00",
        config_revision=_SHA,
        policy_revision=_SHA_B,
    )
    written = OperationIdentityRecord(
        reference=OperationIdentityReference(
            context_id=_IDENTITY_CONTEXT_ID,
            context_digest=operation_identity_digest(context),
        ),
        operation_id=operation_id,
        context=context,
        capability_requests=(
            LeaseCapabilityRequest(
                lease_id=lease.lease_id,
                capability_ids=("github.contents.write",),
            ),
        ),
        superseded_lease_ids=("lease-retired",),
        created_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:39:48+00:00",
    )
    store = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    store.create(written)
    return written, _value(store.read(written.operation_id))


def _runtime_activation_receipt(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonRuntimeActivationStore
    from repoforge.domain.runtime_activation import (
        RuntimeActivationClassification,
        RuntimeActivationIdentity,
        RuntimeActivationReceipt,
    )

    def identity(generation: int) -> RuntimeActivationIdentity:
        return RuntimeActivationIdentity(
            config_generation=generation,
            source_sha256=_SHA,
            resolved_sha256=_SHA_B,
            runtime_active_generation=generation,
            process_identity=_SHA_C,
            tool_surface_hash="d" * 64,
            runtime_phase="healthy",
        )

    store = JsonRuntimeActivationStore(tmp_path, InMemoryLockManager())
    written = RuntimeActivationReceipt(
        receipt_id="receipt-" + "4" * 24,
        operation_id="op-" + "5" * 24,
        # A failed classification is the one that permits every field at once: the
        # terminal-success classifications forbid error evidence, and the non-terminal
        # ones forbid an active identity, so neither can populate the whole record.
        classification=RuntimeActivationClassification.RELOAD_FAILED,
        target_generation=15,
        accepted_identity=identity(15),
        previous_identity=identity(14),
        active_identity=identity(15),
        continuation_reference="op-" + "6" * 24,
        correlation_id="e" * 24,
        effect_boundary_crossed=True,
        accepted_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:39:48+00:00",
        error_code="ACTIVATION_TIMEOUT",
        error_message="Runtime did not report the target generation in time",
    )
    store.create(written)
    envelope = store.read(written.receipt_id)
    return written, envelope.value if envelope is not None else None


def _effect_receipt(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonEffectReceiptStore
    from repoforge.domain.execution_receipt import EffectReceipt, EffectReceiptState

    store = JsonEffectReceiptStore(tmp_path, InMemoryLockManager())
    written = EffectReceipt(
        receipt_id="receipt-" + "7" * 24,
        operation_id="op-" + "8" * 24,
        action="workspace_pr",
        idempotency_key_hash=_SHA,
        request_fingerprint=_SHA_B,
        # The only state that permits a result reference and error evidence together,
        # so it is the only one under which every optional field can carry a value.
        state=EffectReceiptState.FAILED_AFTER_EFFECT,
        accepted_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:39:48+00:00",
        correlation_id="f" * 24,
        pre_identity=(("head_sha", "9a96afa"),),
        post_identity=(("head_sha", "aa53cda"),),
        effect_boundary_crossed=True,
        result_reference="op-" + "9" * 24,
        error_code="GH_RATE_LIMITED",
        error_message="Secondary rate limit reached",
    )
    store.create(written)
    return written, _value(store.read(written.receipt_id))


def _stage_receipt(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonExecutionReceiptStore
    from repoforge.domain.execution_receipt import (
        StageCacheStatus,
        StageReceiptStatus,
        create_stage_receipt,
    )
    from repoforge.domain.verification_dag import ArtifactDigest

    store = JsonExecutionReceiptStore(tmp_path, InMemoryLockManager())
    # `receipt_id` is a digest of the rest of the receipt and is re-derived on validate,
    # so it has to come from the factory rather than be invented here.
    written = create_stage_receipt(
        operation_id="op-" + "b" * 24,
        ordinal=2,
        plan_id="plan-" + "c" * 24,
        plan_hash=_SHA,
        workspace_id="epic-284-repository-iden-9164d41f2f",
        stage_id="pytest",
        kind="profile",
        target="tests",
        boundary="final",
        started_at="2026-07-29T09:26:21+00:00",
        finished_at="2026-07-29T09:39:48+00:00",
        pre_identity=_workspace_identity(_HEAD_SHA),
        post_identity=_workspace_identity(_HEAD_SHA_B),
        target_identity=_SHA_B,
        environment_identity_schema_version=1,
        environment_identity=_SHA_C,
        requested_policy_hash=_SHA,
        effective_policy_hash=_SHA_B,
        status=StageReceiptStatus.FAILED,
        failure_class="test_failure",
        result_reference="op-" + "d" * 24,
        artifact_digests=(ArtifactDigest(path="junit.xml", sha256=_SHA_C),),
        cache_status=StageCacheStatus.MISS,
        source_changed=True,
    )
    store.create(written)
    return written, _value(store.read(written.receipt_id))


def _execution_plan_record() -> object:
    from repoforge.domain.execution_plan import (
        ExecutionPlanBinding,
        PlanStage,
        PlanStageBoundary,
        PlanStageKind,
        PlanStageMutability,
        StageFailurePolicy,
        create_execution_plan,
    )

    # `plan_id`, `plan_hash` and `stage_definition_hash` are digests over the rest of the
    # plan and are re-derived on validate, so the factory owns them.
    return create_execution_plan(
        task_id="task-0123456789abcdef01234567",
        workspace_id="epic-284-repository-iden-9164d41f2f",
        binding=ExecutionPlanBinding(
            head_sha=_HEAD_SHA,
            workspace_fingerprint=_h("1"),
            config_generation=_h("2"),
            policy_hash=_h("3"),
            assessment_snapshot_id=_h("4"),
            evidence_snapshot_ids=(_h("5"),),
            risk_assessment_hash=_h("6"),
            recommendation_hash=_h("7"),
        ),
        ordered_stages=(
            PlanStage(
                stage_id="lint",
                kind=PlanStageKind.DIAGNOSTIC,
                target="src",
                selector="src/repoforge",
                dependencies=(),
                boundary=PlanStageBoundary.ITERATION,
                working_directory="src",
                timeout_seconds=120,
                mutability=PlanStageMutability.READ_ONLY,
                network_policy="none",
                failure_policy=StageFailurePolicy.OPTIONAL,
                artifact_paths=("ruff.json",),
            ),
            PlanStage(
                stage_id="pytest",
                kind=PlanStageKind.PROFILE,
                # The FINAL stage must be last and target the plan's `final_profile`.
                target="verify",
                selector="tests/test_durable_record_round_trip.py",
                dependencies=("lint",),
                boundary=PlanStageBoundary.FINAL,
                working_directory="src",
                timeout_seconds=900,
                mutability=PlanStageMutability.WORKSPACE_WRITE,
                network_policy="restricted",
                failure_policy=StageFailurePolicy.REQUIRED,
                artifact_paths=("junit.xml",),
            ),
        ),
        final_profile="verify",
        created_at="2026-07-29T09:26:21+00:00",
        expires_at="2026-07-30T09:26:21+00:00",
    )


def _execution_plan(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonExecutionPlanStore

    store = JsonExecutionPlanStore(tmp_path, InMemoryLockManager())
    written = _execution_plan_record()
    store.create(written)
    return written, _value(store.read(written.plan_id))  # type: ignore[attr-defined]


def _execution_plan_acceptance(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonExecutionPlanAcceptanceStore

    store = JsonExecutionPlanAcceptanceStore(tmp_path, InMemoryLockManager())
    # The acceptance is derived from the plan rather than constructed, so this covers
    # the fields the store itself fills in as well as the ones a caller passes.
    accepted = store.accept(
        _execution_plan_record(),  # type: ignore[arg-type]
        acceptance_id="acceptance-" + "f" * 24,
        task_id="task-0123456789abcdef01234567",
        accepted_at="2026-07-29T09:26:21+00:00",
    )
    return accepted.value, _value(store.read(accepted.value.acceptance_id))


def _iteration_cache_entry(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonIterationCache
    from repoforge.domain.verification_dag import (
        ArtifactDigest,
        build_iteration_cache_key,
        create_iteration_cache_entry,
    )

    store = JsonIterationCache(tmp_path, InMemoryLockManager())
    # Both `entry_id` and `key.cache_key` are digests over the rest of the record and are
    # re-derived on decode, so the factories own them. Every other field is set here.
    written = create_iteration_cache_entry(
        key=build_iteration_cache_key(
            workspace_identity=_h("a"),
            declared_input_hash=_h("b"),
            stage_definition_hash=_h("c"),
            target_identity=_h("d"),
            working_directory="src",
            environment_identity=_h("e"),
            environment_identity_schema_version=1,
            requested_policy_hash=_h("f"),
            effective_policy_hash=_h("1"),
            toolchain_hash=_h("2"),
            lockfile_hash=_h("3"),
            config_generation=_h("4"),
            policy_hash=_h("5"),
            provider_hash=_h("6"),
            network_policy="deny",
            dependency_receipt_hashes=(_h("7"),),
        ),
        source_receipt_id="receipt-" + "2" * 24,
        artifact_digests=(ArtifactDigest(path="junit.xml", sha256=_SHA_B),),
        created_at="2026-07-29T09:26:21+00:00",
    )
    store.put(written)
    return written, store.read(written.entry_id)


#: `work_item_payload` persists a different field set per `request.kind`, so
#: `OperationWorkRequest` is three record shapes wearing one dataclass. Each branch of
#: that encoder can drift on its own, so each gets its own case, and the fields the
#: other two kinds own are inapplicable rather than missing.
_WORK_REQUEST_FIELDS_BY_KIND: dict[str, frozenset[str]] = {
    "profile": frozenset({"profile_name"}),
    "adhoc": frozenset(
        {
            "argv",
            "script",
            "shell",
            "argv_sequence",
            "working_directory",
            "mutability",
            "stdin_text",
            "declared_effect",
        }
    ),
    "diagnostic": frozenset(
        {
            "diagnostic_id",
            "selector",
            "selector2",
            "intent",
            "expectation",
            "expected_failure_class",
            "force_rerun",
            "rerun_failed",
        }
    ),
}


#: script/shell/argv_sequence are mutually exclusive with argv (enforced by
#: OperationWorkRequest.__post_init__, review finding F-005), so the "adhoc" fixture
#: below populates only argv and cannot also populate these three -- they are proven to
#: round-trip instead by test_operation_work_queue.py's dedicated
#: test_adhoc_work_request_round_trips_script_and_argv_sequence_forms.
_ADHOC_MUTUALLY_EXCLUSIVE_FORMS = frozenset({"script", "shell", "argv_sequence"})


def _work_request_inapplicable(kind: str) -> frozenset[str]:
    owned = _WORK_REQUEST_FIELDS_BY_KIND[kind]
    others = frozenset().union(*_WORK_REQUEST_FIELDS_BY_KIND.values()) - owned
    exempt = _ADHOC_MUTUALLY_EXCLUSIVE_FORMS if kind == "adhoc" else frozenset()
    return frozenset(f"OperationWorkRequest.{name}" for name in others | exempt)


def _operation_work_item(kind: str) -> Callable[[Path], tuple[object, object]]:
    def round_trip(tmp_path: Path) -> tuple[object, object]:
        from repoforge.adapters.persistence import JsonOperationWorkQueue
        from repoforge.domain.operation_work import (
            OperationWorkItem,
            OperationWorkRequest,
            OperationWorkState,
        )

        by_kind: dict[str, dict[str, object]] = {
            "profile": {"profile_name": "verify"},
            "adhoc": {
                "argv": ("pytest", "-q"),
                "working_directory": "src",
                "mutability": "workspace_write",
                "stdin_text": "--- a/x\n+++ b/x\n",
                "declared_effect": "local_history",
            },
            "diagnostic": {
                "diagnostic_id": "diag-1",
                "selector": ("tests/test_durable_record_round_trip.py",),
                "selector2": ("tests/test_upgrade_compatibility.py",),
                "intent": "Prove the round trip holds",
                "expectation": "pass",
                "expected_failure_class": "test_failure",
                "force_rerun": True,
                "rerun_failed": True,
            },
        }

        store = JsonOperationWorkQueue(tmp_path, InMemoryLockManager())
        written = OperationWorkItem(
            operation_id="op-" + "3" * 24,
            request=OperationWorkRequest(
                kind=kind,  # type: ignore[arg-type]
                workspace_id="epic-284-repository-iden-9164d41f2f",
                expected_head_sha=_HEAD_SHA,
                expected_fingerprint=_SHA,
                config_generation=15,
                **by_kind[kind],  # type: ignore[arg-type]
            ),
            state=OperationWorkState.CLAIMED,
            attempt=2,
            owner_id="worker-1",
            lease_expires_at="2026-07-29T09:45:00+00:00",
            child_started=True,
            created_at="2026-07-29T09:26:21+00:00",
            updated_at="2026-07-29T09:39:48+00:00",
        )
        store.create(written)
        return written, store.read(written.operation_id)

    return round_trip


def _approval_request(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonApprovalStore
    from repoforge.domain.approval import (
        ApprovalBinding,
        ApprovalDecision,
        ApprovalRequest,
        ApprovalStatus,
        ApprovalSubject,
    )

    store = JsonApprovalStore(tmp_path, InMemoryLockManager())
    written = ApprovalRequest(
        request_id="apr-" + "4" * 24,
        action="config_apply",
        subject=ApprovalSubject(
            kind="config",
            repo_id="repoforge",
            summary="Widen the adhoc command allowlist",
            capability_delta="expansion",
        ),
        binding=ApprovalBinding(
            proposal_id="proposal-" + "5" * 24,
            payload_digest=_SHA,
            expected_generation=15,
            expected_source_sha256=_SHA_B,
        ),
        reason="The allowlist blocks a command the profile needs",
        created_at="2026-07-29T09:26:21+00:00",
        expires_at="2026-07-30T09:26:21+00:00",
        status=ApprovalStatus.ACCEPTED,
        decision=ApprovalDecision(
            status=ApprovalStatus.ACCEPTED,
            actor="local-operator",
            decided_at="2026-07-29T09:39:48+00:00",
            reason="Reviewed the delta",
        ),
    )
    store.create(written)
    return written, _value(store.read(written.request_id))


def _failure_evidence(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonFailureEvidenceStore
    from repoforge.domain.failure_intelligence import (
        FailureHistorySignal,
        FailureObservation,
        build_failure_evidence,
    )

    store = JsonFailureEvidenceStore(tmp_path, InMemoryLockManager())
    # The classifier derives `failure_class`, `confidence`, `safe_actions` and the
    # excerpt digests from the observation, so the evidence is built rather than
    # constructed. Every field the observation owns is populated here.
    written = build_failure_evidence(
        FailureObservation(
            operation_id="op-" + "c" * 24,
            plan_id="plan-" + "d" * 24,
            plan_hash=_h("1"),
            stage_id="stage-01-profile",
            stage_kind="profile",
            target="verify",
            workspace_id="epic-284-repository-iden-9164d41f2f",
            pre_identity=_workspace_identity(_HEAD_SHA),
            post_identity=_workspace_identity(_HEAD_SHA_B),
            environment_identity=_h("2"),
            error_code="COMMAND_FAILED",
            message="verification failed",
            details={"exit_code": 1},
            failure_domain="tests",
            changed_paths=("src/repoforge/domain/runtime.py",),
            history=(FailureHistorySignal(binding_hash=_h("4"), outcome="failed"),),
            compatibility_binding=_h("3"),
        ),
        created_at="2026-07-29T09:26:21+00:00",
        receipt_id="receipt-" + "e" * 24,
    )
    store.create(written)
    return written, store.read(written.failure_id)


def _issue_graph_identity() -> object:
    from repoforge.domain.issue_graph_publication import IssueGraphIdentity

    return IssueGraphIdentity(
        repo_id="repoforge",
        repository_fingerprint=_h("1"),
        base_commit_sha=_HEAD_SHA,
        live_snapshot_sha256=_h("2"),
        active_generation=15,
        tool_surface_hash=_h("3"),
        input_contract_digest=_h("4"),
        output_contract_digest=_h("5"),
        template_version=2,
        schema_version=1,
    )


def _provider_identity() -> object:
    from repoforge.domain.issue_graph_publication import PublicationProviderIdentity

    return PublicationProviderIdentity(
        provider="github",
        api_version="2022-11-28",
        media_type="application/vnd.github+json",
        adapter="gh_cli",
        capability_hash=_h("6"),
    )


def _proposal_record() -> object:
    from repoforge.domain.issue_graph_proposal import (
        IssueEdgeDraft,
        IssueEdgeKind,
        IssueGraphDraft,
        IssueNodeDraft,
        LiveIssueCandidate,
        plan_issue_graph,
    )

    body = "## Objective\n\nDeliver the work.\n\n## Acceptance criteria\n\n- [ ] Done.\n"
    return plan_issue_graph(
        IssueGraphDraft(
            "repoforge",
            "epic-339",
            (
                IssueNodeDraft("epic-339", "Upgrade gate", "epic", "p0", "ready", None, body),
                IssueNodeDraft("task-340", "Round trip", "task", "p0", "ready", "epic-339", body),
                IssueNodeDraft("task-341", "Gate rule", "task", "p0", "ready", "epic-339", body),
            ),
            (IssueEdgeDraft("task-341", "task-340", IssueEdgeKind.BLOCKED_BY),),
        ),
        _issue_graph_identity(),  # type: ignore[arg-type]
        live_issues=(
            LiveIssueCandidate(339, "Old epic title", "<!-- repoforge-issue:epic-339 -->"),
            LiveIssueCandidate(341, "Gate rule", None),
        ),
        created_at="2026-07-29T09:26:21+00:00",
        expires_at="2026-07-30T09:26:21+00:00",
    )


def _issue_graph_proposal(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonIssueGraphProposalStore

    store = JsonIssueGraphProposalStore(tmp_path, InMemoryLockManager())
    written = _proposal_record()
    store.create(written)  # type: ignore[arg-type]
    return written, _value(store.read(written.proposal_id))  # type: ignore[attr-defined]


def _publication_plan_record() -> object:
    from repoforge.domain.issue_graph_publication import (
        PublicationLiveGraph,
        PublicationLiveNode,
        build_issue_graph_publication_plan,
    )

    proposal = _proposal_record()
    return build_issue_graph_publication_plan(
        proposal,  # type: ignore[arg-type]
        _issue_graph_identity(),  # type: ignore[arg-type]
        live_graph=PublicationLiveGraph(
            nodes=(
                PublicationLiveNode("epic-339", 339, 100339, True, "Upgrade gate", "body"),
                PublicationLiveNode("task-341", 341, 100341, False, "Gate rule", "body"),
            ),
            parent_by_ref=(("task-341", "epic-339"),),
            blocked_by_refs=(("task-341", "task-340"),),
            snapshot_sha256=_h("2"),
        ),
        adopt_refs=("task-341",),
        provider_identity=_provider_identity(),  # type: ignore[arg-type]
        created_at="2026-07-29T09:26:21+00:00",
        expires_at="2026-07-30T09:26:21+00:00",
    )


def _issue_graph_publication_plan(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonIssueGraphPublicationStore

    store = JsonIssueGraphPublicationStore(tmp_path, InMemoryLockManager())
    written = _publication_plan_record()
    store.create_plan(written)  # type: ignore[arg-type]
    return written, _value(store.read_plan(written.plan_id))  # type: ignore[attr-defined]


def _issue_graph_publication(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonIssueGraphPublicationStore
    from repoforge.domain.issue_graph_publication import (
        IssueGraphPublication,
        PublicationState,
        PublicationStepState,
    )

    plan = _publication_plan_record()
    store = JsonIssueGraphPublicationStore(tmp_path, InMemoryLockManager())
    written = IssueGraphPublication(
        publication_id="igpub-" + "1" * 24,
        plan_id=plan.plan_id,  # type: ignore[attr-defined]
        proposal_id=plan.proposal_id,  # type: ignore[attr-defined]
        proposal_hash=plan.proposal_hash,  # type: ignore[attr-defined]
        effect_plan_hash=plan.effect_plan_hash,  # type: ignore[attr-defined]
        identity=plan.identity,  # type: ignore[attr-defined]
        provider_identity=plan.provider_identity,  # type: ignore[attr-defined]
        state=PublicationState.PAUSED,
        # A publication's steps have run, so unlike a freshly built plan's they carry
        # their execution evidence -- which is the half of the step encoder a plan
        # round trip can never reach.
        steps=tuple(
            dataclasses.replace(
                step,
                state=PublicationStepState.APPLIED,
                issue_number=339 + index,
                operation_id="op-" + "2" * 24,
                receipt_id="receipt-" + "3" * 24,
                result_reference="op-" + "4" * 24,
                external_writes=1,
                provider_identity=plan.provider_identity,  # type: ignore[attr-defined]
            )
            for index, step in enumerate(plan.steps)  # type: ignore[attr-defined]
        ),
        node_mapping=(("epic-339", 339),),
        operation_id="op-" + "2" * 24,
        receipt_id="receipt-" + "3" * 24,
        result_reference="op-" + "4" * 24,
        retry_at="2026-07-29T10:00:00+00:00",
        external_writes=2,
        created_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:39:48+00:00",
        expires_at="2026-07-30T09:26:21+00:00",
    )
    store.create_publication(written)
    return written, _value(store.read_publication(written.publication_id))


def _task_capsule(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence import JsonTaskStore
    from repoforge.domain.operation_identity import OperationIdentityReference
    from repoforge.domain.task_capsule import (
        CriterionStatus,
        InstructionOrigin,
        RecordedBy,
        TaskAction,
        TaskCapsule,
        TaskCriterion,
        TaskDecision,
        TaskInstruction,
        TaskOverride,
        TaskQuestion,
        TaskState,
        TrustLevel,
        WorkspaceBinding,
        replace_task,
    )

    store = JsonTaskStore(tmp_path, InMemoryLockManager())
    base = TaskCapsule.new(
        task_id="task-0123456789abcdef01234567",
        intent="Cover every durable record type with a round trip.",
        acceptance_criteria=("The gate fails when a decoder drops a field.",),
        constraints=("Store summaries and identifiers only.",),
        repo_ids=("repoforge",),
        created_at="2026-07-29T09:26:21+00:00",
    )
    written = replace_task(
        base,
        # BLOCKED is the only state under which `blocked_reason` may carry a value.
        state=TaskState.BLOCKED,
        acceptance_criteria=(
            TaskCriterion(
                criterion_id="criterion-1",
                summary="The gate fails when a decoder drops a field.",
                status=CriterionStatus.PASSED,
                evidence_ids=("evidence-1",),
            ),
        ),
        workspace_bindings=(
            WorkspaceBinding(
                workspace_id="epic-284-repository-iden-9164d41f2f",
                repo_id="repoforge",
                head_sha=_HEAD_SHA,
                workspace_fingerprint=_h("1"),
                stale=True,
            ),
        ),
        source_issue_or_pr="repoforge#339",
        active_config_generation=15,
        accepted_plan_id="plan-" + "5" * 24,
        decisions=(
            TaskDecision(
                decision_id="decision-1",
                summary="Discover record types instead of listing them",
                outcome="accepted",
                decided_at="2026-07-29T09:30:00+00:00",
            ),
        ),
        evidence_snapshot_ids=("snapshot-1",),
        receipt_ids=("receipt-" + "6" * 24,),
        current_phase="implementation",
        blocked_reason="Waiting on the runtime record decoder",
        open_questions=(TaskQuestion(question_id="question-1", summary="Cover caches too?"),),
        next_safe_actions=(
            TaskAction(action="implement_domain", reason="Fixtures remain", required=True),
        ),
        updated_at="2026-07-29T09:39:48+00:00",
        principal="ci-operator",
        path_scope=("tests/",),
        instructions=(
            TaskInstruction(
                instruction_id="instruction-1",
                content="Populate every optional field.",
                asserted_origin=InstructionOrigin.USER,
                recorded_by=RecordedBy.OPERATOR,
                trust=TrustLevel.VERIFIED,
                revision=2,
                scope=("tests/",),
                expiry="2026-07-30T09:26:21+00:00",
            ),
        ),
        overrides=(
            TaskOverride(
                override_id="override-1",
                rule_id="rule-1",
                scope=("tests/",),
                reason="The gate owns this path",
                actor="ci-operator",
                expiry="2026-07-30T09:26:21+00:00",
            ),
        ),
        task_revision=2,
        guides_delivered=("guide-1",),
        escalated_rules=("rule-2",),
        mutation_count=3,
        lease_holder="worker-1",
        lease_expires_at="2026-07-29T09:45:00+00:00",
        identity_contexts=(
            OperationIdentityReference(
                context_id=_IDENTITY_CONTEXT_ID,
                context_digest=_IDENTITY_CONTEXT_DIGEST,
            ),
        ),
    )
    store.create(written)
    return written, _value(store.read(written.task_id))


def _process_lease(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.domain.process_lease import (
        ProcessLease,
        ProcessLeaseRole,
        ProcessLeaseStatus,
    )

    store = JsonProcessLeaseAdapter(tmp_path, InMemoryLockManager())
    written = ProcessLease(
        lease_id="worker-" + "0" * 24,
        status=ProcessLeaseStatus.UNPROVEN,
        process_identity=_SHA,
        pid=4321,
        pgid=4321,
        process_start_token="worker-start-token",
        owner_pid=4320,
        owner_process_identity=_SHA,
        release_sha="0123abc",
        generation=12,
        admission_epoch=7,
        started_at="2026-07-29T09:26:21+00:00",
        heartbeat_at="2026-07-29T09:30:00+00:00",
        correlation_id="e" * 24,
        created_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:30:00+00:00",
        error_code="WORKER_UNPROVEN",
        error_message="identity not proven yet",
        role=ProcessLeaseRole.OPERATION_WORKER,
    )
    store.create(written)
    return written, _value(store.read(written.lease_id))


def _runtime_transition(tmp_path: Path) -> tuple[object, object]:
    from repoforge.adapters.persistence.json_runtime_transition_adapter import (
        JsonRuntimeTransitionAdapter,
    )
    from repoforge.domain.runtime_transition import (
        RuntimeTransition,
        RuntimeTransitionStatus,
    )

    store = JsonRuntimeTransitionAdapter(tmp_path, InMemoryLockManager())
    written = RuntimeTransition(
        transition_id="tran-" + "a" * 24,
        status=RuntimeTransitionStatus.ROLLED_BACK,
        target_generation=15,
        config_generation=14,
        correlation_id="e" * 24,
        started_at="2026-07-29T09:26:21+00:00",
        updated_at="2026-07-29T09:39:48+00:00",
        completed_at="2026-07-29T09:39:48+00:00",
        error_code="HEALTH_FAILED",
        error_message="Runtime did not report the target generation in time",
        previous_transition_id="tran-" + "b" * 24,
        kind="rollback",
        from_sha="a" * 64,
        to_sha="b" * 64,
    )
    store.create(written)
    return written, _value(store.read(written.transition_id))


ALL_CASES: tuple[RoundTripCase, ...] = ()


def _register() -> tuple[RoundTripCase, ...]:
    from repoforge.domain.approval import ApprovalRequest
    from repoforge.domain.execution_plan import ExecutionPlan, ExecutionPlanAcceptance
    from repoforge.domain.execution_receipt import EffectReceipt, StageReceipt
    from repoforge.domain.execution_worker import (
        ExecutionWorkerArchiveEntry,
        ExecutionWorkerBinding,
    )
    from repoforge.domain.failure_intelligence import FailureEvidence
    from repoforge.domain.issue_graph_proposal import IssueGraphProposal
    from repoforge.domain.issue_graph_publication import (
        IssueGraphPublication,
        IssueGraphPublicationPlan,
    )
    from repoforge.domain.operation_identity import OperationIdentityRecord
    from repoforge.domain.operation_work import OperationWorkItem
    from repoforge.domain.operation_worker import OperationWorkerBinding
    from repoforge.domain.process_lease import ProcessLease
    from repoforge.domain.repository_identity import RepositoryIdentityBinding
    from repoforge.domain.runtime import RuntimeRecord
    from repoforge.domain.runtime_activation import RuntimeActivationReceipt
    from repoforge.domain.runtime_transition import RuntimeTransition
    from repoforge.domain.task_capsule import TaskCapsule
    from repoforge.domain.verification_dag import IterationCacheEntry

    return (
        RoundTripCase(RuntimeRecord, _runtime_record),
        RoundTripCase(OperationWorkerBinding, _worker_binding),
        RoundTripCase(ProcessLease, _process_lease),
        RoundTripCase(RuntimeTransition, _runtime_transition),
        RoundTripCase(ExecutionWorkerBinding, _execution_worker_binding),
        RoundTripCase(ExecutionWorkerArchiveEntry, _execution_worker_archive),
        RoundTripCase(RepositoryIdentityBinding, _repository_identity_binding),
        RoundTripCase(OperationIdentityRecord, _operation_identity_record),
        RoundTripCase(RuntimeActivationReceipt, _runtime_activation_receipt),
        RoundTripCase(EffectReceipt, _effect_receipt),
        RoundTripCase(StageReceipt, _stage_receipt),
        RoundTripCase(ExecutionPlan, _execution_plan),
        RoundTripCase(ExecutionPlanAcceptance, _execution_plan_acceptance),
        RoundTripCase(IterationCacheEntry, _iteration_cache_entry),
        RoundTripCase(ApprovalRequest, _approval_request),
        RoundTripCase(
            FailureEvidence,
            _failure_evidence,
            # The classifier derives the recovery actions and the affected scope from
            # the observation; which of their fields apply is decided by each action's
            # own `kind`, not by anything this fixture sets.
            inapplicable=frozenset({"RecoveryAction.*", "FailureScope.*"}),
        ),
        RoundTripCase(
            IssueGraphProposal,
            _issue_graph_proposal,
            # A proposal counts external writes only once publication runs against it,
            # so a freshly planned one is 0 by construction.
            inapplicable=frozenset({"IssueGraphProposal.external_writes"}),
        ),
        RoundTripCase(
            IssueGraphPublicationPlan,
            _issue_graph_publication_plan,
            # A plan's steps have not run: their execution fields are empty by
            # construction. The IssueGraphPublication case covers those, on steps that
            # have.
            inapplicable=frozenset({"IssueGraphPublicationStep.*"}),
        ),
        RoundTripCase(IssueGraphPublication, _issue_graph_publication),
        RoundTripCase(TaskCapsule, _task_capsule),
        *(
            RoundTripCase(
                OperationWorkItem,
                _operation_work_item(kind),
                variant=kind,
                inapplicable=_work_request_inapplicable(kind),
            )
            for kind in _WORK_REQUEST_FIELDS_BY_KIND
        ),
    )


ALL_CASES = _register()


@pytest.mark.parametrize("case", ALL_CASES, ids=[case.name for case in ALL_CASES])
def test_a_fully_populated_record_reads_back_equal(case: RoundTripCase, tmp_path: Path) -> None:
    """`read(write(record)) == record`, with every optional field carrying a value.

    Failing here means a durable store loses or mangles a field on the way back. That
    is the shape of the #338 outage: the loss is silent until some invariant, or some
    caller, depends on the field that quietly became a default.
    """
    written, read_back = case.round_trip(tmp_path)

    _assert_every_optional_field_populated(written, inapplicable=case.inapplicable)
    assert read_back is not None, f"{case.name} did not survive the round trip at all"
    assert read_back == written


def test_the_gate_fails_when_the_runtime_decoder_drops_a_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the property has teeth by reintroducing #338 exactly.

    A gate that only ever passes is indistinguishable from one that tests nothing. Here
    the writer keeps writing the restart evidence and the decoder stops reading it --
    the precise defect that took the runtime down -- and the round trip has to notice.

    Note what it does *not* depend on: no invariant, no knowledge that `restarts_total`
    relates to `restart_count`. #338 was only fatal because an invariant happened to
    read the field that went missing. The round trip fails on the drift itself, which is
    why it would also have caught a dropped field that no invariant guards.
    """
    from repoforge.adapters.runtime import state_store

    original = state_store.JsonRuntimeStore.read

    def read_without_the_restart_evidence(self: object) -> object:
        record = original(self)  # type: ignore[arg-type]
        if record is None:
            return None
        return dataclasses.replace(record, restarts_total=0, last_restart_at=None)

    monkeypatch.setattr(state_store.JsonRuntimeStore, "read", read_without_the_restart_evidence)

    written, read_back = _runtime_record(tmp_path)

    assert read_back != written, (
        "the runtime record round trip passed while the decoder was dropping "
        "restarts_total and last_restart_at, so it would not have caught #338"
    )


# -- coverage: the set of record types is discovered, not maintained ---------


def _codec_backed_record_types() -> dict[type, str]:
    """Every record type reachable through a `StateCodec` in the persistence package.

    Discovered structurally -- a codec is anything with `schema_version`, `encode` and
    `decode` -- because two of them (`TaskCapsuleCodec`, `ApprovalRequestCodec`) satisfy
    the protocol without subclassing it, and `StateCodec.__subclasses__()` misses those.
    """
    package = importlib.import_module("repoforge.adapters.persistence")
    discovered: dict[type, str] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        for name, obj in vars(module).items():
            if (
                not inspect.isclass(obj)
                or obj.__module__ != module.__name__
                or not all(hasattr(obj, attr) for attr in ("schema_version", "encode", "decode"))
            ):
                continue
            record_type = typing.get_type_hints(obj.decode).get("return")
            if isinstance(record_type, type):
                discovered[record_type] = f"{module_info.name}.{name}"
    return discovered


#: Durable record types whose store decodes by hand instead of through a `StateCodec`.
#: These carry *more* drift risk, not less -- the decoder enumerates every field, so a
#: field added anywhere else is simply absent from it, which is exactly what #338 was.
HAND_DECODED_RECORD_TYPES: tuple[str, ...] = ("repoforge.domain.runtime.RuntimeRecord",)


def test_every_durable_record_type_has_a_round_trip_case() -> None:
    """A new durable record type must join this property, not silently skip it.

    The list of what to cover is derived from the codebase. Adding a store without
    adding its round trip fails here rather than going unnoticed until an upgrade
    cannot read what a previous release wrote.
    """
    covered = {case.record_type for case in ALL_CASES}
    discovered = _codec_backed_record_types()

    missing = {
        record_type: codec
        for record_type, codec in discovered.items()
        if record_type not in covered
    }

    assert not missing, "durable record types with no round-trip case: " + ", ".join(
        f"{t.__name__} (via {codec})"
        for t, codec in sorted(missing.items(), key=lambda i: i[0].__name__)
    )
