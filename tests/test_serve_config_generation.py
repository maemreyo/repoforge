"""The serving process must know which configuration generation it serves (#313).

The managed MCP runtime built its per-generation service container without passing
`config_generation`, so the container ran at the default 0 while the execution worker ran
the real generation. Every durable verification admitted through the connector was stamped
with generation 0, no worker could claim it, and ~30s later recovery terminalized it:

    OPERATION_GENERATION_STALE
    operation admitted at generation 0
    active worker generation 12

Observed on a live installation, and invisible to the whole test corpus because tests build
BOTH sides at generation 0 -- so the filter matched and everything ran. These tests assert
the production topology instead: the admitting side carries a real generation, and a worker
on that same generation can actually claim what it admitted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import create_forge_environment

from repoforge.application.operations.work_admission import DurableWorkAdmission
from repoforge.application.service import CodingService
from repoforge.bootstrap import build_application
from repoforge.config import load_config
from repoforge.domain.errors import ConfigError
from repoforge.domain.operation_work import OperationWorkRequest
from repoforge.interfaces.cli.main import _ensure_generation, build_generation_service_container


def _store_and_generation(config_path: Path) -> tuple[object, int]:
    """Resolve the generation the way the runtime does at startup.

    `_ensure_generation` is what `rf serve` calls: the fixture writes a bare config, and the
    runtime imports it into an immutable generation on first use. Reading the store directly
    would find no generation at all -- which is precisely how the corpus ended up exercising
    generation 0 everywhere.
    """
    store = _ensure_generation(config_path)
    generation = store.activation_target() or store.active() or store.current()
    assert generation is not None
    return store, generation.generation


def _plant_unclaimable_work(application, *, stamped_generation: int) -> str:
    """Write the operation+work pair directly, as a misconfigured process once would.

    Admission refuses this now (#312), so constructing it through `admit` is impossible --
    but the state can still exist on disk from a release that predates the refusal, and
    that is exactly what these tests are about.
    """
    from repoforge.domain.operation_task import OperationSnapshotBinding
    from repoforge.domain.operation_work import new_work_item

    queue = application.context.operation_work_queue
    assert queue is not None
    operation_id = f"op-{application.context.ids.new_hex(24)}"
    now = application.context.clock.now_iso()
    request = OperationWorkRequest.profile(
        workspace_id="workspace-1",
        profile_name="quick",
        expected_head_sha="a" * 40,
        expected_fingerprint="b" * 64,
        config_generation=stamped_generation,
    )
    application.operations.create(
        operation_id=operation_id,
        kind="workspace_run_profile",
        phase="queued",
        cancel_supported=True,
        workspace_id=request.workspace_id,
        snapshot_binding=OperationSnapshotBinding(
            head_sha=request.expected_head_sha,
            workspace_fingerprint=request.expected_fingerprint,
            config_generation=stamped_generation,
        ),
        now=now,
    )
    queue.create(new_work_item(operation_id=operation_id, request=request, now=now))
    return operation_id


def test_the_managed_runtime_container_carries_its_generation(tmp_path: Path) -> None:
    """The exact bug site: the container is built FROM a generation, so it must carry it."""
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    assert generation > 0, "precondition: the fixture has an accepted generation"

    container = build_generation_service_container(store, generation, allow_incompatible=True)

    assert container.generation == generation
    # The load-bearing assertion: 0 here is what broke every durable verification.
    assert container.service.application.context.config_generation == generation


def test_work_admitted_by_the_runtime_is_claimable_by_a_worker_on_that_generation(
    tmp_path: Path,
) -> None:
    """The production topology the corpus never exercised: request side and worker side
    built independently, each with the REAL generation, must agree."""
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)

    # Request side: exactly how the managed MCP runtime composes itself.
    request_side = build_generation_service_container(
        store, generation, allow_incompatible=True
    ).service
    queue = request_side.application.context.operation_work_queue
    assert queue is not None
    admitted = DurableWorkAdmission(request_side.application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=request_side.application.context.config_generation,
        ),
        operation_kind="workspace_run_profile",
    )

    work = queue.read(admitted.operation_id)
    assert work is not None
    assert work.request.config_generation == generation

    # Worker side: composed the way the execution worker is, with the same generation.
    worker_application = build_application(
        load_config(store.resolved_path(generation)), config_generation=generation
    )
    claimed = worker_application.context.operation_work_queue.claim_next(
        owner_id="worker-test",
        now="2026-07-28T10:00:00+00:00",
        lease_expires_at="2026-07-28T10:01:30+00:00",
        compatible_kinds=frozenset({"profile", "adhoc", "diagnostic"}),
        config_generation=worker_application.context.config_generation,
    )

    assert claimed is not None, "the worker could not claim work the runtime admitted"
    assert claimed.operation_id == admitted.operation_id


def test_a_generation_zero_request_side_is_what_broke(tmp_path: Path) -> None:
    """Pins the mechanism, so a future reader can see why 0 is not a harmless default.

    A worker running a real generation claims only matching work, so an item stamped 0 is
    invisible to it -- which is why the caller waited and recovery later terminalized it.
    Admission refuses to create this state now (#312), so it is planted directly.
    """
    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    application = build_application(config, config_generation=generation)
    operation_id = _plant_unclaimable_work(application, stamped_generation=0)
    queue = application.context.operation_work_queue
    assert queue is not None
    assert queue.read(operation_id).request.config_generation == 0

    # A worker on the real generation cannot see it.
    assert (
        queue.claim_next(
            owner_id="worker-test",
            now="2026-07-28T10:00:00+00:00",
            lease_expires_at="2026-07-28T10:01:30+00:00",
            compatible_kinds=frozenset({"profile", "adhoc", "diagnostic"}),
            config_generation=generation,
        )
        is None
    )


def test_recovery_terminalizes_the_unclaimable_item_with_both_generations(
    tmp_path: Path,
) -> None:
    """The error an operator actually sees names both sides of the mismatch."""
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    application = build_application(config, config_generation=generation)
    operation_id = _plant_unclaimable_work(application, stamped_generation=0)

    report = recover_operation_work(
        application.operations,
        application.context.operation_work_queue,
        now="2026-07-28T10:00:00+00:00",
        expected_config_generation=generation,
    )

    assert report.stale_generation == 1
    terminal = application.operations.status(operation_id)
    assert terminal.error_code == "OPERATION_GENERATION_STALE"
    assert "generation 0" in (terminal.error_message or "")
    assert f"generation {generation}" in (terminal.error_message or "")
    assert application.context.operation_work_queue.read(operation_id) is None


def test_an_unknown_generation_is_still_refused(tmp_path: Path) -> None:
    """The extraction must not have dropped the factory's own guards."""
    env = create_forge_environment(tmp_path)
    store, _ = _store_and_generation(env.config_path)

    with pytest.raises(ConfigError, match="Unknown configuration generation"):
        build_generation_service_container(store, 9_999)


def test_work_from_a_newer_generation_is_left_for_the_replacement_worker(
    tmp_path: Path,
) -> None:
    """A hot reload swaps the request side before the supervisor replaces this worker.

    For those seconds the request side admits against generation N+1 while the worker still
    runs N. That work is valid -- just not this worker's to claim -- so recovery must leave
    it alone. A symmetric `!=` test failed it, destroying good work and reporting the
    operator's own config change as an error.
    """
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    application = build_application(config, config_generation=generation)
    queue = application.context.operation_work_queue
    assert queue is not None
    newer = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=generation + 1,
        ),
        operation_kind="workspace_run_profile",
    )

    report = recover_operation_work(
        application.operations,
        queue,
        now="2026-07-28T10:00:00+00:00",
        expected_config_generation=generation,
    )

    assert report.stale_generation == 0
    assert queue.read(newer.operation_id) is not None
    assert application.operations.status(newer.operation_id).error_code is None


