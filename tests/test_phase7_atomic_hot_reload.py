from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import execution_coordinator_for_tests

from repoforge.adapters.locking import FcntlLockManager
from repoforge.adapters.persistence import JsonOperationStore, JsonRuntimeActivationStore
from repoforge.application.context import (
    ApplicationContext,
    repository_policy_snapshot,
)
from repoforge.application.runtime.activation import GenerationActivator
from repoforge.application.runtime.activation_journal import RuntimeActivationJournal
from repoforge.application.runtime.hot_reload import (
    AtomicServiceRouter,
    GenerationServiceContainer,
    HotReloadCoordinator,
)
from repoforge.config import AppConfig, RepositoryConfig, ServerConfig
from repoforge.contracts.registry import (
    render_v2_schema_bundle,
    validate_generated_contract_artifact,
)
from repoforge.domain.config_generation import CapabilityDeltaKind, ConfigGeneration
from repoforge.domain.errors import ConfigError, ErrorCode, RepoForgeError, SecurityError
from repoforge.domain.runtime import (
    ControlCommand,
    ControlRequest,
    ControlResponse,
    RuntimePhase,
    RuntimeRecord,
)
from repoforge.domain.runtime_activation import (
    RuntimeActivationClassification,
    RuntimeActivationIdentity,
)
from repoforge.domain.workspace import WorkspaceRecord
from repoforge.testing import FixedClock, SequenceIdGenerator


class Service:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self) -> dict[str, str]:
        return {"value": self.value}

    def repo_list(self, *, synthetic: bool = False) -> dict[str, list[dict[str, str]]]:
        return {"repositories": [{"repo_id": self.value}]}


class Gate:
    def operation(self, operation_id: str, *, mutating: bool):
        del operation_id, mutating
        return nullcontext()

    def begin_drain(self, *, reason: str, correlation_id: str) -> None:
        del reason, correlation_id

    def fail_closed(self, *, reason: str, correlation_id: str) -> None:
        del reason, correlation_id

    def reopen(self) -> None:
        pass

    def wait_for_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def snapshot(self) -> dict[str, object]:
        return {"state": "open", "active_reads": 0, "active_writes": 0}


def _container(generation: int, value: str, disposed: list[int]) -> GenerationServiceContainer:
    return GenerationServiceContainer(
        generation=generation,
        service=Service(value),
        gate=Gate(),
        repository_ids=frozenset({value}),
        dispose=lambda: disposed.append(generation),
    )


def _activation_identity(
    generation: int,
    *,
    runtime_active_generation: int | None,
) -> RuntimeActivationIdentity:
    return RuntimeActivationIdentity(
        config_generation=generation,
        source_sha256="a" * 64,
        resolved_sha256="b" * 64,
        runtime_active_generation=runtime_active_generation,
        process_identity="c" * 64 if runtime_active_generation is not None else None,
        tool_surface_hash="d" * 64 if runtime_active_generation is not None else None,
        runtime_phase="healthy" if runtime_active_generation is not None else "accepted",
    )


def test_activation_journal_persists_operation_and_receipt_before_candidate(
    tmp_path: Path,
) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    operations = JsonOperationStore(tmp_path, locks)
    receipts = JsonRuntimeActivationStore(tmp_path, locks)
    journal = RuntimeActivationJournal(
        operations=operations,
        receipts=receipts,
        ids=SequenceIdGenerator(("a" * 24, "b" * 24, "c" * 24)),
        clock=FixedClock("2026-07-22T00:00:00+00:00"),
    )

    attempt = journal.begin(
        target=_activation_identity(2, runtime_active_generation=None),
        previous=_activation_identity(1, runtime_active_generation=1),
        continuation_reference="issue-publication:42",
    )

    assert attempt.operation.operation_id == "op-" + "a" * 24
    assert attempt.receipt.value.receipt_id == "receipt-" + "b" * 24
    assert attempt.operation.phase == "accepted"
    assert attempt.receipt.value.classification.value == "accepted"
    assert attempt.receipt.value.continuation_reference == "issue-publication:42"
    assert operations.read(attempt.operation.operation_id) == attempt.operation
    assert receipts.read(attempt.receipt.value.receipt_id) == attempt.receipt


