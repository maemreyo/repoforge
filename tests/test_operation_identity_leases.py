from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoforge.adapters.persistence.json_operation_identity_store import JsonOperationIdentityStore
from repoforge.adapters.persistence.json_task_store import JsonTaskStore
from repoforge.application.operations.identity import OperationIdentityManager
from repoforge.application.workspace.run_adhoc import WorkspaceAdhocRunner
from repoforge.bootstrap import AdapterOverrides, build_application
from repoforge.config import AppConfig, ServerConfig
from repoforge.domain.durable_state import Revision
from repoforge.domain.errors import ErrorCode, RepoForgeError
from repoforge.domain.operation_identity import (
    LeaseCapabilityRequest,
    OperationIdentityReference,
    bind_task_identity,
    bind_worker_identity,
    expire_operation_leases,
    new_operation_identity_record,
    operation_identity_digest,
    refresh_operation_lease,
    require_operation_lease,
    revoke_operation_leases,
)
from repoforge.domain.operation_task import new_operation_task
from repoforge.domain.operation_worker import (
    OperationWorkerBinding,
    worker_binding_from_payload,
    worker_binding_payload,
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
from repoforge.domain.task_capsule import TASK_CAPSULE_SCHEMA_VERSION, TaskCapsule
from repoforge.testing.fakes import (
    FixedClock,
    InMemoryLockManager,
    InMemoryOperationStore,
    InMemoryWorkerBindingStore,
    RecordingProcessReaper,
    ScriptedCommandExecutor,
)

_OPERATION_ID = "op-" + "1" * 24
_CONTEXT_ID = "identity-" + "2" * 24
_CONFIG = "a" * 64
_POLICY = "b" * 64
_MATERIAL = "c" * 64


def _lease(
    *,
    lease_id: str = "lease-primary",
    profile_id: str = "company",
    repository_id: str = "123456",
    target_kind: AuthTargetKind = AuthTargetKind.REPOSITORY,
    target_id: str = "github-repository-123456",
    actor_id: str = "github-app-42",
    issued_at: str = "2026-07-28T00:00:00+00:00",
    expires_at: str = "2026-07-28T01:00:00+00:00",
    state: AuthLeaseState = AuthLeaseState.ACTIVE,
    config_revision: str = _CONFIG,
    policy_revision: str = _POLICY,
    material_digest: str = _MATERIAL,
    provider_metadata: tuple[tuple[str, str], ...] = (("installation_id", "installation-84"),),
) -> AuthLease:
    return AuthLease(
        lease_id=lease_id,
        profile_id=profile_id,
        provider=RepositoryProvider.GITHUB,
        repository_id=repository_id,
        target_kind=target_kind,
        target_id=target_id,
        actor_id=actor_id,
        credential_ref=OpaqueCredentialReference("github-app", f"ref-{profile_id}"),
        issued_at=issued_at,
        expires_at=expires_at,
        state=state,
        config_revision=config_revision,
        policy_revision=policy_revision,
        material_digest=material_digest,
        provider_metadata=provider_metadata,
    )


def _context(*, nested: bool = False) -> OperationIdentityContext:
    leases = [_lease()]
    if nested:
        leases.append(
            _lease(
                lease_id="lease-submodule",
                profile_id="dependency-reader",
                repository_id="654321",
                target_kind=AuthTargetKind.SUBMODULE,
                target_id="submodule-vendor-sdk",
                actor_id="github-app-77",
                material_digest="d" * 64,
            )
        )
    return OperationIdentityContext(
        operation_id=_OPERATION_ID,
        primary_repository_id="123456",
        actor_class=ActorClass.AUTONOMOUS_AGENT,
        auth_leases=tuple(leases),
        selected_at="2026-07-28T00:00:00+00:00",
        config_revision=_CONFIG,
        policy_revision=_POLICY,
    )


def _requests(*, nested: bool = False) -> tuple[LeaseCapabilityRequest, ...]:
    values = [LeaseCapabilityRequest("lease-primary", ("git_push", "github_api_write"))]
    if nested:
        values.append(LeaseCapabilityRequest("lease-submodule", ("git_fetch",)))
    return tuple(values)


def _record(*, nested: bool = False):
    return new_operation_identity_record(
        _context(nested=nested),
        context_id=_CONTEXT_ID,
        capability_requests=_requests(nested=nested),
        now="2026-07-28T00:00:00+00:00",
    )


def _operation_store() -> InMemoryOperationStore:
    store = InMemoryOperationStore()
    store.create(
        new_operation_task(
            operation_id=_OPERATION_ID,
            kind="publication",
            phase="queued",
            now="2026-07-28T00:00:00+00:00",
            cancel_supported=True,
        )
    )
    return store


def test_context_digest_is_deterministic_and_secret_free() -> None:
    context = _context(nested=True)
    digest = operation_identity_digest(context)
    assert len(digest) == 64
    assert digest == operation_identity_digest(context)
    rendered = json.dumps(context.payload(), sort_keys=True)
    assert "token" not in rendered.lower()
    assert "private_key" not in rendered.lower()


def test_json_sidecar_round_trips_restart_and_rejects_stale_cas(tmp_path: Path) -> None:
    store = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    created = store.create(_record(nested=True))
    assert created.revision == Revision(1)
    restarted = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    loaded = restarted.read(_OPERATION_ID)
    assert loaded == created

    revoked = revoke_operation_leases(
        created.value,
        lease_id="lease-submodule",
        now="2026-07-28T00:10:00+00:00",
    )
    saved = store.save(revoked, expected_revision=Revision(1))
    assert saved.revision == Revision(2)
    with pytest.raises(RepoForgeError) as stale:
        store.save(created.value, expected_revision=Revision(1))
    assert stale.value.code is ErrorCode.STATE_STALE

    encoded = next((tmp_path / "operation-identities").glob("*.json")).read_text()
    assert "must-not-leak" not in encoded
    assert "access_token" not in encoded.lower()


def test_manager_binds_once_and_resume_requires_exact_reference(tmp_path: Path) -> None:
    manager = OperationIdentityManager(
        operations=_operation_store(),
        identities=JsonOperationIdentityStore(tmp_path, InMemoryLockManager()),
    )
    bound = manager.bind(
        _context(),
        context_id=_CONTEXT_ID,
        capability_requests=_requests(),
        now="2026-07-28T00:00:00+00:00",
    )
    assert (
        manager.bind(
            _context(),
            context_id=_CONTEXT_ID,
            capability_requests=_requests(),
            now="2026-07-28T00:01:00+00:00",
        )
        == bound
    )
    assert manager.resume(_OPERATION_ID, bound.reference) == bound

    with pytest.raises(RepoForgeError) as wrong_digest:
        manager.resume(
            _OPERATION_ID,
            OperationIdentityReference(_CONTEXT_ID, "f" * 64),
        )
    assert wrong_digest.value.code is ErrorCode.OPERATION_IDENTITY_MISMATCH

    with pytest.raises(RepoForgeError) as changed_decision:
        manager.bind(
            _context(),
            context_id=_CONTEXT_ID,
            capability_requests=(LeaseCapabilityRequest("lease-primary", ("github_api_read",)),),
            now="2026-07-28T00:02:00+00:00",
        )
    assert changed_decision.value.code is ErrorCode.OPERATION_IDENTITY_MISMATCH


def test_manager_rejects_unknown_or_mismatched_operation(tmp_path: Path) -> None:
    identities = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    missing_manager = OperationIdentityManager(
        operations=InMemoryOperationStore(), identities=identities
    )
    with pytest.raises(RepoForgeError) as missing:
        missing_manager.bind(
            _context(),
            context_id=_CONTEXT_ID,
            capability_requests=_requests(),
            now="2026-07-28T00:00:00+00:00",
        )
    assert missing.value.code is ErrorCode.OPERATION_NOT_FOUND

    wrong_context = replace(_context(), operation_id="op-" + "9" * 24)
    manager = OperationIdentityManager(operations=_operation_store(), identities=identities)
    with pytest.raises(RepoForgeError) as mismatch:
        manager.bind(
            wrong_context,
            context_id=_CONTEXT_ID,
            capability_requests=_requests(),
            now="2026-07-28T00:00:00+00:00",
        )
    assert mismatch.value.code is ErrorCode.OPERATION_NOT_FOUND


def test_write_revalidation_is_exact_for_operation_target_and_capability(tmp_path: Path) -> None:
    manager = OperationIdentityManager(
        operations=_operation_store(),
        identities=JsonOperationIdentityStore(tmp_path, InMemoryLockManager()),
    )
    bound = manager.bind(
        _context(nested=True),
        context_id=_CONTEXT_ID,
        capability_requests=_requests(nested=True),
        now="2026-07-28T00:00:00+00:00",
    )
    lease = manager.require_write(
        operation_id=_OPERATION_ID,
        reference=bound.reference,
        target_kind=AuthTargetKind.REPOSITORY,
        target_id="github-repository-123456",
        capability_id="git_push",
        now="2026-07-28T00:30:00+00:00",
    )
    assert lease.lease_id == "lease-primary"

    for target_kind, target_id, capability, code in (
        (
            AuthTargetKind.SUBMODULE,
            "submodule-vendor-sdk",
            "git_push",
            ErrorCode.CREDENTIAL_CAPABILITY_DENIED,
        ),
        (
            AuthTargetKind.REPOSITORY,
            "github-repository-999999",
            "git_push",
            ErrorCode.OPERATION_IDENTITY_MISMATCH,
        ),
    ):
        with pytest.raises(RepoForgeError) as failure:
            manager.require_write(
                operation_id=_OPERATION_ID,
                reference=bound.reference,
                target_kind=target_kind,
                target_id=target_id,
                capability_id=capability,
                now="2026-07-28T00:30:00+00:00",
            )
        assert failure.value.code is code


def test_nested_targets_never_inherit_primary_lease() -> None:
    record = _record(nested=True)
    nested = require_operation_lease(
        record,
        operation_id=_OPERATION_ID,
        target_kind=AuthTargetKind.SUBMODULE,
        target_id="submodule-vendor-sdk",
        capability_id="git_fetch",
        now="2026-07-28T00:30:00+00:00",
    )
    assert nested.profile_id == "dependency-reader"
    assert nested.lease_id != "lease-primary"


def test_revoke_and_expire_are_selective() -> None:
    record = _record(nested=True)
    revoked = revoke_operation_leases(
        record,
        lease_id="lease-submodule",
        now="2026-07-28T00:10:00+00:00",
    )
    states = {lease.lease_id: lease.state for lease in revoked.context.auth_leases}
    assert states == {
        "lease-primary": AuthLeaseState.ACTIVE,
        "lease-submodule": AuthLeaseState.REVOKED,
    }
    assert revoked.reference == record.reference
    with pytest.raises(RepoForgeError) as denied:
        require_operation_lease(
            revoked,
            operation_id=_OPERATION_ID,
            target_kind=AuthTargetKind.SUBMODULE,
            target_id="submodule-vendor-sdk",
            capability_id="git_fetch",
            now="2026-07-28T00:20:00+00:00",
        )
    assert denied.value.code is ErrorCode.CREDENTIAL_REVOKED

    expired = expire_operation_leases(record, now="2026-07-28T02:00:00+00:00")
    assert {item.state for item in expired.context.auth_leases} == {AuthLeaseState.EXPIRED}
    assert expired.reference == record.reference


def test_same_identity_refresh_succeeds_but_identity_drift_fails() -> None:
    record = _record()
    refreshed_lease = _lease(
        lease_id="lease-primary-refresh",
        issued_at="2026-07-28T00:40:00+00:00",
        expires_at="2026-07-28T02:00:00+00:00",
        material_digest="e" * 64,
    )
    refreshed = refresh_operation_lease(
        record,
        refreshed_lease,
        now="2026-07-28T00:40:00+00:00",
    )
    assert refreshed.context.auth_leases == (refreshed_lease,)
    assert refreshed.superseded_lease_ids == ("lease-primary",)
    assert refreshed.reference == record.reference

    for changed in (
        replace(refreshed_lease, profile_id="personal"),
        replace(refreshed_lease, repository_id="999999"),
        replace(refreshed_lease, actor_id="other-actor"),
        replace(refreshed_lease, config_revision="f" * 64),
    ):
        with pytest.raises(RepoForgeError) as mismatch:
            refresh_operation_lease(record, changed, now="2026-07-28T00:40:00+00:00")
        assert mismatch.value.code is ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH


def test_lease_refresh_allows_new_preflight_timestamp_but_rejects_digest_drift() -> None:
    initial_metadata = (
        ("installation_id", "installation-84"),
        ("github_capability_digest", "1" * 64),
        ("github_permission_digest", "2" * 64),
        ("github_preflight_evidence_digest", "3" * 64),
        ("github_preflight_observed_at", "2026-07-28T00:00:00+00:00"),
    )
    initial = _lease(provider_metadata=initial_metadata)
    context = replace(_context(), auth_leases=(initial,))
    record = new_operation_identity_record(
        context,
        context_id=_CONTEXT_ID,
        capability_requests=_requests(),
        now="2026-07-28T00:00:00+00:00",
    )
    renewed = _lease(
        lease_id="lease-primary-refresh",
        issued_at="2026-07-28T00:40:00+00:00",
        expires_at="2026-07-28T02:00:00+00:00",
        material_digest="e" * 64,
        provider_metadata=tuple(
            (key, "2026-07-28T00:40:00+00:00")
            if key == "github_preflight_observed_at"
            else (key, value)
            for key, value in initial_metadata
        ),
    )

    refreshed = refresh_operation_lease(
        record,
        renewed,
        now="2026-07-28T00:40:00+00:00",
    )

    assert refreshed.reference == record.reference
    assert refreshed.context.auth_leases == (renewed,)

    drifted_metadata = tuple(
        (key, "4" * 64) if key == "github_capability_digest" else (key, value)
        for key, value in renewed.provider_metadata
    )
    with pytest.raises(RepoForgeError) as failure:
        refresh_operation_lease(
            record,
            replace(renewed, provider_metadata=drifted_metadata),
            now="2026-07-28T00:40:00+00:00",
        )

    assert failure.value.code is ErrorCode.CREDENTIAL_REFRESH_IDENTITY_MISMATCH


def test_manager_lifecycle_stale_revision_is_typed(tmp_path: Path) -> None:
    store = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    manager = OperationIdentityManager(
        operations=_operation_store(),
        identities=store,
    )
    bound = manager.bind(
        _context(),
        context_id=_CONTEXT_ID,
        capability_requests=_requests(),
        now="2026-07-28T00:00:00+00:00",
    )
    store.save(bound, expected_revision=Revision(1))
    replacement = _lease(
        lease_id="lease-primary-refresh",
        issued_at="2026-07-28T00:40:00+00:00",
        expires_at="2026-07-28T02:00:00+00:00",
        material_digest="e" * 64,
    )

    for action in (
        lambda: manager.refresh(
            _OPERATION_ID,
            bound.reference,
            replacement,
            expected_revision=Revision(1),
            now="2026-07-28T00:40:00+00:00",
        ),
        lambda: manager.revoke(
            _OPERATION_ID,
            expected_revision=Revision(1),
            lease_id="lease-primary",
            now="2026-07-28T00:40:00+00:00",
        ),
        lambda: manager.expire(
            _OPERATION_ID,
            expected_revision=Revision(1),
            now="2026-07-28T02:00:00+00:00",
        ),
    ):
        with pytest.raises(RepoForgeError) as stale:
            action()
        assert stale.value.code is ErrorCode.OPERATION_IDENTITY_STALE


def test_worker_binding_propagates_reference_and_old_payload_still_loads() -> None:
    reference = _record().reference
    original = OperationWorkerBinding(
        operation_id=_OPERATION_ID,
        child_pid=100,
        child_pgid=100,
        child_start_token="child",
        server_pid=200,
        server_start_token="server",
        created_at="2026-07-28T00:00:00+00:00",
        owner_generation=7,
    )
    bound = bind_worker_identity(original, reference)
    assert bound.identity_context_id == reference.context_id
    assert bound.identity_context_digest == reference.context_digest
    assert worker_binding_from_payload(worker_binding_payload(bound)) == bound

    old_payload = worker_binding_payload(original)
    old_payload.pop("identity_context_id", None)
    old_payload.pop("identity_context_digest", None)
    loaded = worker_binding_from_payload(old_payload)
    assert loaded.identity_context_id is None
    assert loaded.identity_context_digest is None


def test_task_capsule_v3_persists_identity_reference_and_resume_projection(tmp_path: Path) -> None:
    assert TASK_CAPSULE_SCHEMA_VERSION == 3
    task = TaskCapsule.new(
        task_id="task-" + "3" * 24,
        intent="publish",
        acceptance_criteria=("done",),
        constraints=(),
        repo_ids=("demo",),
        created_at="2026-07-28T00:00:00+00:00",
    )
    reference = _record().reference
    bound = bind_task_identity(
        task,
        reference,
        updated_at="2026-07-28T00:01:00+00:00",
    )
    store = JsonTaskStore(tmp_path, InMemoryLockManager())
    store.create(bound)
    loaded = store.read(task.task_id)
    assert loaded is not None
    assert loaded.value.identity_contexts == (reference,)
    assert loaded.value.resume_projection()["identity_contexts"] == [reference.payload()]


def test_store_outage_denies_resume_and_write_without_fallback() -> None:
    class BrokenStore:
        def read(self, operation_id: str):
            del operation_id
            raise RepoForgeError("storage unavailable", code=ErrorCode.STATE_PERSISTENCE_FAILED)

    manager = OperationIdentityManager(
        operations=_operation_store(),
        identities=BrokenStore(),  # type: ignore[arg-type]
    )
    reference = OperationIdentityReference(_CONTEXT_ID, "f" * 64)
    with pytest.raises(RepoForgeError) as failure:
        manager.resume(_OPERATION_ID, reference)
    assert failure.value.code is ErrorCode.CREDENTIAL_BROKER_UNAVAILABLE


def _application(tmp_path: Path):
    return build_application(
        AppConfig(
            source_path=tmp_path / "config.toml",
            server=ServerConfig(
                workspace_root=tmp_path / "workspaces",
                state_root=tmp_path / "state",
            ),
            repositories={},
        ),
        overrides=AdapterOverrides(command=ScriptedCommandExecutor()),
    )


def test_build_application_wires_operation_identity_store(tmp_path: Path) -> None:
    application = _application(tmp_path)
    assert isinstance(application.context.operation_identities, JsonOperationIdentityStore)


def test_operation_delete_removes_identity_sidecar(tmp_path: Path) -> None:
    application = _application(tmp_path)
    task = application.operations.create(
        kind="publication",
        phase="queued",
        cancel_supported=True,
        now="2026-07-28T00:00:00+00:00",
    )
    context = replace(_context(), operation_id=task.operation_id)
    identity_store = application.context.operation_identities
    operation_store = application.context.operation_store
    assert identity_store is not None
    assert operation_store is not None
    manager = OperationIdentityManager(
        operations=operation_store,
        identities=identity_store,
    )
    manager.bind(
        context,
        context_id=_CONTEXT_ID,
        capability_requests=_requests(),
        now="2026-07-28T00:00:00+00:00",
    )
    assert identity_store.read(task.operation_id) is not None

    application.operations.delete(task.operation_id)

    assert identity_store.read(task.operation_id) is None


def test_background_worker_binding_inherits_durable_identity_reference(tmp_path: Path) -> None:
    identities = JsonOperationIdentityStore(tmp_path, InMemoryLockManager())
    created = identities.create(_record())
    bindings = InMemoryWorkerBindingStore()
    reaper = RecordingProcessReaper(start_tokens={7777: "child-token"})
    runner = WorkspaceAdhocRunner(
        SimpleNamespace(
            worker_bindings=bindings,
            reaper=reaper,
            clock=FixedClock("2026-07-28T00:05:00+00:00"),
            operation_identities=identities,
        )
    )  # type: ignore[arg-type]

    runner._persist_worker_binding(_OPERATION_ID, 7777)

    binding = bindings.get(_OPERATION_ID)
    assert binding is not None
    assert binding.identity_context_id == created.value.reference.context_id
    assert binding.identity_context_digest == created.value.reference.context_digest