def test_work_from_an_older_generation_is_still_terminalized(tmp_path: Path) -> None:
    """The rule this branch exists for must keep working: an older generation cannot run."""
    from repoforge.application.operations.recovery import recover_operation_work

    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    application = build_application(config, config_generation=generation)
    queue = application.context.operation_work_queue
    assert queue is not None
    older = DurableWorkAdmission(application.operations, queue).admit(
        OperationWorkRequest.profile(
            workspace_id="workspace-1",
            profile_name="quick",
            expected_head_sha="a" * 40,
            expected_fingerprint="b" * 64,
            config_generation=generation,
        ),
        operation_kind="workspace_run_profile",
    )

    # The worker has moved on to a later generation, so this work is the older side.
    report = recover_operation_work(
        application.operations,
        queue,
        now="2026-07-28T10:00:00+00:00",
        expected_config_generation=generation + 1,
    )

    assert report.stale_generation == 1
    assert application.operations.status(older.operation_id).error_code == (
        "OPERATION_GENERATION_STALE"
    )


def test_admission_refuses_work_no_worker_could_claim(tmp_path: Path) -> None:
    """#312: unclaimable work is refused at the boundary, not 30 seconds later.

    A process that does not know its generation stamps 0, which no worker running a real
    generation will ever claim. Accepting it meant the caller waited and recovery
    terminalized it with a message about staleness -- describing the symptom, in a
    different process, half a minute after the actual fault.
    """
    import pytest

    from repoforge.domain.errors import ErrorCode, RepoForgeError

    env = create_forge_environment(tmp_path)
    store, generation = _store_and_generation(env.config_path)
    config = load_config(store.resolved_path(generation))
    unknowing = CodingService(config, application=build_application(config))
    queue = unknowing.application.context.operation_work_queue
    assert queue is not None

    with pytest.raises(RepoForgeError) as raised:
        DurableWorkAdmission(unknowing.application.operations, queue).admit(
            OperationWorkRequest.profile(
                workspace_id="workspace-1",
                profile_name="quick",
                expected_head_sha="a" * 40,
                expected_fingerprint="b" * 64,
                config_generation=unknowing.application.context.config_generation,
            ),
            operation_kind="workspace_run_profile",
        )

    assert "OPERATION_GENERATION_UNKNOWN" in str(raised.value)
    assert raised.value.code is ErrorCode.CONFIG_INVALID
    # Nothing durable was created: no operation record and no queue entry to reconcile.
    assert queue.list_records(max_records=10).records == ()
    assert unknowing.application.operations.list_records(max_records=10).records == ()


def test_the_test_environment_serves_a_real_generation(tmp_path: Path) -> None:
    """The harness must represent production, or it cannot catch this class of bug.

    Both sides at 0 is what made #313 invisible: the generation filter matched, so no
    admitted work was ever unclaimable in a test.
    """
    from conftest import TEST_CONFIG_GENERATION

    env = create_forge_environment(tmp_path)

    assert TEST_CONFIG_GENERATION > 0
    assert env.service.application.context.config_generation == TEST_CONFIG_GENERATION