def test_activation_journal_completes_with_bound_operation_receipt(
    tmp_path: Path,
) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    operations = JsonOperationStore(tmp_path, locks)
    receipts = JsonRuntimeActivationStore(tmp_path, locks)
    journal = RuntimeActivationJournal(
        operations=operations,
        receipts=receipts,
        ids=SequenceIdGenerator(("d" * 24, "e" * 24, "f" * 24)),
        clock=FixedClock("2026-07-22T00:00:00+00:00"),
    )
    attempt = journal.begin(
        target=_activation_identity(2, runtime_active_generation=None),
        previous=_activation_identity(1, runtime_active_generation=1),
    )

    journal.mark_building(attempt.receipt.value.receipt_id)
    journal.mark_effect(attempt.receipt.value.receipt_id)
    completed = journal.complete(
        attempt.receipt.value.receipt_id,
        classification=RuntimeActivationClassification.HOT_RELOAD,
        active_identity=_activation_identity(2, runtime_active_generation=2),
    )

    assert completed.operation.state.value == "succeeded"
    assert completed.operation.receipt_id == attempt.receipt.value.receipt_id
    assert completed.operation.result_reference == (
        "runtime-activation:" + attempt.receipt.value.receipt_id
    )
    assert completed.receipt.value.classification is RuntimeActivationClassification.HOT_RELOAD
    assert completed.receipt.value.effect_boundary_crossed is True


def test_activation_receipt_store_rejects_stale_revision(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    receipts = JsonRuntimeActivationStore(tmp_path, locks)
    journal = RuntimeActivationJournal(
        operations=JsonOperationStore(tmp_path, locks),
        receipts=receipts,
        ids=SequenceIdGenerator(("1" * 24, "2" * 24, "3" * 24)),
        clock=FixedClock("2026-07-22T00:00:00+00:00"),
    )
    attempt = journal.begin(
        target=_activation_identity(2, runtime_active_generation=None),
        previous=None,
    )
    updated = journal.mark_building(attempt.receipt.value.receipt_id)

    with pytest.raises(RepoForgeError) as stale:
        receipts.save(
            updated.receipt.value,
            expected_revision=attempt.receipt.revision,
        )

    assert stale.value.code is ErrorCode.STATE_STALE


def test_activation_journal_redacts_terminal_failure(tmp_path: Path) -> None:
    locks = FcntlLockManager(tmp_path / "locks")
    journal = RuntimeActivationJournal(
        operations=JsonOperationStore(tmp_path, locks),
        receipts=JsonRuntimeActivationStore(tmp_path, locks),
        ids=SequenceIdGenerator(("4" * 24, "5" * 24, "6" * 24)),
        clock=FixedClock("2026-07-22T00:00:00+00:00"),
    )
    attempt = journal.begin(
        target=_activation_identity(2, runtime_active_generation=None),
        previous=None,
    )

    failed = journal.fail(
        attempt.receipt.value.receipt_id,
        error_code="ACTIVATION_FAILED",
        error_message="candidate token=super-secret failed",
    )

    assert failed.operation.state.value == "failed"
    assert failed.receipt.value.classification is RuntimeActivationClassification.RELOAD_FAILED
    assert "super-secret" not in str(failed.receipt.value.error_message)
    assert "<redacted" in str(failed.receipt.value.error_message)


def test_atomic_router_pins_inflight_request_and_disposes_retired_after_release() -> None:
    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))

    with router.acquire() as old:
        previous = router.swap(_container(2, "new", disposed))
        assert previous.generation == 1
        assert old.service.read() == {"value": "old"}
        assert disposed == []
        with router.acquire() as current:
            assert current.service.read() == {"value": "new"}
        snapshot = router.snapshot()
        assert snapshot["active_generation"] == 2
        assert snapshot["active_requests"] == {1: 1, 2: 0}
        assert snapshot["retired_generations"] == [1]

    assert router.wait_for_retired(1, timeout_seconds=0.2)
    assert disposed == [1]


def test_atomic_router_never_exposes_partial_candidate_under_concurrent_reads() -> None:
    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    observed: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            with router.acquire() as selected:
                observed.append(selected.service.read()["value"])

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.02)
    router.swap(_container(2, "new", disposed))
    time.sleep(0.02)
    stop.set()
    for thread in threads:
        thread.join(timeout=1)

    assert observed
    assert set(observed) == {"old", "new"}


def test_failed_candidate_construction_leaves_active_container_untouched() -> None:
    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))

    def broken_builder(generation: int) -> GenerationServiceContainer:
        raise ConfigError(f"candidate {generation} is invalid")

    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=broken_builder,
        commit_activation=lambda generation, expected: generation,
    )
    with pytest.raises(ConfigError, match="HOT_RELOAD_CANDIDATE_FAILED"):
        coordinator.reload(2, expected_active=1, correlation_id="reload")

    assert router.active_generation == 1
    assert disposed == []


def test_failed_activation_commit_disposes_candidate_and_keeps_active_container() -> None:
    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: _container(generation, "new", disposed),
        commit_activation=lambda generation, expected: (_ for _ in ()).throw(
            ConfigError(f"stale active {expected}")
        ),
    )

    with pytest.raises(ConfigError, match="HOT_RELOAD_COMMIT_FAILED"):
        coordinator.reload(2, expected_active=1, correlation_id="reload")

    assert router.active_generation == 1
    assert disposed == [2]


def _generation(number: int, delta: CapabilityDeltaKind) -> ConfigGeneration:
    return ConfigGeneration(
        number,
        "a" * 64,
        "b" * 64,
        (),
        "now",
        "test",
        None,
        None,
        delta,
        number - 1 or None,
        active=False,
    )


class Configs:
    def __init__(self) -> None:
        self.active_item = replace(_generation(1, CapabilityDeltaKind.EQUIVALENT), active=True)
        self.target: ConfigGeneration | None = None
        self.staged: list[int] = []

    @property
    def source_path(self) -> Path:
        return Path("/config")

    @property
    def root(self) -> Path:
        return Path("/state")

    @property
    def active_resolved_path(self) -> Path:
        return Path("/active")

    def current(self) -> ConfigGeneration:
        return self.target or self.active_item

    def active(self) -> ConfigGeneration:
        return self.active_item

    def activation_target(self) -> ConfigGeneration | None:
        return self.target

    def stage_activation(self, generation: int, *, expected_active: int | None = None):
        assert expected_active == self.active_item.generation
        self.staged.append(generation)
        self.target = _generation(generation, CapabilityDeltaKind.EXPANSION)
        return self.target

    def clear_activation_target(self, *, expected_generation: int | None = None) -> None:
        if expected_generation is None or (
            self.target is not None and self.target.generation == expected_generation
        ):
            self.target = None

    def activate(self, generation: int, *, expected_active: int | None = None):
        assert self.target is not None and self.target.generation == generation
        assert expected_active == self.active_item.generation
        self.active_item = replace(self.target, active=True)
        self.target = None
        return self.active_item

    def history(self):
        return (self.active_item,)

    def read_source_text(self):
        return ""

    def generation_path(self, generation: int):
        return Path(f"/{generation}")

    def resolved_path(self, generation: int):
        return Path(f"/{generation}/resolved.toml")

    def read_resolved_text(self, generation=None):
        return ""

    def accept(self, mutation):
        raise AssertionError

    def import_legacy(self, source_text, resolved_text, *, created_at):
        raise AssertionError

    def rollback(self, generation, *, expected_active, approval_token=None):
        raise AssertionError


class Runtime:
    def __init__(self) -> None:
        self.record = RuntimeRecord(
            1,
            RuntimePhase.HEALTHY,
            100,
            "a" * 64,
            1,
            1,
            "profile",
            "b" * 64,
            "c" * 64,
            "now",
            "now",
            "initial",
            child_pid=101,
            child_process_identity="d" * 64,
        )

    def read(self):
        return self.record

    def write(self, record):
        self.record = record

    def clear(self, *, expected_pid=None):
        self.record = None


class ReloadControl:
    def __init__(self, configs: Configs) -> None:
        self.configs = configs
        self.commands: list[ControlCommand] = []

    def request(self, request: ControlRequest, *, timeout_seconds: float = 10.0) -> ControlResponse:
        del timeout_seconds
        self.commands.append(request.command)
        if request.command is ControlCommand.RELOAD:
            generation = int(dict(request.payload)["generation"])
            self.configs.activate(generation, expected_active=1)
            return ControlResponse(
                1,
                True,
                request.correlation_id,
                "hot_reloaded",
                (("active_generation", generation),),
            )
        return ControlResponse(1, True, request.correlation_id, "ok")


class NoLaunch:
    def start(self, config_path: Path, *, foreground: bool, extra_env: dict[str, str]) -> int:
        raise AssertionError("hot reload must not launch a new supervisor")

    def force_stop(self, record: RuntimeRecord, *, grace_seconds: float = 5.0) -> bool:
        raise AssertionError("hot reload must not stop the healthy supervisor")


def test_generation_activator_prefers_atomic_hot_reload_for_compatible_generation() -> None:
    configs = Configs()
    runtime = Runtime()
    control = ReloadControl(configs)
    activator = GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=control,
        supervisor_control=control,
        launcher=NoLaunch(),
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
    )

    result = activator.activate(_generation(2, CapabilityDeltaKind.EXPANSION), extra_env={})

    assert result.status == "hot_reloaded"
    assert result.active_generation == 2
    assert control.commands == [ControlCommand.RELOAD]
    assert runtime.record is not None
    assert runtime.record.phase is RuntimePhase.HEALTHY
    assert runtime.record.active_generation == 2
    assert runtime.record.accepted_generation == 2


def test_generation_activator_rejects_tampered_contract_before_any_runtime_effect(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "tool-schemas-v2.json"
    tampered = render_v2_schema_bundle()
    tampered["tool_count"] = 27
    artifact.write_text(json.dumps(tampered), encoding="utf-8")
    configs = Configs()
    runtime = Runtime()
    control = ReloadControl(configs)
    activator = GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=control,
        supervisor_control=control,
        launcher=NoLaunch(),
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
        validate_contract_artifacts=lambda: validate_generated_contract_artifact(artifact),
    )

    with pytest.raises(ConfigError, match="CONTRACT_ARTIFACT_MISMATCH"):
        activator.activate(_generation(2, CapabilityDeltaKind.EXPANSION), extra_env={})

    assert configs.staged == []
    assert control.commands == []
    assert runtime.record is not None
    assert runtime.record.active_generation == 1


class Files:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def size(self, path: Path) -> int:
        return path.stat().st_size

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def read_text(self, path: Path) -> str:
        return path.read_text()

    def write_bytes_atomic(self, path: Path, data: bytes, *, preserve_mode: bool = True) -> None:
        del preserve_mode
        path.write_bytes(data)

    def unlink(self, path: Path, *, missing_ok: bool = False) -> None:
        path.unlink(missing_ok=missing_ok)

    def mkdir(self, path: Path, *, parents: bool = True, exist_ok: bool = True) -> None:
        path.mkdir(parents=parents, exist_ok=exist_ok)


class Store:
    def __init__(self, record: WorkspaceRecord) -> None:
        self.record = record

    def save(self, record: WorkspaceRecord) -> None:
        self.record = record

    def load(self, workspace_id: str) -> WorkspaceRecord:
        assert workspace_id == self.record.workspace_id
        return self.record

    def delete(self, workspace_id: str) -> None:
        raise AssertionError

    def list(self):
        return [self.record]


class Git:
    def current_branch(self, path: Path) -> str:
        del path
        return "ai/task"


class Audit:
    path = Path("/audit")

    def record(self, action: str, *, success: bool, details: dict[str, Any]) -> None:
        del action, success, details


class Locks:
    def lock(self, name: str, *, timeout_seconds: float = 30, metadata=None):
        del name, timeout_seconds, metadata
        return nullcontext()


class Ids:
    def new_hex(self, length: int = 24) -> str:
        return "a" * length


class Clock:
    def now_iso(self) -> str:
        return "2026-07-13T00:00:00+00:00"


def test_removed_repository_workspace_uses_snapshotted_read_policy_and_blocks_writes(
    tmp_path: Path,
) -> None:
    repo_path = tmp_path / "source"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    workspace = tmp_path / "workspaces" / "demo" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").write_text("gitdir")
    original = RepositoryConfig(
        "demo",
        repo_path,
        branch_prefix="ai/",
        allowed_paths=("src/**",),
        denied_paths=("src/private/**",),
        read_only=False,
        publish_enabled=True,
    )
    record = WorkspaceRecord(
        "workspace",
        "demo",
        str(workspace),
        "ai/task",
        "main",
        "origin",
        "now",
        metadata={"repository_policy_snapshot": repository_policy_snapshot(original)},
    )
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        {},
    )
    context = ApplicationContext(
        config,
        object(),
        Git(),
        object(),
        Files(),
        Store(record),
        Locks(),
        Gate(),
        Audit(),
        Clock(),
        Ids(),
        object(),
        execution_coordinator_for_tests(),
    )

    loaded, orphaned, path = context.workspace("workspace")

    assert loaded is record
    assert path == workspace.resolve()
    assert orphaned.repo_id == "demo"
    assert orphaned.read_only is True
    assert orphaned.publish_enabled is False
    assert orphaned.allowed_paths == ("src/**",)
    assert orphaned.denied_paths == ("src/private/**",)
    assert orphaned.profiles == {}
    with pytest.raises(SecurityError, match="orphaned_read_only"):
        context.audited(
            "workspace_write_file",
            {"workspace_id": "workspace"},
            lambda: None,
        )


def test_mcp_boundary_pins_each_tool_call_to_one_router_generation() -> None:
    from repoforge.interfaces.mcp.server import _ServiceErrorBoundary

    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    boundary = _ServiceErrorBoundary(router=router)

    with router.acquire() as pinned:
        router.swap(_container(2, "new", disposed))
        assert pinned.service.read() == {"value": "old"}
        assert boundary.call("read") == {"value": "new"}


def test_runtime_host_reload_control_swaps_candidate_without_restarting_process() -> None:
    from repoforge.interfaces.runtime.host import McpRuntimeHost

    disposed: list[int] = []
    committed: list[tuple[int, int | None]] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: _container(generation, "new", disposed),
        commit_activation=lambda generation, expected: committed.append((generation, expected)),
    )
    activated: list[int] = []
    host = McpRuntimeHost(
        router=router,
        reloader=coordinator,
        on_activated=lambda generation: activated.append(generation),
    )

    response = host.handle(
        ControlRequest(
            1,
            ControlCommand.RELOAD,
            "reload",
            (("expected_active", 1), ("generation", 2)),
        )
    )

    assert response.ok is True
    assert response.status == "hot_reloaded"
    assert dict(response.payload)["active_generation"] == 2
    assert committed == [(2, 1)]
    assert activated == [2]
    with router.acquire() as current:
        assert current.service.read() == {"value": "new"}


def test_runtime_host_candidate_failure_keeps_old_generation_available() -> None:
    from repoforge.interfaces.runtime.host import McpRuntimeHost

    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: (_ for _ in ()).throw(
            ConfigError(f"bad candidate {generation}")
        ),
        commit_activation=lambda generation, expected: generation,
    )
    host = McpRuntimeHost(router=router, reloader=coordinator)

    response = host.handle(
        ControlRequest(
            1,
            ControlCommand.RELOAD,
            "reload",
            (("expected_active", 1), ("generation", 2)),
        )
    )

    assert response.ok is False
    assert response.error_code == "HOT_RELOAD_CANDIDATE_FAILED"
    assert router.active_generation == 1
    with router.acquire() as current:
        assert current.service.read() == {"value": "old"}


def test_router_blocks_new_acquisition_during_durable_activation_commit() -> None:
    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    commit_started = threading.Event()
    release_commit = threading.Event()
    acquired: list[str] = []

    def commit() -> None:
        commit_started.set()
        assert release_commit.wait(2)

    swap_thread = threading.Thread(
        target=lambda: router.commit_swap(_container(2, "new", disposed), commit)
    )
    swap_thread.start()
    assert commit_started.wait(1)

    reader = threading.Thread(target=lambda: router.acquire().__enter__())

    def acquire_current() -> None:
        with router.acquire() as current:
            acquired.append(current.service.read()["value"])

    reader = threading.Thread(target=acquire_current)
    reader.start()
    time.sleep(0.05)
    assert acquired == []
    release_commit.set()
    swap_thread.join(1)
    reader.join(1)

    assert acquired == ["new"]


def test_generation_activator_reconciles_lost_reload_response_from_committed_pointer() -> None:
    configs = Configs()
    runtime = Runtime()

    class LostResponseControl:
        def request(
            self, request: ControlRequest, *, timeout_seconds: float = 10.0
        ) -> ControlResponse:
            del timeout_seconds
            assert request.command is ControlCommand.RELOAD
            generation = int(dict(request.payload)["generation"])
            configs.activate(generation, expected_active=1)
            raise ConfigError("RUNTIME_CONTROL_UNAVAILABLE: response lost after commit")

    activator = GenerationActivator(
        configs=configs,
        runtime=runtime,
        mcp_control=LostResponseControl(),
        supervisor_control=LostResponseControl(),
        launcher=NoLaunch(),
        ids=SequenceIdGenerator(("correlation",)),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        config_path=Path("/config"),
    )

    result = activator.activate(_generation(2, CapabilityDeltaKind.EXPANSION), extra_env={})

    assert result.status == "hot_reloaded"
    assert result.active_generation == 2
    assert runtime.record is not None and runtime.record.active_generation == 2


def test_hot_reload_commits_real_generation_pointer_and_router_together(tmp_path: Path) -> None:
    from repoforge.adapters.configuration import ConfigGenerationStore
    from repoforge.adapters.locking import FcntlLockManager
    from repoforge.domain.config_generation import ApprovalEvent, ConfigMutation, sha256_text
    from repoforge.interfaces.runtime.host import McpRuntimeHost

    source_path = tmp_path / "config.toml"
    source_one = 'version = 2\n[tunnel]\nid = "tunnel"\n'
    source_path.write_text(source_one, encoding="utf-8")
    locks = FcntlLockManager(tmp_path / "locks")
    store = ConfigGenerationStore(source_path, tmp_path / "state", locks)
    resolved_one = (
        '[server]\nworkspace_root = "/tmp/w"\nstate_root = "/tmp/s"\n'
        "max_tool_output_chars = 120000\n"
    )
    first = store.accept(
        ConfigMutation(
            source_one,
            resolved_one,
            (),
            "initial",
            "2026-07-13T00:00:00+00:00",
            0,
            sha256_text(source_one),
            "initial",
            ApprovalEvent("tester", "2026-07-13T00:00:00+00:00", "initial", "a" * 64),
        )
    )
    store.stage_activation(first.generation, expected_active=None)
    store.activate(first.generation, expected_active=None)
    source_two = source_one + "# metadata change\n"
    second = store.accept(
        ConfigMutation(
            source_two,
            resolved_one.replace("120000", "119999"),
            (),
            "refresh",
            "2026-07-13T01:00:00+00:00",
            first.generation,
            first.source_sha256,
        )
    )
    store.stage_activation(second.generation, expected_active=first.generation)

    disposed: list[int] = []
    router = AtomicServiceRouter(_container(first.generation, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: _container(generation, "new", disposed),
        commit_activation=lambda generation, expected: store.activate(
            generation, expected_active=expected
        ),
    )
    host = McpRuntimeHost(router=router, reloader=coordinator)

    response = host.handle(
        ControlRequest(
            1,
            ControlCommand.RELOAD,
            "reload",
            (("expected_active", first.generation), ("generation", second.generation)),
        )
    )

    assert response.ok
    assert store.active() is not None and store.active().generation == second.generation
    assert store.activation_target() is None
    assert router.active_generation == second.generation


def test_supervisor_restarts_latest_hot_reloaded_generation_after_child_crash(
    tmp_path: Path,
) -> None:
    from contextlib import nullcontext

    from repoforge.application.runtime.supervisor import RuntimeSupervisor
    from repoforge.domain.runtime import ChildProcess, TunnelProfile

    class Locks:
        def lock(self, name: str, *, timeout_seconds=None, metadata=None):
            del name, timeout_seconds, metadata
            return nullcontext()

    class Server:
        def start(self, handler):
            self.handler = handler

        def close(self):
            pass

    class Processes:
        def identity(self, pid: int) -> str | None:
            return "f" * 64 if pid > 0 else None

    class Configs:
        def __init__(self) -> None:
            self.active_item = replace(_generation(1, CapabilityDeltaKind.EQUIVALENT), active=True)
            self.target_item = _generation(2, CapabilityDeltaKind.EQUIVALENT)
            self.activations: list[int] = []

        def active(self):
            return self.active_item

        def activation_target(self):
            return self.target_item

        def activate(self, generation: int, *, expected_active: int | None = None):
            assert expected_active == self.active_item.generation
            assert self.target_item is not None
            assert self.target_item.generation == generation
            self.activations.append(generation)
            self.active_item = replace(
                _generation(generation, CapabilityDeltaKind.EQUIVALENT), active=True
            )
            self.target_item = None
            return self.active_item

        def clear_activation_target(self, *, expected_generation: int | None = None):
            if self.target_item is not None:
                assert expected_generation in {None, self.target_item.generation}
            self.target_item = None

    class ProfileStore:
        def __init__(self) -> None:
            self.value: str | None = None

        def fingerprint(self) -> str | None:
            return self.value

        def commit(self, profile) -> None:
            self.value = profile.fingerprint

    class Runtime:
        def __init__(self) -> None:
            self.record: RuntimeRecord | None = None

        def read(self) -> RuntimeRecord | None:
            return self.record

        def write(self, record: RuntimeRecord) -> None:
            self.record = record

        def clear(self, *, expected_pid: int | None = None) -> None:
            del expected_pid
            self.record = None

    runtime_path = tmp_path / "mcp-runtime.json"

    def write_mcp_generation(generation: int) -> None:
        runtime_path.write_text(
            '{"pid":999,"process_identity":"' + "f" * 64 + f'","active_generation":{generation}}}',
            encoding="utf-8",
        )

    write_mcp_generation(2)
    configs = Configs()
    runtime = Runtime()

    class Mcp:
        def __init__(self) -> None:
            self.expected_generations: list[int] = []

        def request(self, request, *, timeout_seconds=10.0):
            del timeout_seconds
            record = runtime.read()
            assert record is not None
            self.expected_generations.append(record.accepted_generation)
            if len(self.expected_generations) == 2:
                supervisor._stop.set()
            return ControlResponse(1, True, request.correlation_id, "healthy")

    class Tunnel:
        def __init__(self) -> None:
            self.starts = 0
            self.monitor_checks = 0

        def initialize(self, profile, *, env):
            del profile, env

        def doctor(self, profile, *, env):
            del profile, env
            return True, "ok"

        def start(self, profile, *, env, log_path):
            del profile, env, log_path
            self.starts += 1
            return ChildProcess(200 + self.starts, "f" * 64, "now")

        def terminate(self, child, *, grace_seconds):
            del child, grace_seconds

        def is_alive(self, child):
            del child
            if self.starts == 1:
                record = runtime.read()
                if record is not None and record.phase is RuntimePhase.HEALTHY:
                    self.monitor_checks += 1
                    if self.monitor_checks == 1:
                        configs.active_item = replace(
                            _generation(3, CapabilityDeltaKind.EQUIVALENT), active=True
                        )
                        configs.target_item = None
                        runtime.write(
                            replace(
                                record,
                                active_generation=3,
                                accepted_generation=3,
                            )
                        )
                        write_mcp_generation(3)
                        return False
            return True

    mcp = Mcp()
    tunnel = Tunnel()
    supervisor = RuntimeSupervisor(
        store=runtime,  # type: ignore[arg-type]
        configs=configs,  # type: ignore[arg-type]
        locks=Locks(),  # type: ignore[arg-type]
        control=Server(),  # type: ignore[arg-type]
        mcp_control=mcp,  # type: ignore[arg-type]
        tunnel=tunnel,  # type: ignore[arg-type]
        profile_store=ProfileStore(),
        clock=FixedClock("2026-07-13T00:00:00+00:00"),
        ids=SequenceIdGenerator(("run", "health-1", "health-2")),
        processes=Processes(),  # type: ignore[arg-type]
        mcp_runtime_path=runtime_path,
        log_path=tmp_path / "runtime.log",
        health_timeout_seconds=0.2,
        max_restarts=1,
    )
    profile = TunnelProfile("a" * 64, "repoforge", "tunnel-client", "1.0", ("rf", "serve"))

    result = supervisor.run(
        generation=2,
        profile=profile,
        tool_surface_hash="b" * 64,
        environment={},
    )
    assert result == 0, runtime.read()
    assert tunnel.starts == 2
    assert configs.activations == [2]
    assert mcp.expected_generations == [2, 3]


def test_tampered_orphan_policy_snapshot_fails_closed_to_metadata_only(tmp_path: Path) -> None:
    repo_path = tmp_path / "source"
    repo_path.mkdir()
    (repo_path / ".git").mkdir()
    workspace = tmp_path / "workspaces" / "demo" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").write_text("gitdir")
    original = RepositoryConfig(
        "demo",
        repo_path,
        branch_prefix="ai/",
        allowed_paths=("src/**",),
        denied_paths=("src/private/**",),
    )
    snapshot = repository_policy_snapshot(original)
    snapshot["allowed_paths"] = ["**"]
    record = WorkspaceRecord(
        "workspace",
        "demo",
        str(workspace),
        "ai/task",
        "main",
        "origin",
        "now",
        metadata={"repository_policy_snapshot": snapshot},
    )
    config = AppConfig(
        tmp_path / "config.toml",
        ServerConfig(tmp_path / "workspaces", tmp_path / "state"),
        {},
    )
    context = ApplicationContext(
        config,
        object(),
        Git(),
        object(),
        Files(),
        Store(record),
        Locks(),
        Gate(),
        Audit(),
        Clock(),
        Ids(),
        object(),
        execution_coordinator_for_tests(),
    )

    _, orphaned, _ = context.workspace("workspace")

    assert orphaned.allowed_paths == ("__repoforge_orphaned_metadata_only__",)
    assert orphaned.denied_paths == ("**",)
    assert orphaned.max_changed_files == 1


def test_runtime_host_rejects_duplicate_or_unhashable_reload_fields() -> None:
    from repoforge.interfaces.runtime.host import McpRuntimeHost

    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: _container(generation, "new", disposed),
        commit_activation=lambda generation, expected: None,
    )
    host = McpRuntimeHost(router=router, reloader=coordinator)

    duplicate = host.handle(
        ControlRequest(
            1,
            ControlCommand.RELOAD,
            "duplicate",
            (("generation", 2), ("generation", 3)),
        )
    )
    unhashable = host.handle(
        ControlRequest(
            1,
            ControlCommand.RELOAD,
            "unhashable",
            (("generation", 2), ("expected_active", [])),
        )
    )

    assert duplicate.ok is False
    assert duplicate.error_code == "INVALID_RELOAD_REQUEST"
    assert unhashable.ok is False
    assert unhashable.error_code == "INVALID_RELOAD_REQUEST"
    assert router.active_generation == 1


def test_runtime_host_rejects_boolean_drain_timeout() -> None:
    from repoforge.interfaces.runtime.host import McpRuntimeHost

    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: _container(generation, "new", disposed),
        commit_activation=lambda generation, expected: None,
    )
    host = McpRuntimeHost(router=router, reloader=coordinator)

    response = host.handle(
        ControlRequest(
            1,
            ControlCommand.DRAIN,
            "drain",
            (("timeout_seconds", True),),
        )
    )

    assert response.ok is False
    assert response.error_code == "INVALID_DRAIN_TIMEOUT"


def test_hot_reload_restart_required_code_is_preserved_for_supervisor_fallback() -> None:
    disposed: list[int] = []
    router = AtomicServiceRouter(_container(1, "old", disposed))
    coordinator = HotReloadCoordinator(
        router=router,
        build_candidate=lambda generation: (_ for _ in ()).throw(
            ConfigError("HOT_RELOAD_RESTART_REQUIRED: incompatible generation")
        ),
        commit_activation=lambda generation, expected: None,
    )

    with pytest.raises(ConfigError, match=r"^HOT_RELOAD_RESTART_REQUIRED"):
        coordinator.reload(2, expected_active=1, correlation_id="reload")
